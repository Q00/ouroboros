"""Unit tests for ouroboros.persistence.schema module."""

from ouroboros.persistence.schema import events_table, metadata


class TestEventsTableSchema:
    """Test events table schema definition."""

    def test_events_table_exists(self) -> None:
        """events_table is defined in the schema."""
        assert events_table is not None
        assert events_table.name == "events"

    def test_events_table_has_required_columns(self) -> None:
        """events table has all required columns per AC2."""
        column_names = {col.name for col in events_table.columns}
        required_columns = {
            "id",
            "aggregate_type",
            "aggregate_id",
            "event_type",
            "payload",
            "timestamp",
            "consensus_id",
        }
        assert required_columns.issubset(column_names)

    def test_id_column_is_primary_key(self) -> None:
        """id column is the primary key."""
        id_col = events_table.c.id
        assert id_col.primary_key is True

    def test_payload_column_is_json(self) -> None:
        """payload column stores JSON data."""
        payload_col = events_table.c.payload
        # SQLAlchemy JSON type name varies, check it's JSON-capable
        assert "JSON" in str(payload_col.type).upper()

    def test_timestamp_column_has_default(self) -> None:
        """timestamp column has a server default."""
        timestamp_col = events_table.c.timestamp
        assert timestamp_col.server_default is not None or timestamp_col.default is not None

    def test_consensus_id_is_nullable(self) -> None:
        """consensus_id column is nullable (optional)."""
        consensus_col = events_table.c.consensus_id
        assert consensus_col.nullable is True


class TestEventsTableIndexes:
    """Test events table indexes per AC3."""

    def test_aggregate_type_index_exists(self) -> None:
        """Index exists on aggregate_type column."""
        index_names = {idx.name for idx in events_table.indexes}
        assert any("aggregate_type" in name for name in index_names)

    def test_aggregate_id_index_exists(self) -> None:
        """Index exists on aggregate_id column."""
        index_names = {idx.name for idx in events_table.indexes}
        assert any("aggregate_id" in name for name in index_names)

    def test_composite_index_exists(self) -> None:
        """Composite index on (aggregate_type, aggregate_id) for efficient queries."""
        # Find index that contains both columns
        for idx in events_table.indexes:
            col_names = {col.name for col in idx.columns}
            if "aggregate_type" in col_names and "aggregate_id" in col_names:
                return  # Found composite index
        # If we get here, no composite index found
        assert False, "No composite index on (aggregate_type, aggregate_id) found"


class TestMetadata:
    """Test SQLAlchemy metadata configuration."""

    def test_metadata_contains_events_table(self) -> None:
        """Metadata contains the events table."""
        assert "events" in metadata.tables
