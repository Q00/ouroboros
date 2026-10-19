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
  answer binds as a confirmed repository fact (``LedgerSource.REPO_FACT``).
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
(``boundary/constructor.py``).

Budget: at most ``max_calls`` calls per interview (default 12,
``OUROBOROS_AUTO_INVESTIGATOR_MAX_CALLS``; 0 turns the investigator off), and
all calls together use at most half of the interview phase timeout. A question
past either limit is not found without a call. Each call is a fresh session;
earlier found answers are passed in the prompt as context.

When no runtime can be built (no backend, a plugin-dispatched stage, a failed
preflight), the deterministic answerer and refiner run exactly as before.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import inspect
import json
import os
from pathlib import Path
import time
from typing import TYPE_CHECKING, Any

import structlog

from ouroboros.auto.answerer import AutoAnswer, AutoAnswerSource
from ouroboros.auto.evidence_check import EvidenceSpan, check_span
from ouroboros.auto.ledger import LedgerEntry, LedgerSource, LedgerStatus
from ouroboros.auto.requester import ROUTED_SOURCES, ask_requester

if TYPE_CHECKING:
    from ouroboros.auto.interview_driver import AutoInterviewDriver, InterviewTurn
    from ouroboros.auto.ledger import SeedDraftLedger
    from ouroboros.auto.state import AutoPipelineState

log = structlog.get_logger(__name__)

INVESTIGATOR_TOOLS: tuple[str, ...] = ("Read", "Glob", "Grep")
INVESTIGATOR_PERMISSION_MODE = "default"
MAX_CALLS_ENV = "OUROBOROS_AUTO_INVESTIGATOR_MAX_CALLS"
DEFAULT_MAX_CALLS = 12
# Share of the interview phase timeout all investigator calls may use together.
BUDGET_SHARE_OF_PHASE = 0.5
# Upper bound of one call, and the least time worth starting a call with.
MAX_CALL_SECONDS = 300.0
MIN_CALL_SECONDS = 30.0
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
    ``call_failed``, ``cap_reached`` or ``budget_spent``. Only ``found``
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


