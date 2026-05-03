"""Unit tests for ouroboros.persistence.event_store module."""

import pytest

from ouroboros.core.errors import PersistenceError
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

    async def test_append_excludes_raw_subscribed_payloads(self, event_store: EventStore) -> None:
        """append() stores normalized payloads without raw subscribed event data."""
        event = BaseEvent(
            type="orchestrator.progress.updated",
            aggregate_type="session",
            aggregate_id="sess-123",
            data={
                "progress": {
                    "messages_processed": 2,
                    "runtime": {
                        "backend": "opencode",
                        "native_session_id": "native-123",
                        "metadata": {
                            "resume_token": "resume-123",
                            "subscribed_events": [{"type": "item.completed"}],
                        },
                    },
                    "raw_event": {"type": "thread.delta"},
                }
            },
        )

        await event_store.append(event)

        replayed = await event_store.replay("session", "sess-123")
        assert replayed[0].data == {
            "progress": {
                "messages_processed": 2,
                "runtime": {
                    "backend": "opencode",
                    "native_session_id": "native-123",
                    "metadata": {
                        "resume_token": "resume-123",
                    },
                },
            }
        }

    async def test_append_excludes_raw_subscribed_payloads_nested_in_tuples(
        self, event_store: EventStore
    ) -> None:
        """append() should sanitize raw stream payloads even inside tuple-backed data."""
        event = BaseEvent(
            type="orchestrator.progress.updated",
            aggregate_type="session",
            aggregate_id="sess-123",
            data={
                "progress_batches": (
                    {
                        "messages_processed": 2,
                        "raw_event": {"type": "assistant.message.delta"},
                    },
                    {
                        "runtime": {
                            "backend": "opencode",
                            "native_session_id": "native-123",
                            "metadata": {
                                "resume_token": "resume-123",
                                "subscribed_events": [{"type": "item.completed"}],
                            },
                        },
                    },
                ),
            },
        )

        await event_store.append(event)

        replayed = await event_store.replay("session", "sess-123")
        assert replayed[0].data == {
            "progress_batches": [
                {
                    "messages_processed": 2,
                },
                {
                    "runtime": {
                        "backend": "opencode",
                        "native_session_id": "native-123",
                        "metadata": {
                            "resume_token": "resume-123",
                        },
                    },
                },
            ]
        }

    async def test_replay_history_contains_only_normalized_base_events(
        self, event_store: EventStore
    ) -> None:
        """Replayed history should contain only normalized BaseEvent records."""
        events = [
            BaseEvent(
                type="orchestrator.progress.updated",
                aggregate_type="session",
                aggregate_id="sess-history-123",
                data={
                    "progress": {
                        "step": "session.started",
                        "raw_event": {"type": "session.started"},
                        "runtime": {
                            "backend": "opencode",
                            "metadata": {
                                "resume_token": "resume-123",
                                "subscribed_events": [{"type": "session.started"}],
                            },
                        },
                    }
                },
            ),
            BaseEvent(
                type="orchestrator.tool.called",
                aggregate_type="session",
                aggregate_id="sess-history-123",
                data={
                    "tool_name": "Edit",
                    "tool_input": {"file_path": "src/ouroboros/orchestrator/runner.py"},
                    "raw_subscribed_event": {"type": "tool.started"},
                },
            ),
        ]

        await event_store.append_batch(events)

        replayed = await event_store.replay("session", "sess-history-123")

        assert len(replayed) == 2
        assert all(isinstance(event, BaseEvent) for event in replayed)
        assert replayed[0].data == {
            "progress": {
                "step": "session.started",
                "runtime": {
                    "backend": "opencode",
                    "metadata": {"resume_token": "resume-123"},
                },
            }
        }
        assert replayed[1].data == {
            "tool_name": "Edit",
            "tool_input": {"file_path": "src/ouroboros/orchestrator/runner.py"},
        }

    async def test_append_rejects_non_base_event(self, event_store: EventStore) -> None:
        """append() rejects raw dict payloads in place of normalized BaseEvent records."""
        with pytest.raises(PersistenceError, match="BaseEvent"):
            await event_store.append({"type": "raw.event"})  # type: ignore[arg-type]

    async def test_append_rejects_raw_subscribed_stream_payload(
        self, event_store: EventStore
    ) -> None:
        """append() explicitly rejects raw subscribed runtime payloads."""
        with pytest.raises(PersistenceError, match="raw subscribed event stream payloads"):
            await event_store.append(  # type: ignore[arg-type]
                {
                    "type": "assistant.message.delta",
                    "session_id": "native-123",
                    "delta": {"text": "Applying patch"},
                    "payload": {"raw_chunk": "delta-1"},
                }
            )

    async def test_append_batch_rejects_non_base_event(self, event_store: EventStore) -> None:
        """append_batch() rejects raw dict payloads in place of normalized events."""
        with pytest.raises(PersistenceError, match="BaseEvent"):
            await event_store.append_batch(  # type: ignore[arg-type]
                [
                    BaseEvent(
                        type="test.event.created",
                        aggregate_type="test",
                        aggregate_id="test-123",
                    ),
                    {"type": "raw.event"},
                ]
            )

    async def test_append_batch_rejects_raw_subscribed_stream_payload(
        self, event_store: EventStore
    ) -> None:
        """append_batch() explicitly rejects raw subscribed runtime payloads."""
        with pytest.raises(PersistenceError, match="raw subscribed event stream payloads"):
            await event_store.append_batch(  # type: ignore[arg-type]
                [
                    BaseEvent(
                        type="test.event.created",
                        aggregate_type="test",
                        aggregate_id="test-123",
                    ),
                    {
                        "type": "tool.started",
                        "tool_name": "Edit",
                        "session_id": "native-123",
                        "input": {"file_path": "src/ouroboros/persistence/event_store.py"},
                    },
                ]
            )


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


