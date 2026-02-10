"""Unit tests for Model Router.

Tests cover:
- RoutingContext dataclass
- RoutingRecord dataclass
- ModelRouter class initialization
- Routing with complexity estimation
- History-based routing
- Escalation logic
- Cost optimization
- Statistics tracking
"""

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from ouroboros.plugin.orchestration.router import (
    ModelRouter,
    RoutingContext,
    RoutingRecord,
)
from ouroboros.routing.complexity import TaskContext
from ouroboros.routing.router import PALRouter, RoutingDecision
from ouroboros.routing.tiers import Tier


class TestRoutingContext:
    """Test RoutingContext dataclass."""

    def test_create_routing_context_minimal(self) -> None:
        """Test creating minimal RoutingContext."""
        context = RoutingContext(
            task_type="code",
            token_estimate=1000,
            tool_count=2,
            ac_depth=1,
        )

        assert context.task_type == "code"
        assert context.token_estimate == 1000
        assert context.tool_count == 2
        assert context.ac_depth == 1
        assert context.has_dependencies is False
        assert context.previous_failures == 0
        assert context.previous_successes == 0
        assert context.metadata == {}

    def test_create_routing_context_full(self) -> None:
        """Test creating RoutingContext with all fields."""
        context = RoutingContext(
            task_type="research",
            token_estimate=5000,
            tool_count=5,
            ac_depth=4,
            has_dependencies=True,
            previous_failures=2,
            previous_successes=5,
            metadata={"complexity": "high"},
        )

        assert context.task_type == "research"
        assert context.has_dependencies is True
        assert context.previous_failures == 2
        assert context.previous_successes == 5
        assert context.metadata == {"complexity": "high"}

    def test_routing_context_to_task_context(self) -> None:
        """Test converting RoutingContext to TaskContext."""
        routing_ctx = RoutingContext(
            task_type="code",
            token_estimate=2000,
            tool_count=3,
            ac_depth=2,
        )

        task_ctx = routing_ctx.to_task_context()

        assert task_ctx.token_count == 2000
        assert len(task_ctx.tool_dependencies) == 3
        assert task_ctx.ac_depth == 2

    def test_routing_context_is_frozen(self) -> None:
        """Test RoutingContext is immutable."""
        context = RoutingContext(
            task_type="test",
            token_estimate=100,
            tool_count=1,
            ac_depth=1,
        )

        with pytest.raises(Exception):
            context.task_type = "changed"  # type: ignore[misc]

    def test_token_range_categories(self) -> None:
        """Test token range categorization."""
        assert RoutingContext(
            task_type="t", token_estimate=100, tool_count=1, ac_depth=1
        )._token_range == "tiny"
        assert RoutingContext(
            task_type="t", token_estimate=1000, tool_count=1, ac_depth=1
        )._token_range == "small"
        assert RoutingContext(
            task_type="t", token_estimate=3000, tool_count=1, ac_depth=1
        )._token_range == "medium"
        assert RoutingContext(
            task_type="t", token_estimate=6000, tool_count=1, ac_depth=1
        )._token_range == "large"

    def test_tool_range_categories(self) -> None:
        """Test tool count range categorization."""
        assert RoutingContext(
            task_type="t", token_estimate=100, tool_count=0, ac_depth=1
        )._tool_range == "none"
        assert RoutingContext(
            task_type="t", token_estimate=100, tool_count=1, ac_depth=1
        )._tool_range == "few"
        assert RoutingContext(
            task_type="t", token_estimate=100, tool_count=4, ac_depth=1
        )._tool_range == "some"
        assert RoutingContext(
            task_type="t", token_estimate=100, tool_count=7, ac_depth=1
        )._tool_range == "many"

    def test_depth_range_categories(self) -> None:
        """Test AC depth range categorization."""
        assert RoutingContext(
            task_type="t", token_estimate=100, tool_count=1, ac_depth=0
        )._depth_range == "none"
        assert RoutingContext(
            task_type="t", token_estimate=100, tool_count=1, ac_depth=1
        )._depth_range == "shallow"
        assert RoutingContext(
            task_type="t", token_estimate=100, tool_count=1, ac_depth=3
        )._depth_range == "medium"
        assert RoutingContext(
            task_type="t", token_estimate=100, tool_count=1, ac_depth=5
        )._depth_range == "deep"


