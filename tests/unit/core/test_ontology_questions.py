"""Tests for core/ontology_questions.py - Ontological question framework."""

from unittest.mock import AsyncMock

import pytest

from ouroboros.core.errors import ProviderError, ValidationError
from ouroboros.core.ontology_questions import (
    ONTOLOGICAL_QUESTIONS,
    ONTOLOGY_ANALYSIS_SYSTEM_PROMPT,
    OntologicalInsight,
    OntologicalQuestion,
    OntologicalQuestionType,
    analyze_ontologically,
    build_devil_advocate_prompt,
    build_ontological_prompt,
    get_all_questions,
    get_question,
)
from ouroboros.core.types import Result
from ouroboros.providers.base import CompletionResponse, UsageInfo


class TestOntologicalQuestionType:
    """Tests for OntologicalQuestionType enum."""

    def test_all_types_exist(self) -> None:
        """All four question types are defined."""
        assert OntologicalQuestionType.ESSENCE == "essence"
        assert OntologicalQuestionType.ROOT_CAUSE == "root_cause"
        assert OntologicalQuestionType.PREREQUISITES == "prerequisites"
        assert OntologicalQuestionType.HIDDEN_ASSUMPTIONS == "hidden_assumptions"

    def test_enum_count(self) -> None:
        """Exactly four question types exist."""
        assert len(OntologicalQuestionType) == 4


class TestOntologicalQuestion:
    """Tests for OntologicalQuestion dataclass."""

    def test_frozen_dataclass(self) -> None:
        """OntologicalQuestion is frozen (immutable)."""
        question = OntologicalQuestion(
            type=OntologicalQuestionType.ESSENCE,
            question="What IS this?",
            purpose="Test purpose",
            follow_up="Test follow-up",
        )
        with pytest.raises(AttributeError):
            question.question = "Changed"  # type: ignore[misc]

    def test_all_fields_required(self) -> None:
        """All fields are required."""
        with pytest.raises(TypeError):
            OntologicalQuestion(  # type: ignore[call-arg]
                type=OntologicalQuestionType.ESSENCE,
                question="Test",
            )


class TestOntologicalQuestions:
    """Tests for ONTOLOGICAL_QUESTIONS dictionary."""

    def test_all_types_have_questions(self) -> None:
        """Each question type has a corresponding question defined."""
        for qtype in OntologicalQuestionType:
            assert qtype in ONTOLOGICAL_QUESTIONS
            assert isinstance(ONTOLOGICAL_QUESTIONS[qtype], OntologicalQuestion)

    def test_essence_question(self) -> None:
        """ESSENCE question is properly defined."""
        q = ONTOLOGICAL_QUESTIONS[OntologicalQuestionType.ESSENCE]
        assert "What IS" in q.question
        assert q.type == OntologicalQuestionType.ESSENCE

    def test_root_cause_question(self) -> None:
        """ROOT_CAUSE question is properly defined."""
        q = ONTOLOGICAL_QUESTIONS[OntologicalQuestionType.ROOT_CAUSE]
        assert "root cause" in q.question.lower() or "symptom" in q.question.lower()

    def test_prerequisites_question(self) -> None:
        """PREREQUISITES question is properly defined."""
        q = ONTOLOGICAL_QUESTIONS[OntologicalQuestionType.PREREQUISITES]
        assert "must exist" in q.question.lower() or "first" in q.question.lower()

    def test_hidden_assumptions_question(self) -> None:
        """HIDDEN_ASSUMPTIONS question is properly defined."""
        q = ONTOLOGICAL_QUESTIONS[OntologicalQuestionType.HIDDEN_ASSUMPTIONS]
        assert "assuming" in q.question.lower()


class TestOntologicalInsight:
    """Tests for OntologicalInsight dataclass."""

    def test_frozen_dataclass(self) -> None:
        """OntologicalInsight is frozen (immutable)."""
        insight = OntologicalInsight(
            essence="Test essence",
            is_root_problem=True,
            prerequisites=("prereq1",),
            hidden_assumptions=("assumption1",),
            confidence=0.9,
            reasoning="Test reasoning",
        )
        with pytest.raises(AttributeError):
            insight.essence = "Changed"  # type: ignore[misc]

    def test_valid_insight(self) -> None:
        """Valid insight creation."""
        insight = OntologicalInsight(
            essence="Core problem",
            is_root_problem=True,
            prerequisites=("Database", "API"),
            hidden_assumptions=("User has account",),
            confidence=0.85,
            reasoning="Analysis shows this is fundamental",
        )
        assert insight.essence == "Core problem"
        assert insight.is_root_problem is True
        assert len(insight.prerequisites) == 2
        assert len(insight.hidden_assumptions) == 1
        assert insight.confidence == 0.85


