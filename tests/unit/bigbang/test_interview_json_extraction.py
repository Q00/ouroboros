"""#1838: interview JSON consumers must use the hardened extractor.

A model routinely restates the requested schema before answering; the
pre-#1734 first-fence heuristic then adopted the echoed example as the
authoritative payload and discarded the real answer. Every consumer here
must instead fail closed on ambiguous responses so its existing
retry-then-error path engages, and ambiguity scoring must reject
non-finite score values instead of clamping them into valid clarity.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.bigbang.ambiguity import AmbiguityScorer, _DimensionSpec
from ouroboros.bigbang.interview import InterviewEngine, _parse_question_candidate
from ouroboros.bigbang.pm_interview import PMInterviewEngine
from ouroboros.bigbang.question_classifier import QuestionClassifier


def _echoed_schema_response(placeholder: str, answer: str) -> str:
    """The issue's reproduction: a restated schema fence, then the answer."""
    return (
        "Understood. I will respond in exactly this shape:\n\n"
        f"```json\n{placeholder}\n```\n\n"
        "Now the actual assessment of these requirements:\n\n"
        f"```json\n{answer}\n```\n"
    )


def _scorer() -> AmbiguityScorer:
    return AmbiguityScorer(llm_adapter=MagicMock())


def _pm_engine() -> PMInterviewEngine:
    inner = MagicMock(spec=InterviewEngine)
    classifier = MagicMock(spec=QuestionClassifier)
    classifier.classify = AsyncMock()
    classifier.codebase_context = ""
    return PMInterviewEngine(
        inner=inner,
        llm_adapter=MagicMock(),
        classifier=classifier,
    )


class TestEchoedSchemaFailsClosed:
    """Two fences are ambiguous: no consumer may adopt the echoed example."""

    def test_scoring_response_rejects_echoed_schema(self) -> None:
        placeholder = json.dumps(
            {
                "goal_clarity_score": 0.9,
                "goal_clarity_justification": "<why>",
                "constraint_clarity_score": 0.9,
                "constraint_clarity_justification": "<why>",
                "success_criteria_clarity_score": 0.9,
                "success_criteria_clarity_justification": "<why>",
            }
        )
        answer = json.dumps(
            {
                "goal_clarity_score": 0.15,
                "goal_clarity_justification": "Goal is a one-liner with no deliverable.",
                "constraint_clarity_score": 0.1,
                "constraint_clarity_justification": "No constraints stated.",
                "success_criteria_clarity_score": 0.05,
                "success_criteria_clarity_justification": "No success criteria.",
            }
        )
        with pytest.raises(ValueError):
            _scorer()._parse_scoring_response(_echoed_schema_response(placeholder, answer))

    def test_dimension_response_rejects_echoed_schema(self) -> None:
        placeholder = json.dumps({"clarity_score": 0.9, "justification": "<why>"})
        answer = json.dumps({"clarity_score": 0.1, "justification": "Vague."})
        spec = _DimensionSpec(key="goal_clarity", name="Goal Clarity", weight=0.4, rubric="rubric")
        with pytest.raises(ValueError):
            _scorer()._parse_dimension_response(_echoed_schema_response(placeholder, answer), spec)

    def test_pm_seed_rejects_echoed_schema(self) -> None:
        placeholder = json.dumps(
            {
                "product_name": "<product name>",
                "goal": "<one sentence goal>",
                "user_stories": [],
                "constraints": [],
                "success_criteria": [],
                "deferred_items": [],
                "decide_later_items": [],
                "assumptions": [],
            }
        )
        answer = json.dumps(
            {
                "product_name": "Task Manager",
                "goal": "Manage tasks",
                "user_stories": [
                    {"persona": "User", "action": "add a task", "benefit": "tracking"}
                ],
                "constraints": ["offline-first"],
                "success_criteria": ["tasks persist"],
                "deferred_items": [],
                "decide_later_items": [],
                "assumptions": [],
            }
        )
        engine = _pm_engine()
        with pytest.raises(ValueError):
            engine._parse_pm_seed(
                _echoed_schema_response(placeholder, answer), interview_id="test-1"
            )

    def test_question_classifier_rejects_echoed_schema(self) -> None:
        placeholder = json.dumps({"category": "development", "reframed_question": "<reframed>"})
        answer = json.dumps({"category": "planning", "reframed_question": "What is the deadline?"})
        classifier = QuestionClassifier(llm_adapter=MagicMock())
        with pytest.raises(ValueError):
            classifier._parse_response(
                _echoed_schema_response(placeholder, answer), "original question"
            )

    def test_question_candidate_rejects_echoed_schema(self) -> None:
        placeholder = json.dumps({"question": "<question>", "target_dimension": "goal_clarity"})
        answer = json.dumps({"question": "What should happen on conflict?", "target_dimension": ""})
        candidate = _parse_question_candidate(
            "hacker", _echoed_schema_response(placeholder, answer)
        )
        assert candidate is None, f"an ambiguous response produced a candidate: {candidate!r}"

    def test_single_fence_with_prose_still_parses(self) -> None:
        """The hardened path keeps the ordinary prose-plus-one-fence shape."""
        answer = json.dumps(
            {
                "goal_clarity_score": 0.6,
                "goal_clarity_justification": "Adequate.",
                "constraint_clarity_score": 0.5,
                "constraint_clarity_justification": "Some constraints.",
                "success_criteria_clarity_score": 0.4,
                "success_criteria_clarity_justification": "Partial.",
            }
        )
        response = f"Here is my assessment:\n\n```json\n{answer}\n```\nDone."
        breakdown = _scorer()._parse_scoring_response(response)
        assert breakdown.goal_clarity.clarity_score == 0.6


class TestNonObjectPayloadsRejected:
    """The extractor accepts top-level arrays; every consumer must not."""

    def test_scoring_response_rejects_array_payload(self) -> None:
        response = (
            '```json\n["goal_clarity_score", "constraint_clarity_score",'
            ' "success_criteria_clarity_score"]\n```'
        )
        with pytest.raises(ValueError):
            _scorer()._parse_scoring_response(response)

    def test_dimension_response_rejects_array_payload(self) -> None:
        spec = _DimensionSpec(key="goal_clarity", name="Goal Clarity", weight=0.4, rubric="rubric")
        with pytest.raises(ValueError):
            _scorer()._parse_dimension_response('```json\n["clarity_score"]\n```', spec)


class TestNonFiniteScoresRejected:
    """NaN clamps up to perfect clarity today; all non-finite input must fail."""

    def _scoring_response(self, goal_score: str) -> str:
        return (
            '{"goal_clarity_score": ' + goal_score + ", "
            '"goal_clarity_justification": "j", '
            '"constraint_clarity_score": 0.5, '
            '"constraint_clarity_justification": "j", '
            '"success_criteria_clarity_score": 0.5, '
            '"success_criteria_clarity_justification": "j"}'
        )

    @pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
    def test_non_finite_literals_are_rejected(self, literal: str) -> None:
        with pytest.raises(ValueError):
            _scorer()._parse_scoring_response(self._scoring_response(literal))

    def test_string_smuggled_nan_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            _scorer()._parse_scoring_response(self._scoring_response('"nan"'))

    @pytest.mark.parametrize("literal", ["NaN", "-Infinity"])
    def test_dimension_scores_reject_non_finite(self, literal: str) -> None:
        spec = _DimensionSpec(key="goal_clarity", name="Goal Clarity", weight=0.4, rubric="rubric")
        response = '{"clarity_score": ' + literal + ', "justification": "j"}'
        with pytest.raises(ValueError):
            _scorer()._parse_dimension_response(response, spec)
