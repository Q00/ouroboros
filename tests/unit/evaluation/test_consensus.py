"""Tests for Stage 3 multi-model consensus evaluation."""

from unittest.mock import AsyncMock

import pytest

from ouroboros.core.errors import ProviderError
from ouroboros.core.ontology_aspect import AnalysisResult
from ouroboros.core.types import Result
from ouroboros.evaluation.consensus import (
    ConsensusConfig,
    ConsensusEvaluator,
    DeliberativeConfig,
    DeliberativeConsensus,
    _parse_judgment_response,
    build_consensus_prompt,
    parse_vote_response,
    run_consensus_evaluation,
    run_deliberative_evaluation,
)
from ouroboros.evaluation.models import EvaluationContext, FinalVerdict, VoterRole
from ouroboros.providers.base import CompletionResponse, UsageInfo
from ouroboros.strategies.devil_advocate import DevilAdvocateStrategy


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

    def test_json_with_code_block_after(self) -> None:
        """Extract JSON correctly when code block follows."""
        # Edge case: LLM response with JSON followed by code with braces
        response = """Here is my vote:
        {"approved": true, "confidence": 0.9, "reasoning": "Looks good"}

        And here's some code: function() { return 1; }"""
        result = parse_vote_response(response, "model")

        assert result.is_ok
        assert result.value.approved is True
        assert result.value.confidence == 0.9

    def test_json_with_nested_braces(self) -> None:
        """Handle JSON with nested objects."""
        response = '{"approved": true, "confidence": 0.8, "reasoning": "Config: key=1"}'
        result = parse_vote_response(response, "model")

        assert result.is_ok
        assert result.value.approved is True


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


# ============================================================================
# Deliberative Consensus Tests
# ============================================================================


class TestParseJudgmentResponse:
    """Tests for judgment parsing."""

    def test_valid_approved_judgment(self) -> None:
        """Parse valid approved judgment."""
        response = """{
            "verdict": "approved",
            "confidence": 0.95,
            "reasoning": "Both positions were valid but Advocate's arguments are stronger"
        }"""
        result = _parse_judgment_response(response, "judge-model")

        assert result.is_ok
        judgment = result.value
        assert judgment.verdict == FinalVerdict.APPROVED
        assert judgment.confidence == 0.95
        assert "Advocate" in judgment.reasoning
        assert judgment.conditions is None

    def test_valid_conditional_judgment(self) -> None:
        """Parse valid conditional judgment."""
        response = """{
            "verdict": "conditional",
            "confidence": 0.8,
            "reasoning": "Good but needs improvements",
            "conditions": ["Add error handling", "Improve documentation"]
        }"""
        result = _parse_judgment_response(response, "judge-model")

        assert result.is_ok
        judgment = result.value
        assert judgment.verdict == FinalVerdict.CONDITIONAL
        assert judgment.conditions == ("Add error handling", "Improve documentation")

    def test_valid_rejected_judgment(self) -> None:
        """Parse valid rejected judgment."""
        response = """{
            "verdict": "rejected",
            "confidence": 0.9,
            "reasoning": "Treats symptoms rather than root cause"
        }"""
        result = _parse_judgment_response(response, "judge-model")

        assert result.is_ok
        judgment = result.value
        assert judgment.verdict == FinalVerdict.REJECTED

    def test_invalid_verdict(self) -> None:
        """Error for invalid verdict value."""
        response = '{"verdict": "maybe", "confidence": 0.5, "reasoning": "test"}'
        result = _parse_judgment_response(response, "model")

        assert result.is_err
        assert "Invalid verdict" in result.error.message

    def test_missing_verdict(self) -> None:
        """Error when verdict field missing."""
        response = '{"confidence": 0.9, "reasoning": "test"}'
        result = _parse_judgment_response(response, "model")

        assert result.is_err
        assert "verdict" in result.error.message.lower()

    def test_no_json(self) -> None:
        """Error when no JSON found."""
        response = "I think this should be approved"
        result = _parse_judgment_response(response, "model")

        assert result.is_err
        assert "Could not find JSON" in result.error.message


class TestDeliberativeConfig:
    """Tests for DeliberativeConfig."""

    def test_default_values(self) -> None:
        """Verify default configuration."""
        config = DeliberativeConfig()
        assert "claude" in config.advocate_model.lower() or "anthropic" in config.advocate_model.lower()
        assert "gpt" in config.devil_model.lower() or "openai" in config.devil_model.lower()
        assert "gemini" in config.judge_model.lower() or "google" in config.judge_model.lower()

    def test_custom_models(self) -> None:
        """Create config with custom models."""
        config = DeliberativeConfig(
            advocate_model="custom-advocate",
            devil_model="custom-devil",
            judge_model="custom-judge",
        )
        assert config.advocate_model == "custom-advocate"
        assert config.devil_model == "custom-devil"
        assert config.judge_model == "custom-judge"