class TestBuildOntologicalPrompt:
    """Tests for build_ontological_prompt function."""

    def test_essence_prompt(self) -> None:
        """Build prompt for ESSENCE question."""
        prompt = build_ontological_prompt(OntologicalQuestionType.ESSENCE)
        assert "What IS" in prompt
        assert "ontological analysis" in prompt.lower()

    def test_root_cause_prompt(self) -> None:
        """Build prompt for ROOT_CAUSE question."""
        prompt = build_ontological_prompt(OntologicalQuestionType.ROOT_CAUSE)
        assert "root cause" in prompt.lower() or "symptom" in prompt.lower()

    def test_all_types_generate_prompts(self) -> None:
        """All question types generate valid prompts."""
        for qtype in OntologicalQuestionType:
            prompt = build_ontological_prompt(qtype)
            assert len(prompt) > 0
            assert ":" in prompt  # Contains structured content


class TestBuildDevilAdvocatePrompt:
    """Tests for build_devil_advocate_prompt function."""

    def test_includes_all_questions(self) -> None:
        """Devil's Advocate prompt includes all four questions."""
        prompt = build_devil_advocate_prompt()
        assert "DEVIL'S ADVOCATE" in prompt
        assert "ontological" in prompt.lower()

    def test_includes_guidance(self) -> None:
        """Devil's Advocate prompt includes behavioral guidance."""
        prompt = build_devil_advocate_prompt()
        assert "root" in prompt.lower()
        assert "symptom" in prompt.lower()
        assert "fair" in prompt.lower()


class TestGetAllQuestions:
    """Tests for get_all_questions function."""

    def test_returns_list(self) -> None:
        """Returns a list of all questions."""
        questions = get_all_questions()
        assert isinstance(questions, list)
        assert len(questions) == 4

    def test_all_are_ontological_questions(self) -> None:
        """All returned items are OntologicalQuestion instances."""
        questions = get_all_questions()
        for q in questions:
            assert isinstance(q, OntologicalQuestion)


class TestGetQuestion:
    """Tests for get_question function."""

    def test_get_essence(self) -> None:
        """Get ESSENCE question."""
        q = get_question(OntologicalQuestionType.ESSENCE)
        assert q.type == OntologicalQuestionType.ESSENCE

    def test_get_all_types(self) -> None:
        """Can get all question types."""
        for qtype in OntologicalQuestionType:
            q = get_question(qtype)
            assert q.type == qtype


