"""K1 — per-dimension ambiguity scoring panel.

Covers:
- ``AmbiguityScorer.score_per_dimension`` aggregates concurrent per-dimension
  clarity scores to the SAME weighted formula as the combined single-call path.
- The ``per_dimension`` flag routes ``score`` to the fan-out path while the
  default (flag off) preserves the single-call behavior exactly.
- ``build_ambiguity_dimension_fanout`` (MCP path) builds one payload per
  dimension keyed by ``context.dimension``.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.bigbang.ambiguity import (
    CONSTRAINT_CLARITY_WEIGHT,
    GOAL_CLARITY_WEIGHT,
    SUCCESS_CRITERIA_CLARITY_WEIGHT,
    AmbiguityScorer,
    dimension_specs,
)
from ouroboros.bigbang.interview import InterviewRound, InterviewState
from ouroboros.core.types import Result
from ouroboros.mcp.tools.subagent import build_ambiguity_dimension_fanout
from ouroboros.providers.base import CompletionResponse, UsageInfo


def _completion(content: str, finish_reason: str = "stop") -> CompletionResponse:
    return CompletionResponse(
        content=content,
        model="claude-opus-4-6",
        usage=UsageInfo(prompt_tokens=100, completion_tokens=50, total_tokens=150),
        finish_reason=finish_reason,
    )


def _state() -> InterviewState:
    state = InterviewState(
        interview_id="panel_001",
        initial_context="Build a CLI tool for task management",
    )
    for i in range(2):
        state.rounds.append(
            InterviewRound(
                round_number=i + 1,
                question=f"Question {i + 1}?",
                user_response=f"Answer {i + 1}",
            )
        )
    return state


def _dimension_routing_adapter(scores: dict[str, float]) -> MagicMock:
    """Adapter whose ``complete`` returns the right per-dimension clarity score.

    Routes on the dimension rubric embedded in the system prompt so the result
    is independent of ``asyncio.gather`` scheduling order.
    """

    async def _complete(messages, config):  # type: ignore[no-untyped-def]
        system = messages[0].content
        # Match the most specific rubric first (Success Criteria contains
        # "Criteria"; Goal/Constraint are unambiguous single words).
        if "Success Criteria Clarity" in system:
            clarity = scores["success_criteria_clarity"]
        elif "Constraint Clarity" in system:
            clarity = scores["constraint_clarity"]
        elif "Context Clarity" in system:
            clarity = scores["context_clarity"]
        else:
            clarity = scores["goal_clarity"]
        return Result.ok(_completion(json.dumps({"clarity_score": clarity, "justification": "ok"})))

    adapter = MagicMock()
    adapter.complete = AsyncMock(side_effect=_complete)
    return adapter


class TestPerDimensionAggregation:
    @pytest.mark.asyncio
    async def test_aggregates_to_same_weighted_formula(self) -> None:
        """Per-dimension scores aggregate to 1 - weighted-average of clarity."""
        goal, constraint, success = 0.90, 0.70, 0.60
        adapter = _dimension_routing_adapter(
            {
                "goal_clarity": goal,
                "constraint_clarity": constraint,
                "success_criteria_clarity": success,
            }
        )
        scorer = AmbiguityScorer(llm_adapter=adapter)

        result = await scorer.score_per_dimension(_state())

        assert result.is_ok
        score = result.value
        expected_clarity = (
            goal * GOAL_CLARITY_WEIGHT
            + constraint * CONSTRAINT_CLARITY_WEIGHT
            + success * SUCCESS_CRITERIA_CLARITY_WEIGHT
        )
        assert score.overall_score == pytest.approx(round(1.0 - expected_clarity, 4))
        # One concurrent call per greenfield dimension.
        assert adapter.complete.call_count == 3
        assert score.breakdown.goal_clarity.clarity_score == pytest.approx(goal)
        assert score.breakdown.goal_clarity.weight == pytest.approx(GOAL_CLARITY_WEIGHT)
        assert score.breakdown.context_clarity is None

    @pytest.mark.asyncio
    async def test_matches_combined_path_for_same_clarity(self) -> None:
        """Overall from per-dimension == overall from the combined parser."""
        goal, constraint, success = 0.85, 0.75, 0.65
        adapter = _dimension_routing_adapter(
            {
                "goal_clarity": goal,
                "constraint_clarity": constraint,
                "success_criteria_clarity": success,
            }
        )
        scorer = AmbiguityScorer(llm_adapter=adapter)
        per_dim = (await scorer.score_per_dimension(_state())).value

        combined_adapter = MagicMock()
        combined_adapter.complete = AsyncMock(
            return_value=Result.ok(
                _completion(
                    json.dumps(
                        {
                            "goal_clarity_score": goal,
                            "goal_clarity_justification": "g",
                            "constraint_clarity_score": constraint,
                            "constraint_clarity_justification": "c",
                            "success_criteria_clarity_score": success,
                            "success_criteria_clarity_justification": "s",
                        }
                    )
                )
            )
        )
        combined = (await AmbiguityScorer(llm_adapter=combined_adapter).score(_state())).value

        assert per_dim.overall_score == pytest.approx(combined.overall_score)

    @pytest.mark.asyncio
    async def test_brownfield_scores_four_dimensions(self) -> None:
        adapter = _dimension_routing_adapter(
            {
                "goal_clarity": 0.9,
                "constraint_clarity": 0.8,
                "success_criteria_clarity": 0.7,
                "context_clarity": 0.6,
            }
        )
        scorer = AmbiguityScorer(llm_adapter=adapter)
        result = await scorer.score_per_dimension(_state(), is_brownfield=True)

        assert result.is_ok
        assert adapter.complete.call_count == 4
        assert result.value.breakdown.context_clarity is not None
        assert result.value.breakdown.context_clarity.clarity_score == pytest.approx(0.6)

    @pytest.mark.asyncio
    async def test_flag_routes_score_to_per_dimension(self) -> None:
        adapter = _dimension_routing_adapter(
            {
                "goal_clarity": 0.9,
                "constraint_clarity": 0.8,
                "success_criteria_clarity": 0.7,
            }
        )
        scorer = AmbiguityScorer(llm_adapter=adapter, per_dimension=True)
        result = await scorer.score(_state())

        assert result.is_ok
        assert adapter.complete.call_count == 3

    @pytest.mark.asyncio
    async def test_dimension_failure_is_fail_safe(self) -> None:
        """A single failing dimension returns an error (caller falls back)."""

        async def _complete(messages, config):  # type: ignore[no-untyped-def]
            system = messages[0].content
            if "Constraint Clarity" in system:
                return Result.ok(_completion("not json at all"))
            return Result.ok(_completion(json.dumps({"clarity_score": 0.9, "justification": "ok"})))

        adapter = MagicMock()
        adapter.complete = AsyncMock(side_effect=_complete)
        scorer = AmbiguityScorer(llm_adapter=adapter, max_retries=2, max_format_error_retries=1)

        result = await scorer.score_per_dimension(_state())
        assert result.is_err


class TestDimensionFanoutBuilder:
    def test_builds_one_payload_per_dimension(self) -> None:
        payloads, correlation_key = build_ambiguity_dimension_fanout(
            session_id="s1",
            context_text="Q: goal?\nA: build a thing",
        )
        assert correlation_key == "context.dimension"
        assert len(payloads) == 3
        dims = [p.context["dimension"] for p in payloads]
        assert dims == [spec.key for spec in dimension_specs(is_brownfield=False)]
        # Each payload carries its dimension weight for host-side aggregation.
        assert payloads[0].context["weight"] == pytest.approx(GOAL_CLARITY_WEIGHT)

    def test_brownfield_builds_four_payloads(self) -> None:
        payloads, _ = build_ambiguity_dimension_fanout(
            session_id="s1",
            context_text="Q: goal?\nA: extend the repo",
            is_brownfield=True,
        )
        assert len(payloads) == 4
        assert payloads[-1].context["dimension"] == "context_clarity"

    def test_rejects_empty_inputs(self) -> None:
        with pytest.raises(ValueError, match="session_id must not be empty"):
            build_ambiguity_dimension_fanout(session_id="", context_text="x")
        with pytest.raises(ValueError, match="context_text must not be empty"):
            build_ambiguity_dimension_fanout(session_id="s", context_text="")