class TestEventStoreGetEventsAfter:
    """Test EventStore.get_events_after() incremental fetching."""

    async def test_get_events_after_returns_all_when_last_row_id_is_zero(
        self, event_store: EventStore
    ) -> None:
        """get_events_after() with last_row_id=0 returns all matching events."""
        for i in range(3):
            await event_store.append(
                BaseEvent(
                    type="test.event.created",
                    aggregate_type="execution",
                    aggregate_id="exec-1",
                    data={"order": i},
                )
            )

        events, last_row_id = await event_store.get_events_after("execution", "exec-1", 0)
        assert len(events) == 3
        assert last_row_id > 0

    async def test_get_events_after_returns_only_new_events(self, event_store: EventStore) -> None:
        """get_events_after() only returns events inserted after last_row_id."""
        # Insert first batch
        for i in range(3):
            await event_store.append(
                BaseEvent(
                    type="test.event.created",
                    aggregate_type="execution",
                    aggregate_id="exec-1",
                    data={"batch": 1, "order": i},
                )
            )

        # Get initial cursor
        _, last_row_id = await event_store.get_events_after("execution", "exec-1", 0)

        # Insert second batch
        for i in range(2):
            await event_store.append(
                BaseEvent(
                    type="test.event.created",
                    aggregate_type="execution",
                    aggregate_id="exec-1",
                    data={"batch": 2, "order": i},
                )
            )

        # Should only get the 2 new events
        new_events, new_row_id = await event_store.get_events_after(
            "execution", "exec-1", last_row_id
        )
        assert len(new_events) == 2
        assert all(e.data["batch"] == 2 for e in new_events)
        assert new_row_id > last_row_id

    async def test_get_events_after_returns_empty_when_no_new_events(
        self, event_store: EventStore
    ) -> None:
        """get_events_after() returns empty list when no new events exist."""
        await event_store.append(
            BaseEvent(
                type="test.event.created",
                aggregate_type="execution",
                aggregate_id="exec-1",
                data={},
            )
        )

        _, last_row_id = await event_store.get_events_after("execution", "exec-1", 0)

        # No new events
        events, same_row_id = await event_store.get_events_after("execution", "exec-1", last_row_id)
        assert events == []
        assert same_row_id == last_row_id

    async def test_get_events_after_filters_by_aggregate(self, event_store: EventStore) -> None:
        """get_events_after() only returns events for the specified aggregate."""
        await event_store.append(
            BaseEvent(
                type="test.event.created",
                aggregate_type="execution",
                aggregate_id="exec-1",
                data={"target": True},
            )
        )
        await event_store.append(
            BaseEvent(
                type="test.event.created",
                aggregate_type="execution",
                aggregate_id="exec-2",
                data={"target": False},
            )
        )

        events, _ = await event_store.get_events_after("execution", "exec-1", 0)
        assert len(events) == 1
        assert events[0].data["target"] is True

    async def test_get_events_after_returns_empty_for_nonexistent_aggregate(
        self, event_store: EventStore
    ) -> None:
        """get_events_after() returns empty list for nonexistent aggregate."""
        events, last_row_id = await event_store.get_events_after("execution", "no-such-id", 0)
        assert events == []
        assert last_row_id == 0

    async def test_get_events_after_raises_when_not_initialized(self) -> None:
        """get_events_after() raises PersistenceError when store not initialized."""
        from ouroboros.core.errors import PersistenceError

        store = EventStore("sqlite+aiosqlite:///test.db")
        with pytest.raises(PersistenceError, match="not initialized"):
            await store.get_events_after("test", "test-123", 0)


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


