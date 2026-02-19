"""Tests for Stage 2 semantic evaluation."""

from unittest.mock import AsyncMock

import pytest

from ouroboros.core.errors import ProviderError
from ouroboros.core.types import Result
from ouroboros.evaluation.models import EvaluationContext
from ouroboros.evaluation.semantic import (
    SemanticConfig,
    SemanticEvaluator,
    build_evaluation_prompt,
    parse_semantic_response,
    run_semantic_evaluation,
)
from ouroboros.providers.base import CompletionResponse, UsageInfo


class TestBuildEvaluationPrompt:
    """Tests for prompt building."""

    def test_minimal_context(self) -> None:
        """Build prompt with minimal context."""
        context = EvaluationContext(
            execution_id="exec-1",
            seed_id="seed-1",
            current_ac="User can login",
            artifact="def login(): pass",
        )
        prompt = build_evaluation_prompt(context)

        assert "User can login" in prompt
        assert "def login(): pass" in prompt
        assert "code" in prompt  # default artifact_type

    def test_full_context(self) -> None:
        """Build prompt with full context."""
        context = EvaluationContext(
            execution_id="exec-1",
            seed_id="seed-1",
            current_ac="User can login securely",
            artifact="def login(username, password): ...",
            artifact_type="python",
            goal="Build authentication system",
            constraints=("Must be secure", "No plaintext passwords"),
        )
        prompt = build_evaluation_prompt(context)

        assert "User can login securely" in prompt
        assert "Build authentication system" in prompt
        assert "Must be secure" in prompt
        assert "No plaintext passwords" in prompt


class TestParseSemanticResponse:
    """Tests for response parsing."""

    def test_valid_json_response(self) -> None:
        """Parse valid JSON response."""
        response = """{
            "score": 0.85,
            "ac_compliance": true,
            "goal_alignment": 0.9,
            "drift_score": 0.1,
            "uncertainty": 0.2,
            "reasoning": "Good implementation"
        }"""
        result = parse_semantic_response(response)

        assert result.is_ok
        semantic = result.value
        assert semantic.score == 0.85
        assert semantic.ac_compliance is True
        assert semantic.goal_alignment == 0.9
        assert semantic.drift_score == 0.1
        assert semantic.uncertainty == 0.2
        assert semantic.reasoning == "Good implementation"

    def test_json_with_surrounding_text(self) -> None:
        """Parse JSON embedded in text."""
        response = """Here is my evaluation:
        {"score": 0.8, "ac_compliance": true, "goal_alignment": 0.85, "drift_score": 0.15, "uncertainty": 0.1, "reasoning": "Works well"}
        Thank you for asking."""
        result = parse_semantic_response(response)

        assert result.is_ok
        assert result.value.score == 0.8

    def test_values_clamped_to_range(self) -> None:
        """Values outside [0,1] are clamped."""
        response = """{
            "score": 1.5,
            "ac_compliance": true,
            "goal_alignment": -0.1,
            "drift_score": 2.0,
            "uncertainty": 0.2,
            "reasoning": "Test"
        }"""
        result = parse_semantic_response(response)

        assert result.is_ok
        assert result.value.score == 1.0  # clamped from 1.5
        assert result.value.goal_alignment == 0.0  # clamped from -0.1
        assert result.value.drift_score == 1.0  # clamped from 2.0

    def test_missing_required_field(self) -> None:
        """Error when required field is missing."""
        response = """{
            "score": 0.8,
            "ac_compliance": true
        }"""
        result = parse_semantic_response(response)

        assert result.is_err
        assert "Missing required fields" in result.error.message

    def test_no_json_in_response(self) -> None:
        """Error when no JSON found."""
        response = "This is just text without any JSON"
        result = parse_semantic_response(response)

        assert result.is_err
        assert "Could not find JSON" in result.error.message

    def test_invalid_json(self) -> None:
        """Error on malformed JSON."""
        response = '{"score": 0.8, "ac_compliance": }'
        result = parse_semantic_response(response)

        assert result.is_err
        assert "Invalid JSON" in result.error.message


class TestSemanticConfig:
    """Tests for SemanticConfig."""

    def test_default_values(self) -> None:
        """Verify default configuration."""
        config = SemanticConfig()
        assert config.temperature == 0.2
        assert config.max_tokens == 2048
        assert config.satisfaction_threshold == 0.8

    def test_custom_values(self) -> None:
        """Create config with custom values."""
        config = SemanticConfig(
            model="gpt-4o",
            temperature=0.5,
            satisfaction_threshold=0.9,
        )
        assert config.model == "gpt-4o"
        assert config.temperature == 0.5


