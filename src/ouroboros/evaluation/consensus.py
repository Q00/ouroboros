"""Stage 3: Multi-Model Consensus.

This module provides two consensus evaluation modes:

1. Simple Consensus (ConsensusEvaluator):
   - 3 models evaluate independently
   - 2/3 majority required for approval
   - Fast, straightforward voting

2. Deliberative Consensus (DeliberativeConsensus):
   - Role-based evaluation: Advocate, Devil's Advocate, Judge
   - 2-round deliberation: positions → judgment
   - Devil's Advocate uses ontological questions
   - Deeper analysis of whether solution addresses root cause

The deliberative mode is recommended for complex decisions where
ensuring root cause resolution is important.
"""

import asyncio
from dataclasses import dataclass
import json

from ouroboros.core.errors import ProviderError, ValidationError
from ouroboros.core.ontology_aspect import AnalysisResult
from ouroboros.core.types import Result
from ouroboros.evaluation.models import (
    ConsensusResult,
    DeliberationResult,
    EvaluationContext,
    FinalVerdict,
    JudgmentResult,
    Vote,
    VoterRole,
)
from ouroboros.events.base import BaseEvent
from ouroboros.events.evaluation import (
    create_stage3_completed_event,
    create_stage3_started_event,
)
from ouroboros.providers.base import CompletionConfig, Message, MessageRole
from ouroboros.providers.litellm_adapter import LiteLLMAdapter
from ouroboros.strategies.devil_advocate import ConsensusContext, DevilAdvocateStrategy

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


def _get_consensus_system_prompt() -> str:
    """Lazy-load consensus system prompt to avoid import-time I/O."""
    from ouroboros.agents.loader import load_agent_prompt

    return load_agent_prompt("consensus-reviewer")


def build_consensus_prompt(context: EvaluationContext) -> str:
    """Build the user prompt for consensus voting.

    Args:
        context: Evaluation context

    Returns:
        Formatted prompt string
    """
    constraints_text = (
        "\n".join(f"- {c}" for c in context.constraints) if context.constraints else "None"
    )

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
    """Extract JSON object from text using bracket-matching approach.

    Uses brace counting to find the first complete JSON object,
    avoiding issues with multiple disjoint brace blocks (e.g., code snippets).

    Args:
        text: Raw text potentially containing JSON

    Returns:
        Extracted JSON string or None if not found
    """
    start = text.find("{")
    if start == -1:
        return None

    # Count braces to find matching closing brace
    depth = 0
    in_string = False
    escape_next = False

    for i, char in enumerate(text[start:], start=start):
        if escape_next:
            escape_next = False
            continue

        if char == "\\":
            escape_next = True
            continue

        if char == '"' and not escape_next:
            in_string = not in_string
            continue

        if in_string:
            continue

        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]

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
            Message(role=MessageRole.SYSTEM, content=_get_consensus_system_prompt()),
            Message(role=MessageRole.USER, content=build_consensus_prompt(context)),
        ]

        # Collect votes from all models concurrently
        vote_tasks = [self._get_vote(messages, model) for model in models]
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
        disagreements = tuple(v.reasoning for v in votes if v.approved != approved)

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


# Role-based system prompts for deliberative consensus
def _get_advocate_system_prompt() -> str:
    """Lazy-load advocate system prompt to avoid import-time I/O."""
    from ouroboros.agents.loader import load_agent_prompt

    return load_agent_prompt("advocate")


def _get_judge_system_prompt() -> str:
    """Lazy-load judge system prompt to avoid import-time I/O."""
    from ouroboros.agents.loader import load_agent_prompt

    return load_agent_prompt("judge")


@dataclass(frozen=True, slots=True)
class DeliberativeConfig:
    """Configuration for deliberative consensus.

    Attributes:
        advocate_model: Model for the Advocate role
        devil_model: Model for the Devil's Advocate role
        judge_model: Model for the Judge role
        temperature: Sampling temperature
        max_tokens: Maximum tokens per response
    """

    advocate_model: str = "openrouter/anthropic/claude-sonnet-4-20250514"
    devil_model: str = "openrouter/openai/gpt-4o"
    judge_model: str = "openrouter/google/gemini-2.5-pro"
    temperature: float = 0.3
    max_tokens: int = 2048


