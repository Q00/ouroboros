"""Database schema definitions using SQLAlchemy Core.

This module defines the database table schemas for Ouroboros.
SQLAlchemy Core is used (not ORM) for flexibility and explicit control.

Table: events
    Single unified table for all event types following event sourcing pattern.

Table: brownfield_repos
    Registered brownfield repositories/worktrees discovered by brownfield scan.
"""

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
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

# One durable compare-and-set guard for explicit terminal session lifecycle
# events. The event stream remains append-only; this table only prevents two
# concurrent terminal writers from both claiming the same session transition.
session_terminal_guards_table = Table(
    "session_terminal_guards",
    metadata,
    Column("session_id", String(128), primary_key=True),
    Column("terminal_event_id", String(36), nullable=False),
    Column("terminal_event_type", String(200), nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=text("CURRENT_TIMESTAMP"),
    ),
)

# One immutable start identity per session aggregate. The append-only event
# stream cannot express a uniqueness constraint over a conditional event type,
# so this guard closes the absent-row race for caller-supplied session IDs on
# every supported database backend.
session_start_guards_table = Table(
    "session_start_guards",
    metadata,
    Column("session_id", String(128), primary_key=True),
    Column("start_event_id", String(36), nullable=False),
    Column("execution_id", String(128), nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=text("CURRENT_TIMESTAMP"),
    ),
)

# One durable compare-and-set guard for the Foundation B final acceptance
# decision.  Attempt judgments remain append-only telemetry; this table makes
# the Final Gate decision one-winner per process-local authority generation and
# root AC without changing the event stream's append-only shape.
ac_acceptance_guards_table = Table(
    "ac_acceptance_guards",
    metadata,
    Column("acceptance_generation_id", String(128), primary_key=True),
    Column("root_ac_index", Integer, primary_key=True),
    Column("final_event_id", String(36), nullable=False),
    Column("payload_digest", String(64), nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=text("CURRENT_TIMESTAMP"),
    ),
)

# Brownfield repos table - registered repositories/worktrees from brownfield scan.
# Filesystem discovery is bounded to the scan root. Normal repo roots can add
# Git-reported linked worktrees outside that root, but linked worktree seeds are
# registered themselves only.
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
