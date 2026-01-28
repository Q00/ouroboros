"""Ambiguity scoring module for requirement clarity assessment.

This module implements ambiguity measurement for interview states, determining
when requirements are clear enough (score <= 0.2) to proceed with Seed generation.

The scoring algorithm evaluates three key components:
- Goal Clarity (40%): How well the goal statement is defined
- Constraint Clarity (30%): How clearly constraints are specified
- Success Criteria Clarity (30%): How measurable the success criteria are
"""

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field
import structlog

from ouroboros.bigbang.interview import InterviewState
from ouroboros.core.errors import ProviderError
from ouroboros.core.types import Result
from ouroboros.providers.base import CompletionConfig, Message, MessageRole
from ouroboros.providers.litellm_adapter import LiteLLMAdapter

log = structlog.get_logger()

# Threshold for allowing Seed generation (NFR6)
AMBIGUITY_THRESHOLD = 0.2

# Weights for score components
GOAL_CLARITY_WEIGHT = 0.40
CONSTRAINT_CLARITY_WEIGHT = 0.30
SUCCESS_CRITERIA_CLARITY_WEIGHT = 0.30

DEFAULT_MODEL = "openrouter/google/gemini-2.0-flash-001"

# Temperature for reproducible scoring
SCORING_TEMPERATURE = 0.1

# Maximum token limit to prevent cost explosion
MAX_TOKEN_LIMIT = 8192


class ComponentScore(BaseModel):
    """Individual component score with justification.

    Attributes:
        name: Name of the component being scored.
        clarity_score: Clarity score between 0.0 (unclear) and 1.0 (perfectly clear).
        weight: Weight of this component in the overall score.
        justification: Explanation of why this score was given.
    """

    name: str
    clarity_score: float = Field(ge=0.0, le=1.0)
    weight: float = Field(ge=0.0, le=1.0)
    justification: str


class ScoreBreakdown(BaseModel):
    """Detailed breakdown of ambiguity score with justifications.

    Attributes:
        goal_clarity: Score for goal statement clarity.
        constraint_clarity: Score for constraint specification clarity.
        success_criteria_clarity: Score for success criteria measurability.
    """

    goal_clarity: ComponentScore
    constraint_clarity: ComponentScore
    success_criteria_clarity: ComponentScore

    @property
    def components(self) -> list[ComponentScore]:
        """Return all component scores as a list."""
        return [
            self.goal_clarity,
            self.constraint_clarity,
            self.success_criteria_clarity,
        ]


@dataclass(frozen=True, slots=True)
class AmbiguityScore:
    """Result of ambiguity scoring for an interview state.

    Attributes:
        overall_score: Normalized ambiguity score (0.0 = clear, 1.0 = ambiguous).
        breakdown: Detailed breakdown of component scores.
        is_ready_for_seed: Whether score allows Seed generation (score <= 0.2).
    """

    overall_score: float
    breakdown: ScoreBreakdown

    @property
    def is_ready_for_seed(self) -> bool:
        """Check if ambiguity score allows Seed generation.

        Returns:
            True if overall_score <= AMBIGUITY_THRESHOLD (0.2).
        """
        return self.overall_score <= AMBIGUITY_THRESHOLD


