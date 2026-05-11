"""Adapters from AutoPipeline interfaces to existing Ouroboros handlers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from ouroboros.auto.interview_driver import InterviewBackend, InterviewTurn
from ouroboros.core.seed import Seed
from ouroboros.mcp.errors import MCPServerError
from ouroboros.mcp.job_manager import JobManager, JobStatus
from ouroboros.mcp.tools.authoring_handlers import GenerateSeedHandler, InterviewHandler
from ouroboros.mcp.tools.execution_handlers import StartExecuteSeedHandler
from ouroboros.mcp.tools.ralph_handlers import RalphHandler
from ouroboros.mcp.types import MCPToolResult


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
        existing_job_id: str | None = None,
        on_dispatched: Callable[[dict[str, Any]], None] | None = None,
        on_started: Callable[[dict[str, Any]], None] | None = None,
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
        dispatch_callback = on_dispatched or on_started
        job_manager = self.handler._job_manager  # noqa: SLF001
        if not existing_job_id and lineage_id:
            recovered = await job_manager.find_active_job_by_lineage(lineage_id, job_type="ralph")
            if recovered is not None:
                existing_job_id = recovered.job_id
        if existing_job_id:
            if dispatch_callback is not None:
                dispatch_callback(
                    {
                        "job_id": existing_job_id,
                        "lineage_id": lineage_id,
                        "dispatch_mode": "job",
                        "status": "reattaching",
                    }
                )
            terminal_meta = await _wait_for_job_terminal(
                job_manager,
                existing_job_id,
                timeout_seconds=max_total_seconds,
            )
            terminal_status = _optional_str(terminal_meta.get("status")) or "failed"
            stop_reason = _optional_str(terminal_meta.get("stop_reason"))
            return {
                "job_id": existing_job_id,
                "lineage_id": lineage_id,
                "dispatch_mode": "job",
                "terminal_status": terminal_status,
                "stop_reason": stop_reason,
            }

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
            if dispatch_callback is not None:
                dispatch_callback(envelope)
            return envelope
        # Job mode: wait for the background job to terminate, then map the
        # final job snapshot back into the structured terminal status the
        # pipeline maps onto an auto phase.
        job_id = _optional_str(meta.get("job_id"))
        if not job_id:
            raise HandlerError("ouroboros_ralph did not return a job_id")
        if dispatch_callback is not None:
            # Fire BEFORE we block on the terminal poll so callers can
            # persist ``ralph_job_id`` immediately. The terminal_status /
            # stop_reason are intentionally omitted here — they are not
            # known until the poll returns.
            dispatch_callback(
                {
                    "job_id": job_id,
                    "lineage_id": _optional_str(meta.get("lineage_id")) or lineage_id,
                    "dispatch_mode": "job",
                }
            )
        terminal_meta = await _wait_for_job_terminal(
            job_manager,
            job_id,
            timeout_seconds=max_total_seconds,
        )
        terminal_status = _optional_str(terminal_meta.get("status")) or "failed"
        stop_reason = _optional_str(terminal_meta.get("stop_reason"))
        return {
            "job_id": job_id,
            "lineage_id": _optional_str(meta.get("lineage_id")) or lineage_id,
            "dispatch_mode": "job",
            "terminal_status": terminal_status,
            "stop_reason": stop_reason,
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

    async def __call__(
        self, *, job_id: str, max_total_seconds: float | None = None
    ) -> dict[str, Any]:
        job_manager = self.handler._job_manager  # noqa: SLF001
        terminal_meta = await _wait_for_job_terminal(
            job_manager,
            job_id,
            timeout_seconds=max_total_seconds,
        )
        terminal_status = _optional_str(terminal_meta.get("status")) or "failed"
        stop_reason = _optional_str(terminal_meta.get("stop_reason"))
        return {
            "job_id": job_id,
            "lineage_id": _optional_str(terminal_meta.get("lineage_id")),
            "dispatch_mode": "job",
            "terminal_status": terminal_status,
            "stop_reason": stop_reason,
        }


async def _wait_for_job_terminal(
    job_manager: JobManager,
    job_id: str,
    *,
    poll_interval: float = 0.05,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Poll the job manager until ``job_id`` reaches a terminal state.

    Returns the materialized ``result_meta`` from the terminal snapshot so
    callers can extract ralph's ``status`` / ``stop_reason``. ``status`` in
    the returned mapping is always populated — it falls back to the job's
    own terminal status (e.g. ``"failed"`` for an exception path) when the
    inner ralph result did not provide one.
    """
    loop = asyncio.get_running_loop()
    deadline = None
    if timeout_seconds is not None:
        deadline = loop.time() + max(0.0, timeout_seconds)
    while True:
        snapshot = await job_manager.get_snapshot(job_id)
        if snapshot.is_terminal:
            meta = dict(snapshot.result_meta or {})
            terminal_status = _terminal_job_status(snapshot.status)
            if snapshot.status is JobStatus.CANCELLED:
                # Job-level cancellation is authoritative. A cancellation can
                # race with stale/partial result metadata from the runner, and
                # auto resume must preserve the user-cancelled outcome instead
                # of flattening it into FAILED/COMPLETE.
                meta["status"] = terminal_status
            else:
                meta.setdefault("status", terminal_status)
            return meta
        if deadline is not None:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return {
                    "job_id": job_id,
                    "status": "failed",
                    "stop_reason": "wall_clock_exhausted",
                }
            await asyncio.sleep(min(poll_interval, remaining))
            continue
        await asyncio.sleep(poll_interval)


def _terminal_job_status(status: JobStatus) -> str:
    if status is JobStatus.COMPLETED:
        return "completed"
    if status is JobStatus.CANCELLED:
        return "cancelled"
    return "failed"


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
    path = directory / f"{seed.metadata.seed_id}.yaml"
    path.write_text(
        yaml.dump(seed.to_dict(), default_flow_style=False, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return str(path)


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