class TestSemanticEvaluator:
    """Tests for SemanticEvaluator class."""

    @pytest.fixture
    def mock_llm(self) -> AsyncMock:
        """Create mock LLM adapter."""
        mock = AsyncMock()
        return mock

    @pytest.fixture
    def sample_context(self) -> EvaluationContext:
        """Create sample evaluation context."""
        return EvaluationContext(
            execution_id="exec-1",
            seed_id="seed-1",
            current_ac="User can login",
            artifact="def login(): pass",
            goal="Build auth",
        )

    @pytest.mark.asyncio
    async def test_evaluate_success(
        self,
        mock_llm: AsyncMock,
        sample_context: EvaluationContext,
    ) -> None:
        """Successful evaluation."""
        mock_llm.complete.return_value = Result.ok(
            CompletionResponse(
                content="""{
                    "score": 0.85,
                    "ac_compliance": true,
                    "goal_alignment": 0.9,
                    "drift_score": 0.1,
                    "uncertainty": 0.15,
                    "reasoning": "Good implementation"
                }""",
                model="test-model",
                usage=UsageInfo(100, 50, 150),
            )
        )

        evaluator = SemanticEvaluator(mock_llm)
        result = await evaluator.evaluate(sample_context)

        assert result.is_ok
        semantic_result, events = result.value
        assert semantic_result.score == 0.85
        assert semantic_result.ac_compliance is True
        assert len(events) == 2  # start + complete

    @pytest.mark.asyncio
    async def test_evaluate_generates_events(
        self,
        mock_llm: AsyncMock,
        sample_context: EvaluationContext,
    ) -> None:
        """Events are generated correctly."""
        mock_llm.complete.return_value = Result.ok(
            CompletionResponse(
                content="""{
                    "score": 0.8,
                    "ac_compliance": true,
                    "goal_alignment": 0.8,
                    "drift_score": 0.2,
                    "uncertainty": 0.1,
                    "reasoning": "OK"
                }""",
                model="test",
                usage=UsageInfo(0, 0, 0),
            )
        )

        evaluator = SemanticEvaluator(mock_llm)
        result = await evaluator.evaluate(sample_context)

        assert result.is_ok
        _, events = result.value
        assert events[0].type == "evaluation.stage2.started"
        assert events[1].type == "evaluation.stage2.completed"
        assert events[1].data["score"] == 0.8

    @pytest.mark.asyncio
    async def test_evaluate_llm_error(
        self,
        mock_llm: AsyncMock,
        sample_context: EvaluationContext,
    ) -> None:
        """Handle LLM error."""
        mock_llm.complete.return_value = Result.err(ProviderError("API error", provider="test"))

        evaluator = SemanticEvaluator(mock_llm)
        result = await evaluator.evaluate(sample_context)

        assert result.is_err
        assert isinstance(result.error, ProviderError)

    @pytest.mark.asyncio
    async def test_evaluate_parse_error(
        self,
        mock_llm: AsyncMock,
        sample_context: EvaluationContext,
    ) -> None:
        """Handle parse error."""
        mock_llm.complete.return_value = Result.ok(
            CompletionResponse(
                content="This is not valid JSON at all",
                model="test",
                usage=UsageInfo(0, 0, 0),
            )
        )

        evaluator = SemanticEvaluator(mock_llm)
        result = await evaluator.evaluate(sample_context)

        assert result.is_err
        assert "Could not find JSON" in result.error.message


class TestRunSemanticEvaluation:
    """Tests for convenience function."""

    @pytest.mark.asyncio
    async def test_convenience_function(self) -> None:
        """Test the convenience function works."""
        mock_llm = AsyncMock()
        mock_llm.complete.return_value = Result.ok(
            CompletionResponse(
                content="""{
                    "score": 0.9,
                    "ac_compliance": true,
                    "goal_alignment": 0.9,
                    "drift_score": 0.05,
                    "uncertainty": 0.1,
                    "reasoning": "Excellent"
                }""",
                model="test",
                usage=UsageInfo(0, 0, 0),
            )
        )

        context = EvaluationContext(
            execution_id="exec-1",
            seed_id="seed-1",
            current_ac="Test AC",
            artifact="test code",
        )

        result = await run_semantic_evaluation(context, mock_llm)
        assert result.is_ok
