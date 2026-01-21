"""Atomicity detection for AC decomposition.

Determines whether an Acceptance Criterion (AC) is atomic (can be executed
directly) or non-atomic (needs decomposition into smaller units).

An AC is considered atomic if:
- Complexity score < 0.7
- Required tools < 3
- Estimated duration < 300 seconds

This module provides both LLM-based analysis (preferred) and heuristic
fallback (when LLM fails).

Usage:
    from ouroboros.execution.atomicity import check_atomicity, AtomicityCriteria

    result = await check_atomicity(
        ac_content="Implement user login",
        llm_adapter=adapter,
        criteria=AtomicityCriteria(),
    )

    if result.is_ok:
        if result.value.is_atomic:
            print("AC is atomic - execute directly")
        else:
            print("AC needs decomposition")
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import TYPE_CHECKING, Any

from ouroboros.core.errors import ProviderError, ValidationError
from ouroboros.core.types import Result
from ouroboros.observability.logging import get_logger
from ouroboros.routing.complexity import TaskContext, estimate_complexity

if TYPE_CHECKING:
    from ouroboros.providers.litellm_adapter import LiteLLMAdapter

log = get_logger(__name__)


# Default thresholds from requirements
DEFAULT_MAX_COMPLEXITY = 0.7
DEFAULT_MAX_TOOL_COUNT = 3
DEFAULT_MAX_DURATION_SECONDS = 300


@dataclass(frozen=True, slots=True)
class AtomicityCriteria:
    """Configurable thresholds for atomicity detection.

    Attributes:
        max_complexity: Maximum complexity score for atomic ACs (0.0-1.0).
        max_tool_count: Maximum number of tools for atomic ACs.
        max_duration_seconds: Maximum estimated duration for atomic ACs.
    """

    max_complexity: float = DEFAULT_MAX_COMPLEXITY
    max_tool_count: int = DEFAULT_MAX_TOOL_COUNT
    max_duration_seconds: int = DEFAULT_MAX_DURATION_SECONDS

    def validate(self) -> Result[None, ValidationError]:
        """Validate criteria constraints.

        Returns:
            Result with None on success or ValidationError on failure.
        """
        if not 0.0 <= self.max_complexity <= 1.0:
            return Result.err(
                ValidationError(
                    "max_complexity must be between 0.0 and 1.0",
                    field="max_complexity",
                    value=self.max_complexity,
                )
            )
        if self.max_tool_count < 0:
            return Result.err(
                ValidationError(
                    "max_tool_count must be non-negative",
                    field="max_tool_count",
                    value=self.max_tool_count,
                )
            )
        if self.max_duration_seconds < 0:
            return Result.err(
                ValidationError(
                    "max_duration_seconds must be non-negative",
                    field="max_duration_seconds",
                    value=self.max_duration_seconds,
                )
            )
        return Result.ok(None)


@dataclass(frozen=True, slots=True)
class AtomicityResult:
    """Result of atomicity check.

    Attributes:
        is_atomic: Whether the AC is atomic.
        complexity_score: Normalized complexity (0.0-1.0).
        tool_count: Estimated number of tools required.
        estimated_duration: Estimated duration in seconds.
        reasoning: Human-readable explanation of the decision.
        method: Detection method used ("llm" or "heuristic").
    """

    is_atomic: bool
    complexity_score: float
    tool_count: int
    estimated_duration: int
    reasoning: str
    method: str = "llm"

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for logging/serialization."""
        return {
            "is_atomic": self.is_atomic,
            "complexity_score": self.complexity_score,
            "tool_count": self.tool_count,
            "estimated_duration": self.estimated_duration,
            "reasoning": self.reasoning,
            "method": self.method,
        }


# LLM prompts for atomicity detection
ATOMICITY_SYSTEM_PROMPT = """You are an expert at analyzing task complexity and atomicity.

An acceptance criterion (AC) is considered ATOMIC if it can be:
1. Completed in a single focused session
2. Executed with minimal tools (< 3)
3. Clearly verified when done
4. Estimated at under 300 seconds of execution time

Non-atomic ACs typically:
- Have multiple distinct steps that could be separate tasks
- Require coordinating several different tools/systems
- Have complex verification requirements
- Would benefit from being broken down further

Analyze the given AC and determine if it's atomic or needs decomposition."""

