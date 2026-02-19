"""Unit tests for ouroboros.persistence.event_store module."""

import pytest

from ouroboros.events.base import BaseEvent
from ouroboros.persistence.event_store import EventStore


@pytest.fixture
async def event_store(tmp_path):
    """Create an EventStore with an in-memory SQLite database."""
    db_path = tmp_path / "test_events.db"
    store = EventStore(f"sqlite+aiosqlite:///{db_path}")
    await store.initialize()
    yield store
    await store.close()


@pytest.fixture
def sample_event() -> BaseEvent:
    """Create a sample event for testing."""
    return BaseEvent(
        type="ontology.concept.added",
        aggregate_type="ontology",
        aggregate_id="ont-123",
        data={"concept_name": "authentication", "weight": 1.0},
    )


class TestEventStoreInitialization:
    """Test EventStore initialization."""

    async def test_event_store_creates_tables(self, tmp_path) -> None:
        """EventStore.initialize() creates the events table."""
        db_path = tmp_path / "test_init.db"
        store = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await store.initialize()
        # If we get here without error, tables were created
        await store.close()

    async def test_event_store_can_be_initialized_multiple_times(self, tmp_path) -> None:
        """Calling initialize() multiple times is safe."""
        db_path = tmp_path / "test_multi_init.db"
        store = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await store.initialize()
        await store.initialize()  # Should not raise
        await store.close()


class TestEventStoreAppend:
    """Test EventStore.append() method."""

    async def test_append_stores_event(
        self, event_store: EventStore, sample_event: BaseEvent
    ) -> None:
        """append() successfully stores an event."""
        await event_store.append(sample_event)
        # Verify by replaying
        events = await event_store.replay("ontology", "ont-123")
        assert len(events) == 1
        assert events[0].id == sample_event.id

    async def test_append_multiple_events(self, event_store: EventStore) -> None:
        """append() can store multiple events."""
        events_to_store = [
            BaseEvent(
                type="ontology.concept.added",
                aggregate_type="ontology",
                aggregate_id="ont-123",
                data={"concept_name": f"concept_{i}"},
            )
            for i in range(5)
        ]

        for event in events_to_store:
            await event_store.append(event)

        replayed = await event_store.replay("ontology", "ont-123")
        assert len(replayed) == 5

    async def test_append_preserves_event_data(
        self, event_store: EventStore, sample_event: BaseEvent
    ) -> None:
        """append() preserves all event fields."""
        await event_store.append(sample_event)
        events = await event_store.replay("ontology", "ont-123")

        stored = events[0]
        assert stored.id == sample_event.id
        assert stored.type == sample_event.type
        assert stored.aggregate_type == sample_event.aggregate_type
        assert stored.aggregate_id == sample_event.aggregate_id
        assert stored.data == sample_event.data


