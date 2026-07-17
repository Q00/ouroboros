"""K2 — question candidate panel + deterministic selection.

Covers:
- ``select_question_candidate`` is deterministic: worst-dimension wins, ties
  break by persona priority contrarian > architect > researcher.
- ``ask_next_question`` with the panel enabled generates persona candidates and
  returns the deterministically selected question; the default (panel off) path
  is unchanged.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.bigbang.interview import (
    NOVICE_FRIENDLY_QUESTION_CONTRACT,
    InterviewEngine,
    InterviewRound,
    InterviewState,
    QuestionCandidate,
    _dimension_clarity_from_breakdown,
    _parse_question_candidate,
    select_question_candidate,
)
from ouroboros.core.types import Result
from ouroboros.interview_adapters import (
    InterviewQuestionChoice,
    InterviewQuestionPresentation,
    InterviewQuestionRecommendation,
    render_question_presentation,
)
from ouroboros.providers.base import CompletionResponse, UsageInfo


def _completion(content: str) -> CompletionResponse:
    return CompletionResponse(
        content=content,
        model="claude-opus-4-6",
        usage=UsageInfo(prompt_tokens=100, completion_tokens=50, total_tokens=150),
        finish_reason="stop",
    )


def _novice_presentation(
    label: str,
    *,
    target_dimension: str = "constraint_clarity",
) -> InterviewQuestionPresentation:
    return InterviewQuestionPresentation(
        decision_id=f"{label}_next_outcome",
        target_dimension=target_dimension,
        question=f"Which {label} outcome should guide the next step?",
        choices=(
            InterviewQuestionChoice(
                choice_id=1,
                meaning_key="primary_user_result",
                label="Focus on the primary user result.",
            ),
            InterviewQuestionChoice(
                choice_id=2,
                meaning_key="operating_constraint",
                label="Focus on the main operating constraint.",
            ),
            InterviewQuestionChoice(
                choice_id=3,
                meaning_key="success_check",
                label="Focus on the clearest success check.",
            ),
        ),
        recommendation=InterviewQuestionRecommendation(
            choice_id=1,
            reason="the user result should guide later decisions",
        ),
        free_text_prompt="Reply with the number, or write your own answer.",
    )


def _novice_question(label: str) -> str:
    return render_question_presentation(_novice_presentation(label))


class TestSelectQuestionCandidate:
    def test_picks_worst_dimension(self) -> None:
        candidates = [
            QuestionCandidate("researcher", "q-goal", "goal_clarity"),
            QuestionCandidate("architect", "q-constraint", "constraint_clarity"),
            QuestionCandidate("contrarian", "q-success", "success_criteria_clarity"),
        ]
        # success_criteria_clarity is worst (0.30) -> its candidate wins.
        clarity = {
            "goal_clarity": 0.90,
            "constraint_clarity": 0.70,
            "success_criteria_clarity": 0.30,
        }
        chosen = select_question_candidate(candidates, clarity)
        assert chosen.question == "q-success"

    def test_tie_breaks_by_persona_priority(self) -> None:
        # All three target the SAME (worst) dimension -> persona priority decides:
        # contrarian > architect > researcher.
        candidates = [
            QuestionCandidate("researcher", "q-r", "constraint_clarity"),
            QuestionCandidate("architect", "q-a", "constraint_clarity"),
            QuestionCandidate("contrarian", "q-c", "constraint_clarity"),
        ]
        clarity = {"goal_clarity": 0.9, "constraint_clarity": 0.4}
        chosen = select_question_candidate(candidates, clarity)
        assert chosen.persona == "contrarian"

    def test_architect_beats_researcher_on_tie(self) -> None:
        candidates = [
            QuestionCandidate("researcher", "q-r", "goal_clarity"),
            QuestionCandidate("architect", "q-a", "goal_clarity"),
        ]
        chosen = select_question_candidate(candidates, {"goal_clarity": 0.5})
        assert chosen.persona == "architect"

    def test_unknown_dimension_is_least_urgent(self) -> None:
        candidates = [
            QuestionCandidate("contrarian", "q-unknown", "nonexistent"),
            QuestionCandidate("researcher", "q-known", "goal_clarity"),
        ]
        # goal_clarity is known (0.5) and beats the unknown dimension even though
        # contrarian has higher persona priority.
        chosen = select_question_candidate(candidates, {"goal_clarity": 0.5})
        assert chosen.question == "q-known"

    def test_empty_candidates_raises(self) -> None:
        with pytest.raises(ValueError, match="candidates must not be empty"):
            select_question_candidate([], {"goal_clarity": 0.5})


class TestDimensionClarityFromBreakdown:
    def test_extracts_scores(self) -> None:
        breakdown = {
            "goal_clarity": {"clarity_score": 0.8, "name": "Goal Clarity"},
            "constraint_clarity": {"clarity_score": 0.4},
            "context_clarity": None,
        }
        result = _dimension_clarity_from_breakdown(breakdown)
        assert result == {"goal_clarity": 0.8, "constraint_clarity": 0.4}

    def test_none_returns_empty(self) -> None:
        assert _dimension_clarity_from_breakdown(None) == {}


class TestParseQuestionCandidate:
    def test_parses_json(self) -> None:
        presentation = _novice_presentation(
            "contrarian",
            target_dimension="goal_clarity",
        )
        candidate = _parse_question_candidate(
            "contrarian",
            presentation.model_dump_json(),
        )
        assert candidate is not None
        assert candidate.persona == "contrarian"
        assert candidate.target_dimension == "goal_clarity"

    def test_missing_question_returns_none(self) -> None:
        assert _parse_question_candidate("contrarian", "{}") is None

    def test_bad_json_returns_none(self) -> None:
        assert _parse_question_candidate("contrarian", "not json") is None


class TestAskNextQuestionPanel:
    def _state_with_breakdown(self) -> InterviewState:
        state = InterviewState(
            interview_id="k2_001",
            initial_context="Build a CLI tool for task management",
        )
        for i in range(3):
            state.rounds.append(
                InterviewRound(
                    round_number=i + 1,
                    question=f"Q{i + 1}?",
                    user_response=f"A{i + 1}",
                )
            )
        state.store_ambiguity(
            score=0.5,
            breakdown={
                "goal_clarity": {
                    "name": "Goal Clarity",
                    "clarity_score": 0.9,
                    "weight": 0.4,
                    "justification": "clear",
                },
                "constraint_clarity": {
                    "name": "Constraint Clarity",
                    "clarity_score": 0.3,
                    "weight": 0.3,
                    "justification": "vague",
                },
                "success_criteria_clarity": {
                    "name": "Success Criteria Clarity",
                    "clarity_score": 0.7,
                    "weight": 0.3,
                    "justification": "ok",
                },
            },
        )
        return state

    @pytest.mark.asyncio
    async def test_panel_selects_worst_dimension_question(self, tmp_path) -> None:
        # Each persona targets constraint_clarity (the worst dim) -> tie broken by
        # persona priority: contrarian's question is returned.
        async def _complete(messages, config):  # type: ignore[no-untyped-def]
            system = messages[0].content
            if "contrarian" in system:
                persona = "contrarian"
            elif "architect" in system:
                persona = "architect"
            else:
                persona = "researcher"
            presentation = _novice_presentation(persona)
            assert system.count(NOVICE_FRIENDLY_QUESTION_CONTRACT) == 1
            return Result.ok(_completion(presentation.model_dump_json()))

        adapter = MagicMock()
        adapter.complete = AsyncMock(side_effect=_complete)
        engine = InterviewEngine(
            llm_adapter=adapter, state_dir=tmp_path, question_candidate_panel=True
        )

        result = await engine.ask_next_question(self._state_with_breakdown())
        assert result.is_ok
        assert result.value == _novice_question("contrarian")
        assert adapter.complete.call_count == 3

    @pytest.mark.asyncio
    async def test_panel_discards_contract_violations(self, tmp_path) -> None:
        async def _complete(messages, config):  # type: ignore[no-untyped-def]
            del config
            system = messages[0].content
            if "researcher" in system:
                content = _novice_presentation("researcher").model_dump_json()
            else:
                content = "What should this do and how should success be measured?"
            return Result.ok(_completion(content))

        adapter = MagicMock()
        adapter.complete = AsyncMock(side_effect=_complete)
        engine = InterviewEngine(
            llm_adapter=adapter, state_dir=tmp_path, question_candidate_panel=True
        )

        result = await engine.ask_next_question(self._state_with_breakdown())

        assert result.is_ok
        assert result.value == _novice_question("researcher")
        assert adapter.complete.call_count == 3

    @pytest.mark.asyncio
    async def test_panel_falls_back_without_breakdown(self, tmp_path) -> None:
        presentation = _novice_presentation("single-call")
        question = render_question_presentation(presentation)
        adapter = MagicMock()
        adapter.complete = AsyncMock(
            return_value=Result.ok(_completion(presentation.model_dump_json()))
        )
        engine = InterviewEngine(
            llm_adapter=adapter, state_dir=tmp_path, question_candidate_panel=True
        )
        state = InterviewState(
            interview_id="k2_nobreakdown",
            initial_context="Build a CLI tool for task management",
        )
        for i in range(3):
            state.rounds.append(
                InterviewRound(round_number=i + 1, question=f"Q{i + 1}?", user_response=f"A{i + 1}")
            )

        result = await engine.ask_next_question(state)
        assert result.is_ok
        # No breakdown -> single call fallback (one completion, verbatim).
        assert result.value == question
        assert adapter.complete.call_count == 1