class TestRoutingRecord:
    """Test RoutingRecord dataclass."""

    def test_create_routing_record(self) -> None:
        """Test creating a RoutingRecord."""
        record = RoutingRecord(
            context_hash="abc123",
            tier=Tier.STANDARD,
            success=True,
            timestamp=datetime(2024, 1, 1, tzinfo=UTC),
            duration_seconds=5.5,
        )

        assert record.context_hash == "abc123"
        assert record.tier == Tier.STANDARD
        assert record.success is True
        assert record.duration_seconds == 5.5

    def test_routing_record_defaults(self) -> None:
        """Test RoutingRecord default values."""
        record = RoutingRecord(
            context_hash="xyz",
            tier=Tier.FRUGAL,
            success=False,
            timestamp=datetime.now(UTC),
        )

        assert record.duration_seconds == 0.0


class TestModelRouterInit:
    """Test ModelRouter initialization."""

    def test_router_initializes(self) -> None:
        """Test ModelRouter initialization."""
        router = ModelRouter()

        assert router._total_routes == 0
        assert router._cost_optimization is False
        assert router._history == {}

    def test_router_has_pal_router(self) -> None:
        """Test router has PALRouter instance."""
        router = ModelRouter()

        assert isinstance(router._pal_router, PALRouter)


class TestModelRouterRoute:
    """Test ModelRouter.route method."""

    async def test_route_simple_task_to_frugal(self) -> None:
        """Test routing simple task to Frugal tier."""
        router = ModelRouter()

        context = RoutingContext(
            task_type="code",
            token_estimate=100,
            tool_count=1,
            ac_depth=1,
        )

        tier = await router.route(context)

        assert tier == Tier.FRUGAL

    async def test_route_complex_task_to_frontier(self) -> None:
        """Test routing complex task to Frontier tier."""
        router = ModelRouter()

        context = RoutingContext(
            task_type="code",
            token_estimate=10000,
            tool_count=10,
            ac_depth=10,
        )

        tier = await router.route(context)

        assert tier == Tier.FRONTIER

    async def test_route_increments_total_routes(self) -> None:
        """Test that routing increments counter."""
        router = ModelRouter()

        context = RoutingContext(
            task_type="test",
            token_estimate=100,
            tool_count=1,
            ac_depth=1,
        )

        initial_count = router._total_routes
        await router.route(context)

        assert router._total_routes == initial_count + 1

    async def test_route_with_cost_optimization_downgrades(self) -> None:
        """Test cost optimization can downgrade tier."""
        router = ModelRouter()
        router.set_cost_optimization(enabled=True)

        # Medium complexity would normally route to STANDARD
        context = RoutingContext(
            task_type="code",
            token_estimate=2000,
            tool_count=3,
            ac_depth=2,
        )

        tier = await router.route(context)

        # With cost optimization, might be downgraded to FRUGAL
        assert tier in [Tier.FRUGAL, Tier.STANDARD]

    async def test_route_from_history_uses_previous_tier(self) -> None:
        """Test routing uses history for similar tasks."""
        router = ModelRouter()

        context = RoutingContext(
            task_type="code",
            token_estimate=1000,
            tool_count=2,
            ac_depth=2,
        )

        # Record a successful route to STANDARD
        context_hash = context._hash()
        router._history[context_hash] = [
            RoutingRecord(
                context_hash=context_hash,
                tier=Tier.STANDARD,
                success=True,
                timestamp=datetime.now(UTC),
            )
        ]

        tier = await router.route(context)

        # Should use the previous successful tier
        assert tier == Tier.STANDARD


