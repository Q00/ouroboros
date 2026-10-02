"""The evidence turn: the worker cites the controller's recorded calls by number.

After a worker's turn completes, the controller resumes the same session for
one short, tool-less turn. It shows the worker its own record of the shell
calls the worker made (``call_ledger``), ordered by structural features
(``call_ordering``), and asks the worker to cite, for the current criterion,
the call numbers that are its evidence. The reply is parsed as numbers only.
The controller then replays cited ``tests_passed`` calls on the final artifact
where it can, asks the relevance check (a veto only), and attaches the result
to the typed evidence record (``EvidenceRecord.cited``) for the verifier.

This module holds the pieces that do not touch the dispatch capsule: the
prompt, the reply parser, cited-call replay, and assembly of
``CitedEvidence``. ``orchestrator/evidence_turn_dispatch.py`` runs the turn
through the executor's provider boundary.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from ouroboros.config.models import EvidenceCallOrderingWeights
from ouroboros.observability.logging import get_logger
from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.evidence.call_ledger import (
    CallResult,
    RecordedCall,
    build_call_ledger,
)
from ouroboros.orchestrator.evidence.call_ordering import order_calls, render_call_list
from ouroboros.orchestrator.evidence.cited_evidence import (
    CITABLE_FIELDS,
    COMMANDS_RUN,
    TESTS_PASSED,
    Citations,
    CitedEvidence,
    ReplayOutcome,
)
from ouroboros.orchestrator.evidence.command_replay import (
    MAX_REPLAYED_COMMANDS,
    replay_candidate,
    replay_commands,
)
from ouroboros.orchestrator.evidence.harness_observation import is_harness_observation_message
from ouroboros.orchestrator.evidence.relevance_veto import RelevanceJudge, judge_relevance
from ouroboros.orchestrator.evidence_schema import EvidenceError, extract_evidence

log = get_logger(__name__)

# Runtimes whose shell result carries no exit status, so the structured
# ``is_error`` bit of the tool result is the recorded verdict.
IS_ERROR_EXIT_VERDICT_BACKENDS = frozenset({"claude"})

REPLY_UNPARSEABLE = "reply_unparseable"
REPLY_NOT_NUMBERS = "reply_not_numbers"
TURN_RAN_A_TOOL = "evidence_turn_ran_a_tool"


@dataclass(frozen=True, slots=True)
class EvidenceTurnSettings:
    """Settings of the evidence turn (``execution`` config section)."""

    relevance_veto: bool = True
    weights: EvidenceCallOrderingWeights = field(default_factory=EvidenceCallOrderingWeights)

    @classmethod
    def from_config(cls) -> EvidenceTurnSettings:
        """Read the settings from config; the defaults when config cannot be read."""
        try:
            from ouroboros.config.loader import load_config

            execution = load_config().execution
        except Exception:
            return cls()
        return cls(
            relevance_veto=execution.evidence_relevance_veto,
            weights=execution.evidence_call_ordering,
        )


def citable_fields(required_fields: Sequence[str]) -> tuple[str, ...]:
    """The required evidence fields the evidence turn asks the worker to cite."""
    return tuple(name for name in CITABLE_FIELDS if name in required_fields)


_FIELD_INSTRUCTIONS = {
    TESTS_PASSED: (
        "tests_passed: calls that ran a test or check of this criterion and passed. "
        "Cite a call only if the latest call that ran the same command also passed."
    ),
    COMMANDS_RUN: (
        "commands_run: calls that ran a validation or production command for this "
        "criterion (test, build, lint, generation, docs check)."
    ),
}


def render_evidence_turn_prompt(
    *,
    ac_content: str,
    ordered: Sequence[RecordedCall],
    fields: Sequence[str],
) -> str:
    """The evidence turn's prompt: the numbered list and the citation contract."""
    example = ", ".join(f'"{name}": [<numbers>]' for name in fields)
    instructions = "\n".join(f"- {_FIELD_INSTRUCTIONS[name]}" for name in fields)
    return (
        "[EVIDENCE TURN - harness-injected]\n"
        "Your work on this task is finished. Do not run any tool or command in this turn.\n"
        "Below is the controller's own record of the shell calls in your session: "
        "the call number, the result the runtime recorded, and the recorded command. "
        "The most likely evidence is listed first; every call keeps its number.\n\n"
        f"{render_call_list(ordered)}\n\n"
        f"Acceptance criterion:\n{ac_content}\n\n"
        "Cite this criterion's evidence by call number only:\n"
        f"{instructions}\n\n"
        "Reply with exactly one JSON object and nothing else: "
        f"{{{example}}}. Use [] when no call qualifies. Cite only numbers listed "
        "above, and never cite a failed call under tests_passed."
    )


def _numbers(value: object) -> tuple[int, ...] | None:
    if not isinstance(value, list):
        return None
    numbers: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            return None
        numbers.append(item)
    return tuple(numbers)


def parse_citations(reply: str, fields: Sequence[str]) -> tuple[Citations | None, str | None]:
    """The cited numbers per field, or ``(None, reason)`` when the reply is unusable.

    A field the reply omits is cited with no numbers. A value that is not a
    list of integers makes the whole reply unusable: numbers are the only
    accepted form, so nothing is guessed from text.
    """
    try:
        record = extract_evidence(reply)
    except EvidenceError:
        return None, REPLY_UNPARSEABLE
    cited: dict[str, tuple[int, ...]] = {}
    for name in fields:
        if name not in record.data:
            cited[name] = ()
            continue
        numbers = _numbers(record.data[name])
        if numbers is None:
            return None, REPLY_NOT_NUMBERS
        cited[name] = numbers
    return (
        Citations(
            tests_passed=cited.get(TESTS_PASSED, ()),
            commands_run=cited.get(COMMANDS_RUN, ()),
        ),
        None,
    )


