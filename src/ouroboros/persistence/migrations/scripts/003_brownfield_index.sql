-- Migration: 003_brownfield_index
-- Description: Per-repo lexical search documents for brownfield ranking
-- Created: 2026-10-16

CREATE TABLE IF NOT EXISTS brownfield_repo_index (
    path TEXT PRIMARY KEY,
    doc_text TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL DEFAULT '',
    indexed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