ATOMICITY_USER_TEMPLATE = """Acceptance Criterion:
{ac_content}

Analyze this AC and respond with a JSON object:
{{
    "is_atomic": true/false,
    "complexity_score": 0.0 to 1.0 (0 = trivial, 1 = very complex),
    "tool_count": estimated number of tools needed (integer),
    "estimated_duration": estimated seconds to complete (integer),
    "reasoning": "brief explanation of your assessment"
}}

Only respond with the JSON, no other text."""


def _extract_json_from_response(response: str) -> dict[str, Any] | None:
    """Extract JSON from LLM response, handling various formats.

    Args:
        response: Raw LLM response text.

    Returns:
        Parsed JSON dict or None if parsing fails.
    """
    # Try direct parsing first
    try:
        result = json.loads(response.strip())
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    # Try to find JSON in markdown code blocks
    json_pattern = r"```(?:json)?\s*(.*?)```"
    matches = re.findall(json_pattern, response, re.DOTALL)
    for match in matches:
        try:
            result = json.loads(match.strip())
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            continue

    # Try to find JSON-like content
    brace_pattern = r"\{[^{}]*\}"
    matches = re.findall(brace_pattern, response, re.DOTALL)
    for match in matches:
        try:
            result = json.loads(match.strip())
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            continue

    return None


def _heuristic_atomicity_check(
    ac_content: str,
    criteria: AtomicityCriteria,
) -> AtomicityResult:
    """Fallback heuristic-based atomicity check.

    Uses simple text analysis when LLM is unavailable.

    Args:
        ac_content: The AC text to analyze.
        criteria: Atomicity thresholds.

    Returns:
        AtomicityResult based on heuristic analysis.
    """
    # Estimate token count (rough: 4 chars per token)
    token_count = len(ac_content) // 4

    # Estimate tool dependencies from keywords
    tool_keywords = [
        "database",
        "api",
        "file",
        "git",
        "docker",
        "npm",
        "pip",
        "test",
        "deploy",
        "build",
        "migrate",
        "configure",
        "install",
        "http",
        "rest",
        "graphql",
    ]
    tool_count = sum(1 for keyword in tool_keywords if keyword in ac_content.lower())

    # Estimate complexity based on structure
    complexity_indicators = [
        "and",
        "then",
        "after",
        "before",
        "while",
        "during",
        "multiple",
        "several",
        "various",
        "different",
    ]
    complexity_boost = sum(0.1 for ind in complexity_indicators if ind in ac_content.lower())

    # Use existing complexity estimation
    task_ctx = TaskContext(
        token_count=token_count,
        tool_dependencies=["tool"] * tool_count,
        ac_depth=0,
    )
    complexity_result = estimate_complexity(task_ctx)

    base_complexity = complexity_result.value.score if complexity_result.is_ok else 0.5

    complexity_score = min(1.0, base_complexity + complexity_boost)

    # Estimate duration (rough: 30 seconds per 100 tokens, adjusted by complexity)
    estimated_duration = int((token_count / 100) * 30 * (1 + complexity_score))

    # Determine atomicity
    is_atomic = (
        complexity_score < criteria.max_complexity
        and tool_count < criteria.max_tool_count
        and estimated_duration < criteria.max_duration_seconds
    )

    reasons = []
    if complexity_score >= criteria.max_complexity:
        reasons.append(f"complexity {complexity_score:.2f} >= {criteria.max_complexity}")
    if tool_count >= criteria.max_tool_count:
        reasons.append(f"tools {tool_count} >= {criteria.max_tool_count}")
    if estimated_duration >= criteria.max_duration_seconds:
        reasons.append(f"duration {estimated_duration}s >= {criteria.max_duration_seconds}s")

    if not reasons:
        reasons.append("within all thresholds")

    return AtomicityResult(
        is_atomic=is_atomic,
        complexity_score=complexity_score,
        tool_count=tool_count,
        estimated_duration=estimated_duration,
        reasoning=f"[Heuristic] {'; '.join(reasons)}",
        method="heuristic",
    )


