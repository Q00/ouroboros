-- Migration: 002_brownfield
-- Description: Create brownfield_repos table for managing brownfield project registrations
-- Created: 2026-03-18

CREATE TABLE IF NOT EXISTS brownfield_repos (
    path TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    desc TEXT,
    is_default BOOLEAN NOT NULL DEFAULT 0,
    registered_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Index for quickly finding the default repo
CREATE INDEX IF NOT EXISTS ix_brownfield_repos_is_default ON brownfield_repos (is_default);
