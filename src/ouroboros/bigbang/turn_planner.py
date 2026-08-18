"""Atomic planning of an interview's next question and ambiguity snapshot."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

import structlog

from ouroboros.bigbang.ambiguity import AmbiguityScore, AmbiguityScorer, dimension_specs
from ouroboros.bigbang.interview import (
    AGENT_SDK_CLI_FIXED_FRAMING_CHARS,
    AGENT_SDK_CLI_PER_MESSAGE_FRAMING_CHARS,
    InterviewEngine,
    InterviewState,
)
from ouroboros.core.errors import ProviderError, ValidationError
from ouroboros.core.json_utils import extract_json_payload
from ouroboros.core.types import Result
from ouroboros.providers.base import Message, MessageRole

log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class InterviewTurnPlan:
    """One provider response derived atomically from one interview revision."""

    question: str
    ambiguity: AmbiguityScore | None
    raw_payload: dict[str, Any]


@dataclass(slots=True)
class InterviewTurnPlanner:
    """Generate the next question and its clarity assessment in one LLM call.

    The planner deliberately uses the ordinary ``LLMAdapter.complete`` contract.
    It neither requires nor advertises backend concurrency, so CLI subprocesses,
    direct APIs, and proxy adapters all observe the same single-request behavior.
    """

    engine: InterviewEngine
    scorer: AmbiguityScorer

    async def plan(
        self,
        state: InterviewState,
        *,
        scoring_state: InterviewState | None = None,
        additional_scoring_context: str = "",
        extra_response_contract: str = "",
    ) -> Result[InterviewTurnPlan, ProviderError | ValidationError]:
        """Plan one turn from ``state`` without a score-then-question waterfall."""
        prepared_result = self.engine.prepare_next_question(state)
        if prepared_result.is_err:
            return Result.err(prepared_result.error)
        prepared = prepared_result.value
        if prepared.immediate_question is not None:
            return Result.ok(
                InterviewTurnPlan(
                    question=prepared.immediate_question,
                    ambiguity=None,
                    raw_payload={"next_question": prepared.immediate_question},
                )
            )

        assert prepared.config is not None
        score_view = scoring_state or state
        excluded_score_rounds = [
            str(index)
            for index, (source_round, score_round) in enumerate(
                zip(state.rounds, score_view.rounds, strict=False),
                start=1,
            )
            if source_round.user_response is not None and score_round.user_response is None
        ]
        scoring_authority_note = ""
        if excluded_score_rounds:
            scoring_authority_note = (
                "For clarity scoring only, answers in rounds "
                f"{', '.join(excluded_score_rounds)} are observations, not user decisions; "
                "do not count them as resolved requirements.\n"
            )
        dimensions = dimension_specs(is_brownfield=state.is_brownfield)
        dimension_lines = "\n".join(
            f"- {spec.key}: {spec.rubric} Weight={spec.weight:.2f}." for spec in dimensions
        )
        context_note = ""
        if additional_scoring_context:
            context_note = (
                "\nIntentional deferrals for scoring only; do not penalize them:\n"
                f"{additional_scoring_context}\n"
            )
        context_score_fields = (
            ', "context_clarity_score": 0.0, "context_clarity_justification": "string"'
            if state.is_brownfield
            else ""
        )
        extra_contract = f"\n{extra_response_contract.strip()}\n" if extra_response_contract else ""
        atomic_contract = f"""

## Atomic Interview Turn Contract
Produce the next Socratic question and assess requirement clarity from the same
interview revision. The behavioral rules above apply to `next_question`; do not
emit a closure announcement in that field.

{scoring_authority_note}
{context_note}
Clarity dimensions:
{dimension_lines}

Respond ONLY with one JSON object. Required fields:
{{"next_question": "string", "goal_clarity_score": 0.0,
"goal_clarity_justification": "string", "constraint_clarity_score": 0.0,
"constraint_clarity_justification": "string",
"success_criteria_clarity_score": 0.0,
"success_criteria_clarity_justification": "string"{context_score_fields}}}
{extra_contract}
No prose, Markdown fences, or second JSON object.
"""
        system_content = prepared.messages[0].content + atomic_contract
        history_budget = max(
            0,
            self.engine._MAX_TOTAL_PROMPT_CHARS
            - len(system_content)
            - AGENT_SDK_CLI_FIXED_FRAMING_CHARS
            - AGENT_SDK_CLI_PER_MESSAGE_FRAMING_CHARS,
        )
        conversation_history = self.engine._trim_messages_to_budget(
            list(prepared.conversation_history),
            max_chars=history_budget,
        )
        messages = [
            Message(role=MessageRole.SYSTEM, content=system_content),
            *conversation_history,
        ]
        config = prepared.config
        completion = await self.engine._require_llm_adapter().complete(messages, config)
        if completion.is_err:
            return Result.err(completion.error)

        try:
            payload_text = extract_json_payload(completion.value.content.strip())
            if payload_text is None:
                raise ValueError("no unambiguous JSON turn payload")
            payload = json.loads(payload_text)
            if not isinstance(payload, dict):
                raise ValueError("turn payload must be a JSON object")
            question = str(payload.get("next_question") or "").strip()
            if not question:
                raise ValueError("turn payload is missing next_question")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return Result.err(
                ProviderError(
                    f"Invalid atomic interview turn response: {exc}",
                    details={"interview_id": state.interview_id},
                )
            )

        ambiguity: AmbiguityScore | None
        try:
            ambiguity = self.scorer.parse_score_response(
                payload_text,
                is_brownfield=state.is_brownfield,
            )
        except (KeyError, TypeError, ValueError) as exc:
            ambiguity = None
            log.warning(
                "interview.turn_plan.scoring_unavailable",
                interview_id=state.interview_id,
                error=str(exc),
            )
        return Result.ok(
            InterviewTurnPlan(
                question=question,
                ambiguity=ambiguity,
                raw_payload=payload,
            )
        )


__all__ = ["InterviewTurnPlan", "InterviewTurnPlanner"]