class TestDeliberativeConsensus:
    """Tests for DeliberativeConsensus class."""

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
            goal="Test goal",
        )

    @pytest.fixture
    def mock_devil_strategy_approved(self) -> AsyncMock:
        """Create mock strategy that approves (root cause addressed)."""
        strategy = AsyncMock(spec=DevilAdvocateStrategy)
        strategy.model = "devil"
        strategy.analyze.return_value = AnalysisResult.valid(
            confidence=0.8,
            reasoning=["Addresses root cause"],
        )
        return strategy

    @pytest.fixture
    def mock_devil_strategy_rejected(self) -> AsyncMock:
        """Create mock strategy that rejects (treats symptoms)."""
        strategy = AsyncMock(spec=DevilAdvocateStrategy)
        strategy.model = "devil"
        strategy.analyze.return_value = AnalysisResult.invalid(
            reasoning=["This treats symptoms not root cause"],
            suggestions=["Analyze the underlying data model"],
            confidence=0.85,
        )
        return strategy

    @pytest.mark.asyncio
    async def test_deliberation_approved(
        self,
        mock_llm: AsyncMock,
        sample_context: EvaluationContext,
        mock_devil_strategy_approved: AsyncMock,
    ) -> None:
        """Deliberation with final approval."""
        # Round 1: Advocate approves via LLM, Devil approves via strategy
        # Round 2: Judge approves via LLM
        mock_llm.complete.side_effect = [
            # Advocate
            Result.ok(CompletionResponse(
                content='{"approved": true, "confidence": 0.9, "reasoning": "Well designed"}',
                model="advocate",
                usage=UsageInfo(0, 0, 0),
            )),
            # Judge (Devil is now via strategy, not LLM)
            Result.ok(CompletionResponse(
                content='{"verdict": "approved", "confidence": 0.85, "reasoning": "Both agree"}',
                model="judge",
                usage=UsageInfo(0, 0, 0),
            )),
        ]

        config = DeliberativeConfig(
            advocate_model="advocate",
            devil_model="devil",
            judge_model="judge",
        )
        evaluator = DeliberativeConsensus(mock_llm, config, devil_strategy=mock_devil_strategy_approved)
        result = await evaluator.deliberate(sample_context)

        assert result.is_ok
        deliberation, events = result.value
        assert deliberation.approved is True
        assert deliberation.final_verdict == FinalVerdict.APPROVED
        assert deliberation.is_root_solution is True  # Devil approved via strategy
        assert deliberation.advocate_position.role == VoterRole.ADVOCATE
        assert deliberation.devil_position.role == VoterRole.DEVIL
        mock_devil_strategy_approved.analyze.assert_called_once()

    @pytest.mark.asyncio
    async def test_deliberation_rejected_by_devil(
        self,
        mock_llm: AsyncMock,
        sample_context: EvaluationContext,
        mock_devil_strategy_rejected: AsyncMock,
    ) -> None:
        """Deliberation rejected due to Devil's ontological critique."""
        mock_llm.complete.side_effect = [
            # Advocate approves
            Result.ok(CompletionResponse(
                content='{"approved": true, "confidence": 0.9, "reasoning": "Looks good"}',
                model="advocate",
                usage=UsageInfo(0, 0, 0),
            )),
            # Judge rejects based on Devil's argument (Devil is via strategy)
            Result.ok(CompletionResponse(
                content='{"verdict": "rejected", "confidence": 0.8, "reasoning": "Devil\'s critique is valid"}',
                model="judge",
                usage=UsageInfo(0, 0, 0),
            )),
        ]

        config = DeliberativeConfig(
            advocate_model="advocate",
            devil_model="devil",
            judge_model="judge",
        )
        evaluator = DeliberativeConsensus(mock_llm, config, devil_strategy=mock_devil_strategy_rejected)
        result = await evaluator.deliberate(sample_context)

        assert result.is_ok
        deliberation, _ = result.value
        assert deliberation.approved is False
        assert deliberation.final_verdict == FinalVerdict.REJECTED
        assert deliberation.is_root_solution is False  # Devil rejected via strategy

    @pytest.mark.asyncio
    async def test_deliberation_conditional(
        self,
        mock_llm: AsyncMock,
        sample_context: EvaluationContext,
        mock_devil_strategy_rejected: AsyncMock,
    ) -> None:
        """Deliberation with conditional approval."""
        mock_llm.complete.side_effect = [
            # Advocate approves
            Result.ok(CompletionResponse(
                content='{"approved": true, "confidence": 0.85, "reasoning": "Good overall"}',
                model="advocate",
                usage=UsageInfo(0, 0, 0),
            )),
            # Judge gives conditional (Devil concerns via strategy)
            Result.ok(CompletionResponse(
                content='{"verdict": "conditional", "confidence": 0.75, "reasoning": "Valid with changes", "conditions": ["Add error handling"]}',
                model="judge",
                usage=UsageInfo(0, 0, 0),
            )),
        ]

        config = DeliberativeConfig(
            advocate_model="advocate",
            devil_model="devil",
            judge_model="judge",
        )
        evaluator = DeliberativeConsensus(mock_llm, config, devil_strategy=mock_devil_strategy_rejected)
        result = await evaluator.deliberate(sample_context)

        assert result.is_ok
        deliberation, _ = result.value
        assert deliberation.approved is False  # CONDITIONAL != APPROVED
        assert deliberation.final_verdict == FinalVerdict.CONDITIONAL
        assert deliberation.has_conditions is True
        assert deliberation.judgment.conditions == ("Add error handling",)

    @pytest.mark.asyncio
    async def test_deliberation_advocate_failure(
        self,
        mock_llm: AsyncMock,
        sample_context: EvaluationContext,
        mock_devil_strategy_approved: AsyncMock,
    ) -> None:
        """Handle Advocate failure gracefully."""
        mock_llm.complete.side_effect = [
            Result.err(ProviderError("Advocate API error")),
        ]

        config = DeliberativeConfig(
            advocate_model="advocate",
            devil_model="devil",
            judge_model="judge",
        )
        evaluator = DeliberativeConsensus(mock_llm, config, devil_strategy=mock_devil_strategy_approved)
        result = await evaluator.deliberate(sample_context)

        assert result.is_err
        assert "Advocate" in result.error.message or "failed" in result.error.message.lower()

    @pytest.mark.asyncio
    async def test_deliberation_generates_events(
        self,
        mock_llm: AsyncMock,
        sample_context: EvaluationContext,
        mock_devil_strategy_approved: AsyncMock,
    ) -> None:
        """Events are generated correctly."""
        mock_llm.complete.side_effect = [
            Result.ok(CompletionResponse(
                content='{"approved": true, "confidence": 0.9, "reasoning": "Good"}',
                model="advocate",
                usage=UsageInfo(0, 0, 0),
            )),
            # Judge (Devil is via strategy)
            Result.ok(CompletionResponse(
                content='{"verdict": "approved", "confidence": 0.85, "reasoning": "Agreed"}',
                model="judge",
                usage=UsageInfo(0, 0, 0),
            )),
        ]

        config = DeliberativeConfig(
            advocate_model="advocate",
            devil_model="devil",
            judge_model="judge",
        )
        evaluator = DeliberativeConsensus(mock_llm, config, devil_strategy=mock_devil_strategy_approved)
        result = await evaluator.deliberate(sample_context, trigger_reason="test")

        assert result.is_ok
        _, events = result.value
        assert events[0].type == "evaluation.stage3.started"
        assert "deliberative" in events[0].data["trigger_reason"]
        assert events[1].type == "evaluation.stage3.completed"


