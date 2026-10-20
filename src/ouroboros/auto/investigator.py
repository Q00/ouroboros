"""The ``ooo auto`` self-answer reads the repository before it answers.

``ooo auto`` answers its own interview. The interviewer is unchanged; this
module changes only the answering role. For an interviewer question that the
deterministic answerer (``auto/answerer.py``) would fill with its own guess
(draft source ``conservative_default`` or ``assumption``), one read-only agent
session reads the task workspace and replies with JSON::

    {"status": "found" | "not_found", "answer": str,
     "evidence": [{"path": str, "start_line": int, "end_line": int, "quote": str}]}

Binding is decided without a model (``auto/evidence_check.py``):

- ``found`` with at least one span, and every span passes the check: the
  answer is a confirmed repository fact (``LedgerSource.REPO_FACT``). It
  settles the question for the interview, but it describes the code, not a
  requirement: it reaches the worker only verbatim, with its checked quotes, in
  ``Seed.repo_facts``, never as a constraint, criterion or ontology field, and
  its transcript is declared provenance ``repo_fact`` so Seed extraction
  withholds it.
- anything else is not found. The interviewer is told what the checked spans
  show and that the code does not settle the question. The ledger entry is a
  non-binding assumption that carries the question, so the worker sees it only
  as an open question. The model's answer text is dropped.

Order per round: the investigator first; the requester (``auto/requester.py``)
only for a question the code does not settle. A found answer is not sent to
the requester. The answer refiner (``AutoInterviewDriver.answer_refiner``) is
skipped for every investigated answer, because it would rewrite a not-found
reply into guessed text.

Runtime: ``create_agent_runtime`` with the auto interview stage's runtime
backend, no model pin (the runtime resolves model and reasoning effort from
Ouroboros' role and profile configuration), permission mode ``default`` (the
read-only sandbox class), and only the ``Read``/``Glob``/``Grep`` tools. This is
the same construction the check-package constructor uses
(``boundary/constructor.py``). Isolation: a backend whose runtime enforces
read-only for that mode (``READ_ONLY_ENFORCING_BACKENDS``: Codex, Claude; the
built runtime is checked again, ``enforces_read_only``) reads the workspace
itself. Every other backend reads a throwaway copy of the workspace, deleted
afterwards (``auto/investigation_workspace.py``), so its edits cannot reach the
real workspace. Evidence is always checked against the real workspace, and the
real workspace's fingerprint (git HEAD, diff, untracked files) is compared
before and after each investigation; a change discards the answer
(``workspace_changed``) and is reported in the event.

Limits: at most ``max_calls`` calls per interview run (default 12,
``OUROBOROS_AUTO_INVESTIGATOR_MAX_CALLS``; 0 turns the investigator off). With
a requester, a question past the cap is not found without a call and goes to
the requester. Without one, the interview closes once the last call is spent
(``close_known``) instead of asking questions that can only end not found.
Without a requester, a not-settled question is closed for the interview and
the Seed (its ledger entry is ``DEFAULTED``), reaches the worker as an
``Open question:`` line, and the reply tells the interviewer it stays open for
the implementer. There is no time budget:
the interview waits for each investigation, and the interview phase clock and
the pipeline deadline stop while it runs (``auto/phase_clock.py``). The only
time limit is a hang guard per call for a runtime that never returns (default
1800 s, ``OUROBOROS_AUTO_INVESTIGATOR_CALL_TIMEOUT_SECONDS``); a call past it is
not found. Each call is a fresh session; earlier found answers are passed in
the prompt as context.

When no runtime can be built (no backend, a plugin-dispatched stage, a failed
preflight), the deterministic answerer and refiner run exactly as before.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
import inspect
import json
import os
from pathlib import Path
import time
from typing import TYPE_CHECKING, Any

import structlog

from ouroboros.auto.answerer import AutoAnswer, AutoAnswerSource
from ouroboros.auto.evidence_check import EvidenceSpan, check_span
from ouroboros.auto.investigation_workspace import (
    create_isolated_copy,
    remove_isolated_copy,
    workspace_state,
)
from ouroboros.auto.ledger import REPO_FACT_KEY_INFIX, LedgerEntry, LedgerSource, LedgerStatus
from ouroboros.auto.phase_clock import phase_clock_paused
from ouroboros.auto.requester import ROUTED_SOURCES, ask_requester
from ouroboros.providers.throttle import (
    THROTTLE_TOTAL_CEILING_SECONDS,
    is_throttled,
    throttle_backoff_seconds,
)

if TYPE_CHECKING:
    from ouroboros.auto.interview_driver import (
        AutoInterviewDriver,
        AutoInterviewResult,
        InterviewTurn,
        _InterviewRoundOutcome,
    )
    from ouroboros.auto.ledger import SeedDraftLedger
    from ouroboros.auto.state import AutoPipelineState

log = structlog.get_logger(__name__)

INVESTIGATOR_TOOLS: tuple[str, ...] = ("Read", "Glob", "Grep")
INVESTIGATOR_PERMISSION_MODE = "default"
# Runtime backends that enforce read-only for permission mode ``default``,
# checked in their runtime code:
# - ``codex`` (``CodexCliRuntime``): ``default`` maps to ``SandboxClass.READ_ONLY``
#   and ``codex exec --sandbox read-only`` (``codex_permissions.py``).
# - ``claude`` (``ClaudeAgentAdapter``): the Agent SDK gets
#   ``permission_mode="default"`` (``SandboxClass.READ_ONLY`` in
#   ``claude_permissions.py``) with ``allowed_tools`` limited to the tools passed.
# Any other backend has no verified read-only mode (``gemini`` coerces
# ``default`` to ``acceptEdits`` and launches with ``--approval-mode auto_edit``,
# ``gemini_cli_runtime.py``; its runtime offers no read-only mode), so it reads
# a throwaway copy of the workspace (``auto/investigation_workspace.py``).
READ_ONLY_ENFORCING_BACKENDS = frozenset({"codex", "claude"})
MAX_CALLS_ENV = "OUROBOROS_AUTO_INVESTIGATOR_MAX_CALLS"
DEFAULT_MAX_CALLS = 12
CALL_TIMEOUT_ENV = "OUROBOROS_AUTO_INVESTIGATOR_CALL_TIMEOUT_SECONDS"
# Hang guard for one call: a runtime that never returns. Not a time budget.
DEFAULT_CALL_TIMEOUT_SECONDS = 1800.0
MAX_EVIDENCE_SPANS = 20
MAX_REPLY_CHARS = 100_000
# Earlier found answers passed to the next call as context.
_PRIOR_FINDINGS_IN_PROMPT = 6
_LOG_TEXT_LIMIT = 200

# Fixed ledger value of a not-found entry. The entry renders as its open
# question only (``ledger_seed.ledger_assumptions``); the value keeps the entry
# active and states the fact without guessing.
NOT_SETTLED_VALUE = "The repository code does not settle this interview question."
NOT_SETTLED_REPLY = "The repository code does not settle this question."
# Added to the not-found reply when no requester is wired: nobody else can
# answer, so the question stays open for the implementer, who reads the code.
OPEN_FOR_IMPLEMENTER_REPLY = (
    "It stays an open question for the implementer, who decides it from the code "
    "when making the change."
)

INVESTIGATOR_SYSTEM_PROMPT = (
    "You answer one interview question about a software task by reading the "
    "repository in the current working directory. Only read and search files. "
    "Do not edit, create, or delete files, and do not run commands that change "
    "anything.\n\n"
    "Find what the code shows about the question, then reply with one JSON "
    "object and nothing else:\n"
    '{"status": "found" | "not_found", "answer": string, "evidence": '
    '[{"path": string, "start_line": integer, "end_line": integer, "quote": string}]}\n\n'
    'Use "found" only when the code itself settles the question. Then write the '
    "answer in one to four sentences and cite every fact it rests on: the path "
    "relative to the repository root, the 1-based first and last line, and a "
    "quote copied exactly from those lines.\n"
    'Use "not_found" when the code does not settle the question, for example '
    "when it asks for a decision the code has not made. Then leave answer empty "
    "and cite what the code does show, if anything.\n"
    "Never guess. A citation that does not match the file makes the answer count "
    "as not found."
)

RuntimeFactory = Callable[..., Any]
# Seam for tests; the throttle wait itself (``_call_through_throttle``).
_throttle_sleep = asyncio.sleep


def resolve_call_timeout(environ: Mapping[str, str] | None = None) -> float:
    """Return the per-call hang guard from :data:`CALL_TIMEOUT_ENV` or the default."""
    env = os.environ if environ is None else environ
    raw = env.get(CALL_TIMEOUT_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_CALL_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError as exc:
        msg = f"{CALL_TIMEOUT_ENV} must be a number of seconds, got {raw!r}"
        raise ValueError(msg) from exc
    if not value > 0:
        msg = f"{CALL_TIMEOUT_ENV} must be greater than 0 seconds, got {value:g}"
        raise ValueError(msg)
    return value


def resolve_max_calls(environ: Mapping[str, str] | None = None) -> int:
    """Return the per-interview call cap from :data:`MAX_CALLS_ENV` or the default."""
    env = os.environ if environ is None else environ
    raw = env.get(MAX_CALLS_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_MAX_CALLS
    try:
        value = int(raw)
    except ValueError as exc:
        msg = f"{MAX_CALLS_ENV} must be a whole number of calls, got {raw!r}"
        raise ValueError(msg) from exc
    if value < 0:
        msg = f"{MAX_CALLS_ENV} must be 0 or more, got {value}"
        raise ValueError(msg)
    return value


@dataclass(frozen=True, slots=True)
class Investigation:
    """Outcome of investigating one question.

    ``outcome`` is ``found``, ``not_found``, ``evidence_rejected`` (the reply
    said found but a span failed the check), ``reply_invalid``, ``timeout``,
    ``call_failed``, ``cap_reached``, ``isolation_failed`` (no copy could be
    made; no call) or ``workspace_changed`` (the real workspace changed during
    the investigation; the answer is discarded). Only ``found``
    binds. ``checked`` holds the spans that passed; ``failures`` the reasons
    of those that did not.
    """

    outcome: str
    answer: str = ""
    checked: tuple[EvidenceSpan, ...] = ()
    failures: tuple[str, ...] = ()
    cited: int = 0
    called: bool = False
    elapsed_seconds: float = 0.0
    isolation: str = ""
    throttle_waited_seconds: float = 0.0

    @property
    def found(self) -> bool:
        return self.outcome == "found"


def parse_reply(text: str) -> tuple[str, str, list[EvidenceSpan | None]]:
    """Read ``(status, answer, spans)`` from the reply; ``ValueError`` if unusable.

    The first JSON object in the text is used (bare or inside a code fence).
    A malformed evidence item is kept as ``None`` so it counts as a failed span.
    """
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object")
    value, _ = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(value, dict):
        raise ValueError("reply is not a JSON object")
    status = value.get("status")
    if status not in ("found", "not_found"):
        raise ValueError("status is not found or not_found")
    answer = value.get("answer", "")
    evidence = value.get("evidence", [])
    if not isinstance(answer, str) or not isinstance(evidence, list):
        raise ValueError("answer or evidence has the wrong type")
    if len(evidence) > MAX_EVIDENCE_SPANS:
        raise ValueError("too many evidence spans")
    return status, answer.strip(), [EvidenceSpan.from_reply(item) for item in evidence]


def judge_reply(workspace: Path, text: str, *, alias_root: Path | None = None) -> Investigation:
    """Apply the binding rule to one reply (no model involved).

    Spans are checked against ``workspace``, the real workspace. With
    ``alias_root`` (the copy the runtime read), an absolute path inside the copy
    is read as the same relative path in the real workspace.
    """
    try:
        status, answer, spans = parse_reply(text)
    except ValueError:
        return Investigation("reply_invalid", called=True)
    if alias_root is not None:
        spans = [_unalias(span, alias_root) for span in spans]
    checked: list[EvidenceSpan] = []
    failures: list[str] = []
    for span in spans:
        reason = "malformed_span" if span is None else check_span(workspace, span)
        if reason is None and span is not None:
            checked.append(span)
        else:
            failures.append(reason or "malformed_span")
    if status == "found" and answer and checked and not failures:
        outcome = "found"
    elif status == "found":
        outcome = "evidence_rejected"
    else:
        outcome = "not_found"
    return Investigation(
        outcome,
        answer=answer if outcome == "found" else "",
        checked=tuple(checked),
        failures=tuple(failures),
        cited=len(spans),
        called=True,
    )


def _unalias(span: EvidenceSpan | None, alias_root: Path) -> EvidenceSpan | None:
    if span is None or not Path(span.path).is_absolute():
        return span
    try:
        relative = Path(span.path).resolve().relative_to(alias_root.resolve())
    except (OSError, ValueError):
        return span
    return replace(span, path=relative.as_posix())


def build_prompt(goal: str, question: str, prior: list[tuple[str, str]]) -> str:
    parts = ["Goal:", goal.strip(), "", "Interview question:", question.strip()]
    if prior:
        parts += ["", "Answers already found in this repository earlier in this interview:"]
        parts += [f"- Q: {q}\n  A: {a}" for q, a in prior[-_PRIOR_FINDINGS_IN_PROMPT:]]
    parts += ["", "Reply with the JSON object only."]
    return "\n".join(parts)


@dataclass(slots=True)
class RepoInvestigator:
    """Read-only repository investigation for one interview, with a call cap."""

    runtime_backend: str
    max_calls: int = DEFAULT_MAX_CALLS
    runtime_factory: RuntimeFactory | None = None
    call_timeout_seconds: float = DEFAULT_CALL_TIMEOUT_SECONDS
    calls_made: int = 0
    unavailable: str | None = None
    resolved_model: str | None = None
    resolved_effort: str | None = None
    _found: list[tuple[str, str]] = field(default_factory=list)
    _last_error: object = None

    def _create_runtime(self, workspace: Path) -> Any:
        factory = self.runtime_factory
        if factory is None:
            from ouroboros.orchestrator.runtime_factory import create_agent_runtime

            factory = create_agent_runtime
        return factory(
            backend=self.runtime_backend,
            model=None,
            permission_mode=INVESTIGATOR_PERMISSION_MODE,
            cwd=workspace,
        )

    async def _runtime(self, cwd: Path) -> Any | None:
        """Build and preflight a runtime at ``cwd``; ``None`` marks it unavailable."""
        try:
            runtime = await asyncio.to_thread(self._create_runtime, cwd)
        except Exception as exc:  # noqa: BLE001 - no runtime means the old answerer runs
            self.unavailable = f"runtime_create_failed:{type(exc).__name__}"
            return None
        from ouroboros.orchestrator.runtime_factory import preflight_agent_runtime

        blocker = preflight_agent_runtime(runtime)
        if blocker is not None:
            self.unavailable = f"runtime_unavailable:{blocker}"
            await _close(runtime)
            return None
        return runtime

    async def _call(
        self, runtime: Any, goal: str, question: str, *, counted: bool = True
    ) -> str | None:
        """One session; the reply text, or ``None`` on timeout, ``""`` on failure.

        A failure's error is kept in ``_last_error`` so a throttled call can be
        recognized. ``counted=False`` re-issues a throttled call without using
        a call from the cap.
        """
        self.resolved_model = _runtime_attr(runtime, "_resolved_fallback_model", "_model")
        self.resolved_effort = _runtime_attr(runtime, "_resolved_fallback_reasoning_effort")
        self._last_error = None
        if counted:
            self.calls_made += 1
        try:
            result = await asyncio.wait_for(
                runtime.execute_task_to_result(
                    build_prompt(goal, question, self._found),
                    tools=list(INVESTIGATOR_TOOLS),
                    system_prompt=INVESTIGATOR_SYSTEM_PROMPT,
                ),
                timeout=self.call_timeout_seconds,
            )
        except TimeoutError:
            return None
        except Exception as exc:  # noqa: BLE001 - a failed call is not found, never a guess
            self._last_error = exc
            return ""
        finally:
            await _close(runtime)
        if result.is_err:
            self._last_error = result.error
            return ""
        return result.value.final_message or ""

    async def _call_through_throttle(
        self, runtime: Any, cwd: Path, goal: str, question: str
    ) -> tuple[str | None, float]:
        """Call once; re-issue a throttled call (HTTP 429) after a capped, doubling wait.

        Re-issues do not use calls from the cap. The waits stop at
        ``THROTTLE_TOTAL_CEILING_SECONDS``; the call then counts as failed.
        Returns the reply and the seconds waited.
        """
        reply = await self._call(runtime, goal, question)
        throttled_attempts = 0
        waited = 0.0
        while reply == "" and is_throttled(self._last_error):
            wait = throttle_backoff_seconds(throttled_attempts + 1)
            if waited + wait > THROTTLE_TOTAL_CEILING_SECONDS:
                break
            throttled_attempts += 1
            waited += wait
            log.warning(
                "auto.interview.investigator_throttle_backoff",
                wait_seconds=wait,
                throttled_attempts=throttled_attempts,
            )
            await _throttle_sleep(wait)
            runtime = await self._runtime(cwd)
            if runtime is None:
                break
            reply = await self._call(runtime, goal, question, counted=False)
        return reply, waited

    async def investigate(self, goal: str, question: str, workspace: Path) -> Investigation | None:
        """Investigate ``question``; ``None`` when no runtime is available.

        A backend with a verified read-only mode reads ``workspace`` itself.
        Any other backend reads a throwaway copy (``investigation_workspace``).
        Either way the evidence is checked against the real ``workspace``, and
        an answer is discarded when the real workspace changed meanwhile.
        """
        if self.unavailable is not None:
            return None
        if self.calls_made >= self.max_calls:
            return Investigation("cap_reached")
        before = await asyncio.to_thread(_state_or_none, workspace)
        started = time.monotonic()
        throttle_waited = 0.0
        runtime: Any | None = None
        copy: Path | None = None
        isolation = "read_only_mode"
        try:
            if self.runtime_backend in READ_ONLY_ENFORCING_BACKENDS:
                runtime = await self._runtime(workspace)
                if runtime is None:
                    return None
                if not enforces_read_only(runtime):
                    await _close(runtime)
                    runtime = None
            if runtime is None:
                isolation = "workspace_copy"
                try:
                    copy = await asyncio.to_thread(create_isolated_copy, workspace)
                except Exception:  # noqa: BLE001 - no isolation, no call
                    return Investigation("isolation_failed", isolation=isolation)
                runtime = await self._runtime(copy)
                if runtime is None:
                    return None
            reply, throttle_waited = await self._call_through_throttle(
                runtime, copy or workspace, goal, question
            )
        finally:
            if copy is not None:
                await asyncio.to_thread(remove_isolated_copy, copy)
        elapsed = time.monotonic() - started
        after = await asyncio.to_thread(_state_or_none, workspace)
        done = {
            "called": True,
            "elapsed_seconds": elapsed,
            "isolation": isolation,
            "throttle_waited_seconds": throttle_waited,
        }
        if before is not None and after is not None and before != after:
            return Investigation("workspace_changed", **done)
        if reply is None or reply == "":
            return Investigation("timeout" if reply is None else "call_failed", **done)
        if len(reply) > MAX_REPLY_CHARS:
            return Investigation("reply_invalid", **done)
        verdict = judge_reply(workspace, reply, alias_root=copy)
        if verdict.found:
            self._found.append((question.strip(), verdict.answer))
        return Investigation(
            verdict.outcome,
            answer=verdict.answer,
            checked=verdict.checked,
            failures=verdict.failures,
            cited=verdict.cited,
            **done,
        )


async def _close(runtime: Any) -> None:
    closer = getattr(runtime, "aclose", None)
    if closer is not None and inspect.iscoroutinefunction(closer):
        await closer()


def _state_or_none(workspace: Path) -> str | None:
    """The real workspace fingerprint, or ``None`` when it cannot be taken."""
    try:
        return workspace_state(workspace)
    except Exception:  # noqa: BLE001 - the check is a second line; isolation is the first
        return None


def enforces_read_only(runtime: Any) -> bool:
    """Whether the built ``runtime`` will run read-only, read off the instance.

    Codex: the exec flags it would pass are ``--sandbox read-only``. Claude:
    its permission mode is ``default``. Any other runtime: no.
    """
    backend = getattr(type(runtime), "_runtime_backend", None)
    if backend == "codex":
        build_args = getattr(runtime, "_build_permission_args", None)
        return callable(build_args) and list(build_args()) == ["--sandbox", "read-only"]
    if backend == "claude":
        return getattr(runtime, "permission_mode", None) == INVESTIGATOR_PERMISSION_MODE
    return False


def _runtime_attr(runtime: Any, *names: str) -> str | None:
    for name in names:
        value = getattr(runtime, name, None)
        if isinstance(value, str) and value.strip() and value.strip().lower() != "default":
            return value.strip()
    return None


def _evidence_lines(spans: tuple[EvidenceSpan, ...]) -> list[str]:
    return [f"- {span.label}: {' '.join(span.quote.split())}" for span in spans]


def found_answer(
    draft: AutoAnswer, found: Investigation, question: str, round_number: int
) -> AutoAnswer:
    """The answer read from the repository, as a confirmed checked repo fact.

    Its entries settle the sections the draft targeted, but they never become a
    constraint, criterion or ontology field: they reach the worker only as
    ``Seed.repo_facts`` (``ledger_seed.ledger_repo_facts``), and the transcript
    declares provenance ``repo_fact`` so Seed extraction withholds it. Each key
    is ``<facet>.repo_fact.<round>``, so two found answers for one facet in
    different rounds are separate facts instead of a same-key tie.
    """
    spans = [f"{span.label}: {' '.join(span.quote.split())}" for span in found.checked]
    updates = [
        (
            section,
            LedgerEntry(
                key=f"{entry.key}{REPO_FACT_KEY_INFIX}{round_number}",
                value=found.answer,
                source=LedgerSource.REPO_FACT,
                confidence=0.9,
                status=LedgerStatus.CONFIRMED,
                rationale="Read from the repository; every cited span passed the evidence check.",
                evidence=spans,
                question=question,
            ),
        )
        for section, entry in draft.ledger_updates
    ]
    text = "\n".join(
        [found.answer, "", "Evidence from the repository:", *_evidence_lines(found.checked)]
    )
    return AutoAnswer(
        text, AutoAnswerSource.REPO_FACT, 0.9, updates, declared_provenance="repo_fact"
    )


def not_found_answer(
    draft: AutoAnswer,
    result: Investigation,
    question: str,
    round_number: int,
    *,
    open_for_implementer: bool = False,
) -> AutoAnswer:
    """What the checked code shows, and that it does not settle the question.

    No model answer text is kept. Each entry is a non-binding assumption
    (``DEFAULTED``) that carries the question, keyed ``.investigated.<round>``.
    ``DEFAULTED`` counts as resolved for the ledger, so the section is closed
    for interview readiness and Seed completeness, and the worker sees the
    question as an ``Open question:`` line. ``open_for_implementer`` (no
    requester) also tells the interviewer that the question stays open for the
    implementer.
    """
    labels = [span.label for span in result.checked]
    updates = [
        (
            section,
            LedgerEntry(
                key=f"{entry.key}.investigated.{round_number}",
                value=NOT_SETTLED_VALUE,
                source=LedgerSource.ASSUMPTION,
                confidence=0.5,
                status=LedgerStatus.DEFAULTED,
                rationale="The repository was read; the code does not settle this question.",
                evidence=labels,
                question=question,
            ),
        )
        for section, entry in draft.ledger_updates
    ]
    lines = (
        [f"{NOT_SETTLED_REPLY} {OPEN_FOR_IMPLEMENTER_REPLY}"]
        if open_for_implementer
        else [NOT_SETTLED_REPLY]
    )
    if result.checked:
        lines += ["", "What the code shows:", *_evidence_lines(result.checked)]
    return AutoAnswer("\n".join(lines), AutoAnswerSource.ASSUMPTION, 0.5, updates)


def _clip(text: str, limit: int = _LOG_TEXT_LIMIT) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else f"{flat[: limit - 3]}..."


async def _investigate(
    driver: AutoInterviewDriver,
    draft: AutoAnswer,
    question: str,
    turn: InterviewTurn,
    state: AutoPipelineState,
    round_number: int,
) -> AutoAnswer | None:
    investigator = driver.investigator
    if (
        investigator is None
        or question != turn.question
        or draft.blocker is not None
        or draft.source not in ROUTED_SOURCES
        or not draft.ledger_updates
    ):
        return None
    state.mark_progress(f"investigating the repository (round {round_number}/{driver.max_rounds})")
    driver._save(state)
    # The interview waits for the investigation; its phase clock does not run.
    async with phase_clock_paused(state):
        result = await investigator.investigate(state.goal, question, Path(state.cwd))
    if result is None:
        log.warning(
            "auto.interview.investigator_unavailable",
            auto_session_id=state.auto_session_id,
            reason=investigator.unavailable,
        )
        return None
    data: dict[str, object] = {
        "round": round_number,
        "outcome": result.outcome,
        "called": result.called,
        "calls_made": investigator.calls_made,
        "max_calls": investigator.max_calls,
        "evidence_cited": result.cited,
        "evidence_passed": len(result.checked),
        "evidence_failures": list(result.failures),
        "elapsed_seconds": round(result.elapsed_seconds, 3),
        "isolation": result.isolation,
        "throttle_waited_seconds": round(result.throttle_waited_seconds, 3),
        "runtime_backend": investigator.runtime_backend,
        "model": investigator.resolved_model,
        "reasoning_effort": investigator.resolved_effort,
        "question": _clip(question),
    }
    log.info("auto.interview.investigated", auto_session_id=state.auto_session_id, **data)
    await driver._emit_event(
        "auto.interview.investigated",
        state.auto_session_id,
        aggregate_type="auto_session",
        **data,
    )
    if result.outcome == "workspace_changed":
        log.warning("auto.interview.investigator_workspace_changed", **data)
        await driver._emit_event(
            "auto.interview.investigator_workspace_changed",
            state.auto_session_id,
            aggregate_type="auto_session",
            **data,
        )
    if result.found:
        return found_answer(draft, result, question, round_number)
    return not_found_answer(
        draft, result, question, round_number, open_for_implementer=driver.requester is None
    )


async def settle(
    driver: AutoInterviewDriver,
    answer: AutoAnswer,
    question: str,
    turn: InterviewTurn,
    state: AutoPipelineState,
    ledger: SeedDraftLedger,
    round_number: int,
) -> AutoAnswer:
    """Settle one round's draft: investigate, else refine; then ask the requester.

    Without an investigator (or with no runtime) this is the earlier sequence
    unchanged: ``_refine_answer`` then ``ask_requester``. An investigated
    answer skips the refiner. A found answer has source ``repo_fact`` and so
    is never routed to the requester; a not-found answer has source
    ``assumption`` and is routed with the factual reply as its draft.
    """
    investigated = await _investigate(driver, answer, question, turn, state, round_number)
    if investigated is None:
        answer = await driver._refine_answer(answer, question, state, ledger)
    else:
        answer = investigated
    return await ask_requester(driver, answer, question, turn, state, round_number)


CAP_CLOSURE_MODE = "ledger_only"


async def close_known(
    driver: AutoInterviewDriver,
    state: AutoPipelineState,
    ledger: SeedDraftLedger,
    outcome: _InterviewRoundOutcome,
) -> AutoInterviewResult | None:
    """Without a requester, close the interview with what is known.

    Called after each round. It acts only when no requester is wired and the
    investigator has run, and only in one of two cases:

    - the investigator spent its last call: every later question could only
      end ``cap_reached`` (not found without a call), so more rounds add
      nothing and run toward the interview deadline;
    - the backend reports the interview complete (``seed_ready`` or
      ``completed``) and the ledger is complete, but the backend ambiguity is
      above the gate. A not-settled question counts as closed for readiness
      without a requester, so the open questions alone must not keep the
      interview going.

    When the backend already confirmed readiness within the gate, the
    mutual-agreement close handles it and this does nothing.

    It closes with what is known: sections that no question reached get the
    existing safe-default policy (``finalize_safe_defaultable_gaps``, the same
    as at ``max_rounds``), pushed to the transcript as an assumption. Answered
    and investigated sections stay as they are; a not-settled question stays
    an open question for the worker. The closure mode is ``ledger_only``: the
    ledger is complete, so the backend ambiguity score is not the gate. When
    the ledger cannot be completed (an unsafe gap) or the synthesis cannot be
    recorded, nothing changes and the interview goes on under the existing
    rules.
    """
    from ouroboros.auto.answer_binding import with_provenance
    from ouroboros.auto.interview_driver import (
        AutoInterviewResult,
        _backend_confirmed_seed_ready,
        _revert_safe_default_entries,
        _validate_turn,
    )
    from ouroboros.auto.interview_recovery import with_timeout
    from ouroboros.auto.safe_defaults import (
        build_safe_default_synthesis,
        finalize_safe_defaultable_gaps,
    )

    turn = outcome.turn
    investigator = driver.investigator
    if (
        investigator is None
        or driver.requester is not None
        or investigator.unavailable is not None
        or investigator.calls_made == 0
        or (ledger.is_seed_ready() and _backend_confirmed_seed_ready(turn))
    ):
        return None
    cap_spent = investigator.calls_made >= investigator.max_calls
    backend_done_open = bool(turn.seed_ready or turn.completed) and ledger.is_seed_ready()
    if not (cap_spent or backend_done_open):
        return None
    open_before = list(ledger.open_gaps())
    defaulted: tuple[str, ...] = ()
    if open_before:
        finalization = finalize_safe_defaultable_gaps(
            ledger,
            goal=state.goal,
            provenance="auto interview closed with what is known (no requester)",
            pending_question=turn.question,
            active_profile=getattr(driver.answerer, "active_profile", None),
        )
        if finalization is None or not finalization.completed or not ledger.is_seed_ready():
            if finalization is not None:
                _revert_safe_default_entries(ledger, finalization.defaulted_sections)
            return None
        defaulted = tuple(finalization.defaulted_sections)
        synthesis = build_safe_default_synthesis(finalization)
        if synthesis and state.interview_session_id:
            try:
                synthesis_turn = _validate_turn(
                    await with_timeout(
                        with_provenance(driver.backend, "assumption").answer(
                            state.interview_session_id,
                            synthesis,
                            last_question="[driver closure: interview closed with what is known]",
                        ),
                        state,
                        tool_name="interview.closure_synthesis",
                        timeout_seconds=driver.timeout_seconds,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - keep the transcript and ledger in sync
                _revert_safe_default_entries(ledger, defaulted)
                log.warning(
                    "auto.interview.closure_synthesis_failed",
                    auto_session_id=state.auto_session_id,
                    error=str(exc),
                )
                return None
            state.interview_session_id = synthesis_turn.session_id
    data: dict[str, object] = {
        "reason": "call_cap" if cap_spent else "backend_done_with_open_questions",
        "calls_made": investigator.calls_made,
        "max_calls": investigator.max_calls,
        "open_gaps_before": open_before,
        "defaulted_sections": list(defaulted),
        "closure_mode": CAP_CLOSURE_MODE,
        "round": state.current_round,
    }
    log.info(
        "auto.interview.closed_with_open_questions", auto_session_id=state.auto_session_id, **data
    )
    await driver._emit_event(
        "auto.interview.closed_with_open_questions",
        state.auto_session_id,
        aggregate_type="auto_session",
        **data,
    )
    state.ledger = ledger.to_dict()
    state.pending_question = None
    state.interview_completed = True
    state.interview_closure_mode = CAP_CLOSURE_MODE
    state.mark_progress(
        "interview closed with what is known: "
        + ("investigator call cap reached" if cap_spent else "open questions left to the worker"),
        tool_name="interview_driver",
    )
    driver._save(state)
    return AutoInterviewResult(
        "seed_ready", state.interview_session_id, ledger, state.current_round
    )


def build_repo_investigator(
    runtime_backend: str | None,
    opencode_mode: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> RepoInvestigator | None:
    """The investigator for one auto interview, or ``None`` (old answerer).

    ``None`` when the cap is 0, no backend is named (or the name is unknown),
    or the stage dispatches through a host plugin (the synchronous interview
    cannot consume that). Every other backend gets one: a backend in
    :data:`READ_ONLY_ENFORCING_BACKENDS` reads the workspace in its read-only
    mode, any other reads a throwaway copy.
    """
    max_calls = resolve_max_calls(environ)
    call_timeout = resolve_call_timeout(environ)
    if max_calls == 0 or not runtime_backend:
        return None
    from ouroboros.mcp.tools.subagent import should_dispatch_via_plugin
    from ouroboros.orchestrator.runtime_factory import resolve_agent_runtime_backend

    try:
        resolved = resolve_agent_runtime_backend(runtime_backend)
    except ValueError:
        return None
    if should_dispatch_via_plugin(runtime_backend, opencode_mode):
        return None
    return RepoInvestigator(
        runtime_backend=resolved, max_calls=max_calls, call_timeout_seconds=call_timeout
    )


__all__ = [
    "CALL_TIMEOUT_ENV",
    "DEFAULT_CALL_TIMEOUT_SECONDS",
    "DEFAULT_MAX_CALLS",
    "INVESTIGATOR_PERMISSION_MODE",
    "INVESTIGATOR_TOOLS",
    "MAX_CALLS_ENV",
    "NOT_SETTLED_REPLY",
    "NOT_SETTLED_VALUE",
    "READ_ONLY_ENFORCING_BACKENDS",
    "Investigation",
    "RepoInvestigator",
    "build_repo_investigator",
    "close_known",
    "enforces_read_only",
    "found_answer",
    "judge_reply",
    "not_found_answer",
    "parse_reply",
    "resolve_call_timeout",
    "resolve_max_calls",
    "settle",
]
