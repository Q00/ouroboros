"""Unit tests for ouroboros.bigbang.ambiguity module."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.bigbang.ambiguity import (
    AMBIGUITY_THRESHOLD,
    CONSTRAINT_CLARITY_WEIGHT,
    GOAL_CLARITY_WEIGHT,
    MAX_TOKEN_LIMIT,
    SCORING_TEMPERATURE,
    SUCCESS_CRITERIA_CLARITY_WEIGHT,
    AmbiguityScore,
    AmbiguityScorer,
    ComponentScore,
    ScoreBreakdown,
    format_score_display,
    is_ready_for_seed,
)
from ouroboros.bigbang.interview import InterviewRound, InterviewState
from ouroboros.core.errors import ProviderError
from ouroboros.core.types import Result
from ouroboros.providers.base import CompletionResponse, UsageInfo


def create_mock_completion_response(
    content: str,
    model: str = "openrouter/google/gemini-2.0-flash-001",
    finish_reason: str = "stop",
) -> CompletionResponse:
    """Create a mock completion response."""
    return CompletionResponse(
        content=content,
        model=model,
        usage=UsageInfo(prompt_tokens=200, completion_tokens=100, total_tokens=300),
        finish_reason=finish_reason,
    )


def create_valid_scoring_response(
    goal_score: float = 0.9,
    goal_justification: str = "Goal is well-defined with specific deliverable.",
    constraint_score: float = 0.85,
    constraint_justification: str = "Technical constraints clearly specified.",
    success_score: float = 0.8,
    success_justification: str = "Success criteria are measurable.",
) -> str:
    """Create a valid LLM scoring response string in JSON format."""
    import json

    return json.dumps({
        "goal_clarity_score": goal_score,
        "goal_clarity_justification": goal_justification,
        "constraint_clarity_score": constraint_score,
        "constraint_clarity_justification": constraint_justification,
        "success_criteria_clarity_score": success_score,
        "success_criteria_clarity_justification": success_justification,
    })


def create_interview_state_with_rounds(
    interview_id: str = "test_001",
    initial_context: str = "Build a CLI tool for task management",
    rounds: int = 3,
) -> InterviewState:
    """Create an interview state with specified number of rounds."""
    state = InterviewState(
        interview_id=interview_id,
        initial_context=initial_context,
    )
    for i in range(rounds):
        state.rounds.append(
            InterviewRound(
                round_number=i + 1,
                question=f"Question {i + 1}?",
                user_response=f"Answer {i + 1}",
            )
        )
    return state


class TestComponentScore:
    """Test ComponentScore model."""

    def test_component_score_creation(self) -> None:
        """ComponentScore creates with valid parameters."""
        score = ComponentScore(
            name="Goal Clarity",
            clarity_score=0.85,
            weight=0.4,
            justification="Goal is well-defined.",
        )

        assert score.name == "Goal Clarity"
        assert score.clarity_score == 0.85
        assert score.weight == 0.4
        assert score.justification == "Goal is well-defined."

    def test_component_score_clamps_score(self) -> None:
        """ComponentScore validates score range 0.0 to 1.0."""
        with pytest.raises(ValueError):
            ComponentScore(
                name="Test",
                clarity_score=1.5,  # Invalid: above 1.0
                weight=0.3,
                justification="Test",
            )

        with pytest.raises(ValueError):
            ComponentScore(
                name="Test",
                clarity_score=-0.1,  # Invalid: below 0.0
                weight=0.3,
                justification="Test",
            )

    def test_component_score_boundary_values(self) -> None:
        """ComponentScore accepts boundary values 0.0 and 1.0."""
        min_score = ComponentScore(
            name="Min",
            clarity_score=0.0,
            weight=0.5,
            justification="Minimum",
        )
        max_score = ComponentScore(
            name="Max",
            clarity_score=1.0,
            weight=0.5,
            justification="Maximum",
        )

        assert min_score.clarity_score == 0.0
        assert max_score.clarity_score == 1.0


class TestScoreBreakdown:
    """Test ScoreBreakdown model."""

    def test_score_breakdown_creation(self) -> None:
        """ScoreBreakdown creates with all components."""
        breakdown = ScoreBreakdown(
            goal_clarity=ComponentScore(
                name="Goal Clarity",
                clarity_score=0.9,
                weight=GOAL_CLARITY_WEIGHT,
                justification="Clear goal.",
            ),
            constraint_clarity=ComponentScore(
                name="Constraint Clarity",
                clarity_score=0.85,
                weight=CONSTRAINT_CLARITY_WEIGHT,
                justification="Clear constraints.",
            ),
            success_criteria_clarity=ComponentScore(
                name="Success Criteria Clarity",
                clarity_score=0.8,
                weight=SUCCESS_CRITERIA_CLARITY_WEIGHT,
                justification="Clear success criteria.",
            ),
        )

        assert breakdown.goal_clarity.clarity_score == 0.9
        assert breakdown.constraint_clarity.clarity_score == 0.85
        assert breakdown.success_criteria_clarity.clarity_score == 0.8

    def test_score_breakdown_components_list(self) -> None:
        """ScoreBreakdown.components returns all components."""
        breakdown = ScoreBreakdown(
            goal_clarity=ComponentScore(
                name="Goal Clarity",
                clarity_score=0.9,
                weight=GOAL_CLARITY_WEIGHT,
                justification="Clear goal.",
            ),
            constraint_clarity=ComponentScore(
                name="Constraint Clarity",
                clarity_score=0.85,
                weight=CONSTRAINT_CLARITY_WEIGHT,
                justification="Clear constraints.",
            ),
            success_criteria_clarity=ComponentScore(
                name="Success Criteria Clarity",
                clarity_score=0.8,
                weight=SUCCESS_CRITERIA_CLARITY_WEIGHT,
                justification="Clear success criteria.",
            ),
        )

        components = breakdown.components
        assert len(components) == 3
        assert components[0].name == "Goal Clarity"
        assert components[1].name == "Constraint Clarity"
        assert components[2].name == "Success Criteria Clarity"


class TestAmbiguityScore:
    """Test AmbiguityScore dataclass."""

    def test_ambiguity_score_creation(self) -> None:
        """AmbiguityScore creates with correct values."""
        breakdown = ScoreBreakdown(
            goal_clarity=ComponentScore(
                name="Goal Clarity",
                clarity_score=0.9,
                weight=GOAL_CLARITY_WEIGHT,
                justification="Clear goal.",
            ),
            constraint_clarity=ComponentScore(
                name="Constraint Clarity",
                clarity_score=0.85,
                weight=CONSTRAINT_CLARITY_WEIGHT,
                justification="Clear constraints.",
            ),
            success_criteria_clarity=ComponentScore(
                name="Success Criteria Clarity",
                clarity_score=0.8,
                weight=SUCCESS_CRITERIA_CLARITY_WEIGHT,
                justification="Clear success criteria.",
            ),
        )

        score = AmbiguityScore(overall_score=0.15, breakdown=breakdown)

        assert score.overall_score == 0.15
        assert score.breakdown == breakdown

    def test_is_ready_for_seed_when_below_threshold(self) -> None:
        """is_ready_for_seed returns True when score <= 0.2."""
        breakdown = ScoreBreakdown(
            goal_clarity=ComponentScore(
                name="Goal", clarity_score=0.9, weight=0.4, justification="Test"
            ),
            constraint_clarity=ComponentScore(
                name="Constraint", clarity_score=0.9, weight=0.3, justification="Test"
            ),
            success_criteria_clarity=ComponentScore(
                name="Success", clarity_score=0.9, weight=0.3, justification="Test"
            ),
        )

        score = AmbiguityScore(overall_score=0.15, breakdown=breakdown)
        assert score.is_ready_for_seed is True

    def test_is_ready_for_seed_at_threshold(self) -> None:
        """is_ready_for_seed returns True when score == 0.2."""
        breakdown = ScoreBreakdown(
            goal_clarity=ComponentScore(
                name="Goal", clarity_score=0.8, weight=0.4, justification="Test"
            ),
            constraint_clarity=ComponentScore(
                name="Constraint", clarity_score=0.8, weight=0.3, justification="Test"
            ),
            success_criteria_clarity=ComponentScore(
                name="Success", clarity_score=0.8, weight=0.3, justification="Test"
            ),
        )

        score = AmbiguityScore(overall_score=0.2, breakdown=breakdown)
        assert score.is_ready_for_seed is True

    def test_is_ready_for_seed_when_above_threshold(self) -> None:
        """is_ready_for_seed returns False when score > 0.2."""
        breakdown = ScoreBreakdown(
            goal_clarity=ComponentScore(
                name="Goal", clarity_score=0.7, weight=0.4, justification="Test"
            ),
            constraint_clarity=ComponentScore(
                name="Constraint", clarity_score=0.7, weight=0.3, justification="Test"
            ),
            success_criteria_clarity=ComponentScore(
                name="Success", clarity_score=0.7, weight=0.3, justification="Test"
            ),
        )

        score = AmbiguityScore(overall_score=0.3, breakdown=breakdown)
        assert score.is_ready_for_seed is False

    def test_is_ready_for_seed_highly_ambiguous(self) -> None:
        """is_ready_for_seed returns False for highly ambiguous requirements."""
        breakdown = ScoreBreakdown(
            goal_clarity=ComponentScore(
                name="Goal", clarity_score=0.3, weight=0.4, justification="Unclear"
            ),
            constraint_clarity=ComponentScore(
                name="Constraint", clarity_score=0.2, weight=0.3, justification="Vague"
            ),
            success_criteria_clarity=ComponentScore(
                name="Success", clarity_score=0.1, weight=0.3, justification="Missing"
            ),
        )

        score = AmbiguityScore(overall_score=0.77, breakdown=breakdown)
        assert score.is_ready_for_seed is False


class TestAmbiguityScorerInit:
    """Test AmbiguityScorer initialization."""

    def test_scorer_default_values(self) -> None:
        """AmbiguityScorer initializes with default values."""
        mock_adapter = MagicMock()
        scorer = AmbiguityScorer(llm_adapter=mock_adapter)

        assert scorer.llm_adapter == mock_adapter
        assert scorer.model == "openrouter/google/gemini-2.0-flash-001"
        assert scorer.temperature == SCORING_TEMPERATURE
        assert scorer.initial_max_tokens == 2048
        assert scorer.max_retries is None  # Unlimited by default

    def test_scorer_custom_values(self) -> None:
        """AmbiguityScorer accepts custom values."""
        mock_adapter = MagicMock()
        scorer = AmbiguityScorer(
            llm_adapter=mock_adapter,
            model="custom-model",
            temperature=0.2,
            initial_max_tokens=1024,
            max_retries=5,
        )

        assert scorer.model == "custom-model"
        assert scorer.temperature == 0.2
        assert scorer.initial_max_tokens == 1024
        assert scorer.max_retries == 5


class TestAmbiguityScorerScore:
    """Test AmbiguityScorer.score method."""

    async def test_score_successful(self) -> None:
        """score returns AmbiguityScore on success."""
        mock_adapter = MagicMock()
        mock_adapter.complete = AsyncMock(
            return_value=Result.ok(
                create_mock_completion_response(
                    content=create_valid_scoring_response(
                        goal_score=0.9,
                        constraint_score=0.85,
                        success_score=0.8,
                    )
                )
            )
        )

        scorer = AmbiguityScorer(llm_adapter=mock_adapter)
        state = create_interview_state_with_rounds()

        result = await scorer.score(state)

        assert result.is_ok
        ambiguity = result.value
        assert isinstance(ambiguity, AmbiguityScore)
        assert 0.0 <= ambiguity.overall_score <= 1.0

    async def test_score_calculates_correct_overall(self) -> None:
        """score calculates correct overall ambiguity from clarity scores."""
        mock_adapter = MagicMock()
        # Clarity scores: 0.9 * 0.4 + 0.8 * 0.3 + 0.7 * 0.3 = 0.36 + 0.24 + 0.21 = 0.81
        # Ambiguity = 1 - 0.81 = 0.19
        mock_adapter.complete = AsyncMock(
            return_value=Result.ok(
                create_mock_completion_response(
                    content=create_valid_scoring_response(
                        goal_score=0.9,
                        constraint_score=0.8,
                        success_score=0.7,
                    )
                )
            )
        )

        scorer = AmbiguityScorer(llm_adapter=mock_adapter)
        state = create_interview_state_with_rounds()

        result = await scorer.score(state)

        assert result.is_ok
        ambiguity = result.value
        # Expected: 1 - (0.9 * 0.4 + 0.8 * 0.3 + 0.7 * 0.3) = 1 - 0.81 = 0.19
        assert abs(ambiguity.overall_score - 0.19) < 0.01

    async def test_score_provider_error_retries_then_fails(self) -> None:
        """score retries on provider errors and returns error after max retries."""
        mock_adapter = MagicMock()
        provider_error = ProviderError("Rate limit exceeded", provider="openai")
        mock_adapter.complete = AsyncMock(return_value=Result.err(provider_error))

        scorer = AmbiguityScorer(llm_adapter=mock_adapter, max_retries=3)
        state = create_interview_state_with_rounds()

        result = await scorer.score(state)

        assert result.is_err
        assert "Rate limit exceeded" in result.error.message
        # Should have retried 3 times
        assert mock_adapter.complete.call_count == 3

    async def test_score_provider_error_recovers_on_retry(self) -> None:
        """score recovers when provider error is transient."""
        mock_adapter = MagicMock()
        provider_error = ProviderError("Rate limit exceeded", provider="openai")
        # First call fails, second succeeds
        mock_adapter.complete = AsyncMock(
            side_effect=[
                Result.err(provider_error),
                Result.ok(
                    create_mock_completion_response(
                        content=create_valid_scoring_response()
                    )
                ),
            ]
        )

        scorer = AmbiguityScorer(llm_adapter=mock_adapter, max_retries=3)
        state = create_interview_state_with_rounds()

        result = await scorer.score(state)

        assert result.is_ok
        assert mock_adapter.complete.call_count == 2

    async def test_score_parse_error_after_retries(self) -> None:
        """score returns error after all retries exhausted."""
        mock_adapter = MagicMock()
        mock_adapter.complete = AsyncMock(
            return_value=Result.ok(
                create_mock_completion_response(
                    content="Invalid response format without required fields"
                )
            )
        )

        scorer = AmbiguityScorer(llm_adapter=mock_adapter, max_retries=3)
        state = create_interview_state_with_rounds()

        result = await scorer.score(state)

        assert result.is_err
        assert isinstance(result.error, ProviderError)
        assert "Failed to parse" in result.error.message
        assert "after 3 attempts" in result.error.message
        # Should have retried 3 times
        assert mock_adapter.complete.call_count == 3

    async def test_score_retries_with_increased_tokens_on_truncation(self) -> None:
        """score doubles tokens only when response is truncated (finish_reason=length)."""
        mock_adapter = MagicMock()
        # First call truncated (finish_reason="length"), second succeeds
        mock_adapter.complete = AsyncMock(
            side_effect=[
                Result.ok(
                    create_mock_completion_response(
                        content="GOAL_CLARITY_SCORE: 0.8\nGOAL_CLARITY_JUSTIFICATION: Good",
                        finish_reason="length",  # Truncated!
                    )
                ),
                Result.ok(
                    create_mock_completion_response(
                        content=create_valid_scoring_response()
                    )
                ),
            ]
        )

        scorer = AmbiguityScorer(llm_adapter=mock_adapter, initial_max_tokens=1024)
        state = create_interview_state_with_rounds()

        result = await scorer.score(state)

        assert result.is_ok
        assert mock_adapter.complete.call_count == 2
        # Verify tokens were doubled on truncation retry
        first_config = mock_adapter.complete.call_args_list[0][0][1]
        second_config = mock_adapter.complete.call_args_list[1][0][1]
        assert first_config.max_tokens == 1024
        assert second_config.max_tokens == 2048

    async def test_score_retries_without_token_increase_on_format_error(self) -> None:
        """score does not increase tokens when format error without truncation."""
        mock_adapter = MagicMock()
        # First call has format error but not truncated, second succeeds
        mock_adapter.complete = AsyncMock(
            side_effect=[
                Result.ok(
                    create_mock_completion_response(
                        content="GOAL_CLARITY_SCORE: 0.8\nGOAL_CLARITY_JUSTIFICATION: Good",
                        finish_reason="stop",  # Not truncated
                    )
                ),
                Result.ok(
                    create_mock_completion_response(
                        content=create_valid_scoring_response()
                    )
                ),
            ]
        )

        scorer = AmbiguityScorer(llm_adapter=mock_adapter, initial_max_tokens=1024)
        state = create_interview_state_with_rounds()

        result = await scorer.score(state)

        assert result.is_ok
        assert mock_adapter.complete.call_count == 2
        # Verify tokens were NOT increased (same value)
        first_config = mock_adapter.complete.call_args_list[0][0][1]
        second_config = mock_adapter.complete.call_args_list[1][0][1]
        assert first_config.max_tokens == 1024
        assert second_config.max_tokens == 1024  # Same, not doubled

    async def test_score_uses_reproducible_temperature(self) -> None:
        """score uses low temperature for reproducibility."""
        mock_adapter = MagicMock()
        mock_adapter.complete = AsyncMock(
            return_value=Result.ok(
                create_mock_completion_response(content=create_valid_scoring_response())
            )
        )

        scorer = AmbiguityScorer(llm_adapter=mock_adapter)
        state = create_interview_state_with_rounds()

        await scorer.score(state)

        call_args = mock_adapter.complete.call_args
        config = call_args[0][1]
        assert config.temperature == SCORING_TEMPERATURE  # 0.1

    async def test_score_includes_interview_context(self) -> None:
        """score includes interview context in prompt."""
        mock_adapter = MagicMock()
        mock_adapter.complete = AsyncMock(
            return_value=Result.ok(
                create_mock_completion_response(content=create_valid_scoring_response())
            )
        )

        scorer = AmbiguityScorer(llm_adapter=mock_adapter)
        state = InterviewState(
            interview_id="test_001",
            initial_context="Build a special CLI tool",
        )
        state.rounds.append(
            InterviewRound(
                round_number=1,
                question="What features?",
                user_response="Task tracking and filtering",
            )
        )

        await scorer.score(state)

        call_args = mock_adapter.complete.call_args
        messages = call_args[0][0]
        user_message = messages[1]

        assert "Build a special CLI tool" in user_message.content
        assert "What features?" in user_message.content
        assert "Task tracking and filtering" in user_message.content

    async def test_score_breakdown_contains_justifications(self) -> None:
        """score result breakdown contains justification text."""
        mock_adapter = MagicMock()
        mock_adapter.complete = AsyncMock(
            return_value=Result.ok(
                create_mock_completion_response(
                    content=create_valid_scoring_response(
                        goal_justification="Goal is crystal clear.",
                        constraint_justification="Constraints well-defined.",
                        success_justification="Success metrics specified.",
                    )
                )
            )
        )

        scorer = AmbiguityScorer(llm_adapter=mock_adapter)
        state = create_interview_state_with_rounds()

        result = await scorer.score(state)

        assert result.is_ok
        breakdown = result.value.breakdown

        assert breakdown.goal_clarity.justification == "Goal is crystal clear."
        assert breakdown.constraint_clarity.justification == "Constraints well-defined."
        assert breakdown.success_criteria_clarity.justification == "Success metrics specified."


class TestAmbiguityScorerBuildInterviewContext:
    """Test AmbiguityScorer._build_interview_context method."""

    def test_context_with_no_rounds(self) -> None:
        """_build_interview_context handles state with no rounds."""
        mock_adapter = MagicMock()
        scorer = AmbiguityScorer(llm_adapter=mock_adapter)

        state = InterviewState(
            interview_id="test_001",
            initial_context="Build something",
        )

        context = scorer._build_interview_context(state)

        assert "Build something" in context
        assert "Q:" not in context

    def test_context_with_rounds(self) -> None:
        """_build_interview_context includes all rounds."""
        mock_adapter = MagicMock()
        scorer = AmbiguityScorer(llm_adapter=mock_adapter)

        state = create_interview_state_with_rounds(rounds=2)

        context = scorer._build_interview_context(state)

        assert state.initial_context in context
        assert "Q: Question 1?" in context
        assert "A: Answer 1" in context
        assert "Q: Question 2?" in context
        assert "A: Answer 2" in context


class TestAmbiguityScorerParseResponse:
    """Test AmbiguityScorer._parse_scoring_response method."""

    def test_parse_valid_response(self) -> None:
        """_parse_scoring_response parses valid LLM response."""
        mock_adapter = MagicMock()
        scorer = AmbiguityScorer(llm_adapter=mock_adapter)

        response = create_valid_scoring_response(
            goal_score=0.85,
            goal_justification="Good goal.",
            constraint_score=0.75,
            constraint_justification="Good constraints.",
            success_score=0.65,
            success_justification="Good success criteria.",
        )

        breakdown = scorer._parse_scoring_response(response)

        assert breakdown.goal_clarity.clarity_score == 0.85
        assert breakdown.goal_clarity.justification == "Good goal."
        assert breakdown.constraint_clarity.clarity_score == 0.75
        assert breakdown.constraint_clarity.justification == "Good constraints."
        assert breakdown.success_criteria_clarity.clarity_score == 0.65
        assert breakdown.success_criteria_clarity.justification == "Good success criteria."

    def test_parse_response_clamps_scores(self) -> None:
        """_parse_scoring_response clamps scores to valid range."""
        mock_adapter = MagicMock()
        scorer = AmbiguityScorer(llm_adapter=mock_adapter)

        response = create_valid_scoring_response(
            goal_score=1.5,  # Above max
            constraint_score=-0.3,  # Below min
            success_score=0.5,
        )

        breakdown = scorer._parse_scoring_response(response)

        assert breakdown.goal_clarity.clarity_score == 1.0  # Clamped to max
        assert breakdown.constraint_clarity.clarity_score == 0.0  # Clamped to min
        assert breakdown.success_criteria_clarity.clarity_score == 0.5

    def test_parse_response_missing_field(self) -> None:
        """_parse_scoring_response raises error for missing fields."""
        mock_adapter = MagicMock()
        scorer = AmbiguityScorer(llm_adapter=mock_adapter)

        # JSON with missing field
        response = '{"goal_clarity_score": 0.9, "goal_clarity_justification": "Good goal."}'

        with pytest.raises(ValueError, match="Missing required field"):
            scorer._parse_scoring_response(response)

    def test_parse_response_invalid_json(self) -> None:
        """_parse_scoring_response raises error for invalid JSON."""
        mock_adapter = MagicMock()
        scorer = AmbiguityScorer(llm_adapter=mock_adapter)

        response = "This is not valid JSON at all"

        with pytest.raises(ValueError, match="Invalid JSON response"):
            scorer._parse_scoring_response(response)

    def test_parse_response_with_markdown_code_block(self) -> None:
        """_parse_scoring_response handles JSON in markdown code block."""
        mock_adapter = MagicMock()
        scorer = AmbiguityScorer(llm_adapter=mock_adapter)

        response = """Here is the analysis:
