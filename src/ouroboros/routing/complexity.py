"""Complexity estimation for task routing in Ouroboros.

This module provides complexity estimation for the PAL (Progressive Adaptive LLM)
routing system. Complexity scores determine which tier (Frugal, Standard, or
Frontier) should handle a given task.

Complexity Factors:
- Token count: 30% weight - Approximated task size
- Tool dependency count: 30% weight - Number of external tools needed
- AC nesting depth: 40% weight - Acceptance criteria complexity

Usage:
    from ouroboros.routing.complexity import TaskContext, estimate_complexity

    # Create a task context
    context = TaskContext(
        token_count=500,
        tool_dependencies=["git", "npm"],
        ac_depth=2,
    )

    # Estimate complexity
    result = estimate_complexity(context)
    if result.is_ok:
        score = result.value
        print(f"Complexity: {score.score:.2f}")
        print(f"Breakdown: {score.breakdown}")
"""

from dataclasses import dataclass, field

from ouroboros.core.errors import ValidationError
from ouroboros.core.types import Result
from ouroboros.observability.logging import get_logger

log = get_logger(__name__)


# Weight constants for complexity calculation
WEIGHT_TOKEN_COUNT = 0.30
WEIGHT_TOOL_DEPENDENCIES = 0.30
WEIGHT_AC_DEPTH = 0.40

# Normalization thresholds
# Token count: normalized between 0 (0 tokens) and 1 (>= MAX_TOKEN_THRESHOLD)
MAX_TOKEN_THRESHOLD = 4000  # Tasks above this are considered maximally complex for tokens

# Tool dependencies: normalized between 0 (0 tools) and 1 (>= MAX_TOOL_THRESHOLD)
MAX_TOOL_THRESHOLD = 5  # Tasks with 5+ tools are considered maximally complex for tools

# AC depth: normalized between 0 (depth 0) and 1 (depth >= MAX_DEPTH_THRESHOLD)
MAX_DEPTH_THRESHOLD = 5  # AC depth of 5+ is considered maximally complex


@dataclass(frozen=True, slots=True)
class TaskContext:
    """Context information about a task for complexity estimation.

    This dataclass holds the information needed to estimate a task's complexity.
    All fields contribute to the final complexity score with different weights.

    Attributes:
        token_count: Estimated number of tokens in the task (prompt + expected output).
            Must be non-negative. Default is 0.
        tool_dependencies: List of tool names the task depends on.
            Each unique tool adds to complexity. Default is empty list.
        ac_depth: Acceptance criteria nesting depth (0-indexed).
            Deeper nesting indicates more complex requirements. Default is 0.

    Example:
        context = TaskContext(
            token_count=1500,
            tool_dependencies=["git", "npm", "docker"],
            ac_depth=3,
        )
    """

    token_count: int = 0
    tool_dependencies: list[str] = field(default_factory=list)
    ac_depth: int = 0


@dataclass(frozen=True, slots=True)
class ComplexityScore:
    """Result of complexity estimation.

    Contains both the normalized overall score and a breakdown of how
    each factor contributed to the final score.

    Attributes:
        score: Normalized complexity score between 0.0 and 1.0.
            - < 0.4: Low complexity (Frugal tier)
            - 0.4-0.7: Medium complexity (Standard tier)
            - > 0.7: High complexity (Frontier tier)
        breakdown: Dictionary showing individual factor contributions:
            - "token_score": Normalized token count contribution (0.0-1.0)
            - "tool_score": Normalized tool dependency contribution (0.0-1.0)
            - "depth_score": Normalized AC depth contribution (0.0-1.0)
            - "weighted_token": Token contribution after weight applied
            - "weighted_tool": Tool contribution after weight applied
            - "weighted_depth": Depth contribution after weight applied

    Example:
        score = ComplexityScore(
            score=0.65,
            breakdown={
                "token_score": 0.5,
                "tool_score": 0.6,
                "depth_score": 0.8,
                "weighted_token": 0.15,
                "weighted_tool": 0.18,
                "weighted_depth": 0.32,
            },
        )
    """

    score: float
    breakdown: dict[str, float]