@dataclass
class AmbiguityScorer:
    """Scorer for calculating ambiguity of interview requirements.

    Uses LLM to evaluate clarity of goals, constraints, and success criteria
    from interview conversation, producing reproducible scores.

    Uses adaptive token allocation: starts with `initial_max_tokens` and
    doubles on truncation up to `MAX_TOKEN_LIMIT`. Retries up to `max_retries`
    times on both provider errors and parse failures.

    Attributes:
        llm_adapter: The LLM adapter for completions.
        model: Model identifier to use.
        temperature: Temperature for reproducibility (default 0.1).
        initial_max_tokens: Starting token limit (default 2048).
        max_retries: Maximum retry attempts (default 3).

    Example:
        scorer = AmbiguityScorer(llm_adapter=LiteLLMAdapter())

        result = await scorer.score(interview_state)
        if result.is_ok:
            ambiguity = result.value
            if ambiguity.is_ready_for_seed:
                # Proceed with Seed generation
                ...
            else:
                # Generate additional questions
                questions = scorer.generate_clarification_questions(ambiguity.breakdown)
    """

    llm_adapter: LiteLLMAdapter
    model: str = DEFAULT_MODEL
    temperature: float = SCORING_TEMPERATURE
    initial_max_tokens: int = 2048
    max_retries: int = 3

    async def score(
        self, state: InterviewState
    ) -> Result[AmbiguityScore, ProviderError]:
        """Calculate ambiguity score for interview state.

        Evaluates the interview conversation to determine clarity of:
        - Goal statement (40% weight)
        - Constraints (30% weight)
        - Success criteria (30% weight)

        Uses adaptive token allocation: starts with initial_max_tokens and
        doubles on parse failure, up to max_retries attempts.

        Args:
            state: The interview state to score.

        Returns:
            Result containing AmbiguityScore or ProviderError.
        """
        log.debug(
            "ambiguity.scoring.started",
            interview_id=state.interview_id,
            rounds=len(state.rounds),
        )

        # Build the context from interview
        context = self._build_interview_context(state)

        # Create scoring prompt
        system_prompt = self._build_scoring_system_prompt()
        user_prompt = self._build_scoring_user_prompt(context)

        messages = [
            Message(role=MessageRole.SYSTEM, content=system_prompt),
            Message(role=MessageRole.USER, content=user_prompt),
        ]

        current_max_tokens = self.initial_max_tokens
        last_error: Exception | ProviderError | None = None
        last_response: str = ""

        for attempt in range(self.max_retries):
            config = CompletionConfig(
                model=self.model,
                temperature=self.temperature,
                max_tokens=current_max_tokens,
            )

            result = await self.llm_adapter.complete(messages, config)

            # Fix #3: Retry on provider errors (rate limits, transient failures)
            if result.is_err:
                last_error = result.error
                log.warning(
                    "ambiguity.scoring.provider_error_retrying",
                    interview_id=state.interview_id,
                    error=str(result.error),
                    attempt=attempt + 1,
                    max_retries=self.max_retries,
                )
                continue

            # Parse the LLM response into scores
            try:
                breakdown = self._parse_scoring_response(result.value.content)
                overall_score = self._calculate_overall_score(breakdown)

                ambiguity_score = AmbiguityScore(
                    overall_score=overall_score,
                    breakdown=breakdown,
                )

                log.info(
                    "ambiguity.scoring.completed",
                    interview_id=state.interview_id,
                    overall_score=overall_score,
                    is_ready_for_seed=ambiguity_score.is_ready_for_seed,
                    goal_clarity=breakdown.goal_clarity.clarity_score,
                    constraint_clarity=breakdown.constraint_clarity.clarity_score,
                    success_criteria_clarity=breakdown.success_criteria_clarity.clarity_score,
                    tokens_used=current_max_tokens,
                    attempt=attempt + 1,
                )

                return Result.ok(ambiguity_score)

            except (ValueError, KeyError) as e:
                last_error = e
                last_response = result.value.content

                # Fix #2: Only increase tokens if response was truncated
                is_truncated = result.value.finish_reason == "length"

                if is_truncated:
                    # Fix #1: Cap token growth with MAX_TOKEN_LIMIT
                    next_tokens = min(current_max_tokens * 2, MAX_TOKEN_LIMIT)
                    log.warning(
                        "ambiguity.scoring.truncated_retrying",
                        interview_id=state.interview_id,
                        error=str(e),
                        attempt=attempt + 1,
                        current_tokens=current_max_tokens,
                        next_tokens=next_tokens,
                    )
                    current_max_tokens = next_tokens
                else:
                    # Format error without truncation - retry with same tokens
                    log.warning(
                        "ambiguity.scoring.format_error_retrying",
                        interview_id=state.interview_id,
                        error=str(e),
                        attempt=attempt + 1,
                        finish_reason=result.value.finish_reason,
                    )

        # All retries exhausted
        log.warning(
            "ambiguity.scoring.failed",
            interview_id=state.interview_id,
            error=str(last_error),
            response=last_response[:500] if last_response else None,
            max_retries_exhausted=True,
        )
        return Result.err(
            ProviderError(
                f"Failed to parse scoring response after {self.max_retries} attempts: {last_error}",
                details={"response_preview": last_response[:200] if last_response else None},
            )
        )

    def _build_interview_context(self, state: InterviewState) -> str:
        """Build context string from interview state.

        Args:
            state: The interview state.

        Returns:
            Formatted context string.
        """
        parts = [f"Initial Context: {state.initial_context}"]

        for round_data in state.rounds:
            parts.append(f"\nQ: {round_data.question}")
            if round_data.user_response:
                parts.append(f"A: {round_data.user_response}")

        return "\n".join(parts)

    def _build_scoring_system_prompt(self) -> str:
        """Build system prompt for scoring.

        Returns:
            System prompt string.
        """
        return """You are an expert requirements analyst evaluating the clarity of software requirements.

Your task is to assess how clear and unambiguous the requirements are based on an interview conversation.

Evaluate three components:
1. Goal Clarity (40% weight): Is the goal statement specific and well-defined?
   - Clear: "Build a CLI tool for task management with project grouping"
   - Unclear: "Build something useful for productivity"

2. Constraint Clarity (30% weight): Are constraints and limitations specified?
   - Clear: "Must use Python 3.14+, no external database dependencies"
   - Unclear: No mention of technical constraints or limitations

3. Success Criteria Clarity (30% weight): Are success criteria measurable?
   - Clear: "Tasks can be created, edited, deleted; supports filtering by status"
   - Unclear: "The tool should be easy to use"

For each component, provide:
- A clarity score between 0.0 (completely unclear) and 1.0 (perfectly clear)
- A brief justification (1-2 sentences max) explaining the score

IMPORTANT: You MUST provide ALL six fields below. Keep justifications concise.

Respond in this exact format:
GOAL_CLARITY_SCORE: <score>
GOAL_CLARITY_JUSTIFICATION: <justification in 1-2 sentences>
CONSTRAINT_CLARITY_SCORE: <score>
CONSTRAINT_CLARITY_JUSTIFICATION: <justification in 1-2 sentences>
SUCCESS_CRITERIA_CLARITY_SCORE: <score>
SUCCESS_CRITERIA_CLARITY_JUSTIFICATION: <justification in 1-2 sentences>

Be strict in your evaluation. Scores above 0.8 require very specific, measurable requirements."""

    def _build_scoring_user_prompt(self, context: str) -> str:
        """Build user prompt with interview context.

        Args:
            context: Formatted interview context.

        Returns:
            User prompt string.
        """
        return f"""Please evaluate the clarity of the following requirements conversation:

---
{context}
---

Analyze each component and provide scores with justifications."""

    def _parse_scoring_response(self, response: str) -> ScoreBreakdown:
        """Parse LLM response into ScoreBreakdown.

        Args:
            response: Raw LLM response text.

        Returns:
            Parsed ScoreBreakdown.

        Raises:
            ValueError: If response cannot be parsed.
        """
        lines = response.strip().split("\n")
        scores: dict[str, Any] = {}

        for line in lines:
            line = line.strip()
            if not line:
                continue

            for prefix in [
                "GOAL_CLARITY_SCORE:",
                "GOAL_CLARITY_JUSTIFICATION:",
                "CONSTRAINT_CLARITY_SCORE:",
                "CONSTRAINT_CLARITY_JUSTIFICATION:",
                "SUCCESS_CRITERIA_CLARITY_SCORE:",
                "SUCCESS_CRITERIA_CLARITY_JUSTIFICATION:",
            ]:
                if line.startswith(prefix):
                    key = prefix[:-1].lower()  # Remove colon and lowercase
                    value = line[len(prefix) :].strip()
                    scores[key] = value
                    break

        # Validate all required fields are present
        required_fields = [
            "goal_clarity_score",
            "goal_clarity_justification",
            "constraint_clarity_score",
            "constraint_clarity_justification",
            "success_criteria_clarity_score",
            "success_criteria_clarity_justification",
        ]

        for field_name in required_fields:
            if field_name not in scores:
                raise ValueError(f"Missing required field: {field_name}")

        # Parse scores to float
        def parse_score(value: str) -> float:
            try:
                score = float(value)
                return max(0.0, min(1.0, score))  # Clamp to [0, 1]
            except ValueError as e:
                raise ValueError(f"Invalid score value: {value}") from e

        return ScoreBreakdown(
            goal_clarity=ComponentScore(
                name="Goal Clarity",
                clarity_score=parse_score(scores["goal_clarity_score"]),
                weight=GOAL_CLARITY_WEIGHT,
                justification=scores["goal_clarity_justification"],
            ),
            constraint_clarity=ComponentScore(
                name="Constraint Clarity",
                clarity_score=parse_score(scores["constraint_clarity_score"]),
                weight=CONSTRAINT_CLARITY_WEIGHT,
                justification=scores["constraint_clarity_justification"],
            ),
            success_criteria_clarity=ComponentScore(
                name="Success Criteria Clarity",
                clarity_score=parse_score(scores["success_criteria_clarity_score"]),
                weight=SUCCESS_CRITERIA_CLARITY_WEIGHT,
                justification=scores["success_criteria_clarity_justification"],
            ),
        )

    def _calculate_overall_score(self, breakdown: ScoreBreakdown) -> float:
        """Calculate overall ambiguity score from component clarity scores.

        Ambiguity = 1 - (weighted average of clarity scores)

        Args:
            breakdown: Score breakdown with component clarity scores.

        Returns:
            Overall ambiguity score between 0.0 and 1.0.
        """
        weighted_clarity = sum(
            component.clarity_score * component.weight
            for component in breakdown.components
        )

        # Ambiguity = 1 - clarity
        return round(1.0 - weighted_clarity, 4)

    def generate_clarification_questions(
        self, breakdown: ScoreBreakdown
    ) -> list[str]:
        """Generate clarification questions based on score breakdown.

        Identifies which components need clarification and suggests questions.

        Args:
            breakdown: Score breakdown with component scores.

        Returns:
            List of clarification questions for low-scoring components.
        """
        questions: list[str] = []

        # Threshold for "needs clarification"
        clarification_threshold = 0.8

        if breakdown.goal_clarity.clarity_score < clarification_threshold:
            questions.append(
                "Can you describe the specific problem this solution should solve?"
            )
            questions.append(
                "What is the primary deliverable or output you expect?"
            )

        if breakdown.constraint_clarity.clarity_score < clarification_threshold:
            questions.append(
                "Are there any technical constraints or limitations to consider?"
            )
            questions.append(
                "What should definitely be excluded from the scope?"
            )

        if breakdown.success_criteria_clarity.clarity_score < clarification_threshold:
            questions.append(
                "How will you know when this is successfully completed?"
            )
            questions.append(
                "What specific features or behaviors are essential?"
            )

        return questions


def is_ready_for_seed(score: AmbiguityScore) -> bool:
    """Helper function to check if score allows Seed generation.

    Args:
        score: The ambiguity score to check.

    Returns:
        True if score <= AMBIGUITY_THRESHOLD (0.2), allowing Seed generation.
    """
    return score.is_ready_for_seed


def format_score_display(score: AmbiguityScore) -> str:
    """Format ambiguity score for display after interview round.

    Args:
        score: The ambiguity score to format.

    Returns:
        Formatted string for display.
    """
    lines = [
        f"Ambiguity Score: {score.overall_score:.2f}",
        f"Ready for Seed: {'Yes' if score.is_ready_for_seed else 'No'}",
        "",
        "Component Breakdown:",
    ]

    for component in score.breakdown.components:
        clarity_percent = component.clarity_score * 100
        weight_percent = component.weight * 100
        lines.append(
            f"  {component.name} (weight: {weight_percent:.0f}%): "
            f"{clarity_percent:.0f}% clear"
        )
        lines.append(f"    Justification: {component.justification}")

    return "\n".join(lines)
