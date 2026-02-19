"""Unit tests for ouroboros.persistence.uow module."""

from pathlib import Path

import pytest

from ouroboros.events.base import BaseEvent
from ouroboros.persistence.checkpoint import CheckpointData, CheckpointStore
from ouroboros.persistence.event_store import EventStore
from ouroboros.persistence.uow import PhaseTransaction, UnitOfWork


@pytest.fixture
async def event_store(tmp_path: Path) -> EventStore:
    """Create an EventStore with a temporary database."""
    db_path = tmp_path / "test_events.db"
    store = EventStore(f"sqlite+aiosqlite:///{db_path}")
    await store.initialize()
    yield store
    await store.close()


@pytest.fixture
def checkpoint_store(tmp_path: Path) -> CheckpointStore:
    """Create a CheckpointStore with a temporary directory."""
    store = CheckpointStore(base_path=tmp_path / "checkpoints")
    store.initialize()
    return store


@pytest.fixture
async def uow(event_store: EventStore, checkpoint_store: CheckpointStore) -> UnitOfWork:
    """Create a UnitOfWork instance."""
    return UnitOfWork(event_store, checkpoint_store)


def create_sample_event() -> BaseEvent:
    """Create a sample event for testing with unique ID."""
    return BaseEvent(
        type="test.event.created",
        aggregate_type="test",
        aggregate_id="test-123",
        data={"key": "value"},
    )


@pytest.fixture
def sample_event() -> BaseEvent:
    """Create a sample event for testing."""
    return create_sample_event()


class TestUnitOfWork:
    """Test UnitOfWork pattern."""

    async def test_add_event_accumulates_pending_events(self, uow: UnitOfWork) -> None:
        """UnitOfWork.add_event() accumulates events."""
        assert uow.pending_event_count == 0

        uow.add_event(create_sample_event())
        assert uow.pending_event_count == 1

        uow.add_event(create_sample_event())
        assert uow.pending_event_count == 2

    async def test_add_events_accumulates_multiple_events(self, uow: UnitOfWork) -> None:
        """UnitOfWork.add_events() accumulates multiple events."""
        events = [create_sample_event() for _ in range(3)]

        uow.add_events(events)
        assert uow.pending_event_count == 3

    async def test_has_pending_events_returns_true_when_events_exist(self, uow: UnitOfWork) -> None:
        """UnitOfWork.has_pending_events() returns True when events exist."""
        assert not uow.has_pending_events()

        uow.add_event(create_sample_event())
        assert uow.has_pending_events()

    async def test_commit_persists_events(
        self,
        uow: UnitOfWork,
        sample_event: BaseEvent,
        event_store: EventStore,
    ) -> None:
        """UnitOfWork.commit() persists events to EventStore."""
        uow.add_event(sample_event)

        result = await uow.commit()
        assert result.is_ok

        # Verify event was persisted
        events = await event_store.replay(sample_event.aggregate_type, sample_event.aggregate_id)
        assert len(events) == 1
        assert events[0].type == sample_event.type

    async def test_commit_saves_checkpoint(
        self,
        uow: UnitOfWork,
        sample_event: BaseEvent,
        checkpoint_store: CheckpointStore,
    ) -> None:
        """UnitOfWork.commit() saves checkpoint when provided."""
        uow.add_event(sample_event)

        checkpoint = CheckpointData.create("seed-123", "phase1", {"step": 1})
        result = await uow.commit(checkpoint)
        assert result.is_ok

        # Verify checkpoint was saved
        load_result = checkpoint_store.load("seed-123")
        assert load_result.is_ok
        loaded = load_result.value
        assert loaded.phase == "phase1"

    async def test_commit_clears_pending_events(self, uow: UnitOfWork) -> None:
        """UnitOfWork.commit() clears pending events on success."""
        uow.add_event(create_sample_event())
        uow.add_event(create_sample_event())
        assert uow.pending_event_count == 2

        result = await uow.commit()
        assert result.is_ok
        assert uow.pending_event_count == 0

    async def test_commit_without_checkpoint(
        self,
        uow: UnitOfWork,
        sample_event: BaseEvent,
        event_store: EventStore,
    ) -> None:
        """UnitOfWork.commit() can be called without checkpoint."""
        uow.add_event(sample_event)

        result = await uow.commit()  # No checkpoint
        assert result.is_ok

        # Verify event was still persisted
        events = await event_store.replay(sample_event.aggregate_type, sample_event.aggregate_id)
        assert len(events) == 1

    async def test_rollback_clears_pending_events(self, uow: UnitOfWork) -> None:
        """UnitOfWork.rollback() clears pending events."""
        uow.add_event(create_sample_event())
        uow.add_event(create_sample_event())
        assert uow.pending_event_count == 2

        uow.rollback()
        assert uow.pending_event_count == 0

    async def test_rollback_does_not_persist_events(
        self,
        uow: UnitOfWork,
        sample_event: BaseEvent,
        event_store: EventStore,
    ) -> None:
        """UnitOfWork.rollback() does not persist events."""
        uow.add_event(sample_event)
        uow.rollback()

        # Verify no events were persisted
        events = await event_store.replay(sample_event.aggregate_type, sample_event.aggregate_id)
        assert len(events) == 0

    async def test_multiple_commits_accumulate_events(
        self,
        uow: UnitOfWork,
        event_store: EventStore,
    ) -> None:
        """Multiple commits accumulate events in EventStore."""
        event1 = create_sample_event()
        event2 = create_sample_event()

        # First commit
        uow.add_event(event1)
        result1 = await uow.commit()
        assert result1.is_ok

        # Second commit
        uow.add_event(event2)
        result2 = await uow.commit()
        assert result2.is_ok

        # Verify both events persisted
        events = await event_store.replay(event1.aggregate_type, event1.aggregate_id)
        assert len(events) == 2