class TestEventStoreReplay:
    """Test EventStore.replay() method."""

    async def test_replay_returns_empty_for_nonexistent_aggregate(
        self, event_store: EventStore
    ) -> None:
        """replay() returns empty list for nonexistent aggregate."""
        events = await event_store.replay("nonexistent", "id-999")
        assert events == []

    async def test_replay_returns_events_ordered_by_timestamp(
        self, event_store: EventStore
    ) -> None:
        """replay() returns events in timestamp order."""
        import asyncio

        events_to_store = []
        for i in range(3):
            event = BaseEvent(
                type=f"test.event.created_{i}",
                aggregate_type="test",
                aggregate_id="test-123",
                data={"order": i},
            )
            events_to_store.append(event)
            await event_store.append(event)
            await asyncio.sleep(0.01)  # Small delay for different timestamps

        replayed = await event_store.replay("test", "test-123")
        assert len(replayed) == 3
        # Verify order by checking data
        for i, event in enumerate(replayed):
            assert event.data["order"] == i

    async def test_replay_orders_by_timestamp_then_id_for_ties(
        self, event_store: EventStore
    ) -> None:
        """replay() is deterministic when multiple events share a timestamp."""
        from datetime import UTC, datetime

        shared_ts = datetime(2026, 2, 19, 0, 0, 0, tzinfo=UTC)
        later_id = BaseEvent(
            id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            timestamp=shared_ts,
            type="test.event.created",
            aggregate_type="test",
            aggregate_id="test-order-tie",
            data={"order": "later-id"},
        )
        earlier_id = BaseEvent(
            id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            timestamp=shared_ts,
            type="test.event.created",
            aggregate_type="test",
            aggregate_id="test-order-tie",
            data={"order": "earlier-id"},
        )

        # Insert in reverse lexical id order; replay should sort by (timestamp, id)
        await event_store.append(later_id)
        await event_store.append(earlier_id)

        replayed = await event_store.replay("test", "test-order-tie")
        assert [e.id for e in replayed] == [
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        ]

    async def test_replay_filters_by_aggregate_type(self, event_store: EventStore) -> None:
        """replay() only returns events for the specified aggregate type."""
        event1 = BaseEvent(
            type="ontology.concept.added",
            aggregate_type="ontology",
            aggregate_id="shared-id",
            data={"type": "ontology"},
        )
        event2 = BaseEvent(
            type="execution.ac.completed",
            aggregate_type="execution",
            aggregate_id="shared-id",
            data={"type": "execution"},
        )

        await event_store.append(event1)
        await event_store.append(event2)

        ontology_events = await event_store.replay("ontology", "shared-id")
        execution_events = await event_store.replay("execution", "shared-id")

        assert len(ontology_events) == 1
        assert ontology_events[0].data["type"] == "ontology"
        assert len(execution_events) == 1
        assert execution_events[0].data["type"] == "execution"

    async def test_replay_filters_by_aggregate_id(self, event_store: EventStore) -> None:
        """replay() only returns events for the specified aggregate ID."""
        event1 = BaseEvent(
            type="ontology.concept.added",
            aggregate_type="ontology",
            aggregate_id="ont-1",
            data={"id": "1"},
        )
        event2 = BaseEvent(
            type="ontology.concept.added",
            aggregate_type="ontology",
            aggregate_id="ont-2",
            data={"id": "2"},
        )

        await event_store.append(event1)
        await event_store.append(event2)

        events_1 = await event_store.replay("ontology", "ont-1")
        events_2 = await event_store.replay("ontology", "ont-2")

        assert len(events_1) == 1
        assert events_1[0].data["id"] == "1"
        assert len(events_2) == 1
        assert events_2[0].data["id"] == "2"


class TestEventStoreErrorHandling:
    """Test error handling in EventStore."""

    async def test_append_raises_when_not_initialized(self) -> None:
        """append() raises PersistenceError when store not initialized."""
        from ouroboros.core.errors import PersistenceError

        store = EventStore("sqlite+aiosqlite:///test.db")
        event = BaseEvent(
            type="test.event.created",
            aggregate_type="test",
            aggregate_id="test-123",
        )

        with pytest.raises(PersistenceError, match="not initialized"):
            await store.append(event)

    async def test_replay_raises_when_not_initialized(self) -> None:
        """replay() raises PersistenceError when store not initialized."""
        from ouroboros.core.errors import PersistenceError

        store = EventStore("sqlite+aiosqlite:///test.db")

        with pytest.raises(PersistenceError, match="not initialized"):
            await store.replay("test", "test-123")


class TestEventStoreTransactions:
    """Test transaction handling per AC7."""

    async def test_append_is_atomic(self, event_store: EventStore) -> None:
        """append() uses transactions for atomicity."""
        # This tests that a successful append is committed
        event = BaseEvent(
            type="test.transaction.committed",
            aggregate_type="test",
            aggregate_id="tx-test",
            data={"committed": True},
        )
        await event_store.append(event)

        # Close and reopen to ensure persistence
        await event_store.close()
        await event_store.initialize()

        events = await event_store.replay("test", "tx-test")
        assert len(events) == 1
        assert events[0].data["committed"] is True
