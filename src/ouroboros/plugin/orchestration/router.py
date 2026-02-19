"""Model Router for Progressive Adaptive LLM (PAL) routing.

This module provides intelligent model tier selection based on:
- Task complexity estimation
- Historical performance data
- Cost optimization targets
- Fallback logic for failures

Architecture:
- Extends routing.complexity for complexity estimation
- Uses routing.router PALRouter as base
- Adds learning from historical results

Usage:
    router = ModelRouter()

    # Route a task
    context = RoutingContext(
        task_type="code",
        token_estimate=2000,
        tool_count=3,
        ac_depth=2,
    )
    tier = await router.route(context)

    # Record result for learning
    await router.record_result(context, tier, success=True)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
import json
from typing import Any

from ouroboros.observability.logging import get_logger
from ouroboros.routing.complexity import TaskContext
from ouroboros.routing.router import PALRouter
from ouroboros.routing.tiers import Tier

log = get_logger(__name__)


# =============================================================================
# Routing Context
# =============================================================================


@dataclass(frozen=True, slots=True)
class RoutingContext:
    """Context for routing decisions.

    Attributes:
        task_type: Type of task (code, research, analysis, etc.).
        token_estimate: Estimated token count.
        tool_count: Number of tools needed.
        ac_depth: Acceptance criteria depth.
        has_dependencies: Whether task has dependencies.
        previous_failures: Number of previous failures.
        previous_successes: Number of previous successes.
        metadata: Additional context data.
    """

    task_type: str
    token_estimate: int
    tool_count: int
    ac_depth: int
    has_dependencies: bool = False
    previous_failures: int = 0
    previous_successes: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_task_context(self) -> TaskContext:
        """Convert to TaskContext for complexity estimation."""
        return TaskContext(
            token_count=self.token_estimate,
            tool_dependencies=[f"tool_{i}" for i in range(self.tool_count)],
            ac_depth=self.ac_depth,
        )

    def _hash(self) -> str:
        """Generate hash for history lookup."""
        data = {
            "task_type": self.task_type,
            "token_range": self._token_range,
            "tool_range": self._tool_range,
            "depth_range": self._depth_range,
        }
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]

    @property
    def _token_range(self) -> str:
        """Categorize token count into ranges."""
        if self.token_estimate < 500:
            return "tiny"
        if self.token_estimate < 2000:
            return "small"
        if self.token_estimate < 5000:
            return "medium"
        return "large"

    @property
    def _tool_range(self) -> str:
        """Categorize tool count into ranges."""
        if self.tool_count == 0:
            return "none"
        if self.tool_count <= 2:
            return "few"
        if self.tool_count <= 5:
            return "some"
        return "many"

    @property
    def _depth_range(self) -> str:
        """Categorize AC depth into ranges."""
        if self.ac_depth == 0:
            return "none"
        if self.ac_depth <= 2:
            return "shallow"
        if self.ac_depth <= 4:
            return "medium"
        return "deep"


# =============================================================================
# Routing History
# =============================================================================


@dataclass(slots=True)
class RoutingRecord:
    """Record of a routing decision and outcome.

    Attributes:
        context_hash: Hash of the routing context.
        tier: Tier that was selected.
        success: Whether execution succeeded.
        timestamp: When the routing occurred.
        duration_seconds: Execution duration.
    """

    context_hash: str
    tier: Tier
    success: bool
    timestamp: datetime
    duration_seconds: float = 0.0


# =============================================================================
# Model Router
# =============================================================================


class ModelRouter:
    """Progressive Adaptive LLM (PAL) Router with learning.

    The router selects appropriate model tiers based on:
    1. Task complexity (using existing complexity estimation)
    2. Historical performance for similar tasks
    3. Failure escalation logic
    4. Cost optimization preferences

    Features:
    - Learns from past routing decisions
    - Escalates tier on repeated failures
    - Provides fallback logic
    - Tracks performance metrics

    Example:
        router = ModelRouter()

        # Simple routing
        context = RoutingContext(
            task_type="code",
            token_estimate=1500,
            tool_count=3,
            ac_depth=2,
        )
        tier = await router.route(context)
        print(f"Selected tier: {tier.value}")

        # With cost optimization
        router.set_cost_optimization(enabled=True)
        tier = await router.route(context)

        # Record results for learning
        await router.record_result(context, tier, success=True)

        # Get statistics
        stats = router.get_statistics()
        print(f"Total routes: {stats['total_routes']}")
    """

    # Maximum history records per context hash
    MAX_HISTORY_PER_HASH = 10

    # Maximum total history records
    MAX_TOTAL_HISTORY = 1000

    # Escalation thresholds
    ESCALATION_AFTER_FAILURES = 2

    def __init__(self) -> None:
        """Initialize the model router."""
        self._pal_router = PALRouter()
        self._history: dict[str, list[RoutingRecord]] = {}
        self._cost_optimization = False
        self._total_routes = 0

        log.info("model_router.initialized")

    async def route(self, context: RoutingContext) -> Tier:
        """Route a task to the appropriate model tier.

        Routing logic:
        1. Check history for similar tasks
        2. Apply escalation if repeated failures
        3. Estimate complexity for new tasks
        4. Apply cost optimization if enabled

        Args:
            context: Routing context containing task information.

        Returns:
            Selected Tier (FRUGAL, STANDARD, or FRONTIER).
        """
        self._total_routes += 1

        # Get context hash for history lookup
        context_hash = context._hash()

        # Check history for escalation
        tier = self._route_from_history(context_hash, context.previous_failures)

        if tier is None:
            # New task type - use complexity estimation
            task_context = context.to_task_context()
            result = self._pal_router.route(task_context)

            if result.is_ok:
                tier = result.value.tier
            else:
                # Fallback to STANDARD on error
                log.warning(
                    "model_router.complexity_failed",
                    error=str(result.error),
                )
                tier = Tier.STANDARD

            # Apply cost optimization if enabled
            if self._cost_optimization and tier != Tier.FRUGAL:
                # Try downgrading one tier
                if tier == Tier.FRONTIER:
                    tier = Tier.STANDARD
                elif tier == Tier.STANDARD:
                    tier = Tier.FRUGAL

        log.debug(
            "model_router.routed",
            context_hash=context_hash,
            tier=tier.value,
            task_type=context.task_type,
            cost_optimized=self._cost_optimization,
        )

        return tier

    def _route_from_history(self, context_hash: str, previous_failures: int) -> Tier | None:
        """Check history for similar tasks and apply escalation.

        Args:
            context_hash: Hash of routing context.
            previous_failures: Number of previous failures.

        Returns:
            Tier from history or None if no history available.
        """
        if context_hash not in self._history:
            return None

        records = self._history[context_hash]

        # Check for recent failures
        recent = records[-self.ESCALATION_AFTER_FAILURES :]
        recent_failures = sum(1 for r in recent if not r.success)

        # Escalate if repeated failures
        if recent_failures >= self.ESCALATION_AFTER_FAILURES:
            last_tier = records[-1].tier
            if last_tier == Tier.FRUGAL:
                return Tier.STANDARD
            elif last_tier == Tier.STANDARD:
                return Tier.FRONTIER

        # Use last successful tier
        for record in reversed(records):
            if record.success:
                return record.tier

        # Default to STANDARD if all failed
        return Tier.STANDARD

    async def record_result(
        self,
        context: RoutingContext,
        tier: Tier,
        success: bool,
        duration_seconds: float = 0.0,
    ) -> None:
        """Record routing result for learning.

        Args:
            context: The routing context used.
            tier: The tier that was selected.
            success: Whether execution succeeded.
            duration_seconds: Execution duration.
        """
        context_hash = context._hash()

        record = RoutingRecord(
            context_hash=context_hash,
            tier=tier,
            success=success,
            timestamp=datetime.now(UTC),
            duration_seconds=duration_seconds,
        )

        if context_hash not in self._history:
            self._history[context_hash] = []

        self._history[context_hash].append(record)

        # Prune old records
        if len(self._history[context_hash]) > self.MAX_HISTORY_PER_HASH:
            self._history[context_hash] = self._history[context_hash][-self.MAX_HISTORY_PER_HASH :]

        # Prune total history
        total_records = sum(len(v) for v in self._history.values())
        if total_records > self.MAX_TOTAL_HISTORY:
            self._prune_history()

        log.debug(
            "model_router.result_recorded",
            context_hash=context_hash,
            tier=tier.value,
            success=success,
        )

    def _prune_history(self) -> None:
        """Prune history to maintain maximum size."""
        # Sort context hashes by last access time
        hashes_by_time = sorted(
            self._history.keys(),
            key=lambda h: self._history[h][-1].timestamp if self._history[h] else datetime.min,
        )

        # Remove oldest 20%
        to_remove = len(hashes_by_time) // 5
        for h in hashes_by_time[:to_remove]:
            del self._history[h]

        log.debug(
            "model_router.history_pruned",
            removed=to_remove,
        )

    def set_cost_optimization(self, enabled: bool) -> None:
        """Enable or disable cost optimization.

        When enabled, the router will prefer lower tiers when
        complexity permits.

        Args:
            enabled: Whether to enable cost optimization.
        """
        self._cost_optimization = enabled
        log.info(
            "model_router.cost_optimization_set",
            enabled=enabled,
        )

    def get_statistics(self) -> dict[str, Any]:
        """Get routing statistics.

        Returns:
            Dictionary with routing metrics.
        """
        if not self._history:
            return {
                "total_routes": self._total_routes,
                "unique_contexts": 0,
                "total_records": 0,
            }

        total_records = sum(len(v) for v in self._history.values())
        successful = sum(sum(1 for r in records if r.success) for records in self._history.values())

        tier_counts: dict[str, int] = {t.value: 0 for t in Tier}
        for records in self._history.values():
            for record in records:
                tier_counts[record.tier.value] += 1

        return {
            "total_routes": self._total_routes,
            "unique_contexts": len(self._history),
            "total_records": total_records,
            "successful_routes": successful,
            "success_rate": successful / total_records if total_records > 0 else 0.0,
            "tier_distribution": tier_counts,
            "cost_optimization_enabled": self._cost_optimization,
        }

    def clear_history(self) -> None:
        """Clear all routing history."""
        self._history.clear()
        log.info("model_router.history_cleared")


__all__ = [
    "ModelRouter",
    "RoutingContext",
    "RoutingRecord",
]
