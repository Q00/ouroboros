"""Database schema definitions using SQLAlchemy Core.

This module defines the events table schema for event sourcing.
SQLAlchemy Core is used (not ORM) for flexibility and explicit control.

Table: events
    Single unified table for all event types following event sourcing pattern.
"""

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Index,
    MetaData,
    String,
    Table,
    text,
)

# Global metadata instance for all tables
metadata = MetaData()

# Events table - single unified table for event sourcing
events_table = Table(
    "events",
    metadata,
    # Primary key - UUID as string
    Column("id", String(36), primary_key=True),
    # Aggregate identification for event replay
    Column("aggregate_type", String(100), nullable=False),
    Column("aggregate_id", String(36), nullable=False),
    # Event type following dot.notation.past_tense convention
    # e.g., "ontology.concept.added", "execution.ac.completed"
    Column("event_type", String(200), nullable=False),
    # Event payload as JSON
    Column("payload", JSON, nullable=False),
    # Timestamp with timezone, defaults to UTC now
    Column(
        "timestamp",
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=text("CURRENT_TIMESTAMP"),
    ),
    # Optional consensus ID for multi-model consensus events
    Column("consensus_id", String(36), nullable=True),
    # Indexes for efficient queries
    Index("ix_events_aggregate_type", "aggregate_type"),
    Index("ix_events_aggregate_id", "aggregate_id"),
    Index("ix_events_aggregate_type_id", "aggregate_type", "aggregate_id"),
    Index("ix_events_event_type", "event_type"),
    Index("ix_events_timestamp", "timestamp"),
)