class TestSessionActivitySnapshots:
    """Test aggregated session snapshot queries for orphan detection."""

    async def test_returns_latest_status_and_activity(self, event_store: EventStore) -> None:
        await event_store.append(
            BaseEvent(
                type="orchestrator.session.started",
                aggregate_type="session",
                aggregate_id="sess-running",
                data={
                    "execution_id": "exec-running",
                    "seed_id": "seed-running",
                    "start_time": "2026-04-01T00:00:00+00:00",
                },
            )
        )
        await event_store.append(
            BaseEvent(
                type="orchestrator.progress.updated",
                aggregate_type="session",
                aggregate_id="sess-running",
                data={"progress": {"step": 2}},
            )
        )
        await event_store.append(
            BaseEvent(
                type="orchestrator.session.started",
                aggregate_type="session",
                aggregate_id="sess-completed",
                data={
                    "execution_id": "exec-completed",
                    "seed_id": "seed-completed",
                    "start_time": "2026-04-01T01:00:00+00:00",
                },
            )
        )
        await event_store.append(
            BaseEvent(
                type="orchestrator.progress.updated",
                aggregate_type="session",
                aggregate_id="sess-completed",
                data={"progress": {"runtime_status": "completed"}},
            )
        )

        snapshots = await event_store.get_session_activity_snapshots()
        by_id = {snapshot.session_id: snapshot for snapshot in snapshots}

        assert set(by_id) == {"sess-running", "sess-completed"}
        assert by_id["sess-running"].execution_id == "exec-running"
        assert by_id["sess-running"].seed_id == "seed-running"
        assert by_id["sess-running"].status_event_type is None
        assert by_id["sess-running"].runtime_status is None
        assert by_id["sess-running"].last_activity is not None
        assert by_id["sess-completed"].status_event_type == "orchestrator.progress.updated"
        assert by_id["sess-completed"].runtime_status == "completed"

    async def test_includes_terminal_session_without_started_event(
        self, event_store: EventStore
    ) -> None:
        await event_store.append(
            BaseEvent(
                type="orchestrator.session.completed",
                aggregate_type="session",
                aggregate_id="sess-imported-complete",
                data={"summary": "imported terminal event"},
            )
        )

        snapshots = await event_store.get_session_activity_snapshots()
        by_id = {snapshot.session_id: snapshot for snapshot in snapshots}

        assert set(by_id) == {"sess-imported-complete"}
        snapshot = by_id["sess-imported-complete"]
        assert snapshot.execution_id is None
        assert snapshot.seed_id is None
        assert snapshot.start_time is not None
        assert snapshot.last_activity is not None
        assert snapshot.status_event_type == "orchestrator.session.completed"

    async def test_includes_progress_only_session_without_started_event(
        self, event_store: EventStore
    ) -> None:
        await event_store.append(
            BaseEvent(
                type="orchestrator.progress.updated",
                aggregate_type="session",
                aggregate_id="sess-imported-progress",
                data={"progress": {"step": 1, "runtime_status": "running"}},
            )
        )

        snapshots = await event_store.get_session_activity_snapshots()
        by_id = {snapshot.session_id: snapshot for snapshot in snapshots}

        assert set(by_id) == {"sess-imported-progress"}
        snapshot = by_id["sess-imported-progress"]
        assert snapshot.execution_id is None
        assert snapshot.seed_id is None
        assert snapshot.start_time is not None
        assert snapshot.last_activity is not None
        assert snapshot.status_event_type == "orchestrator.progress.updated"
        assert snapshot.runtime_status == "running"


class TestGetAllSessions:
    """Test get_all_sessions returns all session lifecycle events."""

    async def test_returns_all_session_event_types(self, event_store: EventStore) -> None:
        """get_all_sessions returns started, completed, cancelled, and failed events."""
        started = BaseEvent(
            type="orchestrator.session.started",
            aggregate_type="session",
            aggregate_id="sess-1",
            data={"seed_goal": "Build API"},
        )
        completed = BaseEvent(
            type="orchestrator.session.completed",
            aggregate_type="session",
            aggregate_id="sess-1",
            data={"summary": "done"},
        )
        cancelled = BaseEvent(
            type="orchestrator.session.cancelled",
            aggregate_type="session",
            aggregate_id="sess-2",
            data={"reason": "orphaned"},
        )
        unrelated = BaseEvent(
            type="orchestrator.execution.started",
            aggregate_type="execution",
            aggregate_id="exec-1",
            data={},
        )
        for evt in [started, completed, cancelled, unrelated]:
            await event_store.append(evt)

        result = await event_store.get_all_sessions()
        types = [e.type for e in result]
        assert "orchestrator.session.started" in types
        assert "orchestrator.session.completed" in types
        assert "orchestrator.session.cancelled" in types
        assert "orchestrator.execution.started" not in types

    async def test_returns_events_in_ascending_order(self, event_store: EventStore) -> None:
        """Events are returned oldest-first so callers can replay status."""
        for suffix in ("started", "completed"):
            await event_store.append(
                BaseEvent(
                    type=f"orchestrator.session.{suffix}",
                    aggregate_type="session",
                    aggregate_id="sess-asc",
                    data={},
                )
            )

        result = await event_store.get_all_sessions()
        sess_events = [e for e in result if e.aggregate_id == "sess-asc"]
        assert len(sess_events) == 2
        assert sess_events[0].type == "orchestrator.session.started"
        assert sess_events[1].type == "orchestrator.session.completed"

    async def test_raises_when_not_initialized(self) -> None:
        """get_all_sessions raises PersistenceError when store not initialized."""
        store = EventStore("sqlite+aiosqlite:///dummy.db")
        with pytest.raises(PersistenceError):
            await store.get_all_sessions()


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
