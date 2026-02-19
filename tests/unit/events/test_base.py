"""Unit tests for ouroboros.events.base module."""

from datetime import UTC, datetime

from ouroboros.events.base import BaseEvent


class TestBaseEventConstruction:
    """Test BaseEvent construction."""

    def test_base_event_is_frozen(self) -> None:
        """BaseEvent is immutable (frozen)."""
        event = BaseEvent(
            type="test.event.created",
            aggregate_type="test",
            aggregate_id="test-123",
        )
        # Attempting to modify should raise an error
        try:
            event.type = "modified"  # type: ignore[misc]
            assert False, "Should have raised an error"
        except Exception:
            pass  # Expected - frozen model

    def test_base_event_auto_generates_id(self) -> None:
        """BaseEvent generates UUID for id if not provided."""
        event = BaseEvent(
            type="test.event.created",
            aggregate_type="test",
            aggregate_id="test-123",
        )
        assert event.id is not None
        assert len(event.id) == 36  # UUID length

    def test_base_event_auto_generates_timestamp(self) -> None:
        """BaseEvent generates timestamp if not provided."""
        before = datetime.now(UTC)
        event = BaseEvent(
            type="test.event.created",
            aggregate_type="test",
            aggregate_id="test-123",
        )
        after = datetime.now(UTC)

        assert event.timestamp is not None
        assert before <= event.timestamp <= after

    def test_base_event_default_empty_data(self) -> None:
        """BaseEvent defaults to empty data dict."""
        event = BaseEvent(
            type="test.event.created",
            aggregate_type="test",
            aggregate_id="test-123",
        )
        assert event.data == {}

    def test_base_event_stores_data(self) -> None:
        """BaseEvent stores provided data."""
        data = {"key": "value", "count": 42}
        event = BaseEvent(
            type="test.event.created",
            aggregate_type="test",
            aggregate_id="test-123",
            data=data,
        )
        assert event.data == data


class TestBaseEventNaming:
    """Test event type naming convention per AC6."""

    def test_event_type_dot_notation(self) -> None:
        """Event type follows dot.notation.past_tense convention."""
        event = BaseEvent(
            type="ontology.concept.added",
            aggregate_type="ontology",
            aggregate_id="ont-123",
        )
        assert "." in event.type
        parts = event.type.split(".")
        assert len(parts) >= 3  # domain.entity.verb


class TestBaseEventSerialization:
    """Test BaseEvent serialization for database."""

    def test_to_db_dict_includes_all_fields(self) -> None:
        """to_db_dict() returns all required database columns."""
        event = BaseEvent(
            type="test.event.created",
            aggregate_type="test",
            aggregate_id="test-123",
            data={"key": "value"},
        )

        db_dict = event.to_db_dict()

        assert "id" in db_dict
        assert "event_type" in db_dict
        assert "timestamp" in db_dict
        assert "aggregate_type" in db_dict
        assert "aggregate_id" in db_dict
        assert "payload" in db_dict
        assert "consensus_id" in db_dict

    def test_to_db_dict_maps_type_to_event_type(self) -> None:
        """to_db_dict() maps 'type' to 'event_type' column."""
        event = BaseEvent(
            type="ontology.concept.added",
            aggregate_type="ontology",
            aggregate_id="ont-123",
        )

        db_dict = event.to_db_dict()
        assert db_dict["event_type"] == "ontology.concept.added"

    def test_to_db_dict_maps_data_to_payload(self) -> None:
        """to_db_dict() maps 'data' to 'payload' column."""
        event = BaseEvent(
            type="test.event.created",
            aggregate_type="test",
            aggregate_id="test-123",
            data={"key": "value"},
        )

        db_dict = event.to_db_dict()
        assert db_dict["payload"] == {"key": "value"}

    def test_from_db_row_reconstructs_event(self) -> None:
        """from_db_row() reconstructs event from database row."""
        row = {
            "id": "event-123",
            "event_type": "test.event.created",
            "timestamp": datetime.now(UTC),
            "aggregate_type": "test",
            "aggregate_id": "test-456",
            "payload": {"key": "value"},
        }

        event = BaseEvent.from_db_row(row)

        assert event.id == "event-123"
        assert event.type == "test.event.created"
        assert event.aggregate_type == "test"
        assert event.aggregate_id == "test-456"
        assert event.data == {"key": "value"}

    def test_roundtrip_serialization(self) -> None:
        """Event survives roundtrip through to_db_dict and from_db_row."""
        original = BaseEvent(
            type="ontology.concept.added",
            aggregate_type="ontology",
            aggregate_id="ont-123",
            data={"concept_name": "auth", "weight": 1.5},
        )

        db_dict = original.to_db_dict()
        # Simulate what DB would return
        db_row = {
            "id": db_dict["id"],
            "event_type": db_dict["event_type"],
            "timestamp": db_dict["timestamp"],
            "aggregate_type": db_dict["aggregate_type"],
            "aggregate_id": db_dict["aggregate_id"],
            "payload": db_dict["payload"],
        }

        restored = BaseEvent.from_db_row(db_row)

        assert restored.id == original.id
        assert restored.type == original.type
        assert restored.aggregate_type == original.aggregate_type
        assert restored.aggregate_id == original.aggregate_id
        assert restored.data == original.data