class TestAnalyzeOntologically:
    """Tests for analyze_ontologically function."""

    @pytest.fixture
    def mock_llm(self) -> AsyncMock:
        """Create mock LLM adapter."""
        return AsyncMock()

    @pytest.mark.asyncio
    async def test_successful_analysis(self, mock_llm: AsyncMock) -> None:
        """Successful ontological analysis."""
        mock_llm.complete.return_value = Result.ok(
            CompletionResponse(
                content="""{
                    "essence": "A user authentication problem",
                    "is_root_problem": true,
                    "prerequisites": ["User database", "Session management"],
                    "hidden_assumptions": ["Users have valid emails"],
                    "confidence": 0.85,
                    "reasoning": "This addresses the core authentication need"
                }""",
                model="test-model",
                usage=UsageInfo(100, 200, 300),
            )
        )

        result = await analyze_ontologically(
            mock_llm,
            "Build a login system",
            (OntologicalQuestionType.ESSENCE, OntologicalQuestionType.ROOT_CAUSE),
        )

        assert result.is_ok
        insight = result.value
        assert insight.essence == "A user authentication problem"
        assert insight.is_root_problem is True
        assert len(insight.prerequisites) == 2
        assert insight.confidence == 0.85

    @pytest.mark.asyncio
    async def test_analysis_with_no_question_types(self, mock_llm: AsyncMock) -> None:
        """Analysis with empty question_types uses all questions."""
        mock_llm.complete.return_value = Result.ok(
            CompletionResponse(
                content="""{
                    "essence": "Test",
                    "is_root_problem": false,
                    "prerequisites": [],
                    "hidden_assumptions": [],
                    "confidence": 0.7,
                    "reasoning": "Test"
                }""",
                model="test-model",
                usage=UsageInfo(0, 0, 0),
            )
        )

        result = await analyze_ontologically(mock_llm, "Test context")
        assert result.is_ok

    @pytest.mark.asyncio
    async def test_llm_error_propagated(self, mock_llm: AsyncMock) -> None:
        """LLM errors are propagated."""
        mock_llm.complete.return_value = Result.err(
            ProviderError("API Error")
        )

        result = await analyze_ontologically(mock_llm, "Test context")
        assert result.is_err
        assert isinstance(result.error, ProviderError)

    @pytest.mark.asyncio
    async def test_parse_error_on_invalid_json(self, mock_llm: AsyncMock) -> None:
        """Parse error on invalid JSON response."""
        mock_llm.complete.return_value = Result.ok(
            CompletionResponse(
                content="I think this is important but not JSON",
                model="test-model",
                usage=UsageInfo(0, 0, 0),
            )
        )

        result = await analyze_ontologically(mock_llm, "Test context")
        assert result.is_err
        assert isinstance(result.error, ValidationError)

    @pytest.mark.asyncio
    async def test_handles_json_in_markdown(self, mock_llm: AsyncMock) -> None:
        """Handles JSON embedded in markdown."""
        mock_llm.complete.return_value = Result.ok(
            CompletionResponse(
                content="""Here's my analysis:
                {
                    "essence": "Test",
                    "is_root_problem": true,
                    "prerequisites": [],
                    "hidden_assumptions": [],
                    "confidence": 0.8,
                    "reasoning": "Test"
                }
                That's all.""",
                model="test-model",
                usage=UsageInfo(0, 0, 0),
            )
        )

        result = await analyze_ontologically(mock_llm, "Test context")
        assert result.is_ok
        assert result.value.essence == "Test"

    @pytest.mark.asyncio
    async def test_confidence_clamped(self, mock_llm: AsyncMock) -> None:
        """Confidence is clamped to [0, 1]."""
        mock_llm.complete.return_value = Result.ok(
            CompletionResponse(
                content="""{
                    "essence": "Test",
                    "is_root_problem": true,
                    "prerequisites": [],
                    "hidden_assumptions": [],
                    "confidence": 1.5,
                    "reasoning": "Test"
                }""",
                model="test-model",
                usage=UsageInfo(0, 0, 0),
            )
        )

        result = await analyze_ontologically(mock_llm, "Test context")
        assert result.is_ok
        assert result.value.confidence == 1.0


class TestOntologyAnalysisSystemPrompt:
    """Tests for ONTOLOGY_ANALYSIS_SYSTEM_PROMPT constant."""

    def test_prompt_exists(self) -> None:
        """System prompt exists and is non-empty."""
        assert ONTOLOGY_ANALYSIS_SYSTEM_PROMPT
        assert len(ONTOLOGY_ANALYSIS_SYSTEM_PROMPT) > 100

    def test_prompt_mentions_json(self) -> None:
        """System prompt mentions JSON format."""
        assert "JSON" in ONTOLOGY_ANALYSIS_SYSTEM_PROMPT

    def test_prompt_mentions_all_fields(self) -> None:
        """System prompt mentions all expected output fields."""
        assert "essence" in ONTOLOGY_ANALYSIS_SYSTEM_PROMPT.lower()
        assert "is_root_problem" in ONTOLOGY_ANALYSIS_SYSTEM_PROMPT
        assert "prerequisites" in ONTOLOGY_ANALYSIS_SYSTEM_PROMPT.lower()
        assert "hidden_assumptions" in ONTOLOGY_ANALYSIS_SYSTEM_PROMPT
        assert "confidence" in ONTOLOGY_ANALYSIS_SYSTEM_PROMPT.lower()
        assert "reasoning" in ONTOLOGY_ANALYSIS_SYSTEM_PROMPT.lower()
