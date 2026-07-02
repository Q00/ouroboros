"""Tests for the per-backend outcome flywheel (PR-X X4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sqlite3

import pytest

from ouroboros.orchestrator import backend_outcomes


def _make_events_db(path: Path, rows: list[tuple[str, dict, datetime]]) -> None:
    """Create a minimal events table matching the production schema."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE events ("
        "id TEXT PRIMARY KEY, aggregate_type TEXT, aggregate_id TEXT, "
        "event_type TEXT, payload TEXT, timestamp TEXT, consensus_id TEXT)"
    )
    for i, (event_type, payload, ts) in enumerate(rows):
        conn.execute(
            "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                f"evt-{i}",
                "execution",
                f"ac-{i}",
                event_type,
                json.dumps(payload),
                ts.isoformat(),
                None,
            ),
        )
    conn.commit()
    conn.close()


@pytest.fixture
def fixture_db(tmp_path: Path) -> Path:
    now = datetime.now(UTC)
    rows = [
        ("execution.session.completed", {"runtime_backend": "claude"}, now),
        ("execution.session.completed", {"runtime_backend": "claude"}, now),
        ("execution.session.failed", {"runtime_backend": "claude"}, now),
        ("execution.session.completed", {"runtime_backend": "codex"}, now),
        ("execution.session.failed", {"runtime_backend": "codex"}, now),
        # Noise: unrelated event type + a stale row outside the 30-day window.
        ("execution.ac.completed", {"runtime_backend": "claude"}, now),
        (
            "execution.session.completed",
            {"runtime_backend": "gemini"},
            now - timedelta(days=90),
        ),
    ]
    db = tmp_path / "ouroboros.db"
    _make_events_db(db, rows)
    return db


class TestAggregate:
    def test_counts_completed_and_failed(self, fixture_db: Path) -> None:
        outcomes = backend_outcomes.aggregate_backend_outcomes(db_path=fixture_db)
        assert outcomes["claude"].completed == 2
        assert outcomes["claude"].failed == 1
        assert outcomes["codex"].completed == 1
        assert outcomes["codex"].failed == 1

    def test_ignores_unrelated_event_types(self, fixture_db: Path) -> None:
        outcomes = backend_outcomes.aggregate_backend_outcomes(db_path=fixture_db)
        # execution.ac.completed must not inflate claude's completed count (=2 not 3).
        assert outcomes["claude"].completed == 2

    def test_window_excludes_stale_rows(self, fixture_db: Path) -> None:
        outcomes = backend_outcomes.aggregate_backend_outcomes(db_path=fixture_db, window_days=30)
        assert "gemini" not in outcomes

    def test_window_none_includes_stale(self, fixture_db: Path) -> None:
        outcomes = backend_outcomes.aggregate_backend_outcomes(db_path=fixture_db, window_days=None)
        assert outcomes["gemini"].completed == 1


class TestWeights:
    def test_success_rate_weights(self, fixture_db: Path) -> None:
        weights = backend_outcomes.outcome_weights(db_path=fixture_db)
        assert weights["claude"] == pytest.approx(2 / 3)
        assert weights["codex"] == pytest.approx(1 / 2)

    def test_missing_db_is_silent(self, tmp_path: Path) -> None:
        assert backend_outcomes.outcome_weights(db_path=tmp_path / "nope.db") == {}

    def test_corrupt_db_is_silent(self, tmp_path: Path) -> None:
        bad = tmp_path / "corrupt.db"
        bad.write_text("this is not sqlite")
        assert backend_outcomes.outcome_weights(db_path=bad) == {}