async def check_atomicity(
    ac_content: str,
    llm_adapter: LiteLLMAdapter,
    criteria: AtomicityCriteria | None = None,
    *,
    use_llm: bool = True,
    model: str = "openrouter/google/gemini-2.0-flash-001",
) -> Result[AtomicityResult, ProviderError | ValidationError]:
    """Check if an AC is atomic using LLM + heuristic fallback.

    Attempts LLM-based analysis first, falling back to heuristics
    if LLM fails or is disabled.

    Args:
        ac_content: The acceptance criterion text to analyze.
        llm_adapter: LLM adapter for making completion requests.
        criteria: Atomicity thresholds (uses defaults if None).
        use_llm: Whether to attempt LLM analysis first.
        model: Model to use for LLM analysis.

    Returns:
        Result containing AtomicityResult or error.

    Example:
        result = await check_atomicity(
            "Implement user authentication with JWT",
            llm_adapter,
            AtomicityCriteria(max_complexity=0.6),
        )
        if result.is_ok and result.value.is_atomic:
            print("Execute directly")
    """
    if criteria is None:
        criteria = AtomicityCriteria()

    # Validate criteria
    validation_result = criteria.validate()
    if validation_result.is_err:
        return Result.err(validation_result.error)

    log.debug(
        "atomicity.check.started",
        ac_length=len(ac_content),
        use_llm=use_llm,
    )

    # Skip LLM if disabled
    if not use_llm:
        result = _heuristic_atomicity_check(ac_content, criteria)
        log.info(
            "atomicity.check.completed",
            is_atomic=result.is_atomic,
            method="heuristic",
            complexity=result.complexity_score,
        )
        return Result.ok(result)

    # Try LLM-based analysis
    from ouroboros.providers.base import CompletionConfig, Message, MessageRole

    messages = [
        Message(role=MessageRole.SYSTEM, content=ATOMICITY_SYSTEM_PROMPT),
        Message(role=MessageRole.USER, content=ATOMICITY_USER_TEMPLATE.format(ac_content=ac_content)),
    ]

    config = CompletionConfig(
        model=model,
        temperature=0.3,  # Lower for consistent analysis
        max_tokens=500,
    )

    llm_result = await llm_adapter.complete(messages, config)

    if llm_result.is_err:
        log.warning(
            "atomicity.check.llm_failed",
            error=str(llm_result.error),
            falling_back_to_heuristic=True,
        )
        # Fallback to heuristic
        result = _heuristic_atomicity_check(ac_content, criteria)
        log.info(
            "atomicity.check.completed",
            is_atomic=result.is_atomic,
            method="heuristic_fallback",
            complexity=result.complexity_score,
        )
        return Result.ok(result)

    # Parse LLM response
    response_text = llm_result.value.content
    parsed = _extract_json_from_response(response_text)

    if parsed is None:
        log.warning(
            "atomicity.check.parse_failed",
            response_preview=response_text[:200],
            falling_back_to_heuristic=True,
        )
        # Fallback to heuristic
        result = _heuristic_atomicity_check(ac_content, criteria)
        return Result.ok(result)

    try:
        # Extract values with defaults
        is_atomic_raw = parsed.get("is_atomic", True)
        complexity_score = float(parsed.get("complexity_score", 0.5))
        tool_count = int(parsed.get("tool_count", 1))
        estimated_duration = int(parsed.get("estimated_duration", 60))
        reasoning = str(parsed.get("reasoning", "LLM analysis"))

        # Apply criteria to determine atomicity
        is_atomic = (
            is_atomic_raw
            and complexity_score < criteria.max_complexity
            and tool_count < criteria.max_tool_count
            and estimated_duration < criteria.max_duration_seconds
        )

        result = AtomicityResult(
            is_atomic=is_atomic,
            complexity_score=complexity_score,
            tool_count=tool_count,
            estimated_duration=estimated_duration,
            reasoning=reasoning,
            method="llm",
        )

        log.info(
            "atomicity.check.completed",
            is_atomic=result.is_atomic,
            method="llm",
            complexity=result.complexity_score,
            tool_count=result.tool_count,
        )

        return Result.ok(result)

    except (ValueError, TypeError, KeyError) as e:
        log.warning(
            "atomicity.check.parse_error",
            error=str(e),
            parsed=parsed,
            falling_back_to_heuristic=True,
        )
        # Fallback to heuristic
        result = _heuristic_atomicity_check(ac_content, criteria)
        return Result.ok(result)
