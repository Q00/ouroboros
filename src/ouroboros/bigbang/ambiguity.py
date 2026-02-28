"""Ambiguity scoring module for requirement clarity assessment.

This module implements ambiguity measurement for interview states, determining
when requirements are clear enough (score <= 0.2) to proceed with Seed generation.

The scoring algorithm evaluates three key components:
- Goal Clarity (40%): How well the goal statement is defined
- Constraint Clarity (30%): How clearly constraints are specified
- Success Criteria Clarity (30%): How measurable the success criteria are
"""

from dataclasses import dataclass
import json
import re
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

# Weights for greenfield score components (3 dimensions)
GOAL_CLARITY_WEIGHT = 0.40
CONSTRAINT_CLARITY_WEIGHT = 0.30
SUCCESS_CRITERIA_CLARITY_WEIGHT = 0.30

# Weights for brownfield score components (4 dimensions)
BROWNFIELD_GOAL_CLARITY_WEIGHT = 0.35
BROWNFIELD_CONSTRAINT_CLARITY_WEIGHT = 0.25
BROWNFIELD_SUCCESS_CRITERIA_CLARITY_WEIGHT = 0.25
BROWNFIELD_CONTEXT_CLARITY_WEIGHT = 0.15

DEFAULT_MODEL = "claude-opus-4-6"

# Temperature for reproducible scoring
SCORING_TEMPERATURE = 0.1

# Maximum token limit (None = no limit, rely on model's context window)
MAX_TOKEN_LIMIT: int | None = None


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
        context_clarity: Score for codebase context clarity (brownfield only).
    """

    goal_clarity: ComponentScore
    constraint_clarity: ComponentScore
    success_criteria_clarity: ComponentScore
    context_clarity: ComponentScore | None = None

    @property
    def components(self) -> list[ComponentScore]:
        """Return all component scores as a list."""
        result = [
            self.goal_clarity,
            self.constraint_clarity,
            self.success_criteria_clarity,
        ]
        if self.context_clarity is not None:
            result.append(self.context_clarity)
        return result


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
    doubles on truncation up to `MAX_TOKEN_LIMIT`. Retries until success
    by default (unlimited), or up to `max_retries` if specified.

    Attributes:
        llm_adapter: The LLM adapter for completions.
        model: Model identifier to use.
        temperature: Temperature for reproducibility (default 0.1).
        initial_max_tokens: Starting token limit (default 2048).
        max_retries: Maximum retry attempts, or None for unlimited (default).

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
    max_retries: int | None = 10  # Default to 10 retries (None = unlimited)
    max_format_error_retries: int = 5  # Stop after N format errors (non-truncation)

    async def score(
        self,
        state: InterviewState,
        is_brownfield: bool = False,
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

        # Use brownfield flag from state if available
        is_brownfield = is_brownfield or getattr(state, "is_brownfield", False)

        # Build the context from interview
        context = self._build_interview_context(state)

        # Create scoring prompt
        system_prompt = self._build_scoring_system_prompt(is_brownfield=is_brownfield)
        user_prompt = self._build_scoring_user_prompt(context)

        messages = [
            Message(role=MessageRole.SYSTEM, content=system_prompt),
            Message(role=MessageRole.USER, content=user_prompt),
        ]

        current_max_tokens = self.initial_max_tokens
        last_error: Exception | ProviderError | None = None
        last_response: str = ""
        attempt = 0

        while True:
            # Check retry limit if set
            if self.max_retries is not None and attempt >= self.max_retries:
                break

            attempt += 1

            config = CompletionConfig(
                model=self.model,
                temperature=self.temperature,
                max_tokens=current_max_tokens,
            )

            result = await self.llm_adapter.complete(messages, config)

            # Retry on provider errors (rate limits, transient failures)
            if result.is_err:
                last_error = result.error
                log.warning(
                    "ambiguity.scoring.provider_error_retrying",
                    interview_id=state.interview_id,
                    error=str(result.error),
                    attempt=attempt,
                    max_retries=self.max_retries or "unlimited",
                )
                continue

            # Parse the LLM response into scores
            try:
                breakdown = self._parse_scoring_response(
                    result.value.content,
                    is_brownfield=is_brownfield,
                )
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
                    attempt=attempt,
                )

                return Result.ok(ambiguity_score)

            except (ValueError, KeyError) as e:
                last_error = e
                last_response = result.value.content

                # Only increase tokens if response was truncated
                is_truncated = result.value.finish_reason == "length"

                if is_truncated:
                    # Double tokens on truncation, capped at MAX_TOKEN_LIMIT if set
                    next_tokens = current_max_tokens * 2
                    if MAX_TOKEN_LIMIT is not None:
                        next_tokens = min(next_tokens, MAX_TOKEN_LIMIT)
                    log.warning(
                        "ambiguity.scoring.truncated_retrying",
                        interview_id=state.interview_id,
                        error=str(e),
                        attempt=attempt,
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
                        attempt=attempt,
                        finish_reason=result.value.finish_reason,
                    )

        # All retries exhausted (only reached if max_retries is set)
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

    def _build_scoring_system_prompt(self, is_brownfield: bool = False) -> str:
        """Build system prompt for scoring.

        Args:
            is_brownfield: Whether this is a brownfield project.

        Returns:
            System prompt string.
        """
        if is_brownfield:
            return """You are an expert requirements analyst. Evaluate the clarity of software requirements.

