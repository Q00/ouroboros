-- Migration: 001_initial
-- Description: Create initial events table for event sourcing
-- Created: 2026-01-16

CREATE TABLE IF NOT EXISTS events (
    id VARCHAR(36) PRIMARY KEY,
    aggregate_type VARCHAR(100) NOT NULL,
    aggregate_id VARCHAR(36) NOT NULL,
    event_type VARCHAR(200) NOT NULL,
    payload JSON NOT NULL,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    consensus_id VARCHAR(36)
);

-- Indexes for efficient queries
CREATE INDEX IF NOT EXISTS ix_events_aggregate_type ON events (aggregate_type);
CREATE INDEX IF NOT EXISTS ix_events_aggregate_id ON events (aggregate_id);
CREATE INDEX IF NOT EXISTS ix_events_aggregate_type_id ON events (aggregate_type, aggregate_id);
CREATE INDEX IF NOT EXISTS ix_events_event_type ON events (event_type);
CREATE INDEX IF NOT EXISTS ix_events_timestamp ON events (timestamp);
