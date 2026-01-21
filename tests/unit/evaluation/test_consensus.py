"""Tests for Stage 3 multi-model consensus evaluation."""

from unittest.mock import AsyncMock

import pytest

from ouroboros.core.errors import ProviderError
from ouroboros.core.types import Result
from ouroboros.evaluation.consensus import (
    ConsensusConfig,
    ConsensusEvaluator,
    build_consensus_prompt,
    parse_vote_response,
    run_consensus_evaluation,
)
from ouroboros.evaluation.models import EvaluationContext
from ouroboros.providers.base import CompletionResponse, UsageInfo


class TestBuildConsensusPrompt:
    """Tests for prompt building."""

    def test_minimal_context(self) -> None:
        """Build prompt with minimal context."""
        context = EvaluationContext(
            execution_id="exec-1",
            seed_id="seed-1",
            current_ac="User can login",
            artifact="def login(): pass",
        )
        prompt = build_consensus_prompt(context)

        assert "User can login" in prompt
        assert "def login(): pass" in prompt
        assert "consensus approval" in prompt.lower()

    def test_full_context(self) -> None:
        """Build prompt with full context."""
        context = EvaluationContext(
            execution_id="exec-1",
            seed_id="seed-1",
            current_ac="User can logout",
            artifact="def logout(): session.clear()",
            goal="Build auth system",
            constraints=("Must be secure",),
        )
        prompt = build_consensus_prompt(context)

        assert "User can logout" in prompt
        assert "Build auth system" in prompt
        assert "Must be secure" in prompt


class TestParseVoteResponse:
    """Tests for vote parsing."""

    def test_valid_vote(self) -> None:
        """Parse valid vote response."""
        response = """{
            "approved": true,
            "confidence": 0.95,
            "reasoning": "Looks good"
        }"""
        result = parse_vote_response(response, "gpt-4o")

        assert result.is_ok
        vote = result.value
        assert vote.model == "gpt-4o"
        assert vote.approved is True
        assert vote.confidence == 0.95
        assert vote.reasoning == "Looks good"

    def test_vote_with_surrounding_text(self) -> None:
        """Parse vote embedded in text."""
        response = """My evaluation:
        {"approved": false, "confidence": 0.8, "reasoning": "Issues found"}
        End of review."""
        result = parse_vote_response(response, "claude")

        assert result.is_ok
        assert result.value.approved is False

    def test_confidence_clamped(self) -> None:
        """Confidence clamped to [0,1]."""
        response = '{"approved": true, "confidence": 1.5, "reasoning": "Test"}'
        result = parse_vote_response(response, "model")

        assert result.is_ok
        assert result.value.confidence == 1.0

    def test_default_confidence(self) -> None:
        """Default confidence when missing."""
        response = '{"approved": true, "reasoning": "OK"}'
        result = parse_vote_response(response, "model")

        assert result.is_ok
        assert result.value.confidence == 0.5

    def test_missing_approved(self) -> None:
        """Error when approved field missing."""
        response = '{"confidence": 0.9, "reasoning": "Test"}'
        result = parse_vote_response(response, "model")

        assert result.is_err
        assert "approved" in result.error.message.lower()

    def test_no_json(self) -> None:
        """Error when no JSON found."""
        response = "I approve this artifact"
        result = parse_vote_response(response, "model")

        assert result.is_err
        assert "Could not find JSON" in result.error.message


class TestConsensusConfig:
    """Tests for ConsensusConfig."""

    def test_default_values(self) -> None:
        """Verify default configuration."""
        config = ConsensusConfig()
        assert len(config.models) == 3
        assert config.majority_threshold == 0.66
        assert config.diversity_required is True

    def test_custom_models(self) -> None:
        """Create config with custom models."""
        config = ConsensusConfig(
            models=("model-a", "model-b", "model-c", "model-d"),
            majority_threshold=0.75,
        )
        assert len(config.models) == 4
        assert config.majority_threshold == 0.75


