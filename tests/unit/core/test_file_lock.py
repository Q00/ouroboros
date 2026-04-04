"""Tests for stdlib-backed file locking."""

from __future__ import annotations

from pathlib import Path

from ouroboros.core.file_lock import file_lock


def test_file_lock_creates_lockfile(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_text("{}")

    with file_lock(target):
        lock_path = target.with_suffix(".json.lock")
        assert lock_path.exists()
        assert lock_path.read_text() == "0"
