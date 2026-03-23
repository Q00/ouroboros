"""Unit tests for decide-later items tracking.

Tests that DECIDE_LATER classified questions store original question text
in a separate decide_later_items list, flowing through to PMSeed and PM document.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from ouroboros.bigbang.interview import (
    InterviewEngine,
    InterviewRound,
    InterviewState,
    InterviewStatus,
)
from ouroboros.bigbang.pm_document import generate_pm_markdown
from ouroboros.bigbang.pm_interview import PMInterviewEngine
from ouroboros.bigbang.pm_seed import PMSeed
from ouroboros.bigbang.question_classifier import (
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


def _mock_completion(content: str = "What problem does this solve?") -> CompletionResponse:
    """Create a mock completion response."""
    return CompletionResponse(
        content=content,
        model="claude-opus-4-6",
        usage=UsageInfo(prompt_tokens=100, completion_tokens=50, total_tokens=150),
        finish_reason="stop",
    )


def _make_adapter() -> MagicMock:
    """Create a mock LLM adapter."""
    adapter = MagicMock()
    adapter.complete = AsyncMock(return_value=Result.ok(_mock_completion()))
    return adapter


def _make_engine(adapter: MagicMock | None = None) -> PMInterviewEngine:
    """Create a PMInterviewEngine with mocked dependencies."""
    if adapter is None:
        adapter = _make_adapter()

    inner = MagicMock(spec=InterviewEngine)
    inner.ask_next_question = AsyncMock()
    inner.record_response = AsyncMock()
    inner.start_interview = AsyncMock()

    classifier = MagicMock(spec=QuestionClassifier)
    classifier.classify = AsyncMock()
    classifier.codebase_context = ""

    return PMInterviewEngine(
        inner=inner,
        classifier=classifier,
        llm_adapter=adapter,
    )


def _make_state(**kwargs) -> InterviewState:
    """Create a minimal InterviewState."""
    defaults = {
        "interview_id": "test-interview-1",
        "initial_context": "Build a task manager",
        "rounds": [],
        "status": InterviewStatus.IN_PROGRESS,
        "max_rounds": 10,
        "current_round": 0,
    }
    defaults.update(kwargs)
    return InterviewState(**defaults)


class TestDecideLaterItemsList:
    """Tests for decide-later items stored as original question text in list."""

    @pytest.mark.asyncio
    async def test_decide_later_stores_original_question_text(self):
        """DECIDE_LATER stores original question text in decide_later_items list."""
        engine = _make_engine()
        state = _make_state()

        original_q = "What caching strategy should we use for session tokens?"

        # Inner engine generates the question
        engine.inner.ask_next_question.return_value = Result.ok(original_q)
        engine.inner.record_response.return_value = Result.ok(state)

        # Classifier says DECIDE_LATER
        classification = ClassificationResult(
            original_question=original_q,
            category=QuestionCategory.DECIDE_LATER,
            reframed_question=original_q,
            reasoning="Depends on architecture decisions not yet made.",
            decide_later=True,
            placeholder_response="To be determined after architecture review.",
        )
        assert classification.output_type == ClassifierOutputType.DECIDE_LATER

        # First call returns DECIDE_LATER, second call returns a PASSTHROUGH
        passthrough_q = "Who are the primary users?"
        passthrough_classification = ClassificationResult(
            original_question=passthrough_q,
            category=QuestionCategory.PLANNING,
            reframed_question=passthrough_q,
            reasoning="Planning question.",
        )

        engine.classifier.classify = AsyncMock(
            side_effect=[Result.ok(classification), Result.ok(passthrough_classification)]
        )
        engine.inner.ask_next_question = AsyncMock(
            side_effect=[Result.ok(original_q), Result.ok(passthrough_q)]
        )

        result = await engine.ask_next_question(state)

        assert result.is_ok
        # The decide-later question is stored in decide_later_items
        assert original_q in engine.decide_later_items
        # It should NOT be in deferred_items
        assert original_q not in engine.deferred_items

    @pytest.mark.asyncio
    async def test_decide_later_items_list_is_separate_from_deferred(self):
        """decide_later_items and deferred_items are independent lists."""
        engine = _make_engine()
        state = _make_state()

        decide_later_q = "How should we handle data migration?"
        deferred_q = "Which ORM should we use for the database layer?"

        # Set up two rounds: first DECIDE_LATER, then DEFERRED, then PASSTHROUGH
        decide_later_class = ClassificationResult(
            original_question=decide_later_q,
            category=QuestionCategory.DECIDE_LATER,
            reframed_question=decide_later_q,
            reasoning="Premature.",
            decide_later=True,
            placeholder_response="TBD after schema design.",
        )
        deferred_class = ClassificationResult(
            original_question=deferred_q,
            category=QuestionCategory.DEVELOPMENT,
            reframed_question=deferred_q,
            reasoning="Deeply technical.",
            defer_to_dev=True,
        )
        passthrough_q = "What are the success metrics?"
        passthrough_class = ClassificationResult(
            original_question=passthrough_q,
            category=QuestionCategory.PLANNING,
            reframed_question=passthrough_q,
            reasoning="Planning.",
        )

        engine.classifier.classify = AsyncMock(
            side_effect=[
                Result.ok(decide_later_class),
                Result.ok(deferred_class),
                Result.ok(passthrough_class),
            ]
        )
        engine.inner.ask_next_question = AsyncMock(
            side_effect=[
                Result.ok(decide_later_q),
                Result.ok(deferred_q),
                Result.ok(passthrough_q),
            ]
        )
        engine.inner.record_response = AsyncMock(return_value=Result.ok(state))

        result = await engine.ask_next_question(state)

        assert result.is_ok
        assert result.value == passthrough_q

        # Verify separation
        assert decide_later_q in engine.decide_later_items
        assert decide_later_q not in engine.deferred_items
        assert deferred_q in engine.deferred_items
        assert deferred_q not in engine.decide_later_items

    @pytest.mark.asyncio
    async def test_multiple_decide_later_items_accumulate(self):
        """Multiple DECIDE_LATER questions all accumulate in the list."""
        engine = _make_engine()
        state = _make_state()

        q1 = "What rate limiting strategy should we use?"
        q2 = "How should we handle GDPR data retention?"
        q3 = "What's the target latency for API responses?"
        passthrough_q = "What is the core problem?"

        classifications = [
            ClassificationResult(
                original_question=q1,
                category=QuestionCategory.DECIDE_LATER,
                reframed_question=q1,
                reasoning="Premature.",
                decide_later=True,
                placeholder_response="TBD.",
            ),
            ClassificationResult(
                original_question=q2,
                category=QuestionCategory.DECIDE_LATER,
                reframed_question=q2,
                reasoning="Needs legal review.",
                decide_later=True,
                placeholder_response="Needs legal input.",
            ),
            ClassificationResult(
                original_question=q3,
                category=QuestionCategory.DECIDE_LATER,
                reframed_question=q3,
                reasoning="Depends on infra.",
                decide_later=True,
                placeholder_response="After infra decision.",
            ),
            ClassificationResult(
                original_question=passthrough_q,
                category=QuestionCategory.PLANNING,
                reframed_question=passthrough_q,
                reasoning="Planning.",
            ),
        ]

        engine.classifier.classify = AsyncMock(side_effect=[Result.ok(c) for c in classifications])
        engine.inner.ask_next_question = AsyncMock(
            side_effect=[Result.ok(q1), Result.ok(q2), Result.ok(q3), Result.ok(passthrough_q)]
        )
        engine.inner.record_response = AsyncMock(return_value=Result.ok(state))

        result = await engine.ask_next_question(state)

        assert result.is_ok
        assert len(engine.decide_later_items) == 3
        assert engine.decide_later_items == [q1, q2, q3]

    def test_decide_later_items_empty_by_default(self):
        """decide_later_items starts as empty list."""
        engine = _make_engine()
        assert engine.decide_later_items == []
        assert isinstance(engine.decide_later_items, list)


class TestDecideLaterInPMSeed:
    """Tests for decide_later_items field in PMSeed dataclass."""

    def test_pm_seed_has_decide_later_items_field(self):
        """PMSeed has a decide_later_items field (tuple of strings)."""
        seed = PMSeed(
            product_name="Test",
            goal="Test goal",
            decide_later_items=("Question 1?", "Question 2?"),
        )
        assert seed.decide_later_items == ("Question 1?", "Question 2?")

    def test_pm_seed_decide_later_items_default_empty(self):
        """PMSeed decide_later_items defaults to empty tuple."""
        seed = PMSeed()
        assert seed.decide_later_items == ()

    def test_pm_seed_to_dict_includes_decide_later_items(self):
        """to_dict() includes decide_later_items as a list."""
        seed = PMSeed(
            product_name="Test",
            decide_later_items=("What rate limiting?", "What caching?"),
        )
        d = seed.to_dict()
        assert "decide_later_items" in d
        assert d["decide_later_items"] == ["What rate limiting?", "What caching?"]

    def test_pm_seed_from_dict_parses_decide_later_items(self):
        """from_dict() parses decide_later_items correctly."""
        data = {
            "product_name": "Test",
            "decide_later_items": ["Question A?", "Question B?"],
        }
        seed = PMSeed.from_dict(data)
        assert seed.decide_later_items == ("Question A?", "Question B?")

    def test_pm_seed_from_dict_missing_decide_later_items(self):
        """from_dict() handles missing decide_later_items gracefully."""
        data = {"product_name": "Test"}
        seed = PMSeed.from_dict(data)
        assert seed.decide_later_items == ()

    def test_pm_seed_roundtrip_yaml(self, tmp_path):
        """PMSeed decide_later_items survive YAML roundtrip."""
        items = ("What caching strategy?", "What deployment model?")
        seed = PMSeed(
            product_name="Test",
            decide_later_items=items,
        )
        yaml_str = yaml.dump(seed.to_dict(), default_flow_style=False)
        loaded = yaml.safe_load(yaml_str)
        restored = PMSeed.from_dict(loaded)
        assert restored.decide_later_items == items

    def test_pm_seed_frozen(self):
        """PMSeed.decide_later_items is immutable (frozen dataclass)."""
        seed = PMSeed(decide_later_items=("Q1?",))
        with pytest.raises(AttributeError):
            seed.decide_later_items = ("Q2?",)


class TestDecideLaterInPMDocument:
    """Tests for decide-later items rendering in PM document."""

    def test_pm_document_renders_decide_later_section(self):
        """PM document includes a 'Decide Later' section."""
        seed = PMSeed(
            product_name="Test Product",
            goal="Test goal",
            decide_later_items=(
                "What caching strategy should we use?",
                "How should we handle data migration?",
            ),
        )
        md = generate_pm_markdown(seed)

        assert "## Decide Later" in md
        assert "deferred or identified as premature" in md
        assert "- What caching strategy should we use?" in md
        assert "- How should we handle data migration?" in md

    def test_pm_document_no_decide_later_when_empty(self):
        """PM document omits 'Decide Later' section when no items."""
        seed = PMSeed(
            product_name="Test Product",
            goal="Test goal",
            decide_later_items=(),
        )
        md = generate_pm_markdown(seed)
        assert "## Decide Later" not in md

    def test_pm_document_merges_deferred_and_decide_later(self):
        """PM document merges deferred and decide-later into single Decide Later section."""
        seed = PMSeed(
            product_name="Test Product",
            goal="Test goal",
            deferred_items=("Which ORM to use?",),
            decide_later_items=("What rate limiting strategy?",),
        )
        md = generate_pm_markdown(seed)

        assert "## Decide Later" in md
        assert "- Which ORM to use?" in md
        assert "- What rate limiting strategy?" in md
        assert "## Deferred Items" not in md


class TestDecideLaterExtractionFlow:
    """Tests for decide-later items flowing through PM seed extraction."""

    @pytest.mark.asyncio
    async def test_extraction_prompt_includes_decide_later_items(self):
        """Extraction prompt lists decide-later items for the LLM."""
        engine = _make_engine()
        engine.decide_later_items = [
            "What caching strategy?",
            "What rate limiting?",
        ]

        state = _make_state(
            rounds=[
                InterviewRound(
                    question="What problem?",
                    user_response="Users need X",
                    round_number=1,
                ),
            ]
        )

        context = engine._build_interview_context(state)
        prompt = engine._build_extraction_prompt(context)

        assert "decide_later_items" in prompt
        assert "What caching strategy?" in prompt
        assert "What rate limiting?" in prompt
        assert "premature or unknowable" in prompt

    @pytest.mark.asyncio
    async def test_parse_pm_seed_merges_decide_later_items(self):
        """_parse_pm_seed merges classifier decide-later items with LLM extraction."""
        engine = _make_engine()
        engine.decide_later_items = ["What caching strategy?"]

        response_json = json.dumps(
            {
                "product_name": "Task Manager",
                "goal": "Manage tasks",
                "user_stories": [],
                "constraints": [],
                "success_criteria": [],
                "deferred_items": [],
                "decide_later_items": ["What deployment model?"],
                "assumptions": [],
            }
        )

        seed = engine._parse_pm_seed(response_json, interview_id="test-1")

        # Both LLM-extracted and classifier items should be present
        assert "What deployment model?" in seed.decide_later_items
        assert "What caching strategy?" in seed.decide_later_items
        assert len(seed.decide_later_items) == 2

    @pytest.mark.asyncio
    async def test_parse_pm_seed_deduplicates_decide_later_items(self):
        """_parse_pm_seed deduplicates decide-later items."""
        engine = _make_engine()
        engine.decide_later_items = ["What caching strategy?"]

        response_json = json.dumps(
            {
                "product_name": "Test",
                "goal": "Test",
                "user_stories": [],
                "constraints": [],
                "success_criteria": [],
                "deferred_items": [],
                "decide_later_items": ["What caching strategy?"],  # Same as classifier
                "assumptions": [],
            }
        )

        seed = engine._parse_pm_seed(response_json, interview_id="test-1")

        assert seed.decide_later_items == ("What caching strategy?",)

    @pytest.mark.asyncio
    async def test_parse_pm_seed_no_decide_later_items_in_response(self):
        """_parse_pm_seed handles missing decide_later_items in LLM response."""
        engine = _make_engine()
        engine.decide_later_items = ["Original question?"]

        response_json = json.dumps(
            {
                "product_name": "Test",
                "goal": "Test",
                "user_stories": [],
                "constraints": [],
                "success_criteria": [],
                "deferred_items": [],
                "assumptions": [],
                # No decide_later_items key
            }
        )

        seed = engine._parse_pm_seed(response_json, interview_id="test-1")

        # Classifier items still included
        assert seed.decide_later_items == ("Original question?",)


class TestDecideLaterSummary:
    """Tests for decide-later summary shown at interview end."""

    def test_get_decide_later_summary_returns_items_list(self):
        """get_decide_later_summary returns a copy of the items list."""
        engine = _make_engine()
        engine.decide_later_items = [
            "What caching strategy should we use?",
            "How should we handle data migration?",
        ]
        summary = engine.get_decide_later_summary()

        assert summary == [
            "What caching strategy should we use?",
            "How should we handle data migration?",
        ]
        # Verify it's a copy, not the same reference
        assert summary is not engine.decide_later_items

    def test_get_decide_later_summary_empty_when_no_items(self):
        """get_decide_later_summary returns empty list when no items."""
        engine = _make_engine()
        assert engine.get_decide_later_summary() == []

    def test_format_decide_later_summary_numbered_list(self):
        """format_decide_later_summary produces a numbered list."""
        engine = _make_engine()
        engine.decide_later_items = [
            "What caching strategy should we use?",
            "How should we handle data migration?",
            "What rate limiting approach?",
        ]
        summary = engine.format_decide_later_summary()

        assert "Items to decide later:" in summary
        assert "1. What caching strategy should we use?" in summary
        assert "2. How should we handle data migration?" in summary
        assert "3. What rate limiting approach?" in summary

    def test_format_decide_later_summary_empty_string_when_no_items(self):
        """format_decide_later_summary returns empty string when no items."""
        engine = _make_engine()
        assert engine.format_decide_later_summary() == ""

    def test_format_decide_later_summary_single_item(self):
        """format_decide_later_summary works with a single item."""
        engine = _make_engine()
        engine.decide_later_items = ["What deployment model?"]
        summary = engine.format_decide_later_summary()

        assert "Items to decide later:" in summary
        assert "1. What deployment model?" in summary
        # Should not contain item 2
        assert "2." not in summary

    def test_format_decide_later_summary_preserves_original_question_text(self):
        """Summary shows original question text as stored during classification."""
        engine = _make_engine()
        original_text = "What caching strategy should we use for session tokens?"
        engine.decide_later_items = [original_text]
        summary = engine.format_decide_later_summary()

        assert original_text in summary
