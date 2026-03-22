"""Unit tests for LineageGuard — transition validation before persistence.

Tests the gated_append() flow:
- Non-lineage events pass through without validation
- lineage.created always passes
- Valid transitions are allowed
- Invalid transitions raise TransitionError
- Rewind re-enables generation events after convergence
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from ouroboros.core.errors import TransitionError
from ouroboros.core.lineage import LineageStatus
from ouroboros.events.base import BaseEvent
from ouroboros.events.lineage import (
    lineage_converged,
    lineage_created,
    lineage_exhausted,
    lineage_generation_started,
    lineage_rewound,
    lineage_stagnated,
)
from ouroboros.evolution.guard import LineageGuard


@pytest.fixture
def mock_event_store() -> AsyncMock:
    store = AsyncMock()
    store.append = AsyncMock()
    store.replay_lineage = AsyncMock(return_value=[])
    return store


@pytest.fixture
def guard(mock_event_store: AsyncMock) -> LineageGuard:
    return LineageGuard(mock_event_store)


class TestNonLineagePassthrough:
    """Non-lineage events bypass guard entirely."""

    @pytest.mark.asyncio
    async def test_drift_event_passes_through(
        self, guard: LineageGuard, mock_event_store: AsyncMock
    ) -> None:
        event = BaseEvent(
            type="drift.measured",
            aggregate_type="execution",
            aggregate_id="exec_123",
            data={},
        )
        await guard.gated_append(event)
        mock_event_store.append.assert_awaited_once_with(event)

    @pytest.mark.asyncio
    async def test_session_event_passes_through(
        self, guard: LineageGuard, mock_event_store: AsyncMock
    ) -> None:
        event = BaseEvent(
            type="orchestrator.session.started",
            aggregate_type="session",
            aggregate_id="sess_123",
            data={},
        )
        await guard.gated_append(event)
        mock_event_store.append.assert_awaited_once_with(event)


class TestCreatedAlwaysPasses:
    """lineage.created always passes without status check."""

    @pytest.mark.asyncio
    async def test_created_bypasses_validation(
        self, guard: LineageGuard, mock_event_store: AsyncMock
    ) -> None:
        event = lineage_created("lin_abc", "test goal")
        await guard.gated_append(event)
        mock_event_store.append.assert_awaited_once_with(event)
        # replay_lineage should NOT be called for created events
        mock_event_store.replay_lineage.assert_not_awaited()


class TestValidTransitions:
    """Events allowed by the transition matrix are stored."""

    @pytest.mark.asyncio
    async def test_active_allows_generation_started(
        self, guard: LineageGuard, mock_event_store: AsyncMock
    ) -> None:
        """ACTIVE lineage allows generation.started."""
        # No terminal events → status is ACTIVE
        mock_event_store.replay_lineage.return_value = []
        event = lineage_generation_started("lin_abc", 1, "wondering")
        await guard.gated_append(event)
        mock_event_store.append.assert_awaited_once_with(event)


class TestInvalidTransitions:
    """Events not in the transition matrix raise TransitionError."""

    @pytest.mark.asyncio
    async def test_converged_rejects_generation_started(
        self, guard: LineageGuard, mock_event_store: AsyncMock
    ) -> None:
        """CONVERGED lineage rejects generation.started."""
        # Simulate converged state
        mock_event_store.replay_lineage.return_value = [
            lineage_created("lin_abc", "goal"),
            lineage_converged("lin_abc", 3, "stable", 0.98),
        ]
        event = lineage_generation_started("lin_abc", 4, "wondering")
        with pytest.raises(TransitionError) as exc_info:
            await guard.gated_append(event)
        assert exc_info.value.current_status == "converged"
        assert exc_info.value.event_type == "lineage.generation.started"
        # Event should NOT be stored
        mock_event_store.append.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_exhausted_rejects_generation_started(
        self, guard: LineageGuard, mock_event_store: AsyncMock
    ) -> None:
        mock_event_store.replay_lineage.return_value = [
            lineage_created("lin_abc", "goal"),
            lineage_exhausted("lin_abc", 30, 30),
        ]
        event = lineage_generation_started("lin_abc", 31, "wondering")
        with pytest.raises(TransitionError):
            await guard.gated_append(event)

    @pytest.mark.asyncio
    async def test_stagnated_maps_to_converged(
        self, guard: LineageGuard, mock_event_store: AsyncMock
    ) -> None:
        """lineage.stagnated results in CONVERGED status (same as projector)."""
        mock_event_store.replay_lineage.return_value = [
            lineage_created("lin_abc", "goal"),
            lineage_stagnated("lin_abc", 5, "Stagnation detected", 3),
        ]
        event = lineage_generation_started("lin_abc", 6, "wondering")
        with pytest.raises(TransitionError) as exc_info:
            await guard.gated_append(event)
        assert exc_info.value.current_status == "converged"


class TestRewindReactivation:
    """Rewind restores ACTIVE status, enabling new generation events."""

    @pytest.mark.asyncio
    async def test_rewind_after_converged_allows_generation(
        self, guard: LineageGuard, mock_event_store: AsyncMock
    ) -> None:
        # Converged, then rewound → ACTIVE
        mock_event_store.replay_lineage.return_value = [
            lineage_created("lin_abc", "goal"),
            lineage_converged("lin_abc", 3, "stable", 0.98),
            lineage_rewound("lin_abc", 3, 2),
        ]
        event = lineage_generation_started("lin_abc", 3, "wondering")
        await guard.gated_append(event)
        mock_event_store.append.assert_awaited_once_with(event)

    @pytest.mark.asyncio
    async def test_converged_allows_rewound(
        self, guard: LineageGuard, mock_event_store: AsyncMock
    ) -> None:
        mock_event_store.replay_lineage.return_value = [
            lineage_created("lin_abc", "goal"),
            lineage_converged("lin_abc", 3, "stable", 0.98),
        ]
        event = lineage_rewound("lin_abc", 3, 2)
        await guard.gated_append(event)
        mock_event_store.append.assert_awaited_once_with(event)
