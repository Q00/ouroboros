"""Stage 3: Multi-Model Consensus.

Multi-model voting using Frontier tier:
- 3 different models evaluate independently
- 2/3 majority required for approval
- Disagreements are logged with reasoning

The ConsensusEvaluator uses multiple LLM models for diverse verification.
"""

import asyncio
from dataclasses import dataclass
import json

from ouroboros.core.errors import ProviderError, ValidationError
from ouroboros.core.types import Result
from ouroboros.evaluation.models import ConsensusResult, EvaluationContext, Vote
from ouroboros.events.base import BaseEvent
from ouroboros.events.evaluation import (
    create_stage3_completed_event,
    create_stage3_started_event,
)
from ouroboros.providers.base import CompletionConfig, Message, MessageRole
from ouroboros.providers.litellm_adapter import LiteLLMAdapter

# Default models for consensus voting (Frontier tier)
# Can be overridden via ConsensusConfig.models
DEFAULT_CONSENSUS_MODELS: tuple[str, ...] = (
    "openrouter/openai/gpt-4o",
    "openrouter/anthropic/claude-sonnet-4-20250514",
    "openrouter/google/gemini-2.5-pro",
)


@dataclass(frozen=True, slots=True)
class ConsensusConfig:
    """Configuration for consensus evaluation.

    Attributes:
        models: List of models to use for voting (at least 3)
        temperature: Sampling temperature
        max_tokens: Maximum tokens per response
        majority_threshold: Required majority ratio (default 2/3)
        diversity_required: Require different providers
    """

    models: tuple[str, ...] = DEFAULT_CONSENSUS_MODELS
    temperature: float = 0.3
    max_tokens: int = 1024
    majority_threshold: float = 0.66  # 2/3 = 0.6666...
    diversity_required: bool = True


CONSENSUS_SYSTEM_PROMPT = """You are a senior code reviewer participating in a consensus evaluation. Your vote will be combined with other reviewers to reach a decision.

You must respond ONLY with a valid JSON object in the following exact format:
{
    "approved": <boolean>,
    "confidence": <float between 0.0 and 1.0>,
    "reasoning": "<string explaining your vote>"
}

Evaluation criteria for approval:
- The artifact correctly implements the acceptance criterion
- The implementation aligns with the stated goal
- No significant issues or concerns
- Code quality is acceptable

Be honest and thorough. If you have concerns, vote against approval with clear reasoning.
Confidence should reflect how certain you are about your decision."""


def build_consensus_prompt(context: EvaluationContext) -> str:
    """Build the user prompt for consensus voting.

    Args:
        context: Evaluation context

    Returns:
        Formatted prompt string
    """
    constraints_text = "\n".join(f"- {c}" for c in context.constraints) if context.constraints else "None"

    return f"""Review the following artifact for consensus approval:

## Acceptance Criterion
{context.current_ac}

## Original Goal
{context.goal if context.goal else "Not specified"}

## Constraints
{constraints_text}

## Artifact ({context.artifact_type})
```
{context.artifact}
```

Cast your vote as a JSON object with: approved (boolean), confidence (0-1), and reasoning."""


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


def parse_vote_response(response_text: str, model: str) -> Result[Vote, ValidationError]:
    """Parse LLM response into Vote.

    Args:
        response_text: Raw LLM response
        model: Model that cast the vote

    Returns:
        Result containing Vote or ValidationError
    """
    # Extract JSON using index-based approach (handles nested braces)
    json_str = extract_json_payload(response_text)

    if not json_str:
        return Result.err(
            ValidationError(
                f"Could not find JSON in vote from {model}",
                field="response",
                value=response_text[:100],
            )
        )

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        return Result.err(
            ValidationError(
                f"Invalid JSON in vote from {model}: {e}",
                field="response",
            )
        )

    # Validate required fields
    if "approved" not in data:
        return Result.err(
            ValidationError(
                f"Missing 'approved' field in vote from {model}",
                field="approved",
            )
        )

    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.5))))
        return Result.ok(
            Vote(
                model=model,
                approved=bool(data["approved"]),
                confidence=confidence,
                reasoning=str(data.get("reasoning", "No reasoning provided")),
            )
        )
    except (TypeError, ValueError) as e:
        return Result.err(
            ValidationError(
                f"Invalid field types in vote from {model}: {e}",
                field="response",
            )
        )


