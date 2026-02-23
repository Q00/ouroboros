"""Stage 2: Semantic Evaluation.

LLM-based semantic evaluation using Standard tier:
- AC Compliance: Whether acceptance criteria are met
- Goal Alignment: Alignment with original goal
- Drift Measurement: Deviation from seed intent

The SemanticEvaluator uses the LiteLLM adapter for LLM calls.
"""

from dataclasses import dataclass
import json

from ouroboros.core.errors import ProviderError, ValidationError
from ouroboros.core.types import Result
from ouroboros.evaluation.models import EvaluationContext, SemanticResult
from ouroboros.events.base import BaseEvent
from ouroboros.events.evaluation import (
    create_stage2_completed_event,
    create_stage2_started_event,
)
from ouroboros.providers.base import CompletionConfig, LLMAdapter, Message, MessageRole

# Default model for semantic evaluation (Standard tier)
# Can be overridden via SemanticConfig.model
DEFAULT_SEMANTIC_MODEL = "openrouter/google/gemini-2.0-flash-001"


@dataclass(frozen=True, slots=True)
class SemanticConfig:
    """Configuration for semantic evaluation.

    Attributes:
        model: LLM model to use for evaluation
        temperature: Sampling temperature (lower for consistency)
        max_tokens: Maximum tokens for response
        satisfaction_threshold: Minimum score to pass (default 0.8)
    """

    model: str = DEFAULT_SEMANTIC_MODEL
    temperature: float = 0.2
    max_tokens: int = 2048
    satisfaction_threshold: float = 0.8


def _get_evaluation_system_prompt() -> str:
    """Lazy-load evaluation system prompt to avoid import-time I/O."""
    from ouroboros.agents.loader import load_agent_prompt

    return load_agent_prompt("semantic-evaluator")


def build_evaluation_prompt(context: EvaluationContext) -> str:
    """Build the user prompt for evaluation.

    Args:
        context: Evaluation context with artifact and criteria

    Returns:
        Formatted prompt string
    """
    constraints_text = (
        "\n".join(f"- {c}" for c in context.constraints)
        if context.constraints
        else "None specified"
    )

    return f"""Evaluate the following artifact:

## Acceptance Criterion
{context.current_ac}

## Original Goal
{context.goal if context.goal else "Not specified"}

## Constraints
{constraints_text}

## Artifact Type
{context.artifact_type}

## Artifact Content
```
{context.artifact}
```

Provide your evaluation as a JSON object."""


def extract_json_payload(text: str) -> str | None:
    """Extract JSON object from text using index-based approach.

    More reliable than regex for handling nested braces in code snippets.

    Args:
        text: Raw text potentially containing JSON

    Returns:
        Extracted JSON string or None if not found
    """
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return None


def parse_semantic_response(response_text: str) -> Result[SemanticResult, ValidationError]:
    """Parse LLM response into SemanticResult.

    Args:
        response_text: Raw LLM response text

    Returns:
        Result containing SemanticResult or ValidationError
    """
    # Extract JSON using index-based approach (handles nested braces)
    json_str = extract_json_payload(response_text)

    if not json_str:
        return Result.err(
            ValidationError(
                "Could not find JSON in response",
                field="response",
                value=response_text[:100],
            )
        )

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        return Result.err(
            ValidationError(
                f"Invalid JSON in response: {e}",
                field="response",
                value=json_str[:100],
            )
        )

    # Validate required fields
    required_fields = [
        "score",
        "ac_compliance",
        "goal_alignment",
        "drift_score",
        "uncertainty",
        "reasoning",
    ]
    missing = [f for f in required_fields if f not in data]
    if missing:
        return Result.err(
            ValidationError(
                f"Missing required fields: {missing}",
                field="response",
                details={"missing_fields": missing},
            )
        )

    # Validate and clamp numeric ranges
    try:
        score = max(0.0, min(1.0, float(data["score"])))
        goal_alignment = max(0.0, min(1.0, float(data["goal_alignment"])))
        drift_score = max(0.0, min(1.0, float(data["drift_score"])))
        uncertainty = max(0.0, min(1.0, float(data["uncertainty"])))

        return Result.ok(
            SemanticResult(
                score=score,
                ac_compliance=bool(data["ac_compliance"]),
                goal_alignment=goal_alignment,
                drift_score=drift_score,
                uncertainty=uncertainty,
                reasoning=str(data["reasoning"]),
            )
        )
    except (TypeError, ValueError) as e:
        return Result.err(
            ValidationError(
                f"Invalid field types: {e}",
                field="response",
                details={"error": str(e)},
            )
        )


class SemanticEvaluator:
    """Stage 2 semantic evaluation using LLM.

    Evaluates artifacts for AC compliance, goal alignment, and drift.
    Uses Standard tier LLM for balanced cost/quality.

    Example:
        evaluator = SemanticEvaluator(llm_adapter)
        result = await evaluator.evaluate(context, execution_id)
    """

    def __init__(
        self,
        llm_adapter: LLMAdapter,
        config: SemanticConfig | None = None,
    ) -> None:
        """Initialize evaluator.

        Args:
            llm_adapter: LLM adapter for completions
            config: Evaluation configuration
        """
        self._llm = llm_adapter
        self._config = config or SemanticConfig()

    async def evaluate(
        self,
        context: EvaluationContext,
    ) -> Result[tuple[SemanticResult, list[BaseEvent]], ProviderError | ValidationError]:
        """Evaluate an artifact semantically.

        Args:
            context: Evaluation context

        Returns:
            Result containing SemanticResult and events, or error
        """
        events: list[BaseEvent] = []

        # Emit start event
        events.append(
            create_stage2_started_event(
                execution_id=context.execution_id,
                model=self._config.model,
                current_ac=context.current_ac,
            )
        )

        # Build messages
        messages = [
            Message(role=MessageRole.SYSTEM, content=_get_evaluation_system_prompt()),
            Message(role=MessageRole.USER, content=build_evaluation_prompt(context)),
        ]

        # Call LLM
        completion_config = CompletionConfig(
            model=self._config.model,
            temperature=self._config.temperature,
            max_tokens=self._config.max_tokens,
        )

        llm_result = await self._llm.complete(messages, completion_config)
        if llm_result.is_err:
            return Result.err(llm_result.error)

        response = llm_result.value

        # Parse response
        parse_result = parse_semantic_response(response.content)
        if parse_result.is_err:
            return Result.err(parse_result.error)

        semantic_result = parse_result.value

        # Emit completion event
        events.append(
            create_stage2_completed_event(
                execution_id=context.execution_id,
                score=semantic_result.score,
                ac_compliance=semantic_result.ac_compliance,
                goal_alignment=semantic_result.goal_alignment,
                drift_score=semantic_result.drift_score,
                uncertainty=semantic_result.uncertainty,
            )
        )

        return Result.ok((semantic_result, events))


async def run_semantic_evaluation(
    context: EvaluationContext,
    llm_adapter: LLMAdapter,
    config: SemanticConfig | None = None,
) -> Result[tuple[SemanticResult, list[BaseEvent]], ProviderError | ValidationError]:
    """Convenience function for running semantic evaluation.

    Args:
        context: Evaluation context
        llm_adapter: LLM adapter
        config: Optional configuration

    Returns:
        Result with SemanticResult and events
    """
    evaluator = SemanticEvaluator(llm_adapter, config)
    return await evaluator.evaluate(context)