```json
{"goal_clarity_score": 0.85, "goal_clarity_justification": "Clear goal with details.", "constraint_clarity_score": 0.75, "constraint_clarity_justification": "Good constraints.", "success_criteria_clarity_score": 0.65, "success_criteria_clarity_justification": "Clear criteria."}
```
"""

        breakdown = scorer._parse_scoring_response(response)

        assert breakdown.goal_clarity.clarity_score == 0.85
        assert breakdown.goal_clarity.justification == "Clear goal with details."


class TestAmbiguityScorerCalculateOverall:
    """Test AmbiguityScorer._calculate_overall_score method."""

    def test_calculate_overall_perfectly_clear(self) -> None:
        """_calculate_overall_score returns 0.0 for perfect clarity."""
        mock_adapter = MagicMock()
        scorer = AmbiguityScorer(llm_adapter=mock_adapter)

        breakdown = ScoreBreakdown(
            goal_clarity=ComponentScore(
                name="Goal", clarity_score=1.0, weight=0.4, justification="Perfect"
            ),
            constraint_clarity=ComponentScore(
                name="Constraint", clarity_score=1.0, weight=0.3, justification="Perfect"
            ),
            success_criteria_clarity=ComponentScore(
                name="Success", clarity_score=1.0, weight=0.3, justification="Perfect"
            ),
        )

        overall = scorer._calculate_overall_score(breakdown)

        assert overall == 0.0

    def test_calculate_overall_completely_ambiguous(self) -> None:
        """_calculate_overall_score returns 1.0 for complete ambiguity."""
        mock_adapter = MagicMock()
        scorer = AmbiguityScorer(llm_adapter=mock_adapter)

        breakdown = ScoreBreakdown(
            goal_clarity=ComponentScore(
                name="Goal", clarity_score=0.0, weight=0.4, justification="None"
            ),
            constraint_clarity=ComponentScore(
                name="Constraint", clarity_score=0.0, weight=0.3, justification="None"
            ),
            success_criteria_clarity=ComponentScore(
                name="Success", clarity_score=0.0, weight=0.3, justification="None"
            ),
        )

        overall = scorer._calculate_overall_score(breakdown)

        assert overall == 1.0

    def test_calculate_overall_mixed_scores(self) -> None:
        """_calculate_overall_score correctly weights mixed scores."""
        mock_adapter = MagicMock()
        scorer = AmbiguityScorer(llm_adapter=mock_adapter)

        # Clarity = 0.8 * 0.4 + 0.6 * 0.3 + 0.4 * 0.3 = 0.32 + 0.18 + 0.12 = 0.62
        # Ambiguity = 1 - 0.62 = 0.38
        breakdown = ScoreBreakdown(
            goal_clarity=ComponentScore(
                name="Goal", clarity_score=0.8, weight=0.4, justification="Good"
            ),
            constraint_clarity=ComponentScore(
                name="Constraint", clarity_score=0.6, weight=0.3, justification="OK"
            ),
            success_criteria_clarity=ComponentScore(
                name="Success", clarity_score=0.4, weight=0.3, justification="Weak"
            ),
        )

        overall = scorer._calculate_overall_score(breakdown)

        assert abs(overall - 0.38) < 0.01


class TestAmbiguityScorerGenerateClarificationQuestions:
    """Test AmbiguityScorer.generate_clarification_questions method."""

    def test_generate_questions_all_clear(self) -> None:
        """generate_clarification_questions returns empty for clear requirements."""
        mock_adapter = MagicMock()
        scorer = AmbiguityScorer(llm_adapter=mock_adapter)

        breakdown = ScoreBreakdown(
            goal_clarity=ComponentScore(
                name="Goal", clarity_score=0.9, weight=0.4, justification="Clear"
            ),
            constraint_clarity=ComponentScore(
                name="Constraint", clarity_score=0.85, weight=0.3, justification="Clear"
            ),
            success_criteria_clarity=ComponentScore(
                name="Success", clarity_score=0.8, weight=0.3, justification="Clear"
            ),
        )

        questions = scorer.generate_clarification_questions(breakdown)

        assert len(questions) == 0

    def test_generate_questions_unclear_goal(self) -> None:
        """generate_clarification_questions includes goal questions for low goal score."""
        mock_adapter = MagicMock()
        scorer = AmbiguityScorer(llm_adapter=mock_adapter)

        breakdown = ScoreBreakdown(
            goal_clarity=ComponentScore(
                name="Goal", clarity_score=0.5, weight=0.4, justification="Unclear"
            ),
            constraint_clarity=ComponentScore(
                name="Constraint", clarity_score=0.9, weight=0.3, justification="Clear"
            ),
            success_criteria_clarity=ComponentScore(
                name="Success", clarity_score=0.9, weight=0.3, justification="Clear"
            ),
        )

        questions = scorer.generate_clarification_questions(breakdown)

        assert len(questions) == 2
        assert any("problem" in q.lower() for q in questions)
        assert any("deliverable" in q.lower() or "output" in q.lower() for q in questions)

    def test_generate_questions_unclear_constraints(self) -> None:
        """generate_clarification_questions includes constraint questions for low constraint score."""
        mock_adapter = MagicMock()
        scorer = AmbiguityScorer(llm_adapter=mock_adapter)

        breakdown = ScoreBreakdown(
            goal_clarity=ComponentScore(
                name="Goal", clarity_score=0.9, weight=0.4, justification="Clear"
            ),
            constraint_clarity=ComponentScore(
                name="Constraint", clarity_score=0.5, weight=0.3, justification="Unclear"
            ),
            success_criteria_clarity=ComponentScore(
                name="Success", clarity_score=0.9, weight=0.3, justification="Clear"
            ),
        )

        questions = scorer.generate_clarification_questions(breakdown)

        assert len(questions) == 2
        assert any("technical" in q.lower() or "constraint" in q.lower() for q in questions)
        assert any("exclude" in q.lower() or "scope" in q.lower() for q in questions)

    def test_generate_questions_unclear_success(self) -> None:
        """generate_clarification_questions includes success questions for low success score."""
        mock_adapter = MagicMock()
        scorer = AmbiguityScorer(llm_adapter=mock_adapter)

        breakdown = ScoreBreakdown(
            goal_clarity=ComponentScore(
                name="Goal", clarity_score=0.9, weight=0.4, justification="Clear"
            ),
            constraint_clarity=ComponentScore(
                name="Constraint", clarity_score=0.9, weight=0.3, justification="Clear"
            ),
            success_criteria_clarity=ComponentScore(
                name="Success", clarity_score=0.5, weight=0.3, justification="Unclear"
            ),
        )

        questions = scorer.generate_clarification_questions(breakdown)

        assert len(questions) == 2
        assert any("complete" in q.lower() or "success" in q.lower() for q in questions)
        assert any("feature" in q.lower() or "essential" in q.lower() for q in questions)

    def test_generate_questions_all_unclear(self) -> None:
        """generate_clarification_questions includes all questions when everything unclear."""
        mock_adapter = MagicMock()
        scorer = AmbiguityScorer(llm_adapter=mock_adapter)

        breakdown = ScoreBreakdown(
            goal_clarity=ComponentScore(
                name="Goal", clarity_score=0.3, weight=0.4, justification="Vague"
            ),
            constraint_clarity=ComponentScore(
                name="Constraint", clarity_score=0.2, weight=0.3, justification="Missing"
            ),
            success_criteria_clarity=ComponentScore(
                name="Success", clarity_score=0.1, weight=0.3, justification="None"
            ),
        )

        questions = scorer.generate_clarification_questions(breakdown)

        assert len(questions) == 6


class TestIsReadyForSeedHelper:
    """Test is_ready_for_seed helper function."""

    def test_is_ready_for_seed_true(self) -> None:
        """is_ready_for_seed returns True for low ambiguity."""
        breakdown = ScoreBreakdown(
            goal_clarity=ComponentScore(
                name="Goal", clarity_score=0.9, weight=0.4, justification="Clear"
            ),
            constraint_clarity=ComponentScore(
                name="Constraint", clarity_score=0.9, weight=0.3, justification="Clear"
            ),
            success_criteria_clarity=ComponentScore(
                name="Success", clarity_score=0.9, weight=0.3, justification="Clear"
            ),
        )
        score = AmbiguityScore(overall_score=0.1, breakdown=breakdown)

        assert is_ready_for_seed(score) is True

    def test_is_ready_for_seed_false(self) -> None:
        """is_ready_for_seed returns False for high ambiguity."""
        breakdown = ScoreBreakdown(
            goal_clarity=ComponentScore(
                name="Goal", clarity_score=0.5, weight=0.4, justification="Unclear"
            ),
            constraint_clarity=ComponentScore(
                name="Constraint", clarity_score=0.5, weight=0.3, justification="Unclear"
            ),
            success_criteria_clarity=ComponentScore(
                name="Success", clarity_score=0.5, weight=0.3, justification="Unclear"
            ),
        )
        score = AmbiguityScore(overall_score=0.5, breakdown=breakdown)

        assert is_ready_for_seed(score) is False


class TestFormatScoreDisplay:
    """Test format_score_display helper function."""

    def test_format_score_display_ready(self) -> None:
        """format_score_display shows 'Yes' when ready for seed."""
        breakdown = ScoreBreakdown(
            goal_clarity=ComponentScore(
                name="Goal Clarity",
                clarity_score=0.9,
                weight=0.4,
                justification="Well-defined goal.",
            ),
            constraint_clarity=ComponentScore(
                name="Constraint Clarity",
                clarity_score=0.85,
                weight=0.3,
                justification="Clear constraints.",
            ),
            success_criteria_clarity=ComponentScore(
                name="Success Criteria Clarity",
                clarity_score=0.8,
                weight=0.3,
                justification="Measurable criteria.",
            ),
        )
        score = AmbiguityScore(overall_score=0.15, breakdown=breakdown)

        output = format_score_display(score)

        assert "0.15" in output
        assert "Ready for Seed: Yes" in output
        assert "Goal Clarity" in output
        assert "90% clear" in output
        assert "Well-defined goal." in output

    def test_format_score_display_not_ready(self) -> None:
        """format_score_display shows 'No' when not ready for seed."""
        breakdown = ScoreBreakdown(
            goal_clarity=ComponentScore(
                name="Goal Clarity",
                clarity_score=0.5,
                weight=0.4,
                justification="Vague goal.",
            ),
            constraint_clarity=ComponentScore(
                name="Constraint Clarity",
                clarity_score=0.5,
                weight=0.3,
                justification="Missing constraints.",
            ),
            success_criteria_clarity=ComponentScore(
                name="Success Criteria Clarity",
                clarity_score=0.5,
                weight=0.3,
                justification="No criteria specified.",
            ),
        )
        score = AmbiguityScore(overall_score=0.5, breakdown=breakdown)

        output = format_score_display(score)

        assert "0.50" in output
        assert "Ready for Seed: No" in output

    def test_format_score_display_includes_all_components(self) -> None:
        """format_score_display includes all component information."""
        breakdown = ScoreBreakdown(
            goal_clarity=ComponentScore(
                name="Goal Clarity",
                clarity_score=0.8,
                weight=0.4,
                justification="Justification 1",
            ),
            constraint_clarity=ComponentScore(
                name="Constraint Clarity",
                clarity_score=0.7,
                weight=0.3,
                justification="Justification 2",
            ),
            success_criteria_clarity=ComponentScore(
                name="Success Criteria Clarity",
                clarity_score=0.6,
                weight=0.3,
                justification="Justification 3",
            ),
        )
        score = AmbiguityScore(overall_score=0.27, breakdown=breakdown)

        output = format_score_display(score)

        assert "Goal Clarity (weight: 40%)" in output
        assert "80% clear" in output
        assert "Justification: Justification 1" in output

        assert "Constraint Clarity (weight: 30%)" in output
        assert "70% clear" in output
        assert "Justification: Justification 2" in output

        assert "Success Criteria Clarity (weight: 30%)" in output
        assert "60% clear" in output
        assert "Justification: Justification 3" in output


class TestAmbiguityThreshold:
    """Test ambiguity threshold constant."""

    def test_threshold_value(self) -> None:
        """AMBIGUITY_THRESHOLD is 0.2 as per NFR6."""
        assert AMBIGUITY_THRESHOLD == 0.2


class TestWeightConstants:
    """Test weight constants sum to 1.0."""

    def test_weights_sum_to_one(self) -> None:
        """Component weights sum to 1.0."""
        total = (
            GOAL_CLARITY_WEIGHT
            + CONSTRAINT_CLARITY_WEIGHT
            + SUCCESS_CRITERIA_CLARITY_WEIGHT
        )
        assert abs(total - 1.0) < 0.001

    def test_goal_weight(self) -> None:
        """Goal clarity weight is 40%."""
        assert GOAL_CLARITY_WEIGHT == 0.4

    def test_constraint_weight(self) -> None:
        """Constraint clarity weight is 30%."""
        assert CONSTRAINT_CLARITY_WEIGHT == 0.3

    def test_success_weight(self) -> None:
        """Success criteria clarity weight is 30%."""
        assert SUCCESS_CRITERIA_CLARITY_WEIGHT == 0.3


class TestScoringTemperature:
    """Test scoring temperature for reproducibility."""

    def test_temperature_is_low(self) -> None:
        """SCORING_TEMPERATURE is 0.1 for reproducibility."""
        assert SCORING_TEMPERATURE == 0.1


class TestMaxTokenLimit:
    """Test MAX_TOKEN_LIMIT constant."""

    def test_max_token_limit_value(self) -> None:
        """MAX_TOKEN_LIMIT is None (no limit, rely on model's context window)."""
        assert MAX_TOKEN_LIMIT is None

    async def test_token_growth_unbounded_when_no_limit(self) -> None:
        """Token growth doubles without cap when MAX_TOKEN_LIMIT is None."""
        mock_adapter = MagicMock()
        # All calls fail with truncation - tokens should keep doubling
        mock_adapter.complete = AsyncMock(
            return_value=Result.ok(
                create_mock_completion_response(
                    content="GOAL_CLARITY_SCORE: 0.8\nGOAL_CLARITY_JUSTIFICATION: Good",
                    finish_reason="length",  # Truncated
                )
            )
        )

        # Start with 4096, should try 4096 -> 8192 -> 16384 (no cap)
        scorer = AmbiguityScorer(
            llm_adapter=mock_adapter, initial_max_tokens=4096, max_retries=3
        )
        state = create_interview_state_with_rounds()

        result = await scorer.score(state)

        assert result.is_err  # All retries fail
        assert mock_adapter.complete.call_count == 3
        # Verify token progression: 4096 -> 8192 -> 16384 (no cap)
        configs = [call[0][1] for call in mock_adapter.complete.call_args_list]
        assert configs[0].max_tokens == 4096
        assert configs[1].max_tokens == 8192  # Doubled
        assert configs[2].max_tokens == 16384  # Doubled again, no cap