def _parse_judgment_response(
    response_text: str,
    model: str,
) -> Result[JudgmentResult, ValidationError]:
    """Parse LLM response into JudgmentResult.

    Args:
        response_text: Raw LLM response
        model: Model that made the judgment

    Returns:
        Result containing JudgmentResult or ValidationError
    """
    json_str = extract_json_payload(response_text)

    if not json_str:
        return Result.err(
            ValidationError(
                f"Could not find JSON in judgment from {model}",
                field="response",
                value=response_text[:100],
            )
        )

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        return Result.err(
            ValidationError(
                f"Invalid JSON in judgment from {model}: {e}",
                field="response",
            )
        )

    # Validate required fields
    if "verdict" not in data:
        return Result.err(
            ValidationError(
                f"Missing 'verdict' field in judgment from {model}",
                field="verdict",
            )
        )

    # Parse verdict
    verdict_str = str(data["verdict"]).lower()
    verdict_map = {
        "approved": FinalVerdict.APPROVED,
        "rejected": FinalVerdict.REJECTED,
        "conditional": FinalVerdict.CONDITIONAL,
    }

    if verdict_str not in verdict_map:
        return Result.err(
            ValidationError(
                f"Invalid verdict '{verdict_str}' from {model}",
                field="verdict",
                value=verdict_str,
            )
        )

    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.5))))
        conditions = data.get("conditions")
        if conditions is not None:
            conditions = tuple(str(c) for c in conditions)

        return Result.ok(
            JudgmentResult(
                verdict=verdict_map[verdict_str],
                confidence=confidence,
                reasoning=str(data.get("reasoning", "No reasoning provided")),
                conditions=conditions,
            )
        )
    except (TypeError, ValueError) as e:
        return Result.err(
            ValidationError(
                f"Invalid field types in judgment from {model}: {e}",
                field="response",
            )
        )