class ConsensusEvaluator:
    """Stage 3 multi-model consensus evaluator.

    Uses multiple Frontier tier models for diverse verification.
    Requires 2/3 majority for approval.

    Example:
        evaluator = ConsensusEvaluator(llm_adapter)
        result = await evaluator.evaluate(context, trigger_reason)
    """

    def __init__(
        self,
        llm_adapter: LiteLLMAdapter,
        config: ConsensusConfig | None = None,
    ) -> None:
        """Initialize evaluator.

        Args:
            llm_adapter: LLM adapter for completions
            config: Consensus configuration
        """
        self._llm = llm_adapter
        self._config = config or ConsensusConfig()

    async def evaluate(
        self,
        context: EvaluationContext,
        trigger_reason: str = "manual",
    ) -> Result[tuple[ConsensusResult, list[BaseEvent]], ProviderError | ValidationError]:
        """Run consensus evaluation with multiple models.

        Args:
            context: Evaluation context
            trigger_reason: Why consensus was triggered

        Returns:
            Result containing ConsensusResult and events, or error
        """
        events: list[BaseEvent] = []
        models = list(self._config.models)

        # Emit start event
        events.append(
            create_stage3_started_event(
                execution_id=context.execution_id,
                models=models,
                trigger_reason=trigger_reason,
            )
        )

        # Build messages
        messages = [
            Message(role=MessageRole.SYSTEM, content=CONSENSUS_SYSTEM_PROMPT),
            Message(role=MessageRole.USER, content=build_consensus_prompt(context)),
        ]

        # Collect votes from all models concurrently
        vote_tasks = [
            self._get_vote(messages, model)
            for model in models
        ]
        vote_results = await asyncio.gather(*vote_tasks, return_exceptions=True)

        # Process results
        votes: list[Vote] = []
        errors: list[str] = []

        for model, result in zip(models, vote_results, strict=True):
            if isinstance(result, Exception):
                errors.append(f"{model}: {result}")
                continue
            if result.is_err:
                errors.append(f"{model}: {result.error.message}")
                continue
            votes.append(result.value)

        # Need at least 2 votes to proceed
        if len(votes) < 2:
            return Result.err(
                ValidationError(
                    f"Not enough votes collected: {len(votes)}/3",
                    details={"errors": errors},
                )
            )

        # Calculate consensus
        approving = sum(1 for v in votes if v.approved)
        majority_ratio = approving / len(votes)
        approved = majority_ratio >= self._config.majority_threshold

        # Collect disagreements (reasoning from dissenting votes)
        disagreements = tuple(
            v.reasoning for v in votes if v.approved != approved
        )

        consensus_result = ConsensusResult(
            approved=approved,
            votes=tuple(votes),
            majority_ratio=majority_ratio,
            disagreements=disagreements,
        )

        # Emit completion event
        events.append(
            create_stage3_completed_event(
                execution_id=context.execution_id,
                approved=approved,
                votes=[
                    {
                        "model": v.model,
                        "approved": v.approved,
                        "confidence": v.confidence,
                        "reasoning": v.reasoning,
                    }
                    for v in votes
                ],
                majority_ratio=majority_ratio,
                disagreements=list(disagreements),
            )
        )

        return Result.ok((consensus_result, events))

    async def _get_vote(
        self,
        messages: list[Message],
        model: str,
    ) -> Result[Vote, ProviderError | ValidationError]:
        """Get a single vote from a model.

        Args:
            messages: Prompt messages
            model: Model to query

        Returns:
            Result containing Vote or error
        """
        config = CompletionConfig(
            model=model,
            temperature=self._config.temperature,
            max_tokens=self._config.max_tokens,
        )

        llm_result = await self._llm.complete(messages, config)
        if llm_result.is_err:
            return Result.err(llm_result.error)

        return parse_vote_response(llm_result.value.content, model)


async def run_consensus_evaluation(
    context: EvaluationContext,
    llm_adapter: LiteLLMAdapter,
    trigger_reason: str = "manual",
    config: ConsensusConfig | None = None,
) -> Result[tuple[ConsensusResult, list[BaseEvent]], ProviderError | ValidationError]:
    """Convenience function for running consensus evaluation.

    Args:
        context: Evaluation context
        llm_adapter: LLM adapter
        trigger_reason: Why consensus was triggered
        config: Optional configuration

    Returns:
        Result with ConsensusResult and events
    """
    evaluator = ConsensusEvaluator(llm_adapter, config)
    return await evaluator.evaluate(context, trigger_reason)
