"""Adapters from AutoPipeline interfaces to existing Ouroboros handlers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

import yaml

from ouroboros.auto.interview_driver import InterviewBackend, InterviewTurn
from ouroboros.core.seed import Seed
from ouroboros.mcp.errors import MCPServerError
from ouroboros.mcp.job_manager import JobManager, JobStatus
from ouroboros.mcp.tools.authoring_handlers import GenerateSeedHandler, InterviewHandler
from ouroboros.mcp.tools.evaluation_handlers import LateralThinkHandler
from ouroboros.mcp.tools.execution_handlers import StartExecuteSeedHandler
from ouroboros.mcp.tools.qa import QAHandler
from ouroboros.mcp.tools.ralph_handlers import RalphHandler
from ouroboros.mcp.types import MCPToolResult
from ouroboros.resilience.lateral import ThinkingPersona

_SAFE_SEED_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


class HandlerError(RuntimeError):
    """Raised when an MCP handler returns an error result."""


class PartialInterviewStartError(HandlerError):
    """Raised when interview start failed but a session_id was persisted server-side.

    Carries the persisted ``session_id`` so callers (e.g. the auto interview
    driver) can record it on durable state and resume the same interview
    after a transient first-question failure such as an LLM timeout.
    See Q00/ouroboros#687.
    """

    def __init__(self, message: str, *, session_id: str) -> None:
        super().__init__(message)
        self.session_id = session_id


def _unwrap(result, *, tool_name: str) -> MCPToolResult:
    if result.is_err:
        error: MCPServerError = result.error
        raise HandlerError(f"{tool_name} failed: {error}")
    value = result.value
    if value.is_error:
        text = value.content[0].text if value.content else "handler returned error"
        raise HandlerError(f"{tool_name} failed: {text}")
    return value


class HandlerInterviewBackend(InterviewBackend):
    """InterviewBackend backed by ``ouroboros_interview`` handler calls."""

    def __init__(self, handler: InterviewHandler, *, cwd: str) -> None:
        self.handler = handler
        self.cwd = cwd

    async def start(self, goal: str, *, cwd: str, interview_id: str | None = None) -> InterviewTurn:
        arguments: dict[str, str] = {"initial_context": goal, "cwd": cwd or self.cwd}
        if interview_id:
            arguments["interview_id"] = interview_id
        outcome = await self.handler.handle(arguments)
        # Recoverable error path: handler persisted state but failed to
        # produce the first question.  ONLY trust an explicit
        # ``meta.session_id`` from the handler — never fall back to the
        # caller-supplied ``interview_id``, otherwise auto state would
        # record persistence evidence that the handler never produced
        # (Q00/ouroboros#723 review).
        if not outcome.is_err:
            value = outcome.value
            if value.is_error:
                meta = value.meta or {}
                session_id = _optional_str(meta.get("session_id"))
                if session_id:
                    text = (
                        value.content[0].text
                        if value.content
                        else "ouroboros_interview returned error"
                    )
                    raise PartialInterviewStartError(
                        f"ouroboros_interview failed: {text}",
                        session_id=session_id,
                    )
        result = _unwrap(outcome, tool_name="ouroboros_interview")
        return _turn_from_result(result)

    async def answer(self, session_id: str, answer: str) -> InterviewTurn:
        result = _unwrap(
            await self.handler.handle({"session_id": session_id, "answer": answer}),
            tool_name="ouroboros_interview",
        )
        return _turn_from_result(result, fallback_session_id=session_id)

    async def resume(self, session_id: str) -> InterviewTurn:
        result = _unwrap(
            await self.handler.handle({"session_id": session_id}),
            tool_name="ouroboros_interview",
        )
        return _turn_from_result(result, fallback_session_id=session_id)

    def is_session_persisted(self, session_id: str) -> bool:
        """Return True when ``interview_<session_id>.json`` exists on disk.

        Used by ``AutoInterviewDriver`` to decide whether a pre-allocated
        id may be retained on auto state after a driver-level
        ``asyncio.wait_for`` cancel — without this probe the driver cannot
        distinguish "handler crashed before persisting" from "handler
        persisted then got cancelled".  Routes through
        ``InterviewHandler.resolved_state_dir`` so the probe always
        targets the directory the engine actually writes to (Q00/ouroboros#723).
        """
        if not session_id:
            return False
        state_dir = self.handler.resolved_state_dir()
        return (state_dir / f"interview_{session_id}.json").exists()


class HandlerSeedGenerator:
    """Callable seed generator backed by ``ouroboros_generate_seed``."""

    def __init__(self, handler: GenerateSeedHandler) -> None:
        self.handler = handler

    async def __call__(self, session_id: str) -> Seed:
        result = _unwrap(
            await self.handler.handle({"session_id": session_id}),
            tool_name="ouroboros_generate_seed",
        )
        text = result.content[0].text if result.content else ""
        seed_yaml = _extract_seed_yaml(text)
        raw = yaml.safe_load(seed_yaml)
        if not isinstance(raw, dict):
            raise HandlerError("ouroboros_generate_seed returned non-object Seed YAML")
        return Seed.from_dict(raw)


class HandlerRunStarter:
    """Callable run starter backed by ``ouroboros_start_execute_seed``."""

    def __init__(self, handler: StartExecuteSeedHandler, *, cwd: str) -> None:
        self.handler = handler
        self.cwd = cwd

    async def __call__(self, seed: Seed, *, idempotency_key: str = "") -> dict[str, object]:
        seed_yaml = yaml.dump(
            seed.to_dict(), default_flow_style=False, allow_unicode=True, sort_keys=False
        )
        arguments: dict[str, object] = {"seed_content": seed_yaml, "cwd": self.cwd}
        if idempotency_key:
            arguments["idempotency_key"] = idempotency_key
        result = _unwrap(
            await self.handler.handle(arguments),
            tool_name="ouroboros_start_execute_seed",
        )
        meta = result.meta or {}
        run_meta: dict[str, object] = {
            "job_id": _optional_str(meta.get("job_id")),
            "session_id": _optional_str(meta.get("session_id")),
            "execution_id": _optional_str(meta.get("execution_id")),
        }
        if isinstance(meta.get("_subagent"), dict):
            run_meta["_subagent"] = meta["_subagent"]
        return run_meta


class HandlerRalphStarter:
    """Callable Ralph starter backed by ``ouroboros_ralph``.

    Bridges :class:`AutoPipeline`'s RUN → RALPH_HANDOFF transition to the
    runtime-owned Ralph loop introduced in Q00/ouroboros#528. Awaits the
    background job to a terminal state in non-plugin runtimes so the auto
    pipeline can produce a final ``COMPLETE`` / ``BLOCKED`` / ``FAILED``
    auto phase from the same ``AutoPipeline.run()`` call. In plugin mode
    the handler returns ``delegated_to_plugin`` immediately and the
    pipeline records ``ralph_dispatch_mode="plugin"`` without invoking
    job tools.
    """

    def __init__(self, handler: RalphHandler) -> None:
        self.handler = handler

    async def __call__(
        self,
        seed: Seed,
        *,
        lineage_id: str,
        max_total_seconds: float | None = None,
        per_iteration_timeout_seconds: float | None = None,
        on_dispatched: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Dispatch the Ralph loop and wait for terminal completion.

        ``on_dispatched`` is invoked with the dispatch envelope *before*
        the wait-for-terminal poll begins, so callers (notably
        :meth:`AutoPipeline._handoff_to_ralph`) can checkpoint
        ``ralph_job_id`` / ``ralph_dispatch_mode`` immediately after the
        background job has been created. Without this hook, a process
        restart, deadline trip, or client disconnect *after* dispatch but
        *before* terminal completion would leave the persisted state with
        only ``ralph_lineage_id`` — and resume could not poll the
        still-running job (Q00/ouroboros#773 review-6).
        """
        seed_yaml = yaml.dump(
            seed.to_dict(), default_flow_style=False, allow_unicode=True, sort_keys=False
        )
        arguments: dict[str, Any] = {
            "lineage_id": lineage_id,
            "seed_content": seed_yaml,
        }
        if max_total_seconds is not None:
            arguments["max_total_seconds"] = max_total_seconds
        if per_iteration_timeout_seconds is not None:
            arguments["per_iteration_timeout_seconds"] = per_iteration_timeout_seconds
        result = _unwrap(
            await self.handler.handle(arguments),
            tool_name="ouroboros_ralph",
        )
        meta = result.meta or {}
        dispatch_mode = _optional_str(meta.get("dispatch_mode"))
        status = _optional_str(meta.get("status"))
        # Plugin mode: handler returned an envelope with no job_id and a
        # ``delegated_to_plugin`` status. The auto pipeline records this and
        # transitions straight to COMPLETE — there is nothing to wait for.
        if status == "delegated_to_plugin" or dispatch_mode == "plugin":
            envelope = {
                "job_id": None,
                "lineage_id": _optional_str(meta.get("lineage_id")) or lineage_id,
                "dispatch_mode": "plugin",
                "terminal_status": "delegated_to_plugin",
                "stop_reason": None,
            }
            if on_dispatched is not None:
                on_dispatched(envelope)
            return envelope
        # Job mode: wait for the background job to terminate, then map the
        # final job snapshot back into the structured terminal status the
        # pipeline maps onto an auto phase.
        job_id = _optional_str(meta.get("job_id"))
        if not job_id:
            raise HandlerError("ouroboros_ralph did not return a job_id")
        if on_dispatched is not None:
            # Fire BEFORE we block on the terminal poll so callers can
            # persist ``ralph_job_id`` immediately. The terminal_status /
            # stop_reason are intentionally omitted here — they are not
            # known until the poll returns.
            on_dispatched(
                {
                    "job_id": job_id,
                    "lineage_id": _optional_str(meta.get("lineage_id")) or lineage_id,
                    "dispatch_mode": "job",
                }
            )
        job_manager = self.handler._job_manager  # noqa: SLF001
        terminal_meta = await _wait_for_job_terminal(job_manager, job_id)
        terminal_status = _optional_str(terminal_meta.get("status")) or "failed"
        stop_reason = _optional_str(terminal_meta.get("stop_reason"))
        return {
            "job_id": job_id,
            "lineage_id": _optional_str(meta.get("lineage_id")) or lineage_id,
            "dispatch_mode": "job",
            "terminal_status": terminal_status,
            "stop_reason": stop_reason,
            # RFC #809 Phase 2.1: surface the Ralph job's result_text so
            # ``AutoPipeline._evaluate_or_complete`` can grade it against
            # the Seed AC via ``HandlerEvaluator``. ``""`` is a VALID graded
            # artifact (a Ralph run with intentionally empty output must
            # still be graded), so route through ``_artifact_text`` —
            # ``_optional_str`` would collapse the empty string to None and
            # silently skip EVALUATE.
            "result_text": _artifact_text(terminal_meta.get("__result_text__")),
        }


class HandlerRalphPoller:
    """Callable Ralph job poller backed by the same ``RalphHandler`` ``JobManager``.

    Used by :class:`AutoPipeline` on resume from a persisted ``RALPH_HANDOFF``
    checkpoint (Q00/ouroboros#773 review-5 finding 1). Without this hook a
    long-lived runtime such as MCP — where the Ralph background job keeps
    running after the client disconnects — would leave any interrupted
    ``--complete-product`` session stranded in the non-terminal handoff
    state forever, since the legacy resume path only emitted guidance text.
    The poller waits for the persisted ``ralph_job_id`` to reach a terminal
    snapshot and returns the same ``terminal_status`` / ``stop_reason`` /
    ``dispatch_mode`` shape as :class:`HandlerRalphStarter` so the pipeline
    can re-use a single COMPLETE / BLOCKED / FAILED mapping.
    """

    def __init__(self, handler: RalphHandler) -> None:
        self.handler = handler

    async def __call__(self, *, job_id: str) -> dict[str, Any]:
        job_manager = self.handler._job_manager  # noqa: SLF001
        terminal_meta = await _wait_for_job_terminal(job_manager, job_id)
        terminal_status = _optional_str(terminal_meta.get("status")) or "failed"
        stop_reason = _optional_str(terminal_meta.get("stop_reason"))
        return {
            "job_id": job_id,
            "lineage_id": _optional_str(terminal_meta.get("lineage_id")),
            "dispatch_mode": "job",
            "terminal_status": terminal_status,
            "stop_reason": stop_reason,
            # RFC #809 Phase 2.1: surface the Ralph job's result_text so a
            # resumed RALPH_HANDOFF checkpoint can still grade the artifact
            # via EVALUATE — same contract as the starter path. ``""`` is a
            # VALID graded artifact, so use ``_artifact_text`` rather than
            # ``_optional_str``.
            "result_text": _artifact_text(terminal_meta.get("__result_text__")),
        }


@dataclass(frozen=True, slots=True)
class EvaluateResult:
    """Structured result returned by :class:`HandlerEvaluator`.

    Mirrors the relevant subset of ``ouroboros_qa``'s response meta so the
    pipeline can persist the verdict on :class:`AutoPipelineState` and reuse
    it on resume without re-invoking the LLM judge.

    ``error`` is non-empty when the QA handler returned a transient failure
    (resumable). The pipeline treats this as a BLOCKED state, not FAILED.
    """

    passed: bool
    score: float
    verdict: str
    differences: tuple[str, ...] = ()
    suggestions: tuple[str, ...] = ()
    error: str | None = None


class HandlerEvaluator:
    """Callable QA evaluator backed by ``ouroboros_qa`` :class:`QAHandler`.

    Builds a ``quality_bar`` from the Seed's acceptance criteria using the
    exact phrasing established by ``evolution_handlers.py`` ("The execution
    must satisfy all acceptance criteria"). Grades a run artifact against
    that bar with ``pass_threshold=0.80`` and returns a typed
    :class:`EvaluateResult`. The artifact is opaque to the adapter — callers
    pull it from the appropriate runtime surface (e.g. the Ralph terminal
    job snapshot's ``result_text``).

    The adapter is intentionally thin so :class:`AutoPipeline._run_evaluate`
    owns the decision policy (transition to COMPLETE vs mark_blocked, cache
    by artifact hash for resume idempotency, etc.).
    """

    def __init__(self, qa_handler: QAHandler) -> None:
        self.qa_handler = qa_handler

    async def __call__(self, seed: Seed, run_artifact: str) -> EvaluateResult:
        # Empty Ralph artifact: ``QAHandler`` rejects ``""`` with
        # ``"artifact is required"``. The auto pipeline's intent is that an
        # empty run output still counts as a graded failure (the AC like
        # "Command prints stable output" cannot be satisfied by empty
        # stdout), so synthesize the verdict directly without round-tripping
        # to the QA tool.
        if not run_artifact:
            return EvaluateResult(
                passed=False,
                score=0.0,
                verdict="fail",
                differences=("run artifact was empty",),
                suggestions=("ensure the run produces observable output",),
            )

        if seed.acceptance_criteria:
            ac_lines = [f"- {ac}" for ac in seed.acceptance_criteria]
            quality_bar = "The execution must satisfy all acceptance criteria:\n" + "\n".join(
                ac_lines
            )
        else:
            quality_bar = "The execution must satisfy the seed's intent."

        seed_yaml = yaml.dump(
            seed.to_dict(), default_flow_style=False, allow_unicode=True, sort_keys=False
        )
        result = await self.qa_handler.handle(
            {
                "artifact": run_artifact,
                "artifact_type": "test_output",
                "quality_bar": quality_bar,
                "seed_content": seed_yaml,
                "pass_threshold": 0.80,
            }
        )
        if result.is_err:
            return EvaluateResult(
                passed=False,
                score=0.0,
                verdict="fail",
                error=str(result.error),
            )
        meta = result.value.meta or {}
        # Plugin-mode delegation envelope: ``QAHandler`` returns
        # ``status="delegated_to_subagent"`` with no ``passed`` / ``score``
        # fields when ``should_dispatch_via_plugin`` is on. The auto pipeline
        # cannot wait for the out-of-band Task pane to complete inline, so
        # treat the envelope as a transient error rather than silently
        # interpreting "no passed field" as ``passed=False``. The MCP handler
        # also avoids wiring this adapter in plugin mode (see
        # ``auto_handler._run``), but this guard makes the contract safe for
        # any caller that constructs the adapter directly.
        if str(meta.get("status", "")) == "delegated_to_subagent":
            return EvaluateResult(
                passed=False,
                score=0.0,
                verdict="fail",
                error=(
                    "QAHandler returned a plugin-delegation envelope; the auto pipeline "
                    "cannot grade artifacts via out-of-band subagent dispatch in this phase"
                ),
            )
        return EvaluateResult(
            passed=bool(meta.get("passed", False)),
            score=float(meta.get("score", 0.0)),
            verdict=str(meta.get("verdict", "fail")),
            differences=tuple(meta.get("differences", ()) or ()),
            suggestions=tuple(meta.get("suggestions", ()) or ()),
        )


@dataclass(frozen=True, slots=True)
class LateralResult:
    """Structured result returned by :class:`HandlerLateralThinker`.

    Mirrors the inline-fallback subset of ``ouroboros_lateral_think``'s
    response meta + content text. The pipeline persists these fields on
    :class:`AutoPipelineState` so a resumed session honors the cached
    persona suggestion without re-invoking the lateral_think tool.

    ``error`` is non-empty when the handler returned a transient failure
    (resumable). The pipeline treats it as BLOCKED, not FAILED.
    """

    persona: str
    approach_summary: str
    text: str
    error: str | None = None


class HandlerLateralThinker:
    """Callable lateral thinker backed by ``ouroboros_lateral_think``.

    Wraps :class:`LateralThinkHandler` in single-persona mode. Builds
    ``problem_context`` from the QA verdict's differences/suggestions and
    ``current_approach`` from the run artifact, then calls the handler and
    maps the inline-fallback response (the path the auto pipeline actually
    takes — multi-persona plugin dispatch requires runtime context the
    auto pipeline does not own today) onto a typed :class:`LateralResult`.

    Phase 2.2 ships single-persona advisory only. Multi-persona parallel
    dispatch via OpenCode plugin bridges is deferred to P2.2b.
    """

    def __init__(self, handler: LateralThinkHandler) -> None:
        self.handler = handler

    async def __call__(
        self,
        *,
        persona: ThinkingPersona,
        qa_differences: tuple[str, ...],
        qa_suggestions: tuple[str, ...],
        run_artifact: str,
    ) -> LateralResult:
        problem_context = _build_lateral_problem_context(qa_differences, qa_suggestions)
        current_approach = _build_lateral_current_approach(run_artifact)
        result = await self.handler.handle(
            {
                "persona": persona.value,
                "problem_context": problem_context,
                "current_approach": current_approach,
            }
        )
        if result.is_err:
            return LateralResult(
                persona=persona.value,
                approach_summary="",
                text="",
                error=str(result.error),
            )
        value = result.value
        meta = value.meta or {}
        # Plugin-mode / multi-persona-fanout delegation envelope: the handler
        # returns ``status="delegated_to_subagent"`` (or ``dispatch_mode=
        # "plugin"``) when it dispatches to an OpenCode Task pane. The auto
        # pipeline's Phase 2.2 advisory layer is synchronous and cannot wait
        # for that out-of-band response; treat the envelope as a transient
        # error so the session blocks with ``tool_name="lateral_thinker"``
        # (resumable) rather than persisting an empty/placeholder advice.
        if (
            str(meta.get("status", "")) == "delegated_to_subagent"
            or str(meta.get("dispatch_mode", "")) == "plugin"
        ):
            return LateralResult(
                persona=persona.value,
                approach_summary="",
                text="",
                error=(
                    "lateral_think returned a plugin-delegation envelope; the auto "
                    "pipeline cannot consume out-of-band subagent persona output in "
                    "Phase 2.2 (single-persona inline mode only)"
                ),
            )
        text = value.content[0].text if value.content else ""
        return LateralResult(
            persona=str(meta.get("persona", persona.value)),
            approach_summary=str(meta.get("approach_summary", "")),
            text=text,
        )


def _build_lateral_problem_context(
    qa_differences: tuple[str, ...], qa_suggestions: tuple[str, ...]
) -> str:
    """Build the ``problem_context`` payload from QA verdict shape."""
    lines = ["EVALUATE failed: the run output did not satisfy the Seed acceptance criteria."]
    if qa_differences:
        lines.append("")
        lines.append("QA differences:")
        lines.extend(f"- {item}" for item in qa_differences)
    if qa_suggestions:
        lines.append("")
        lines.append("QA suggestions:")
        lines.extend(f"- {item}" for item in qa_suggestions)
    return "\n".join(lines)


def _build_lateral_current_approach(run_artifact: str) -> str:
    """Build the ``current_approach`` payload from the run artifact.

    Keeps a bounded preview of the run artifact so an enormous ralph stdout
    dump doesn't dominate the lateral_think prompt token budget. The preview
    is head-biased (verdicts and exit-status lines that matter live near the
    top) and the artifact's tail is summarised with a length indicator.
    """
    preview_bytes = 4_000
    if len(run_artifact) <= preview_bytes:
        body = run_artifact
    else:
        body = (
            f"{run_artifact[:preview_bytes]}\n\n"
            f"... [truncated, full artifact is {len(run_artifact)} characters]"
        )
    return (
        f"Most recent run artifact (the work the Seed produced and that QA just rejected):\n{body}"
    )


async def _wait_for_job_terminal(
    job_manager: JobManager, job_id: str, *, poll_interval: float = 0.05
) -> dict[str, Any]:
    """Poll the job manager until ``job_id`` reaches a terminal state.

    Returns the materialized ``result_meta`` from the terminal snapshot so
    callers can extract ralph's ``status`` / ``stop_reason``. ``status`` in
    the returned mapping is always populated — it falls back to the job's
    own terminal status (e.g. ``"failed"`` for an exception path) when the
    inner ralph result did not provide one.

    Surfaces the snapshot's ``result_text`` under the synthetic
    ``__result_text__`` key so callers (notably the Ralph adapter ⇒ EVALUATE
    pipeline path) can grade the human-readable artifact without having to
    re-poll the job manager themselves. The key is namespaced with
    leading/trailing underscores to avoid colliding with any
    Ralph-supplied meta key.
    """
    while True:
        snapshot = await job_manager.get_snapshot(job_id)
        if snapshot.is_terminal:
            meta = dict(snapshot.result_meta or {})
            meta.setdefault(
                "status",
                "completed" if snapshot.status is JobStatus.COMPLETED else "failed",
            )
            if snapshot.result_text is not None:
                meta["__result_text__"] = snapshot.result_text
            return meta
        await asyncio.sleep(poll_interval)


def load_seed(path: str | Path) -> Seed:
    """Load a persisted auto-generated Seed."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise HandlerError(f"Seed file is not an object: {path}")
    return Seed.from_dict(raw)


def save_seed(seed: Seed, *, seeds_dir: Path | None = None) -> str:
    """Persist an auto-generated Seed in the standard seed directory."""
    directory = seeds_dir or (Path.home() / ".ouroboros" / "seeds")
    directory.mkdir(parents=True, exist_ok=True)
    seed_id = _safe_seed_id_for_filename(seed.metadata.seed_id)
    path = directory / f"{seed_id}.yaml"
    _require_path_inside_directory(path, directory)
    path.write_text(
        yaml.dump(seed.to_dict(), default_flow_style=False, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return str(path)


def _safe_seed_id_for_filename(seed_id: str) -> str:
    """Return a seed id that is safe to interpolate into a filename."""
    candidate = seed_id.strip()
    if not candidate or not _SAFE_SEED_ID_PATTERN.fullmatch(candidate):
        msg = f"Seed id is not safe for persistence filename: {seed_id!r}"
        raise HandlerError(msg)
    return candidate


def _require_path_inside_directory(path: Path, directory: Path) -> None:
    """Fail closed if a computed seed path escapes the seed directory."""
    resolved_directory = directory.resolve()
    resolved_path = path.resolve()
    if resolved_path.parent != resolved_directory:
        msg = f"Seed path escapes seed directory: {resolved_path}"
        raise HandlerError(msg)


def _turn_from_result(
    result: MCPToolResult, *, fallback_session_id: str | None = None
) -> InterviewTurn:
    meta = result.meta or {}
    session_id = _optional_str(meta.get("session_id")) or fallback_session_id
    if not session_id:
        raise HandlerError("ouroboros_interview did not return a session_id")
    text = result.content[0].text if result.content else ""
    return InterviewTurn(
        question=_extract_interview_question(text, session_id=session_id),
        session_id=session_id,
        seed_ready=bool(meta.get("seed_ready")),
        completed=bool(meta.get("completed")),
    )


def _extract_interview_question(text: str, *, session_id: str) -> str:
    """Strip this session's human-readable interview envelope from handler text."""
    stripped = text.strip()
    if not stripped:
        return ""
    if "\n\n" in stripped:
        head, tail = stripped.split("\n\n", 1)
        if head in {
            f"Interview started. Session ID: {session_id}",
            f"Session {session_id}",
        }:
            return tail.strip()
    return stripped


def _extract_seed_yaml(text: str) -> str:
    marker = "--- Seed YAML ---"
    if marker not in text:
        raise HandlerError("Seed response did not include Seed YAML marker")
    return text.split(marker, 1)[1].strip()


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _artifact_text(value: object) -> str | None:
    """Return ``value`` verbatim when it is a string (including ``""``), else None.

    Distinct from :func:`_optional_str` because an artifact graded by
    EVALUATE is a valid input even when empty: a Ralph job whose output is
    intentionally empty must still be evaluated against the Seed AC.
    Returning ``None`` for ``""`` would silently skip EVALUATE and produce a
    false-pass — the bug fixed by RFC #809 Phase 2.1.
    """
    return value if isinstance(value, str) else None
