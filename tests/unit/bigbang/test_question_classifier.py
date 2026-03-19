"""Unit tests for ouroboros.bigbang.question_classifier module.

Tests ClassifierOutputType enum, ClassificationResult properties,
and QuestionClassifier pass-through, reframe, and defer paths.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.bigbang.question_classifier import (
    _DEFAULT_PLACEHOLDER,
    ClassificationResult,
    ClassifierOutputType,
    QuestionCategory,
    QuestionClassifier,
)
from ouroboros.core.types import Result
from ouroboros.providers.base import (
    CompletionResponse,
    UsageInfo,
)


def _mock_completion(content: str) -> CompletionResponse:
    """Create a mock completion response."""
    return CompletionResponse(
        content=content,
        model="claude-sonnet-4-20250514",
        usage=UsageInfo(prompt_tokens=50, completion_tokens=30, total_tokens=80),
        finish_reason="stop",
    )


# ──────────────────────────────────────────────────────────────
# ClassifierOutputType enum
# ──────────────────────────────────────────────────────────────


class TestClassifierOutputType:
    """Test ClassifierOutputType enum values."""

    def test_passthrough_value(self) -> None:
        assert ClassifierOutputType.PASSTHROUGH == "passthrough"

    def test_reframed_value(self) -> None:
        assert ClassifierOutputType.REFRAMED == "reframed"

    def test_deferred_value(self) -> None:
        assert ClassifierOutputType.DEFERRED == "deferred"

    def test_is_str_enum(self) -> None:
        assert isinstance(ClassifierOutputType.PASSTHROUGH, str)


# ──────────────────────────────────────────────────────────────
# ClassificationResult.output_type property
# ──────────────────────────────────────────────────────────────


class TestClassificationResultOutputType:
    """Test the output_type property of ClassificationResult."""

    def test_planning_returns_passthrough(self) -> None:
        """PLANNING category + defer_to_dev=False -> PASSTHROUGH."""
        result = ClassificationResult(
            original_question="Who are the target users?",
            category=QuestionCategory.PLANNING,
            reframed_question="Who are the target users?",
            reasoning="Direct business question",
            defer_to_dev=False,
        )
        assert result.output_type == ClassifierOutputType.PASSTHROUGH

    def test_development_returns_reframed(self) -> None:
        """DEVELOPMENT category + defer_to_dev=False -> REFRAMED."""
        result = ClassificationResult(
            original_question="Which database should we use?",
            category=QuestionCategory.DEVELOPMENT,
            reframed_question="What are your data storage needs?",
            reasoning="Technical question reframed",
            defer_to_dev=False,
        )
        assert result.output_type == ClassifierOutputType.REFRAMED

    def test_deferred_returns_deferred(self) -> None:
        """defer_to_dev=True -> DEFERRED regardless of category."""
        result = ClassificationResult(
            original_question="Should we use gRPC or REST?",
            category=QuestionCategory.DEVELOPMENT,
            reframed_question="Should we use gRPC or REST?",
            reasoning="Deeply technical",
            defer_to_dev=True,
        )
        assert result.output_type == ClassifierOutputType.DEFERRED

    def test_deferred_overrides_planning_category(self) -> None:
        """Even if category is PLANNING, defer_to_dev=True -> DEFERRED."""
        result = ClassificationResult(
            original_question="Some question",
            category=QuestionCategory.PLANNING,
            reframed_question="Some question",
            reasoning="edge case",
            defer_to_dev=True,
        )
        assert result.output_type == ClassifierOutputType.DEFERRED

    def test_decide_later_returns_decide_later(self) -> None:
        """DECIDE_LATER category with decide_later=True -> DECIDE_LATER."""
        result = ClassificationResult(
            original_question="How should we handle scaling?",
            category=QuestionCategory.DECIDE_LATER,
            reframed_question="How should we handle scaling?",
            reasoning="Premature — depends on usage patterns",
            decide_later=True,
            placeholder_response="To be determined after initial launch.",
        )
        assert result.output_type == ClassifierOutputType.DECIDE_LATER

    def test_decide_later_overrides_defer_to_dev(self) -> None:
        """decide_later=True takes precedence over defer_to_dev=True."""
        result = ClassificationResult(
            original_question="What caching strategy?",
            category=QuestionCategory.DEVELOPMENT,
            reframed_question="What caching strategy?",
            reasoning="Both flags set",
            defer_to_dev=True,
            decide_later=True,
            placeholder_response="TBD",
        )
        assert result.output_type == ClassifierOutputType.DECIDE_LATER


# ──────────────────────────────────────────────────────────────
# ClassificationResult.question_for_pm property
# ──────────────────────────────────────────────────────────────


class TestClassificationResultQuestionForPM:
    """Test the question_for_pm property — the pass-through path."""

    def test_passthrough_returns_original_question(self) -> None:
        """PASSTHROUGH returns the original question unchanged."""
        original = "What problem does this product solve for users?"
        result = ClassificationResult(
            original_question=original,
            category=QuestionCategory.PLANNING,
            reframed_question=original,  # same for planning
            reasoning="Planning question",
        )
        assert result.question_for_pm == original
        assert result.question_for_pm is result.original_question

    def test_passthrough_ignores_reframed_question(self) -> None:
        """PASSTHROUGH uses original_question even if reframed_question differs.

        This edge case can occur if the classifier returns a slightly
        different reframed_question even though category is PLANNING.
        The pass-through path must always return the original.
        """
        original = "What are the business goals?"
        different_reframe = "What are your business goals and objectives?"
        result = ClassificationResult(
            original_question=original,
            category=QuestionCategory.PLANNING,
            reframed_question=different_reframe,
            reasoning="Planning but classifier tweaked wording",
        )
        # Pass-through must return original, NOT the reframed version
        assert result.question_for_pm == original
        assert result.question_for_pm != different_reframe

    def test_reframed_returns_reframed_question(self) -> None:
        """REFRAMED returns the reframed question, not the original."""
        result = ClassificationResult(
            original_question="Which ORM should we use?",
            category=QuestionCategory.DEVELOPMENT,
            reframed_question="How do you expect users to interact with data?",
            reasoning="Reframed for PM",
        )
        assert result.question_for_pm == "How do you expect users to interact with data?"

    def test_deferred_returns_empty_string(self) -> None:
        """DEFERRED returns empty string — question should not be shown to PM."""
        result = ClassificationResult(
            original_question="Should we use gRPC or REST?",
            category=QuestionCategory.DEVELOPMENT,
            reframed_question="Should we use gRPC or REST?",
            reasoning="Purely technical",
            defer_to_dev=True,
        )
        assert result.question_for_pm == ""

    def test_decide_later_returns_empty_string(self) -> None:
        """DECIDE_LATER returns empty string — auto-answered, not shown to PM."""
        result = ClassificationResult(
            original_question="How will this scale to millions of users?",
            category=QuestionCategory.DECIDE_LATER,
            reframed_question="How will this scale to millions of users?",
            reasoning="Premature at this stage",
            decide_later=True,
            placeholder_response="To be determined post-launch.",
        )
        assert result.question_for_pm == ""


# ──────────────────────────────────────────────────────────────
# QuestionClassifier — pass-through via LLM
# ──────────────────────────────────────────────────────────────


class TestQuestionClassifierPassthrough:
    """Test the QuestionClassifier produces PASSTHROUGH results for planning questions."""

    @pytest.mark.asyncio
    async def test_classify_planning_question_is_passthrough(self) -> None:
        """A planning question classified by the LLM results in PASSTHROUGH output type."""
        adapter = MagicMock()
        planning_q = "What are the key success metrics for this product?"

        adapter.complete = AsyncMock(
            return_value=Result.ok(
                _mock_completion(
                    json.dumps(
                        {
                            "category": "planning",
                            "reframed_question": planning_q,
                            "reasoning": "Success metrics are a PM concern",
                            "defer_to_dev": False,
                        }
                    )
                )
            )
        )

        classifier = QuestionClassifier(llm_adapter=adapter)
        result = await classifier.classify(planning_q)

        assert result.is_ok
        classification = result.value
        assert classification.output_type == ClassifierOutputType.PASSTHROUGH
        assert classification.question_for_pm == planning_q
        assert classification.original_question == planning_q

    @pytest.mark.asyncio
    async def test_classify_parse_failure_defaults_to_passthrough(self) -> None:
        """When LLM response cannot be parsed, default is PASSTHROUGH."""
        adapter = MagicMock()
        question = "What is the target market?"

        adapter.complete = AsyncMock(
            return_value=Result.ok(_mock_completion("This is not valid JSON at all"))
        )

        classifier = QuestionClassifier(llm_adapter=adapter)
        result = await classifier.classify(question)

        # Should not fail — falls back to planning/passthrough
        assert result.is_ok
        classification = result.value
        assert classification.output_type == ClassifierOutputType.PASSTHROUGH
        assert classification.question_for_pm == question

    @pytest.mark.asyncio
    async def test_passthrough_preserves_question_exactly(self) -> None:
        """Pass-through must preserve the exact question string, no trimming or modification."""
        adapter = MagicMock()
        # Question with special formatting that must be preserved
        planning_q = "  What timeline constraints exist?\n  Are there hard deadlines?  "

        adapter.complete = AsyncMock(
            return_value=Result.ok(
                _mock_completion(
                    json.dumps(
                        {
                            "category": "planning",
                            "reframed_question": planning_q,
                            "reasoning": "Timeline is PM domain",
                            "defer_to_dev": False,
                        }
                    )
                )
            )
        )

        classifier = QuestionClassifier(llm_adapter=adapter)
        result = await classifier.classify(planning_q)

        assert result.is_ok
        # The original question must be preserved exactly
        assert result.value.question_for_pm == planning_q


# ──────────────────────────────────────────────────────────────
# QuestionClassifier._parse_response — decide-later parsing
# ──────────────────────────────────────────────────────────────


class TestQuestionClassifierParseDecideLater:
    """Test _parse_response handling of decide-later responses."""

    def test_parses_decide_later_with_placeholder(self) -> None:
        """Parses a well-formed decide-later JSON response."""
        classifier = QuestionClassifier(llm_adapter=MagicMock())

        response = json.dumps(
            {
                "category": "decide_later",
                "reframed_question": "How to handle scaling?",
                "reasoning": "Post-MVP concern",
                "defer_to_dev": False,
                "decide_later": True,
                "placeholder_response": "TBD after MVP launch.",
            }
        )

        result = classifier._parse_response(response, "How to handle scaling?")

        assert result.category == QuestionCategory.DECIDE_LATER
        assert result.decide_later is True
        assert result.placeholder_response == "TBD after MVP launch."
        assert result.defer_to_dev is False
        assert result.output_type == ClassifierOutputType.DECIDE_LATER

    def test_decide_later_category_auto_sets_flag(self) -> None:
        """If category is decide_later but flag is missing, flag is auto-set."""
        classifier = QuestionClassifier(llm_adapter=MagicMock())

        response = json.dumps(
            {
                "category": "decide_later",
                "reframed_question": "Q?",
                "reasoning": "premature",
            }
        )

        result = classifier._parse_response(response, "Q?")

        assert result.category == QuestionCategory.DECIDE_LATER
        assert result.decide_later is True
        assert result.placeholder_response == _DEFAULT_PLACEHOLDER

    def test_decide_later_empty_placeholder_gets_default(self) -> None:
        """decide_later=true with empty placeholder gets the default."""
        classifier = QuestionClassifier(llm_adapter=MagicMock())

        response = json.dumps(
            {
                "category": "decide_later",
                "reframed_question": "Q?",
                "reasoning": "premature",
                "decide_later": True,
                "placeholder_response": "",
            }
        )

        result = classifier._parse_response(response, "Q?")

        assert result.decide_later is True
        assert result.placeholder_response == _DEFAULT_PLACEHOLDER

    def test_planning_has_no_decide_later(self) -> None:
        """Planning classification has decide_later=False by default."""
        classifier = QuestionClassifier(llm_adapter=MagicMock())

        response = json.dumps(
            {
                "category": "planning",
                "reframed_question": "Who are users?",
                "reasoning": "Business question",
            }
        )

        result = classifier._parse_response(response, "Who are users?")

        assert result.category == QuestionCategory.PLANNING
        assert result.decide_later is False
        assert result.placeholder_response == ""


# ──────────────────────────────────────────────────────────────
# QuestionClassifier.classify() — decide-later end-to-end
# ──────────────────────────────────────────────────────────────


class TestQuestionClassifierDecideLater:
    """Test classify() end-to-end with decide-later output."""

    @pytest.mark.asyncio
    async def test_classify_decide_later_question(self) -> None:
        """LLM classifies a premature question as decide-later."""
        adapter = MagicMock()
        question = "How should we handle multi-region deployment?"

        adapter.complete = AsyncMock(
            return_value=Result.ok(
                _mock_completion(
                    json.dumps(
                        {
                            "category": "decide_later",
                            "reframed_question": question,
                            "reasoning": "Multi-region is a future scaling concern",
                            "defer_to_dev": False,
                            "decide_later": True,
                            "placeholder_response": "Will be determined after initial deployment.",
                        }
                    )
                )
            )
        )

        classifier = QuestionClassifier(llm_adapter=adapter)
        result = await classifier.classify(question)

        assert result.is_ok
        cr = result.value
        assert cr.category == QuestionCategory.DECIDE_LATER
        assert cr.decide_later is True
        assert cr.output_type == ClassifierOutputType.DECIDE_LATER
        assert cr.question_for_pm == ""
        assert "initial deployment" in cr.placeholder_response
