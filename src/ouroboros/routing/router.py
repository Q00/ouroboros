"""PAL Router for tier selection based on task complexity.

This module implements the PAL (Progressive Adaptive LLM) router that determines
which model tier should handle a task based on its estimated complexity.

Routing Thresholds:
- Complexity < 0.4: Frugal tier (simple tasks)
- Complexity 0.4-0.7: Standard tier (moderate tasks)
- Complexity > 0.7: Frontier tier (complex tasks)

Design Principles:
- Stateless: The router holds no internal state. All decisions are based purely
  on the input TaskContext passed to the route() method.
- Pure Function: Given the same input, the router will always produce the same
  output. This enables easy testing and predictable behavior.
- Result Type: Returns Result type for consistent error handling.

Usage:
    from ouroboros.routing.router import PALRouter
    from ouroboros.routing.complexity import TaskContext

    router = PALRouter()

    # Route a simple task
    context = TaskContext(token_count=100, tool_dependencies=[], ac_depth=1)
    result = router.route(context)
    if result.is_ok:
        tier = result.value
        print(f"Route to: {tier.value}")  # "frugal"

    # Alternatively, use the convenience function
    from ouroboros.routing.router import route_task
    result = route_task(context)
"""

from dataclasses import dataclass

from ouroboros.core.errors import ValidationError
from ouroboros.core.types import Result
from ouroboros.observability.logging import get_logger
from ouroboros.routing.complexity import (
    ComplexityScore,
    TaskContext,
    estimate_complexity,
)
from ouroboros.routing.tiers import Tier

log = get_logger(__name__)


# Routing thresholds
THRESHOLD_FRUGAL = 0.4  # Below this -> Frugal
THRESHOLD_STANDARD = 0.7  # Below this (but >= FRUGAL) -> Standard, above -> Frontier


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    """Result of a routing decision.

    Contains the selected tier and the complexity analysis that led to the decision.

    Attributes:
        tier: The selected model tier (Frugal, Standard, or Frontier).
        complexity: The complexity score that determined the routing.

    Example:
        decision = RoutingDecision(
            tier=Tier.STANDARD,
            complexity=ComplexityScore(score=0.55, breakdown={...}),
        )
        print(f"Route to {decision.tier.value} (score: {decision.complexity.score})")
    """

    tier: Tier
    complexity: ComplexityScore


def _select_tier_from_score(score: float) -> Tier:
    """Select the appropriate tier based on complexity score.

    Args:
        score: Complexity score between 0.0 and 1.0.

    Returns:
        The appropriate Tier based on threshold comparison.
    """
    if score < THRESHOLD_FRUGAL:
        return Tier.FRUGAL
    if score < THRESHOLD_STANDARD:
        return Tier.STANDARD
    return Tier.FRONTIER


class PALRouter:
    """Stateless router for tier selection based on task complexity.

    The PAL Router determines which model tier should handle a task by:
    1. Estimating task complexity from the provided context
    2. Comparing complexity to routing thresholds
    3. Returning the appropriate tier

    This router is completely stateless - it holds no internal state and makes
    all decisions based purely on the input provided to each method call.
    This design enables:
    - Easy unit testing with predictable outputs
    - Thread-safe operation without synchronization
    - Simple reasoning about behavior

    Routing Thresholds:
    - Complexity < 0.4: Frugal tier
    - Complexity 0.4-0.7: Standard tier
    - Complexity > 0.7: Frontier tier

    Example:
        router = PALRouter()

        # Route a simple task
        simple_context = TaskContext(token_count=200, tool_dependencies=[], ac_depth=1)
        result = router.route(simple_context)
        assert result.value.tier == Tier.FRUGAL

        # Route a complex task
        complex_context = TaskContext(
            token_count=5000,
            tool_dependencies=["git", "docker", "npm", "aws"],
            ac_depth=5,
        )
        result = router.route(complex_context)
        assert result.value.tier == Tier.FRONTIER
    """

    def route(
        self,
        context: TaskContext,
    ) -> Result[RoutingDecision, ValidationError]:
        """Route a task to the appropriate tier based on complexity.

        This is a pure function - given the same context, it will always
        return the same routing decision.

        Args:
            context: Task context containing complexity factors.
                All routing decisions are based solely on this input.

        Returns:
            Result containing RoutingDecision on success or ValidationError on failure.

        Example:
            router = PALRouter()
            context = TaskContext(
                token_count=1000,
                tool_dependencies=["git"],
                ac_depth=2,
            )
            result = router.route(context)
            if result.is_ok:
                decision = result.value
                print(f"Tier: {decision.tier.value}")
                print(f"Score: {decision.complexity.score:.2f}")
        """
        # Estimate complexity
        complexity_result = estimate_complexity(context)
        if complexity_result.is_err:
            return Result.err(complexity_result.error)

        complexity = complexity_result.value

        # Select tier based on score
        tier = _select_tier_from_score(complexity.score)

        log.info(
            "routing.decision.made",
            tier=tier.value,
            complexity_score=complexity.score,
            token_count=context.token_count,
            tool_count=len(context.tool_dependencies),
            ac_depth=context.ac_depth,
        )

        return Result.ok(RoutingDecision(tier=tier, complexity=complexity))


def route_task(context: TaskContext) -> Result[RoutingDecision, ValidationError]:
    """Convenience function to route a task without instantiating PALRouter.

    This is a pure function wrapper around PALRouter.route() for simple use cases.

    Args:
        context: Task context containing complexity factors.

    Returns:
        Result containing RoutingDecision on success or ValidationError on failure.

    Example:
        from ouroboros.routing.router import route_task
        from ouroboros.routing.complexity import TaskContext

        context = TaskContext(token_count=500, tool_dependencies=["git"], ac_depth=2)
        result = route_task(context)
        if result.is_ok:
            print(f"Route to: {result.value.tier.value}")
    """
    router = PALRouter()
    return router.route(context)