def _normalize_token_count(token_count: int) -> float:
    """Normalize token count to 0.0-1.0 range.

    Args:
        token_count: Number of tokens in the task.

    Returns:
        Normalized score between 0.0 and 1.0.
    """
    if token_count <= 0:
        return 0.0
    if token_count >= MAX_TOKEN_THRESHOLD:
        return 1.0
    return token_count / MAX_TOKEN_THRESHOLD


def _normalize_tool_dependencies(tool_count: int) -> float:
    """Normalize tool dependency count to 0.0-1.0 range.

    Args:
        tool_count: Number of tool dependencies.

    Returns:
        Normalized score between 0.0 and 1.0.
    """
    if tool_count <= 0:
        return 0.0
    if tool_count >= MAX_TOOL_THRESHOLD:
        return 1.0
    return tool_count / MAX_TOOL_THRESHOLD


def _normalize_ac_depth(ac_depth: int) -> float:
    """Normalize AC nesting depth to 0.0-1.0 range.

    Args:
        ac_depth: Acceptance criteria nesting depth.

    Returns:
        Normalized score between 0.0 and 1.0.
    """
    if ac_depth <= 0:
        return 0.0
    if ac_depth >= MAX_DEPTH_THRESHOLD:
        return 1.0
    return ac_depth / MAX_DEPTH_THRESHOLD


def _validate_task_context(context: TaskContext) -> Result[None, ValidationError]:
    """Validate task context inputs.

    Args:
        context: The task context to validate.

    Returns:
        Result containing None on success or ValidationError on failure.
    """
    if context.token_count < 0:
        error = ValidationError(
            "Token count must be non-negative",
            field="token_count",
            value=context.token_count,
        )
        log.warning(
            "complexity.validation.failed",
            field="token_count",
            value=context.token_count,
        )
        return Result.err(error)

    if context.ac_depth < 0:
        error = ValidationError(
            "AC depth must be non-negative",
            field="ac_depth",
            value=context.ac_depth,
        )
        log.warning(
            "complexity.validation.failed",
            field="ac_depth",
            value=context.ac_depth,
        )
        return Result.err(error)

    return Result.ok(None)


def estimate_complexity(
    context: TaskContext,
) -> Result[ComplexityScore, ValidationError]:
    """Estimate the complexity of a task based on its context.

    Calculates a weighted complexity score from:
    - Token count: 30% weight
    - Tool dependency count: 30% weight
    - AC nesting depth: 40% weight

    Args:
        context: Task context containing complexity factors.

    Returns:
        Result containing ComplexityScore on success or ValidationError on failure.

    Example:
        context = TaskContext(
            token_count=2000,
            tool_dependencies=["git", "npm"],
            ac_depth=3,
        )
        result = estimate_complexity(context)
        if result.is_ok:
            print(f"Score: {result.value.score}")  # e.g., 0.59
    """
    # Validate input
    validation_result = _validate_task_context(context)
    if validation_result.is_err:
        return Result.err(validation_result.error)

    # Calculate normalized scores for each factor
    token_score = _normalize_token_count(context.token_count)
    tool_score = _normalize_tool_dependencies(len(context.tool_dependencies))
    depth_score = _normalize_ac_depth(context.ac_depth)

    # Apply weights
    weighted_token = token_score * WEIGHT_TOKEN_COUNT
    weighted_tool = tool_score * WEIGHT_TOOL_DEPENDENCIES
    weighted_depth = depth_score * WEIGHT_AC_DEPTH

    # Calculate final score
    final_score = weighted_token + weighted_tool + weighted_depth

    # Build breakdown for transparency
    breakdown = {
        "token_score": token_score,
        "tool_score": tool_score,
        "depth_score": depth_score,
        "weighted_token": weighted_token,
        "weighted_tool": weighted_tool,
        "weighted_depth": weighted_depth,
    }

    complexity_score = ComplexityScore(score=final_score, breakdown=breakdown)

    log.debug(
        "complexity.estimated",
        score=final_score,
        token_count=context.token_count,
        tool_count=len(context.tool_dependencies),
        ac_depth=context.ac_depth,
        breakdown=breakdown,
    )

    return Result.ok(complexity_score)