class TestRunDeliberativeEvaluation:
    """Tests for convenience function."""

    @pytest.mark.asyncio
    async def test_convenience_function(self) -> None:
        """Test the convenience function works."""
        mock_llm = AsyncMock()
        mock_llm.complete.side_effect = [
            Result.ok(CompletionResponse(
                content='{"approved": true, "confidence": 0.9, "reasoning": "Good"}',
                model="advocate",
                usage=UsageInfo(0, 0, 0),
            )),
            # Judge (Devil is via strategy)
            Result.ok(CompletionResponse(
                content='{"verdict": "approved", "confidence": 0.85, "reasoning": "Agreed"}',
                model="judge",
                usage=UsageInfo(0, 0, 0),
            )),
        ]

        # Create mock devil strategy
        mock_devil_strategy = AsyncMock(spec=DevilAdvocateStrategy)
        mock_devil_strategy.model = "devil"
        mock_devil_strategy.analyze.return_value = AnalysisResult.valid(
            confidence=0.8,
            reasoning=["Addresses root cause"],
        )

        context = EvaluationContext(
            execution_id="exec-1",
            seed_id="seed-1",
            current_ac="Test AC",
            artifact="test",
        )
        config = DeliberativeConfig(
            advocate_model="advocate",
            devil_model="devil",
            judge_model="judge",
        )

        result = await run_deliberative_evaluation(
            context,
            mock_llm,
            trigger_reason="test",
            config=config,
            devil_strategy=mock_devil_strategy,
        )
        assert result.is_ok
        deliberation, _ = result.value
        assert deliberation.approved is True
