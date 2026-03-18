"""Database schema definitions using SQLAlchemy Core.

This module defines the database table schemas for Ouroboros.
SQLAlchemy Core is used (not ORM) for flexibility and explicit control.

Table: events
    Single unified table for all event types following event sourcing pattern.

Table: brownfield_repos
    Registered brownfield repositories scanned from the user's home directory.
"""

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Index,
    MetaData,
    String,
    Table,
    Text,
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
    Index("ix_events_agg_type_id_timestamp", "aggregate_type", "aggregate_id", "timestamp"),
)

# Brownfield repos table - registered repositories from home directory scan
brownfield_repos_table = Table(
    "brownfield_repos",
    metadata,
    # Absolute path as primary key (unique per filesystem)
    Column("path", Text, primary_key=True),
    # Human-readable repository name (derived from directory name)
    Column("name", Text, nullable=False),
    # One-line description summarized from README/CLAUDE.md
    Column("desc", Text, nullable=True),
    # Whether this repo is the default brownfield context for PM interviews
    Column("is_default", Boolean, nullable=False, default=False, server_default=text("0")),
    # Timestamp when the repo was registered
    Column(
        "registered_at",
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=text("CURRENT_TIMESTAMP"),
    ),
)
