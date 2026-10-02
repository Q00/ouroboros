"""A model's judgement of whether a cited passing call checks the criterion, as a veto only.

The evidence turn proves a ``tests_passed`` citation from the controller's own
records: the call exists, passed, is the latest run of its command, and runs a
verification invocation. Whether that verification is about this criterion is
not structural, so a model is asked. Its answer can only withhold the
citation's evidence (``WithheldReason.RELEVANCE_VETO``); it can never prove a
citation the records did not prove. Any answer other than an explicit
``not_relevant`` for a listed number, and any failure to get one, changes
nothing.

The check is on by default (``execution.evidence_relevance_veto``) and its
decision is recorded per cited call (``RelevanceDecision``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from ouroboros.observability.logging import get_logger
from ouroboros.orchestrator.evidence.call_ledger import RecordedCall
from ouroboros.orchestrator.evidence.cited_evidence import RelevanceDecision
from ouroboros.orchestrator.evidence_schema import EvidenceError, extract_evidence

log = get_logger(__name__)

RELEVANT = "relevant"
NOT_RELEVANT = "not_relevant"
UNDECIDED = "undecided"
UNAVAILABLE = "unavailable"

_PROMPT = """You review evidence for one acceptance criterion of a software task.
Each numbered row is a shell call the controller recorded, with its recorded
result and its recorded command. For each row, decide whether running that
command checks the behavior the criterion states.

Criterion:
{criterion}

Recorded calls:
{rows}

Reply with exactly one JSON object mapping each row number to "relevant" or
"not_relevant", for example {{"3": "relevant", "7": "not_relevant"}}. Answer
"not_relevant" only when the command does not exercise the behavior the
criterion states. Do not add any other text."""


class RelevanceJudge(Protocol):
    """Decides, per recorded call, whether it checks the criterion."""

    async def __call__(
        self, *, criterion: str, calls: Sequence[RecordedCall]
    ) -> Mapping[int, str]: ...


def _parse_answers(text: str, numbers: frozenset[int]) -> dict[int, str]:
    try:
        record = extract_evidence(text)
    except EvidenceError:
        return {}
    answers: dict[int, str] = {}
    for key, value in record.data.items():
        try:
            number = int(str(key).strip())
        except ValueError:
            continue
        if number in numbers and value in (RELEVANT, NOT_RELEVANT):
            answers[number] = value
    return answers


class LLMRelevanceJudge:
    """The relevance check through one tool-less model completion."""

    def __init__(self, *, cwd: str | None = None, adapter: Any = None) -> None:
        self._cwd = cwd
        self._adapter = adapter

    def _llm(self) -> Any:
        if self._adapter is None:
            from ouroboros.providers.factory import create_llm_adapter

            self._adapter = create_llm_adapter(allowed_tools=[], max_turns=1, cwd=self._cwd)
        return self._adapter

    async def __call__(self, *, criterion: str, calls: Sequence[RecordedCall]) -> Mapping[int, str]:
        from ouroboros.providers.base import CompletionConfig, Message, MessageRole

        rows = "\n".join(
            f"[{call.number}] {call.result_label()} :: {call.command}" for call in calls
        )
        response = await self._llm().complete(
            [
                Message(
                    role=MessageRole.USER, content=_PROMPT.format(criterion=criterion, rows=rows)
                )
            ],
            CompletionConfig(
                model="default", role="evidence_relevance", temperature=0.0, max_tokens=800
            ),
        )
        if response.is_err:
            raise RuntimeError(f"relevance check failed: {response.error}")
        return _parse_answers(response.value.content, frozenset(call.number for call in calls))


async def judge_relevance(
    judge: RelevanceJudge | None,
    *,
    criterion: str,
    calls: Sequence[RecordedCall],
) -> tuple[RelevanceDecision, ...]:
    """One recorded decision per call; only an explicit ``not_relevant`` vetoes."""
    if not calls:
        return ()
    if judge is None:
        return tuple(
            RelevanceDecision(number=call.number, relevant=None, status=UNAVAILABLE)
            for call in calls
        )
    try:
        answers = await judge(criterion=criterion, calls=calls)
    except Exception as exc:  # the check can only withhold; its failure withholds nothing
        log.warning("evidence.relevance_veto.unavailable", error=type(exc).__name__)
        return tuple(
            RelevanceDecision(number=call.number, relevant=None, status=UNAVAILABLE)
            for call in calls
        )
    decisions = []
    for call in calls:
        answer = answers.get(call.number)
        if answer == NOT_RELEVANT:
            decisions.append(RelevanceDecision(call.number, relevant=False, status=NOT_RELEVANT))
        elif answer == RELEVANT:
            decisions.append(RelevanceDecision(call.number, relevant=True, status=RELEVANT))
        else:
            decisions.append(RelevanceDecision(call.number, relevant=None, status=UNDECIDED))
    return tuple(decisions)


__all__ = [
    "NOT_RELEVANT",
    "RELEVANT",
    "UNAVAILABLE",
    "UNDECIDED",
    "LLMRelevanceJudge",
    "RelevanceJudge",
    "judge_relevance",
]