class TestConsensusEvaluator:
    """Tests for ConsensusEvaluator class."""

    @pytest.fixture
    def mock_llm(self) -> AsyncMock:
        """Create mock LLM adapter."""
        return AsyncMock()

    @pytest.fixture
    def sample_context(self) -> EvaluationContext:
        """Create sample evaluation context."""
        return EvaluationContext(
            execution_id="exec-1",
            seed_id="seed-1",
            current_ac="Test criterion",
            artifact="test code",
        )

    @pytest.mark.asyncio
    async def test_consensus_approved(
        self,
        mock_llm: AsyncMock,
        sample_context: EvaluationContext,
    ) -> None:
        """Consensus with 3/3 approval."""
        mock_llm.complete.return_value = Result.ok(
            CompletionResponse(
                content='{"approved": true, "confidence": 0.9, "reasoning": "Good"}',
                model="test",
                usage=UsageInfo(0, 0, 0),
            )
        )

        config = ConsensusConfig(models=("m1", "m2", "m3"))
        evaluator = ConsensusEvaluator(mock_llm, config)
        result = await evaluator.evaluate(sample_context)

        assert result.is_ok
        consensus, events = result.value
        assert consensus.approved is True
        assert consensus.majority_ratio == 1.0
        assert len(consensus.votes) == 3

    @pytest.mark.asyncio
    async def test_consensus_rejected(
        self,
        mock_llm: AsyncMock,
        sample_context: EvaluationContext,
    ) -> None:
        """Consensus with 0/3 approval."""
        mock_llm.complete.return_value = Result.ok(
            CompletionResponse(
                content='{"approved": false, "confidence": 0.8, "reasoning": "Issues"}',
                model="test",
                usage=UsageInfo(0, 0, 0),
            )
        )

        config = ConsensusConfig(models=("m1", "m2", "m3"))
        evaluator = ConsensusEvaluator(mock_llm, config)
        result = await evaluator.evaluate(sample_context)

        assert result.is_ok
        consensus, _ = result.value
        assert consensus.approved is False
        assert consensus.majority_ratio == 0.0

    @pytest.mark.asyncio
    async def test_consensus_2_of_3(
        self,
        mock_llm: AsyncMock,
        sample_context: EvaluationContext,
    ) -> None:
        """Consensus with 2/3 approval (passes threshold)."""
        # First two approve, third rejects
        mock_llm.complete.side_effect = [
            Result.ok(CompletionResponse(
                content='{"approved": true, "confidence": 0.9, "reasoning": "Good"}',
                model="m1",
                usage=UsageInfo(0, 0, 0),
            )),
            Result.ok(CompletionResponse(
                content='{"approved": true, "confidence": 0.85, "reasoning": "OK"}',
                model="m2",
                usage=UsageInfo(0, 0, 0),
            )),
            Result.ok(CompletionResponse(
                content='{"approved": false, "confidence": 0.7, "reasoning": "Concerns"}',
                model="m3",
                usage=UsageInfo(0, 0, 0),
            )),
        ]

        config = ConsensusConfig(models=("m1", "m2", "m3"))
        evaluator = ConsensusEvaluator(mock_llm, config)
        result = await evaluator.evaluate(sample_context)

        assert result.is_ok
        consensus, _ = result.value
        # 2/3 = 0.6666... which is >= 0.66 threshold
        assert consensus.approved is True
        assert abs(consensus.majority_ratio - 0.6666) < 0.01
        assert len(consensus.disagreements) == 1

    @pytest.mark.asyncio
    async def test_consensus_1_of_3(
        self,
        mock_llm: AsyncMock,
        sample_context: EvaluationContext,
    ) -> None:
        """Consensus with 1/3 approval (fails threshold)."""
        mock_llm.complete.side_effect = [
            Result.ok(CompletionResponse(
                content='{"approved": true, "confidence": 0.9, "reasoning": "Good"}',
                model="m1",
                usage=UsageInfo(0, 0, 0),
            )),
            Result.ok(CompletionResponse(
                content='{"approved": false, "confidence": 0.85, "reasoning": "Bad"}',
                model="m2",
                usage=UsageInfo(0, 0, 0),
            )),
            Result.ok(CompletionResponse(
                content='{"approved": false, "confidence": 0.8, "reasoning": "No"}',
                model="m3",
                usage=UsageInfo(0, 0, 0),
            )),
        ]

        config = ConsensusConfig(models=("m1", "m2", "m3"))
        evaluator = ConsensusEvaluator(mock_llm, config)
        result = await evaluator.evaluate(sample_context)

        assert result.is_ok
        consensus, _ = result.value
        assert consensus.approved is False  # 1/3 < 0.67
        assert abs(consensus.majority_ratio - 0.33) < 0.01

    @pytest.mark.asyncio
    async def test_consensus_generates_events(
        self,
        mock_llm: AsyncMock,
        sample_context: EvaluationContext,
    ) -> None:
        """Events are generated correctly."""
        mock_llm.complete.return_value = Result.ok(
            CompletionResponse(
                content='{"approved": true, "confidence": 0.9, "reasoning": "OK"}',
                model="test",
                usage=UsageInfo(0, 0, 0),
            )
        )

        config = ConsensusConfig(models=("m1", "m2", "m3"))
        evaluator = ConsensusEvaluator(mock_llm, config)
        result = await evaluator.evaluate(sample_context, trigger_reason="uncertainty")

        assert result.is_ok
        _, events = result.value
        assert events[0].type == "evaluation.stage3.started"
        assert events[0].data["trigger_reason"] == "uncertainty"
        assert events[1].type == "evaluation.stage3.completed"

    @pytest.mark.asyncio
    async def test_partial_failures_handled(
        self,
        mock_llm: AsyncMock,
        sample_context: EvaluationContext,
    ) -> None:
        """Handle partial model failures."""
        mock_llm.complete.side_effect = [
            Result.ok(CompletionResponse(
                content='{"approved": true, "confidence": 0.9, "reasoning": "Good"}',
                model="m1",
                usage=UsageInfo(0, 0, 0),
            )),
            Result.err(ProviderError("API error")),
            Result.ok(CompletionResponse(
                content='{"approved": true, "confidence": 0.85, "reasoning": "OK"}',
                model="m3",
                usage=UsageInfo(0, 0, 0),
            )),
        ]

        config = ConsensusConfig(models=("m1", "m2", "m3"))
        evaluator = ConsensusEvaluator(mock_llm, config)
        result = await evaluator.evaluate(sample_context)

        # Should still work with 2 votes
        assert result.is_ok
        consensus, _ = result.value
        assert len(consensus.votes) == 2
        assert consensus.approved is True

    @pytest.mark.asyncio
    async def test_too_few_votes_error(
        self,
        mock_llm: AsyncMock,
        sample_context: EvaluationContext,
    ) -> None:
        """Error when too few votes collected."""
        mock_llm.complete.side_effect = [
            Result.ok(CompletionResponse(
                content='{"approved": true, "confidence": 0.9, "reasoning": "Good"}',
                model="m1",
                usage=UsageInfo(0, 0, 0),
            )),
            Result.err(ProviderError("Error 1")),
            Result.err(ProviderError("Error 2")),
        ]

        config = ConsensusConfig(models=("m1", "m2", "m3"))
        evaluator = ConsensusEvaluator(mock_llm, config)
        result = await evaluator.evaluate(sample_context)

        assert result.is_err
        assert "Not enough votes" in result.error.message


class TestRunConsensusEvaluation:
    """Tests for convenience function."""

    @pytest.mark.asyncio
    async def test_convenience_function(self) -> None:
        """Test the convenience function works."""
        mock_llm = AsyncMock()
        mock_llm.complete.return_value = Result.ok(
            CompletionResponse(
                content='{"approved": true, "confidence": 0.9, "reasoning": "OK"}',
                model="test",
                usage=UsageInfo(0, 0, 0),
            )
        )

        context = EvaluationContext(
            execution_id="exec-1",
            seed_id="seed-1",
            current_ac="Test AC",
            artifact="test",
        )
        config = ConsensusConfig(models=("m1", "m2", "m3"))

        result = await run_consensus_evaluation(
            context,
            mock_llm,
            trigger_reason="test",
            config=config,
        )
        assert result.is_ok