def judge_reply(workspace: Path, text: str) -> Investigation:
    """Apply the binding rule to one reply (no model involved)."""
    try:
        status, answer, spans = parse_reply(text)
    except ValueError:
        return Investigation("reply_invalid", called=True)
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
    calls_made: int = 0
    spent_seconds: float = 0.0
    unavailable: str | None = None
    resolved_model: str | None = None
    resolved_effort: str | None = None
    _found: list[tuple[str, str]] = field(default_factory=list)

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

    async def investigate(
        self, goal: str, question: str, workspace: Path, *, budget_seconds: float
    ) -> Investigation | None:
        """Investigate ``question``; ``None`` when no runtime is available."""
        if self.unavailable is not None:
            return None
        if self.calls_made >= self.max_calls:
            return Investigation("cap_reached")
        remaining = budget_seconds - self.spent_seconds
        if remaining < MIN_CALL_SECONDS:
            return Investigation("budget_spent")
        try:
            runtime = await asyncio.to_thread(self._create_runtime, workspace)
        except Exception as exc:  # noqa: BLE001 - no runtime means the old answerer runs
            self.unavailable = f"runtime_create_failed:{type(exc).__name__}"
            return None
        from ouroboros.orchestrator.runtime_factory import preflight_agent_runtime

        blocker = preflight_agent_runtime(runtime)
        if blocker is not None:
            self.unavailable = f"runtime_unavailable:{blocker}"
            return None
        self.resolved_model = _runtime_attr(runtime, "_resolved_fallback_model", "_model")
        self.resolved_effort = _runtime_attr(runtime, "_resolved_fallback_reasoning_effort")
        self.calls_made += 1
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(
                runtime.execute_task_to_result(
                    build_prompt(goal, question, self._found),
                    tools=list(INVESTIGATOR_TOOLS),
                    system_prompt=INVESTIGATOR_SYSTEM_PROMPT,
                ),
                timeout=min(MAX_CALL_SECONDS, remaining),
            )
        except TimeoutError:
            result = None
        except Exception:  # noqa: BLE001 - a failed call is not found, never a guess
            result = False
        finally:
            closer = getattr(runtime, "aclose", None)
            if closer is not None and inspect.iscoroutinefunction(closer):
                await closer()
        elapsed = time.monotonic() - started
        self.spent_seconds += elapsed
        if result is None or result is False or result.is_err:
            outcome = "timeout" if result is None else "call_failed"
            return Investigation(outcome, called=True, elapsed_seconds=elapsed)
        reply = result.value.final_message or ""
        if len(reply) > MAX_REPLY_CHARS:
            return Investigation("reply_invalid", called=True, elapsed_seconds=elapsed)
        verdict = judge_reply(workspace, reply)
        if verdict.found:
            self._found.append((question.strip(), verdict.answer))
        return Investigation(
            verdict.outcome,
            answer=verdict.answer,
            checked=verdict.checked,
            failures=verdict.failures,
            cited=verdict.cited,
            called=True,
            elapsed_seconds=elapsed,
        )


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
    """The answer read from the repository, bound as a confirmed repo fact.

    It fills the sections the draft targeted. Each entry key gets a
    ``.repo.<round>`` suffix, so two found answers for the same facet in
    different rounds are separate facts instead of a same-key tie.
    """
    labels = [span.label for span in found.checked]
    updates = [
        (
            section,
            LedgerEntry(
                key=f"{entry.key}.repo.{round_number}",
                value=found.answer,
                source=LedgerSource.REPO_FACT,
                confidence=0.9,
                status=LedgerStatus.CONFIRMED,
                rationale="Read from the repository; every cited span passed the evidence check.",
                evidence=labels,
                question=question,
            ),
        )
        for section, entry in draft.ledger_updates
    ]
    text = "\n".join(
        [found.answer, "", "Evidence from the repository:", *_evidence_lines(found.checked)]
    )
    non_goals = [found.answer] if any(s == "non_goals" for s, _ in updates) else []
    return AutoAnswer(text, AutoAnswerSource.REPO_FACT, 0.9, updates, non_goals=non_goals)


def not_found_answer(
    draft: AutoAnswer, result: Investigation, question: str, round_number: int
) -> AutoAnswer:
    """What the checked code shows, and that it does not settle the question.

    No model answer text is kept. Each entry is a non-binding assumption
    (``DEFAULTED``) that carries the question, keyed ``.investigated.<round>``.
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
    lines = [NOT_SETTLED_REPLY]
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
    result = await investigator.investigate(
        state.goal,
        question,
        Path(state.cwd),
        budget_seconds=driver.timeout_seconds * BUDGET_SHARE_OF_PHASE,
    )
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
    if result.found:
        return found_answer(draft, result, question, round_number)
    return not_found_answer(draft, result, question, round_number)


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


def build_repo_investigator(
    runtime_backend: str | None,
    opencode_mode: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> RepoInvestigator | None:
    """The investigator for one auto interview, or ``None`` (old answerer).

    ``None`` when the cap is 0, no backend is named, or the stage dispatches
    through a host plugin (the synchronous interview cannot consume that).
    """
    max_calls = resolve_max_calls(environ)
    if max_calls == 0 or not runtime_backend:
        return None
    from ouroboros.mcp.tools.subagent import should_dispatch_via_plugin

    if should_dispatch_via_plugin(runtime_backend, opencode_mode):
        return None
    return RepoInvestigator(runtime_backend=runtime_backend, max_calls=max_calls)


__all__ = [
    "DEFAULT_MAX_CALLS",
    "INVESTIGATOR_PERMISSION_MODE",
    "INVESTIGATOR_TOOLS",
    "MAX_CALLS_ENV",
    "NOT_SETTLED_REPLY",
    "NOT_SETTLED_VALUE",
    "Investigation",
    "RepoInvestigator",
    "build_repo_investigator",
    "found_answer",
    "judge_reply",
    "not_found_answer",
    "parse_reply",
    "resolve_max_calls",
    "settle",
]
