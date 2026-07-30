from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest
from typer.testing import CliRunner
import yaml

from ouroboros.cli.commands.status import app

runner = CliRunner(env={"COLUMNS": "240"})


@pytest.fixture
def persisted_status_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    config_dir = tmp_path / ".ouroboros"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(
        yaml.dump(
            {
                "orchestrator": {"runtime_backend": "codex"},
                "llm": {"backend": "codex"},
                "persistence": {"enabled": True, "database_path": "ouroboros.db"},
            }
        ),
        encoding="utf-8",
    )
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)
    monkeypatch.setattr("shutil.which", lambda name: f"/opt/bin/{name}")

    with sqlite3.connect(config_dir / "ouroboros.db") as connection:
        connection.execute(
            "CREATE TABLE events ("
            "id TEXT PRIMARY KEY, aggregate_type TEXT NOT NULL, "
            "aggregate_id TEXT NOT NULL, event_type TEXT NOT NULL, "
            "payload JSON NOT NULL, timestamp DATETIME NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    "evt-start",
                    "orchestrator_session",
                    "orch_fixture",
                    "orchestrator.session.started",
                    json.dumps({"execution_id": "exec_fixture"}),
                    "2026-07-29 00:00:00",
                ),
                (
                    "evt-terminal",
                    "execution",
                    "exec_fixture",
                    "execution.terminal",
                    json.dumps({"session_id": "orch_fixture", "status": "complete"}),
                    "2026-07-29 00:01:00",
                ),
            ],
        )
    return config_dir


def test_health_reports_event_store_count(persisted_status_home: Path) -> None:
    # Given: a configured canonical event store with two persisted rows.
    # When: health inspects the database.
    result = runner.invoke(app, ["health"])

    # Then: the observable detail binds health to the actual event count.
    assert result.exit_code == 0
    assert "ouroboros.db" in result.output
    assert "events=2" in result.output


def test_executions_lists_persisted_terminal_status(persisted_status_home: Path) -> None:
    # Given: one execution with a terminal event.
    # When: the execution list is requested.
    result = runner.invoke(app, ["executions", "--limit", "1"])

    # Then: persisted identity and status replace example rows.
    assert result.exit_code == 0
    assert "exec_fixture" in result.output
    assert "complete" in result.output
    assert "exec-001" not in result.output


def test_execution_shows_persisted_details_and_events(persisted_status_home: Path) -> None:
    # Given: one persisted execution and its event history.
    # When: details with events are requested.
    result = runner.invoke(app, ["execution", "exec_fixture", "--events"])

    # Then: the CLI renders the stored terminal state and event type.
    assert result.exit_code == 0
    assert "exec_fixture" in result.output
    assert "complete" in result.output
    assert "execution.terminal" in result.output
    assert "Would show details" not in result.output


def test_executions_reports_missing_database_without_traceback(
    persisted_status_home: Path,
) -> None:
    # Given: configuration points to a database that is no longer available.
    (persisted_status_home / "ouroboros.db").unlink()

    # When: execution history is requested.
    result = runner.invoke(app, ["executions"])

    # Then: the boundary fails loudly without leaking a traceback.
    assert result.exit_code == 1
    assert "Database unavailable" in result.output
    assert "Traceback" not in result.output