class TestModelRouterHistory:
    """Test ModelRouter history-based routing."""

    async def test_route_from_history_with_failures_escalates(self) -> None:
        """Test repeated failures cause escalation."""
        router = ModelRouter()

        context = RoutingContext(
            task_type="code",
            token_estimate=100,
            tool_count=1,
            ac_depth=1,
        )

        context_hash = context._hash()

        # Record multiple FRUGAL failures
        router._history[context_hash] = [
            RoutingRecord(
                context_hash=context_hash,
                tier=Tier.FRUGAL,
                success=False,
                timestamp=datetime.now(UTC),
            ),
            RoutingRecord(
                context_hash=context_hash,
                tier=Tier.FRUGAL,
                success=False,
                timestamp=datetime.now(UTC),
            ),
        ]

        tier = await router.route(context)

        # Should escalate to STANDARD
        assert tier == Tier.STANDARD

    async def test_route_from_history_escalates_frugal_to_standard(self) -> None:
        """Test escalation from FRUGAL to STANDARD."""
        router = ModelRouter()

        context = RoutingContext(
            task_type="test",
            token_estimate=100,
            tool_count=1,
            ac_depth=1,
        )

        context_hash = context._hash()
        router._history[context_hash] = [
            RoutingRecord(
                context_hash=context_hash,
                tier=Tier.FRUGAL,
                success=False,
                timestamp=datetime.now(UTC),
            ),
            RoutingRecord(
                context_hash=context_hash,
                tier=Tier.FRUGAL,
                success=False,
                timestamp=datetime.now(UTC),
            ),
        ]

        tier = await router.route(context)

        assert tier == Tier.STANDARD

    async def test_route_from_history_escalates_standard_to_frontier(self) -> None:
        """Test escalation from STANDARD to FRONTIER."""
        router = ModelRouter()

        context = RoutingContext(
            task_type="test",
            token_estimate=100,
            tool_count=1,
            ac_depth=1,
        )

        context_hash = context._hash()
        router._history[context_hash] = [
            RoutingRecord(
                context_hash=context_hash,
                tier=Tier.STANDARD,
                success=False,
                timestamp=datetime.now(UTC),
            ),
            RoutingRecord(
                context_hash=context_hash,
                tier=Tier.STANDARD,
                success=False,
                timestamp=datetime.now(UTC),
            ),
        ]

        tier = await router.route(context)

        assert tier == Tier.FRONTIER

    async def test_route_from_history_uses_last_successful(self) -> None:
        """Test uses last successful tier from history."""
        router = ModelRouter()

        context = RoutingContext(
            task_type="test",
            token_estimate=100,
            tool_count=1,
            ac_depth=1,
        )

        context_hash = context._hash()
        router._history[context_hash] = [
            RoutingRecord(
                context_hash=context_hash,
                tier=Tier.FRUGAL,
                success=True,
                timestamp=datetime.now(UTC),
            ),
            RoutingRecord(
                context_hash=context_hash,
                tier=Tier.FRUGAL,
                success=False,
                timestamp=datetime.now(UTC),
            ),
        ]

        tier = await router.route(context)

        # Should use the successful tier
        assert tier == Tier.FRUGAL


class TestModelRouterRecordResult:
    """Test ModelRouter.record_result method."""

    async def test_record_result_stores_in_history(self) -> None:
        """Test recording result stores in history."""
        router = ModelRouter()

        context = RoutingContext(
            task_type="code",
            token_estimate=100,
            tool_count=1,
            ac_depth=1,
        )

        await router.record_result(
            context=context,
            tier=Tier.FRUGAL,
            success=True,
            duration_seconds=1.5,
        )

        context_hash = context._hash()
        assert context_hash in router._history
        assert len(router._history[context_hash]) == 1
        assert router._history[context_hash][0].success is True
        assert router._history[context_hash][0].tier == Tier.FRUGAL

    async def test_record_result_prunes_old_records(self) -> None:
        """Test old records are pruned when limit exceeded."""
        router = ModelRouter()

        context = RoutingContext(
            task_type="test",
            token_estimate=100,
            tool_count=1,
            ac_depth=1,
        )

        context_hash = context._hash()

        # Add more records than MAX_HISTORY_PER_HASH
        for _ in range(router.MAX_HISTORY_PER_HASH + 5):
            await router.record_result(
                context=context,
                tier=Tier.FRUGAL,
                success=True,
            )

        # Should be pruned to max
        assert len(router._history[context_hash]) <= router.MAX_HISTORY_PER_HASH