class DeliberativeConsensus:
    """Two-round deliberative consensus evaluator.

    Uses role-based evaluation with ontological questioning:
    - Round 1: Advocate and Devil's Advocate present positions (parallel)
    - Round 2: Judge reviews both and makes final decision

    The Devil's Advocate uses DevilAdvocateStrategy with AOP-based
    ontological analysis to ensure the solution addresses the root
    cause rather than just treating symptoms.

    Example:
        evaluator = DeliberativeConsensus(llm_adapter)
        result = await evaluator.deliberate(context, trigger_reason)

        # With custom strategy for testing
        mock_strategy = MockDevilStrategy()
        evaluator = DeliberativeConsensus(llm_adapter, devil_strategy=mock_strategy)
    """

    def __init__(
        self,
        llm_adapter: LiteLLMAdapter,
        config: DeliberativeConfig | None = None,
        devil_strategy: DevilAdvocateStrategy | None = None,
    ) -> None:
        """Initialize evaluator.

        Args:
            llm_adapter: LLM adapter for completions
            config: Deliberative configuration
            devil_strategy: Optional custom strategy for Devil's Advocate.
                If None, creates default DevilAdvocateStrategy.
        """
        self._llm = llm_adapter
        self._config = config or DeliberativeConfig()
        self._devil_strategy = devil_strategy or DevilAdvocateStrategy(
            llm_adapter=llm_adapter,
            model=self._config.devil_model,
            temperature=self._config.temperature,
            max_tokens=self._config.max_tokens,
        )

    async def deliberate(
        self,
        context: EvaluationContext,
        trigger_reason: str = "manual",
    ) -> Result[tuple[DeliberationResult, list[BaseEvent]], ProviderError | ValidationError]:
        """Run 2-round deliberative consensus.

        Round 1: Advocate and Devil's Advocate present positions concurrently
        Round 2: Judge reviews both positions and makes final decision

        Args:
            context: Evaluation context
            trigger_reason: Why consensus was triggered

        Returns:
            Result containing DeliberationResult and events, or error
        """
        events: list[BaseEvent] = []

        # Emit start event
        events.append(
            create_stage3_started_event(
                execution_id=context.execution_id,
                models=[
                    self._config.advocate_model,
                    self._config.devil_model,
                    self._config.judge_model,
                ],
                trigger_reason=f"deliberative:{trigger_reason}",
            )
        )

        # Round 1: Get Advocate and Devil's Advocate positions concurrently
        advocate_task = self._get_position(context, VoterRole.ADVOCATE)
        devil_task = self._get_position(context, VoterRole.DEVIL)

        # Type hint for asyncio.gather with return_exceptions=True
        results: list[
            Result[Vote, ProviderError | ValidationError] | BaseException
        ] = await asyncio.gather(advocate_task, devil_task, return_exceptions=True)
        advocate_result, devil_result = results[0], results[1]

        # Handle Round 1 errors - type narrowing via isinstance
        if isinstance(advocate_result, BaseException):
            return Result.err(ValidationError(f"Advocate failed: {advocate_result}"))
        if advocate_result.is_err:
            return Result.err(advocate_result.error)
        advocate_vote = advocate_result.value

        if isinstance(devil_result, BaseException):
            return Result.err(ValidationError(f"Devil's Advocate failed: {devil_result}"))
        if devil_result.is_err:
            return Result.err(devil_result.error)
        devil_vote = devil_result.value

        # Round 2: Judge reviews both positions
        judgment_result = await self._get_judgment(context, advocate_vote, devil_vote)

        if judgment_result.is_err:
            return Result.err(judgment_result.error)
        judgment = judgment_result.value

        # Determine if Devil confirmed this addresses root cause
        # Devil approves (approved=True) means they couldn't find fundamental issues
        is_root_solution = devil_vote.approved

        deliberation_result = DeliberationResult(
            final_verdict=judgment.verdict,
            advocate_position=advocate_vote,
            devil_position=devil_vote,
            judgment=judgment,
            is_root_solution=is_root_solution,
        )

        # Emit completion event
        events.append(
            create_stage3_completed_event(
                execution_id=context.execution_id,
                approved=deliberation_result.approved,
                votes=[
                    {
                        "model": advocate_vote.model,
                        "role": advocate_vote.role,
                        "approved": advocate_vote.approved,
                        "confidence": advocate_vote.confidence,
                        "reasoning": advocate_vote.reasoning,
                    },
                    {
                        "model": devil_vote.model,
                        "role": devil_vote.role,
                        "approved": devil_vote.approved,
                        "confidence": devil_vote.confidence,
                        "reasoning": devil_vote.reasoning,
                    },
                ],
                majority_ratio=1.0 if deliberation_result.approved else 0.0,
                disagreements=[],
            )
        )

        return Result.ok((deliberation_result, events))

    async def _get_position(
        self,
        context: EvaluationContext,
        role: VoterRole,
    ) -> Result[Vote, ProviderError | ValidationError]:
        """Get a position from Advocate or Devil's Advocate.

        Args:
            context: Evaluation context
            role: The role (ADVOCATE or DEVIL)

        Returns:
            Result containing Vote or error
        """
        if role == VoterRole.ADVOCATE:
            # Advocate uses direct LLM call with role-specific prompt
            system_prompt = _get_advocate_system_prompt()
            model = self._config.advocate_model

            messages = [
                Message(role=MessageRole.SYSTEM, content=system_prompt),
                Message(role=MessageRole.USER, content=build_consensus_prompt(context)),
            ]

            config = CompletionConfig(
                model=model,
                temperature=self._config.temperature,
                max_tokens=self._config.max_tokens,
            )

            llm_result = await self._llm.complete(messages, config)
            if llm_result.is_err:
                return Result.err(llm_result.error)

            vote_result = parse_vote_response(llm_result.value.content, model)
            if vote_result.is_err:
                return Result.err(vote_result.error)

            vote = vote_result.value
            return Result.ok(
                Vote(
                    model=vote.model,
                    approved=vote.approved,
                    confidence=vote.confidence,
                    reasoning=vote.reasoning,
                    role=role,
                )
            )

        elif role == VoterRole.DEVIL:
            # Devil uses AOP-based DevilAdvocateStrategy for ontological analysis
            return await self._get_devil_position(context)

        else:
            return Result.err(ValidationError(f"Invalid role for position: {role}"))

    async def _get_devil_position(
        self,
        context: EvaluationContext,
    ) -> Result[Vote, ProviderError | ValidationError]:
        """Get Devil's Advocate position using ontological analysis.

        Uses DevilAdvocateStrategy to analyze whether the artifact
        addresses root cause or treats symptoms.

        Args:
            context: Evaluation context

        Returns:
            Result containing Vote with Devil's Advocate role
        """
        # Convert EvaluationContext to ConsensusContext for strategy
        consensus_ctx = ConsensusContext(
            artifact=context.artifact,
            goal=context.goal,
            current_ac=context.current_ac,
            constraints=context.constraints,
        )

        # Strategy handles errors gracefully (returns AnalysisResult.invalid on LLM failure)
        analysis = await self._devil_strategy.analyze(consensus_ctx)

        # Convert AnalysisResult to Vote
        vote = self._analysis_to_vote(analysis)
        return Result.ok(vote)

    def _analysis_to_vote(self, analysis: AnalysisResult) -> Vote:
        """Convert AnalysisResult to Vote for Devil's Advocate.

        Maps ontological analysis result to consensus voting format:
        - is_valid -> approved
        - confidence -> confidence
        - reasoning + suggestions -> reasoning

        Args:
            analysis: The ontological analysis result

        Returns:
            Vote with Devil's Advocate role
        """
        # Build reasoning text
        if analysis.is_valid:
            reasoning_text = (
                analysis.reasoning[0]
                if analysis.reasoning
                else "Passed ontological analysis: addresses root cause"
            )
        else:
            # Combine reasoning and suggestions for invalid case
            parts = list(analysis.reasoning)
            if analysis.suggestions:
                parts.append("Suggestions: " + "; ".join(analysis.suggestions))
            reasoning_text = "\n".join(parts) if parts else "Failed ontological analysis"

        return Vote(
            model=self._devil_strategy.model,
            approved=analysis.is_valid,
            confidence=analysis.confidence,
            reasoning=reasoning_text,
            role=VoterRole.DEVIL,
        )

    async def _get_judgment(
        self,
        context: EvaluationContext,
        advocate_vote: Vote,
        devil_vote: Vote,
    ) -> Result[JudgmentResult, ProviderError | ValidationError]:
        """Get final judgment from Judge.

        Args:
            context: Evaluation context
            advocate_vote: The Advocate's position
            devil_vote: The Devil's Advocate's position

        Returns:
            Result containing JudgmentResult or error
        """
        # Build prompt with both positions
        user_prompt = f"""{build_consensus_prompt(context)}

---

## Round 1 Positions

### ADVOCATE's Position
Approved: {advocate_vote.approved}
Confidence: {advocate_vote.confidence:.2f}
Reasoning: {advocate_vote.reasoning}

### DEVIL'S ADVOCATE's Position (Ontological Analysis)
Approved: {devil_vote.approved}
Confidence: {devil_vote.confidence:.2f}
Reasoning: {devil_vote.reasoning}

---

Based on both positions above, make your final judgment."""

        messages = [
            Message(role=MessageRole.SYSTEM, content=_get_judge_system_prompt()),
            Message(role=MessageRole.USER, content=user_prompt),
        ]

        config = CompletionConfig(
            model=self._config.judge_model,
            temperature=self._config.temperature,
            max_tokens=self._config.max_tokens,
        )

        llm_result = await self._llm.complete(messages, config)
        if llm_result.is_err:
            return Result.err(llm_result.error)

        return _parse_judgment_response(llm_result.value.content, self._config.judge_model)


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


async def run_deliberative_evaluation(
    context: EvaluationContext,
    llm_adapter: LiteLLMAdapter,
    trigger_reason: str = "manual",
    config: DeliberativeConfig | None = None,
    devil_strategy: DevilAdvocateStrategy | None = None,
) -> Result[tuple[DeliberationResult, list[BaseEvent]], ProviderError | ValidationError]:
    """Convenience function for running deliberative consensus.

    Recommended for complex decisions where ensuring root cause
    resolution is important. Uses AOP-based DevilAdvocateStrategy
    for ontological analysis.

    Args:
        context: Evaluation context
        llm_adapter: LLM adapter
        trigger_reason: Why consensus was triggered
        config: Optional configuration
        devil_strategy: Optional custom strategy for Devil's Advocate

    Returns:
        Result with DeliberationResult and events
    """
    evaluator = DeliberativeConsensus(llm_adapter, config, devil_strategy)
    return await evaluator.deliberate(context, trigger_reason)