def turn_ran_a_tool(messages: Sequence[AgentMessage]) -> bool:
    """Whether the evidence turn's own messages include a tool call."""
    return any(
        message.tool_name is not None and not is_harness_observation_message(message)
        for message in messages
    )


async def replay_cited_calls(
    citations: Citations,
    ledger: Sequence[RecordedCall],
    *,
    task_cwd: str | None,
    env: dict[str, str],
    timeout_seconds: float,
    sandbox_enabled: bool | None,
) -> tuple[ReplayOutcome, ...]:
    """Replay each cited passing ``tests_passed`` call on the final artifact, where replayable.

    A call that is not a replay candidate, that the allowlist refuses, or
    that cannot run because no execution sandbox is available yields no
    outcome: its recorded result stands. The caller decides whether replay is
    permitted at all (verify gate on, the worker held Bash authority).
    """
    if task_cwd is None:
        return ()
    by_number = {call.number: call for call in ledger}
    candidates = []
    numbers: list[int] = []
    for number in dict.fromkeys(citations.tests_passed):
        call = by_number.get(number)
        if call is None or call.result is not CallResult.PASSED or not call.in_workspace:
            continue
        candidate = replay_candidate(call.command, task_cwd, transcript_returncode=call.exit_status)
        if candidate is None:
            continue
        candidates.append(candidate)
        numbers.append(number)
        if len(candidates) >= MAX_REPLAYED_COMMANDS:
            break
    outcomes: list[ReplayOutcome] = []
    for number, candidate in zip(numbers, candidates, strict=True):
        runs = await replay_commands(
            (candidate,),
            workspace=task_cwd,
            env=env,
            timeout_seconds=timeout_seconds,
            sandbox_enabled=sandbox_enabled,
        )
        if runs:
            outcomes.append(ReplayOutcome(number=number, succeeded=runs[0].succeeded))
    return tuple(outcomes)


def relevance_candidates(
    citations: Citations, ledger: Sequence[RecordedCall]
) -> tuple[RecordedCall, ...]:
    """Cited ``tests_passed`` calls whose recorded result passed (what the veto may withhold)."""
    by_number = {call.number: call for call in ledger}
    return tuple(
        call
        for number in dict.fromkeys(citations.tests_passed)
        if (call := by_number.get(number)) is not None and call.result is CallResult.PASSED
    )


@dataclass(frozen=True, slots=True)
class ReplayPermission:
    """Whether and how the controller may replay cited calls."""

    allowed: bool
    env: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = 600.0
    sandbox_enabled: bool | None = None


async def build_cited_evidence(
    *,
    reply: str,
    reply_messages: Sequence[AgentMessage],
    primary_messages: Sequence[AgentMessage],
    ac_content: str,
    fields: Sequence[str],
    task_cwd: str | None,
    runtime_backend: str,
    settings: EvidenceTurnSettings,
    judge: RelevanceJudge | None,
    replay: ReplayPermission,
) -> CitedEvidence:
    """Assemble what the verifier needs from the evidence turn's reply."""
    is_error_verdict = runtime_backend in IS_ERROR_EXIT_VERDICT_BACKENDS
    if turn_ran_a_tool(reply_messages):
        return CitedEvidence(
            citations=None, is_error_is_exit_verdict=is_error_verdict, reply_error=TURN_RAN_A_TOOL
        )
    citations, error = parse_citations(reply, fields)
    if citations is None:
        return CitedEvidence(
            citations=None, is_error_is_exit_verdict=is_error_verdict, reply_error=error
        )
    ledger = build_call_ledger(
        primary_messages, task_cwd=task_cwd, is_error_is_exit_verdict=is_error_verdict
    )
    replays: tuple[ReplayOutcome, ...] = ()
    if replay.allowed:
        replays = await replay_cited_calls(
            citations,
            ledger,
            task_cwd=task_cwd,
            env=replay.env,
            timeout_seconds=replay.timeout_seconds,
            sandbox_enabled=replay.sandbox_enabled,
        )
    relevance = ()
    if settings.relevance_veto:
        relevance = await judge_relevance(
            judge, criterion=ac_content, calls=relevance_candidates(citations, ledger)
        )
    return CitedEvidence(
        citations=citations,
        is_error_is_exit_verdict=is_error_verdict,
        replays=replays,
        relevance=relevance,
    )


def evidence_turn_prompt_for(
    *,
    primary_messages: Sequence[AgentMessage],
    ac_content: str,
    fields: Sequence[str],
    task_cwd: str | None,
    runtime_backend: str,
    settings: EvidenceTurnSettings,
) -> str | None:
    """The evidence turn prompt, or None when there is nothing to cite."""
    if not fields:
        return None
    ledger = build_call_ledger(
        primary_messages,
        task_cwd=task_cwd,
        is_error_is_exit_verdict=runtime_backend in IS_ERROR_EXIT_VERDICT_BACKENDS,
    )
    if not ledger:
        return None
    ordered = order_calls(
        ledger, messages=primary_messages, task_cwd=task_cwd, weights=settings.weights
    )
    return render_evidence_turn_prompt(ac_content=ac_content, ordered=ordered, fields=fields)


__all__ = [
    "IS_ERROR_EXIT_VERDICT_BACKENDS",
    "REPLY_NOT_NUMBERS",
    "REPLY_UNPARSEABLE",
    "TURN_RAN_A_TOOL",
    "EvidenceTurnSettings",
    "ReplayPermission",
    "build_cited_evidence",
    "citable_fields",
    "evidence_turn_prompt_for",
    "parse_citations",
    "relevance_candidates",
    "render_evidence_turn_prompt",
    "replay_cited_calls",
    "turn_ran_a_tool",
]