class TestModelRouterPruneHistory:
    """Test ModelRouter._prune_history method."""

    async def test_prune_history_removes_old_contexts(self) -> None:
        """Test pruning removes oldest contexts when total exceeded."""
        router = ModelRouter()

        # Create multiple contexts with records
        for i in range(15):  # More than MAX_TOTAL_HISTORY
            context = RoutingContext(
                task_type=f"task-{i}",
                token_estimate=100,
                tool_count=1,
                ac_depth=1,
            )
            await router.record_result(
                context=context,
                tier=Tier.FRUGAL,
                success=True,
            )

        # Total should be pruned
        total_records = sum(len(v) for v in router._history.values())
        assert total_records <= router.MAX_TOTAL_HISTORY


class TestModelRouterSetCostOptimization:
    """Test ModelRouter.set_cost_optimization method."""

    def test_set_cost_optimization_enables(self) -> None:
        """Test enabling cost optimization."""
        router = ModelRouter()
        router.set_cost_optimization(enabled=True)

        assert router._cost_optimization is True

    def test_set_cost_optimization_disables(self) -> None:
        """Test disabling cost optimization."""
        router = ModelRouter()
        router.set_cost_optimization(enabled=True)
        router.set_cost_optimization(enabled=False)

        assert router._cost_optimization is False


class TestModelRouterGetStatistics:
    """Test ModelRouter.get_statistics method."""

    def test_get_statistics_empty_history(self) -> None:
        """Test statistics with empty history."""
        router = ModelRouter()

        stats = router.get_statistics()

        assert stats["total_routes"] == 0
        assert stats["unique_contexts"] == 0
        assert stats["total_records"] == 0
        # cost_optimization_enabled only included when history exists
        assert stats.get("cost_optimization_enabled", False) is False

    def test_get_statistics_with_routes(self) -> None:
        """Test statistics after routing."""
        router = ModelRouter()
        router._total_routes = 10

        stats = router.get_statistics()

        assert stats["total_routes"] == 10

    async def test_get_statistics_with_history(self) -> None:
        """Test statistics include history data."""
        router = ModelRouter()

        context = RoutingContext(
            task_type="test",
            token_estimate=100,
            tool_count=1,
            ac_depth=1,
        )

        # Record some results
        await router.record_result(context, Tier.FRUGAL, True)
        await router.record_result(context, Tier.FRUGAL, True)
        await router.record_result(context, Tier.FRUGAL, False)

        stats = router.get_statistics()

        assert stats["unique_contexts"] == 1
        assert stats["total_records"] == 3
        assert stats["successful_routes"] == 2
        assert stats["success_rate"] == pytest.approx(2/3)


class TestModelRouterClearHistory:
    """Test ModelRouter.clear_history method."""

    async def test_clear_history_removes_all(self) -> None:
        """Test clearing history removes all records."""
        router = ModelRouter()

        context = RoutingContext(
            task_type="test",
            token_estimate=100,
            tool_count=1,
            ac_depth=1,
        )

        await router.record_result(context, Tier.FRUGAL, True)

        router.clear_history()

        assert router._history == {}


class TestModelRouterEscalation:
    """Test ModelRouter escalation logic."""

    async def test_escalation_threshold_constant(self) -> None:
        """Test escalation threshold is defined."""
        router = ModelRouter()

        assert router.ESCALATION_AFTER_FAILURES == 2