Evaluate four components:
1. Goal Clarity (35%): Is the goal specific and well-defined?
2. Constraint Clarity (25%): Are constraints and limitations specified?
3. Success Criteria Clarity (25%): Are success criteria measurable?
4. Context Clarity (15%): Is the existing codebase context clear? Are referenced codebases, patterns, and conventions well understood?

Score each from 0.0 (unclear) to 1.0 (perfectly clear). Scores above 0.8 require very specific requirements.

RESPOND ONLY WITH VALID JSON. No other text before or after.

Required JSON format:
{"goal_clarity_score": 0.0, "goal_clarity_justification": "string", "constraint_clarity_score": 0.0, "constraint_clarity_justification": "string", "success_criteria_clarity_score": 0.0, "success_criteria_clarity_justification": "string", "context_clarity_score": 0.0, "context_clarity_justification": "string"}"""

        return """You are an expert requirements analyst. Evaluate the clarity of software requirements.

Evaluate three components:
1. Goal Clarity (40%): Is the goal specific and well-defined?
2. Constraint Clarity (30%): Are constraints and limitations specified?
3. Success Criteria Clarity (30%): Are success criteria measurable?

Score each from 0.0 (unclear) to 1.0 (perfectly clear). Scores above 0.8 require very specific requirements.

RESPOND ONLY WITH VALID JSON. No other text before or after.

Required JSON format:
{"goal_clarity_score": 0.0, "goal_clarity_justification": "string", "constraint_clarity_score": 0.0, "constraint_clarity_justification": "string", "success_criteria_clarity_score": 0.0, "success_criteria_clarity_justification": "string"}"""

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

    def _parse_scoring_response(
        self,
        response: str,
        is_brownfield: bool = False,
    ) -> ScoreBreakdown:
        """Parse LLM response into ScoreBreakdown.

        Args:
            response: Raw LLM response text.
            is_brownfield: Whether to parse brownfield context_clarity dimension.

        Returns:
            Parsed ScoreBreakdown.

        Raises:
            ValueError: If response cannot be parsed.
        """
        # Extract JSON from response (handle markdown code blocks)
        text = response.strip()

        # Try to find JSON in markdown code block
        json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if json_match:
            text = json_match.group(1)
        else:
            # Try to find raw JSON object
            json_match = re.search(r"\{.*\}", text, re.DOTALL)
            if json_match:
                text = json_match.group(0)

        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON response: {e}") from e

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
            if field_name not in data:
                raise ValueError(f"Missing required field: {field_name}")

        # Parse and clamp scores
        def clamp_score(value: Any) -> float:
            score = float(value)
            return max(0.0, min(1.0, score))

        # Select weights based on project type
        if is_brownfield:
            goal_weight = BROWNFIELD_GOAL_CLARITY_WEIGHT
            constraint_weight = BROWNFIELD_CONSTRAINT_CLARITY_WEIGHT
            criteria_weight = BROWNFIELD_SUCCESS_CRITERIA_CLARITY_WEIGHT
        else:
            goal_weight = GOAL_CLARITY_WEIGHT
            constraint_weight = CONSTRAINT_CLARITY_WEIGHT
            criteria_weight = SUCCESS_CRITERIA_CLARITY_WEIGHT

        # Parse context clarity for brownfield projects
        context_clarity: ComponentScore | None = None
        if is_brownfield and "context_clarity_score" in data:
            context_clarity = ComponentScore(
                name="Context Clarity",
                clarity_score=clamp_score(data["context_clarity_score"]),
                weight=BROWNFIELD_CONTEXT_CLARITY_WEIGHT,
                justification=str(data.get("context_clarity_justification", "")),
            )

        return ScoreBreakdown(
            goal_clarity=ComponentScore(
                name="Goal Clarity",
                clarity_score=clamp_score(data["goal_clarity_score"]),
                weight=goal_weight,
                justification=str(data["goal_clarity_justification"]),
            ),
            constraint_clarity=ComponentScore(
                name="Constraint Clarity",
                clarity_score=clamp_score(data["constraint_clarity_score"]),
                weight=constraint_weight,
                justification=str(data["constraint_clarity_justification"]),
            ),
            success_criteria_clarity=ComponentScore(
                name="Success Criteria Clarity",
                clarity_score=clamp_score(data["success_criteria_clarity_score"]),
                weight=criteria_weight,
                justification=str(data["success_criteria_clarity_justification"]),
            ),
            context_clarity=context_clarity,
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
            component.clarity_score * component.weight for component in breakdown.components
        )

        # Ambiguity = 1 - clarity
        return round(1.0 - weighted_clarity, 4)

    def generate_clarification_questions(self, breakdown: ScoreBreakdown) -> list[str]:
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
            questions.append("Can you describe the specific problem this solution should solve?")
            questions.append("What is the primary deliverable or output you expect?")

        if breakdown.constraint_clarity.clarity_score < clarification_threshold:
            questions.append("Are there any technical constraints or limitations to consider?")
            questions.append("What should definitely be excluded from the scope?")

        if breakdown.success_criteria_clarity.clarity_score < clarification_threshold:
            questions.append("How will you know when this is successfully completed?")
            questions.append("What specific features or behaviors are essential?")

        if (
            breakdown.context_clarity is not None
            and breakdown.context_clarity.clarity_score < clarification_threshold
        ):
            questions.append("Can you point to the specific directories of the existing codebase?")
            questions.append("What existing patterns or conventions must the new code follow?")

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
            f"  {component.name} (weight: {weight_percent:.0f}%): {clarity_percent:.0f}% clear"
        )
        lines.append(f"    Justification: {component.justification}")

    return "\n".join(lines)