class TestPhaseTransaction:
    """Test PhaseTransaction context manager."""

    async def test_phase_transaction_auto_commits_on_success(
        self,
        uow: UnitOfWork,
        sample_event: BaseEvent,
        event_store: EventStore,
        checkpoint_store: CheckpointStore,
    ) -> None:
        """PhaseTransaction auto-commits on successful context exit."""
        seed_id = "seed-123"
        phase = "planning"
        state = {"step": 1}

        async with PhaseTransaction(uow, seed_id, phase, state) as tx:
            tx.add_event(sample_event)

        # Verify event was persisted
        events = await event_store.replay(sample_event.aggregate_type, sample_event.aggregate_id)
        assert len(events) == 1

        # Verify checkpoint was saved
        load_result = checkpoint_store.load(seed_id)
        assert load_result.is_ok
        assert load_result.value.phase == phase

    async def test_phase_transaction_rolls_back_on_exception(
        self,
        uow: UnitOfWork,
        sample_event: BaseEvent,
        event_store: EventStore,
        checkpoint_store: CheckpointStore,
    ) -> None:
        """PhaseTransaction rolls back on exception."""
        seed_id = "seed-123"
        phase = "planning"
        state = {"step": 1}

        with pytest.raises(ValueError):
            async with PhaseTransaction(uow, seed_id, phase, state) as tx:
                tx.add_event(sample_event)
                raise ValueError("Test error")

        # Verify no events were persisted
        events = await event_store.replay(sample_event.aggregate_type, sample_event.aggregate_id)
        assert len(events) == 0

        # Verify no checkpoint was saved
        load_result = checkpoint_store.load(seed_id)
        assert load_result.is_err  # No checkpoint should exist

    async def test_phase_transaction_add_events(
        self,
        uow: UnitOfWork,
        event_store: EventStore,
    ) -> None:
        """PhaseTransaction.add_events() adds multiple events."""
        seed_id = "seed-123"
        phase = "planning"
        state = {"step": 1}

        events = [create_sample_event() for _ in range(3)]

        async with PhaseTransaction(uow, seed_id, phase, state) as tx:
            tx.add_events(events)

        # Verify all events were persisted
        persisted = await event_store.replay(events[0].aggregate_type, events[0].aggregate_id)
        assert len(persisted) == 3

    async def test_phase_transaction_with_no_events(
        self, uow: UnitOfWork, checkpoint_store: CheckpointStore
    ) -> None:
        """PhaseTransaction works even with no events."""
        seed_id = "seed-123"
        phase = "planning"
        state = {"step": 1}

        async with PhaseTransaction(uow, seed_id, phase, state):
            pass  # No events added

        # Checkpoint should still be saved
        load_result = checkpoint_store.load(seed_id)
        assert load_result.is_ok
        assert load_result.value.phase == phase

    async def test_phase_transaction_propagates_exceptions(self, uow: UnitOfWork) -> None:
        """PhaseTransaction propagates exceptions (doesn't suppress)."""
        seed_id = "seed-123"
        phase = "planning"
        state = {"step": 1}

        with pytest.raises(RuntimeError, match="Test error"):
            async with PhaseTransaction(uow, seed_id, phase, state):
                raise RuntimeError("Test error")


class TestUnitOfWorkIntegration:
    """Integration tests for UnitOfWork with real stores."""

    async def test_full_workflow_with_phase_boundaries(
        self,
        event_store: EventStore,
        checkpoint_store: CheckpointStore,
    ) -> None:
        """Test complete workflow with multiple phases."""
        uow = UnitOfWork(event_store, checkpoint_store)
        seed_id = "seed-123"

        event1 = create_sample_event()
        event2 = create_sample_event()

        # Phase 1: Planning
        async with PhaseTransaction(uow, seed_id, "planning", {"step": 1}) as tx:
            tx.add_event(event1)

        # Phase 2: Execution
        async with PhaseTransaction(uow, seed_id, "execution", {"step": 2}) as tx:
            tx.add_event(event2)

        # Verify all events persisted
        events = await event_store.replay(event1.aggregate_type, event1.aggregate_id)
        assert len(events) == 2

        # Verify latest checkpoint is from execution phase
        load_result = checkpoint_store.load(seed_id)
        assert load_result.is_ok
        assert load_result.value.phase == "execution"

        # Verify planning checkpoint was rotated to rollback
        rollback_path = checkpoint_store._base_path / f"checkpoint_{seed_id}.json.1"
        assert rollback_path.exists()
