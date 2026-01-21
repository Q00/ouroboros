"""Routing module for Ouroboros.

This module handles model tier routing and selection, including:
- Tier enumeration and configuration (Frugal, Standard, Frontier)
- Complexity estimation for routing decisions
- PAL (Progressive Adaptive LLM) router for automatic tier selection
- Escalation on failure with automatic tier upgrades
- Downgrade on success for cost optimization
"""

from ouroboros.routing.complexity import (
    ComplexityScore,
    TaskContext,
    estimate_complexity,
)
from ouroboros.routing.downgrade import (
    DOWNGRADE_THRESHOLD,
    SIMILARITY_THRESHOLD,
    DowngradeManager,
    DowngradeResult,
    PatternMatcher,
    SuccessTracker,
)
from ouroboros.routing.escalation import (
    FAILURE_THRESHOLD,
    EscalationAction,
    EscalationManager,
    FailureTracker,
    StagnationEvent,
)
from ouroboros.routing.router import PALRouter, RoutingDecision, route_task
from ouroboros.routing.tiers import Tier, get_model_for_tier, get_tier_config

__all__ = [
    # Tiers
    "Tier",
    "get_model_for_tier",
    "get_tier_config",
    # Complexity
    "TaskContext",
    "ComplexityScore",
    "estimate_complexity",
    # Router
    "PALRouter",
    "RoutingDecision",
    "route_task",
    # Escalation
    "EscalationManager",
    "EscalationAction",
    "FailureTracker",
    "StagnationEvent",
    "FAILURE_THRESHOLD",
    # Downgrade
    "DowngradeManager",
    "DowngradeResult",
    "SuccessTracker",
    "PatternMatcher",
    "DOWNGRADE_THRESHOLD",
    "SIMILARITY_THRESHOLD",
]
