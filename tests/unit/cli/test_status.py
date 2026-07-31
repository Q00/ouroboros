"""Unit tests for the status command."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3

import pytest
from typer.testing import CliRunner
import yaml

from ouroboros.cli.commands.status import app
from ouroboros.cli.formatters import console
from ouroboros.events.base import BaseEvent
from ouroboros.persistence.event_store import EventStore

runner = CliRunner(env={"COLUMNS": "240"})


API_ENV_KEYS = [
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "OPENROUTER_API_KEY",
    "OUROBOROS_LLM_BACKEND",
    "OUROBOROS_RUNTIME",
    "OUROBOROS_AGENT_RUNTIME",
    "OUROBOROS_CLI_PATH",
    "OUROBOROS_CODEX_CLI_PATH",
    "OUROBOROS_COPILOT_CLI_PATH",
    "OUROBOROS_GEMINI_CLI_PATH",
    "OUROBOROS_GJC_CLI_PATH",
    "OUROBOROS_GOOSE_CLI_PATH",
    "OUROBOROS_HERMES_CLI_PATH",
    "OUROBOROS_KIRO_CLI_PATH",
    "OUROBOROS_OPENCODE_CLI_PATH",
    "CODEX_HOME",
]


@pytest.fixture(autouse=True)
def _wide_rich_console() -> Iterator[None]:
    """Keep Rich health tables from truncating asserted status details."""
    previous_width = console._width  # noqa: SLF001
    previous_height = console._height  # noqa: SLF001
    console._width = 240  # noqa: SLF001
    console._height = 80  # noqa: SLF001
    try:
        yield
    finally:
        console._width = previous_width  # noqa: SLF001
        console._height = previous_height  # noqa: SLF001


def _clear_auth_env(monkeypatch) -> None:
    for key in API_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _make_cli(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def _write_config(
    config_dir: Path,
    *,
    cli_path: Path | str | None = None,
    backend: str = "claude",
    extra_orchestrator: dict[str, str] | None = None,
) -> Path:
    data = {
        "orchestrator": {
            "runtime_backend": backend,
            "cli_path": str(cli_path) if cli_path is not None else None,
        },
        "llm": {"backend": "claude_code"},
        "persistence": {"database_path": "data/ouroboros.db"},
    }
    if extra_orchestrator:
        data["orchestrator"].update(extra_orchestrator)
    config_path = config_dir / "config.yaml"
    config_path.write_text(yaml.dump(data))
    configured_db = config_dir / "data" / "ouroboros.db"
    if configured_db.is_file() and configured_db.stat().st_size == 0:
        _write_execution_events(configured_db, ())
    return config_path


def _write_credentials(config_dir: Path, api_key: str = "sk-present") -> None:
    (config_dir / "credentials.yaml").write_text(
        yaml.dump({"providers": {"anthropic": {"api_key": api_key}}})
    )


def _write_execution_events(db_path: Path, events: tuple[BaseEvent, ...]) -> None:
    async def write() -> None:
        store = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await store.initialize()
        try:
            for event in events:
                await store.append(event)
        finally:
            await store.close()

    asyncio.run(write())


def test_status_auto_invalid_session_id_exits_nonzero() -> None:
    result = runner.invoke(app, ["auto", "missing"])

    assert result.exit_code == 1
    assert "auto_session_id must start with auto_" in result.output


def test_executions_lists_recent_persisted_execution_statuses(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    db_path = config_dir / "data" / "ouroboros.db"
    db_path.parent.mkdir(parents=True)
    _write_config(config_dir)
    now = datetime.now(UTC)
    _write_execution_events(
        db_path,
        (
            BaseEvent(
                type="execution.started",
                timestamp=now - timedelta(seconds=2),
                aggregate_type="execution",
                aggregate_id="exec_real",
                data={"status": "running"},
            ),
            BaseEvent(
                type="execution.terminal",
                timestamp=now,
                aggregate_type="execution",
                aggregate_id="exec_real",
                data={"status": "complete"},
            ),
            BaseEvent(
                type="execution.failed",
                timestamp=now - timedelta(seconds=1),
                aggregate_type="execution",
                aggregate_id="exec_failed",
                data={},
            ),
        ),
    )
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)

    result = runner.invoke(app, ["executions", "--limit", "2"])

    assert result.exit_code == 0
    assert "exec_real" in result.output
    assert "complete" in result.output
    assert "exec_failed" in result.output
    assert "failed" in result.output
    assert "exec-001" not in result.output


def test_executions_fails_when_configured_database_is_missing(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_config(config_dir)
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)

    result = runner.invoke(app, ["executions"])

    assert result.exit_code == 1
    assert "database does not exist" in result.output
    assert "exec-001" not in result.output


def test_executions_falls_back_to_default_runtime_database(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_config(config_dir)
    _write_execution_events(
        config_dir / "ouroboros.db",
        (
            BaseEvent(
                type="execution.terminal",
                aggregate_type="execution",
                aggregate_id="exec_runtime_default",
                data={"status": "complete"},
            ),
        ),
    )
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)

    result = runner.invoke(app, ["executions"])

    assert result.exit_code == 0
    assert "exec_runtime_default" in result.output


def test_execution_commands_use_default_database_without_config(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_execution_events(
        config_dir / "ouroboros.db",
        (
            BaseEvent(
                type="execution.terminal",
                aggregate_type="execution",
                aggregate_id="exec_no_config",
                data={"status": "complete"},
            ),
        ),
    )
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)

    list_result = runner.invoke(app, ["executions"])
    detail_result = runner.invoke(app, ["execution", "exec_no_config", "--events"])

    assert list_result.exit_code == 0
    assert detail_result.exit_code == 0
    assert "exec_no_config" in list_result.output
    assert "execution.terminal" in detail_result.output


def test_executions_ignores_child_aggregates_and_subtask_status(
    monkeypatch, tmp_path: Path
) -> None:
    config_dir = tmp_path / "config"
    db_path = config_dir / "data" / "ouroboros.db"
    db_path.parent.mkdir(parents=True)
    _write_config(config_dir)
    now = datetime.now(UTC)
    _write_execution_events(
        db_path,
        (
            BaseEvent(
                type="workflow.progress.updated",
                timestamp=now - timedelta(seconds=2),
                aggregate_type="execution",
                aggregate_id="exec_running",
                data={},
            ),
            BaseEvent(
                type="execution.subtask.updated",
                timestamp=now - timedelta(seconds=1),
                aggregate_type="execution",
                aggregate_id="exec_running",
                data={"status": "complete"},
            ),
            BaseEvent(
                type="execution.session.completed",
                timestamp=now,
                aggregate_type="execution",
                aggregate_id="exec_running:ac:child",
                data={"execution_id": "exec_running", "status": "complete"},
            ),
        ),
    )
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)

    result = runner.invoke(app, ["executions"])
    child_result = runner.invoke(app, ["execution", "exec_running:ac:child"])

    assert result.exit_code == 0
    assert "exec_running" in result.output
    assert "running" in result.output
    assert "exec_running:ac:child" not in result.output
    assert child_result.exit_code == 2
    assert "no persisted execution found" in child_result.output


def test_executions_normalizes_untrusted_terminal_status_markup(
    monkeypatch, tmp_path: Path
) -> None:
    config_dir = tmp_path / "config"
    db_path = config_dir / "data" / "ouroboros.db"
    db_path.parent.mkdir(parents=True)
    _write_config(config_dir)
    _write_execution_events(
        db_path,
        (
            BaseEvent(
                type="execution.terminal",
                aggregate_type="execution",
                aggregate_id="[bold]exec[/]",
                data={"status": "[bold red]complete[/]"},
            ),
        ),
    )
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)

    result = runner.invoke(app, ["executions"])

    assert result.exit_code == 0
    assert "[bold]exec[/]" in result.output
    assert "unknown" in result.output


def test_executions_keeps_terminal_status_after_late_progress(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    db_path = config_dir / "data" / "ouroboros.db"
    db_path.parent.mkdir(parents=True)
    _write_config(config_dir)
    now = datetime.now(UTC)
    _write_execution_events(
        db_path,
        (
            BaseEvent(
                type="execution.terminal",
                timestamp=now - timedelta(seconds=1),
                aggregate_type="execution",
                aggregate_id="exec_terminal",
                data={"session_id": "session_same", "status": "failed"},
            ),
            BaseEvent(
                type="workflow.progress.updated",
                timestamp=now,
                aggregate_type="execution",
                aggregate_id="exec_terminal",
                data={"session_id": "session_same"},
            ),
        ),
    )
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)

    list_result = runner.invoke(app, ["executions"])
    detail_result = runner.invoke(app, ["execution", "exec_terminal"])

    assert list_result.exit_code == 0
    assert "failed" in list_result.output
    assert detail_result.exit_code == 0
    assert "failed" in detail_result.output


@pytest.mark.parametrize(
    ("terminal_type", "expected_status"),
    (
        ("execution.completed", "complete"),
        ("execution.failed", "failed"),
    ),
)
def test_executions_keeps_legacy_terminal_after_late_progress(
    monkeypatch,
    tmp_path: Path,
    terminal_type: str,
    expected_status: str,
) -> None:
    config_dir = tmp_path / "config"
    db_path = config_dir / "data" / "ouroboros.db"
    db_path.parent.mkdir(parents=True)
    _write_config(config_dir)
    now = datetime.now(UTC)
    _write_execution_events(
        db_path,
        (
            BaseEvent(
                type=terminal_type,
                timestamp=now - timedelta(seconds=1),
                aggregate_type="execution",
                aggregate_id="exec_legacy_terminal",
                data={"session_id": "session_same"},
            ),
            BaseEvent(
                type="workflow.progress.updated",
                timestamp=now,
                aggregate_type="execution",
                aggregate_id="exec_legacy_terminal",
                data={"session_id": "session_same"},
            ),
        ),
    )
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)

    list_result = runner.invoke(app, ["executions"])
    detail_result = runner.invoke(app, ["execution", "exec_legacy_terminal"])

    assert list_result.exit_code == 0
    assert expected_status in list_result.output
    assert detail_result.exit_code == 0
    assert expected_status in detail_result.output


def test_executions_allows_progress_after_same_aggregate_pause(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    db_path = config_dir / "data" / "ouroboros.db"
    db_path.parent.mkdir(parents=True)
    _write_config(config_dir)
    now = datetime.now(UTC)
    _write_execution_events(
        db_path,
        (
            BaseEvent(
                type="execution.terminal",
                timestamp=now - timedelta(seconds=1),
                aggregate_type="execution",
                aggregate_id="exec_paused",
                data={"session_id": "session_same", "status": "paused"},
            ),
            BaseEvent(
                type="workflow.progress.updated",
                timestamp=now,
                aggregate_type="execution",
                aggregate_id="exec_paused",
                data={"session_id": "session_same"},
            ),
        ),
    )
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)

    list_result = runner.invoke(app, ["executions"])
    detail_result = runner.invoke(app, ["execution", "exec_paused"])

    assert list_result.exit_code == 0
    assert "running" in list_result.output
    assert detail_result.exit_code == 0
    assert "running" in detail_result.output


@pytest.mark.parametrize("terminal_status", (" FAILED ", "\tFAILED\n"))
def test_executions_normalizes_absorbing_terminal_status_whitespace(
    monkeypatch,
    tmp_path: Path,
    terminal_status: str,
) -> None:
    config_dir = tmp_path / "config"
    db_path = config_dir / "data" / "ouroboros.db"
    db_path.parent.mkdir(parents=True)
    _write_config(config_dir)
    now = datetime.now(UTC)
    _write_execution_events(
        db_path,
        (
            BaseEvent(
                type="execution.terminal",
                timestamp=now - timedelta(seconds=1),
                aggregate_type="execution",
                aggregate_id="exec_whitespace",
                data={"session_id": "session_same", "status": terminal_status},
            ),
            BaseEvent(
                type="workflow.progress.updated",
                timestamp=now,
                aggregate_type="execution",
                aggregate_id="exec_whitespace",
                data={"session_id": "session_same"},
            ),
        ),
    )
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)

    list_result = runner.invoke(app, ["executions"])
    detail_result = runner.invoke(app, ["execution", "exec_whitespace"])

    assert list_result.exit_code == 0
    assert "failed" in list_result.output
    assert detail_result.exit_code == 0
    assert "failed" in detail_result.output


@pytest.mark.parametrize(
    ("terminal_session_id", "progress_session_id"),
    ((" session_same ", "session_same"), ("", None)),
)
def test_executions_normalizes_session_ids_before_lifecycle_preselection(
    monkeypatch,
    tmp_path: Path,
    terminal_session_id: str,
    progress_session_id: str | None,
) -> None:
    config_dir = tmp_path / "config"
    db_path = config_dir / "data" / "ouroboros.db"
    db_path.parent.mkdir(parents=True)
    _write_config(config_dir)
    now = datetime.now(UTC)
    progress_data = {"session_id": progress_session_id} if progress_session_id is not None else {}
    _write_execution_events(
        db_path,
        (
            BaseEvent(
                type="execution.terminal",
                timestamp=now - timedelta(seconds=1),
                aggregate_type="execution",
                aggregate_id="exec_session_normalization",
                data={"session_id": terminal_session_id, "status": "failed"},
            ),
            BaseEvent(
                type="workflow.progress.updated",
                timestamp=now,
                aggregate_type="execution",
                aggregate_id="exec_session_normalization",
                data=progress_data,
            ),
        ),
    )
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)

    list_result = runner.invoke(app, ["executions"])
    detail_result = runner.invoke(app, ["execution", "exec_session_normalization"])

    assert list_result.exit_code == 0
    assert "failed" in list_result.output
    assert detail_result.exit_code == 0
    assert "failed" in detail_result.output


def test_executions_prefers_newer_session_for_shared_execution_id(
    monkeypatch, tmp_path: Path
) -> None:
    config_dir = tmp_path / "config"
    db_path = config_dir / "data" / "ouroboros.db"
    db_path.parent.mkdir(parents=True)
    _write_config(config_dir)
    now = datetime.now(UTC)
    _write_execution_events(
        db_path,
        (
            BaseEvent(
                type="execution.terminal",
                timestamp=now - timedelta(seconds=1),
                aggregate_type="execution",
                aggregate_id="exec_shared",
                data={"session_id": "session_old", "status": "complete"},
            ),
            BaseEvent(
                type="execution.run.configuration_resolved",
                timestamp=now,
                aggregate_type="execution",
                aggregate_id="exec_shared",
                data={"session_id": "session_new"},
            ),
        ),
    )
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)

    list_result = runner.invoke(app, ["executions"])
    detail_result = runner.invoke(app, ["execution", "exec_shared"])
    events_result = runner.invoke(app, ["execution", "exec_shared", "--events"])

    assert list_result.exit_code == 0
    assert "running" in list_result.output
    assert detail_result.exit_code == 0
    assert "running" in detail_result.output
    assert events_result.exit_code == 0
    assert "running" in events_result.output.split("Execution Events", maxsplit=1)[0]


def test_execution_status_joins_pause_then_resume_session_cluster(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    db_path = config_dir / "data" / "ouroboros.db"
    db_path.parent.mkdir(parents=True)
    _write_config(config_dir)
    now = datetime.now(UTC)
    _write_execution_events(
        db_path,
        (
            BaseEvent(
                type="orchestrator.session.started",
                timestamp=now - timedelta(seconds=3),
                aggregate_type="session",
                aggregate_id="orch_resume",
                data={"execution_id": "exec_original", "seed_id": "seed_resume"},
            ),
            BaseEvent(
                type="execution.terminal",
                timestamp=now - timedelta(seconds=2),
                aggregate_type="execution",
                aggregate_id="exec_original",
                data={"session_id": "orch_resume", "status": "paused"},
            ),
            BaseEvent(
                type="workflow.progress.updated",
                timestamp=now,
                aggregate_type="execution",
                aggregate_id="orch_resume",
                data={"session_id": "orch_resume", "completed_count": 1},
            ),
        ),
    )
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)

    list_result = runner.invoke(app, ["executions"])
    detail_result = runner.invoke(app, ["execution", "exec_original"])
    events_result = runner.invoke(app, ["execution", "exec_original", "--events"])

    assert list_result.exit_code == 0
    assert "exec_original" in list_result.output
    assert "orch_resume" not in list_result.output
    assert "running" in list_result.output
    assert detail_result.exit_code == 0
    assert "running" in detail_result.output
    assert "workflow.progress.updated" in events_result.output
    assert "execution.terminal" in events_result.output


def test_execution_status_keeps_cross_aggregate_terminal_after_late_progress(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    db_path = config_dir / "data" / "ouroboros.db"
    db_path.parent.mkdir(parents=True)
    _write_config(config_dir)
    now = datetime.now(UTC)
    _write_execution_events(
        db_path,
        (
            BaseEvent(
                type="orchestrator.session.started",
                timestamp=now - timedelta(seconds=3),
                aggregate_type="session",
                aggregate_id="orch_complete",
                data={"execution_id": "exec_original", "seed_id": "seed_resume"},
            ),
            BaseEvent(
                type="execution.terminal",
                timestamp=now - timedelta(seconds=1),
                aggregate_type="execution",
                aggregate_id="exec_original",
                data={"session_id": "orch_complete", "status": "complete"},
            ),
            BaseEvent(
                type="workflow.progress.updated",
                timestamp=now,
                aggregate_type="execution",
                aggregate_id="orch_complete",
                data={"session_id": "orch_complete", "completed_count": 1},
            ),
        ),
    )
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)

    list_result = runner.invoke(app, ["executions"])
    detail_result = runner.invoke(app, ["execution", "exec_original"])
    events_result = runner.invoke(app, ["execution", "exec_original", "--events"])

    assert list_result.exit_code == 0
    assert "exec_original" in list_result.output
    assert "orch_complete" not in list_result.output
    assert "complete" in list_result.output
    assert detail_result.exit_code == 0
    assert "complete" in detail_result.output
    assert "workflow.progress.updated" in events_result.output
    assert "execution.terminal" in events_result.output


def test_execution_status_preserves_each_root_session_before_cross_aggregate_fold(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    db_path = config_dir / "data" / "ouroboros.db"
    db_path.parent.mkdir(parents=True)
    _write_config(config_dir)
    now = datetime.now(UTC)
    _write_execution_events(
        db_path,
        (
            BaseEvent(
                type="orchestrator.session.started",
                timestamp=now - timedelta(seconds=3),
                aggregate_type="session",
                aggregate_id="newer",
                data={"execution_id": "exec_shared", "seed_id": "seed_resume"},
            ),
            BaseEvent(
                type="execution.terminal",
                timestamp=now - timedelta(seconds=2),
                aggregate_type="execution",
                aggregate_id="exec_shared",
                data={"session_id": "newer", "status": "failed"},
            ),
            BaseEvent(
                type="execution.terminal",
                timestamp=now - timedelta(seconds=1),
                aggregate_type="execution",
                aggregate_id="exec_shared",
                data={"session_id": "older", "status": "failed"},
            ),
            BaseEvent(
                type="workflow.progress.updated",
                timestamp=now,
                aggregate_type="execution",
                aggregate_id="newer",
                data={"session_id": "newer", "completed_count": 1},
            ),
        ),
    )
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)

    list_result = runner.invoke(app, ["executions"])
    detail_result = runner.invoke(app, ["execution", "exec_shared"])

    assert list_result.exit_code == 0
    assert "exec_shared" in list_result.output
    assert "failed" in list_result.output
    assert detail_result.exit_code == 0
    assert "failed" in detail_result.output


def test_execution_finds_terminal_beyond_first_event_page(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    db_path = config_dir / "data" / "ouroboros.db"
    db_path.parent.mkdir(parents=True)
    _write_config(config_dir)
    now = datetime.now(UTC)
    progress_events = tuple(
        BaseEvent(
            type="workflow.progress.updated",
            timestamp=now + timedelta(microseconds=index),
            aggregate_type="execution",
            aggregate_id="exec_paged_terminal",
            data={"session_id": "session_paged"},
        )
        for index in range(500)
    )
    terminal = BaseEvent(
        type="execution.terminal",
        timestamp=now - timedelta(seconds=1),
        aggregate_type="execution",
        aggregate_id="exec_paged_terminal",
        data={"session_id": "session_paged", "status": "failed"},
    )
    _write_execution_events(db_path, (terminal, *progress_events))
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)

    result = runner.invoke(app, ["execution", "exec_paged_terminal"])
    list_result = runner.invoke(app, ["executions"])

    assert result.exit_code == 0
    assert "failed" in result.output
    assert list_result.exit_code == 0
    assert "failed" in list_result.output


def test_executions_limit_counts_distinct_root_executions(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    db_path = config_dir / "data" / "ouroboros.db"
    db_path.parent.mkdir(parents=True)
    _write_config(config_dir)
    now = datetime.now(UTC)
    noisy_events = tuple(
        BaseEvent(
            type="workflow.progress.updated",
            timestamp=now + timedelta(microseconds=index),
            aggregate_type="execution",
            aggregate_id="exec_noisy",
            data={},
        )
        for index in range(20)
    )
    other_event = BaseEvent(
        type="workflow.progress.updated",
        timestamp=now - timedelta(seconds=1),
        aggregate_type="execution",
        aggregate_id="exec_other",
        data={},
    )
    _write_execution_events(db_path, (*noisy_events, other_event))
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)

    result = runner.invoke(app, ["executions", "--limit", "2"])

    assert result.exit_code == 0
    assert "exec_noisy" in result.output
    assert "exec_other" in result.output


def test_execution_shows_persisted_details_and_events(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    db_path = config_dir / "data" / "ouroboros.db"
    db_path.parent.mkdir(parents=True)
    _write_config(config_dir)
    now = datetime.now(UTC)
    _write_execution_events(
        db_path,
        (
            BaseEvent(
                type="execution.started",
                timestamp=now - timedelta(seconds=1),
                aggregate_type="execution",
                aggregate_id="exec_detail",
                data={"status": "running"},
            ),
            BaseEvent(
                type="execution.terminal",
                timestamp=now,
                aggregate_type="execution",
                aggregate_id="exec_detail",
                data={"status": "complete"},
            ),
        ),
    )
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)

    result = runner.invoke(app, ["execution", "exec_detail", "--events"])

    assert result.exit_code == 0
    assert "exec_detail" in result.output
    assert "complete" in result.output
    assert "execution.started" in result.output
    assert "execution.terminal" in result.output
    assert "Would show" not in result.output


def test_execution_events_uses_one_resolved_snapshot(monkeypatch, tmp_path: Path) -> None:
    now = datetime.now(UTC)
    running = BaseEvent(
        type="workflow.progress.updated",
        timestamp=now - timedelta(seconds=1),
        aggregate_type="execution",
        aggregate_id="exec_advancing",
        data={"status": "running"},
    )
    failed = BaseEvent(
        type="execution.terminal",
        timestamp=now,
        aggregate_type="execution",
        aggregate_id="exec_advancing",
        data={"status": "failed"},
    )
    path_reads = 0
    event_reads = 0

    def resolved_path() -> Path:
        nonlocal path_reads
        path_reads += 1
        return tmp_path / f"events-{path_reads}.db"

    async def advancing_events(
        _db_path: Path,
        _execution_id: str,
        *,
        include_all: bool,
    ) -> list[BaseEvent]:
        nonlocal event_reads
        event_reads += 1
        return [running] if event_reads == 1 else [failed, running]

    monkeypatch.setattr(
        "ouroboros.cli.commands.status._configured_event_store_path",
        resolved_path,
    )
    monkeypatch.setattr(
        "ouroboros.cli.commands.status._execution_events",
        advancing_events,
    )

    result = runner.invoke(app, ["execution", "exec_advancing", "--events"])

    header, event_table = result.output.split("Execution Events", maxsplit=1)
    assert result.exit_code == 0
    assert not ("running" in header and "failed" in event_table)
    assert path_reads == 1
    assert event_reads == 1


def test_execution_unknown_id_exits_two(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    db_path = config_dir / "data" / "ouroboros.db"
    db_path.parent.mkdir(parents=True)
    _write_config(config_dir)
    _write_execution_events(db_path, ())
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)

    result = runner.invoke(app, ["execution", "[bold]exec_missing[/]"])

    assert result.exit_code == 2
    assert "no persisted execution found" in result.output
    assert "[bold]exec_missing[/]" in result.output


def test_health_reports_all_ok(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    data_dir = config_dir / "data"
    data_dir.mkdir(parents=True)
    _clear_auth_env(monkeypatch)
    cli = _make_cli(tmp_path / "bin" / "claude")
    db = data_dir / "ouroboros.db"
    db.write_text("")
    _write_config(config_dir, cli_path=cli)
    _write_credentials(config_dir)
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)

    result = runner.invoke(app, ["health"])

    assert result.exit_code == 0
    assert "Configuration" in result.output
    assert "Database" in result.output
    assert "Runtime backend" in result.output
    assert "Credentials" in result.output
    assert "sk-present" not in result.output
    assert "key present" in result.output


def test_health_rejects_zero_byte_database(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    data_dir = config_dir / "data"
    data_dir.mkdir(parents=True)
    _clear_auth_env(monkeypatch)
    cli = _make_cli(tmp_path / "bin" / "claude")
    _write_config(config_dir, cli_path=cli)
    (data_dir / "ouroboros.db").write_text("")
    _write_credentials(config_dir)
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)

    result = runner.invoke(app, ["health"])

    assert result.exit_code == 1
    assert "invalid event store" in result.output


def test_health_rejects_incompatible_event_store_schema(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    data_dir = config_dir / "data"
    data_dir.mkdir(parents=True)
    _clear_auth_env(monkeypatch)
    cli = _make_cli(tmp_path / "bin" / "claude")
    _write_config(config_dir, cli_path=cli)
    with sqlite3.connect(data_dir / "ouroboros.db") as connection:
        connection.execute("CREATE TABLE events(foo TEXT)")
    _write_credentials(config_dir)
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)

    result = runner.invoke(app, ["health"])

    assert result.exit_code == 1
    assert "invalid event store" in result.output


def test_health_exits_nonzero_on_config_load_failure(monkeypatch, tmp_path: Path) -> None:
    _clear_auth_env(monkeypatch)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text("orchestrator: []")
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)

    result = runner.invoke(app, ["health"])

    assert result.exit_code == 1
    assert "Configuration" in result.output
    assert "error" in result.output
    assert "Invalid config section" in result.output


def test_health_exits_nonzero_when_runtime_cli_missing(monkeypatch, tmp_path: Path) -> None:
    _clear_auth_env(monkeypatch)
    config_dir = tmp_path / "config"
    (config_dir / "data").mkdir(parents=True)
    (config_dir / "data" / "ouroboros.db").write_text("")
    missing_cli = tmp_path / "missing" / "claude"
    _write_config(config_dir, cli_path=missing_cli)
    _write_credentials(config_dir)
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)
    monkeypatch.setattr("shutil.which", lambda _name: None)

    result = runner.invoke(app, ["health"])

    assert result.exit_code == 1
    assert "Runtime backend" in result.output
    assert "CLI not found" in result.output
    assert str(missing_cli) in result.output


def test_health_emits_copyable_full_detail_lines_for_long_diagnostics(
    monkeypatch, tmp_path: Path
) -> None:
    _clear_auth_env(monkeypatch)
    narrow_runner = CliRunner(env={"COLUMNS": "80"})
    config_dir = tmp_path / ("config-" + "c" * 120)
    (config_dir / "data").mkdir(parents=True)
    missing_cli = tmp_path / ("very-long-runtime-path-" + "x" * 180) / "claude"
    _write_config(config_dir, cli_path=missing_cli)
    _write_credentials(config_dir)
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)
    monkeypatch.setattr("shutil.which", lambda _name: None)

    result = narrow_runner.invoke(app, ["health"])

    assert result.exit_code == 1
    expected_config = config_dir / "config.yaml"
    expected_database = config_dir / "data" / "ouroboros.db"
    assert f"Configuration: ok - {expected_config}" in result.output
    assert (
        f"Database: warning - missing; will be created on first run: {expected_database}"
    ) in result.output
    assert f"Runtime backend: error - claude CLI not found: {missing_cli}" in result.output


def test_health_exits_nonzero_when_credentials_file_missing(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    (config_dir / "data").mkdir(parents=True)
    _clear_auth_env(monkeypatch)
    cli = _make_cli(tmp_path / "bin" / "claude")
    _write_execution_events(config_dir / "data" / "ouroboros.db", ())
    _write_config(config_dir, cli_path=cli)
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)

    result = runner.invoke(app, ["health"])

    assert result.exit_code == 1
    assert "Credentials" in result.output
    assert "missing" in result.output
    assert "credentials.yaml" in result.output
    assert "API_KEY" not in result.output


def test_health_warns_but_exits_zero_for_missing_database_and_empty_key(
    monkeypatch, tmp_path: Path
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    _clear_auth_env(monkeypatch)
    cli = _make_cli(tmp_path / "bin" / "claude")
    _write_config(config_dir, cli_path=cli)
    (config_dir / "credentials.yaml").write_text(
        yaml.dump({"providers": {"anthropic": {"api_key": ""}}})
    )
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)

    result = runner.invoke(app, ["health"])

    assert result.exit_code == 0
    assert "Database" in result.output
    assert "warning" in result.output
    assert "will be created on first run" in result.output
    assert "key is empty" in result.output


def test_health_accepts_provider_api_key_from_environment(monkeypatch, tmp_path: Path) -> None:
    _clear_auth_env(monkeypatch)
    config_dir = tmp_path / "config"
    (config_dir / "data").mkdir(parents=True)
    cli = _make_cli(tmp_path / "bin" / "claude")
    (config_dir / "data" / "ouroboros.db").write_text("")
    _write_config(config_dir, cli_path=cli)
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")

    result = runner.invoke(app, ["health"])

    assert result.exit_code == 0
    assert "ANTHROPIC_API_KEY present" in result.output
    assert "sk-from-env" not in result.output


def test_health_uses_llm_backend_environment_override_for_codex_oauth(
    monkeypatch, tmp_path: Path
) -> None:
    _clear_auth_env(monkeypatch)
    config_dir = tmp_path / "config"
    (config_dir / "data").mkdir(parents=True)
    cli = _make_cli(tmp_path / "bin" / "claude")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text("{}")
    (config_dir / "data" / "ouroboros.db").write_text("")
    _write_config(config_dir, cli_path=cli)
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)
    monkeypatch.setenv("OUROBOROS_LLM_BACKEND", "codex")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    result = runner.invoke(app, ["health"])

    assert result.exit_code == 0
    assert "codex OAuth file present" in result.output


def test_health_rejects_configured_runtime_cli_that_is_not_executable(
    monkeypatch, tmp_path: Path
) -> None:
    _clear_auth_env(monkeypatch)
    config_dir = tmp_path / "config"
    (config_dir / "data").mkdir(parents=True)
    cli = tmp_path / "bin" / "claude"
    cli.parent.mkdir()
    cli.write_text("#!/bin/sh\n")
    cli.chmod(0o644)
    (config_dir / "data" / "ouroboros.db").write_text("")
    _write_config(config_dir, cli_path=cli)
    _write_credentials(config_dir)
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)
    monkeypatch.setattr("shutil.which", lambda _name: None)

    result = runner.invoke(app, ["health"])

    assert result.exit_code == 1
    assert "Runtime backend" in result.output
    assert "CLI not found" in result.output


def test_health_honors_agent_runtime_and_cli_path_environment_overrides(
    monkeypatch, tmp_path: Path
) -> None:
    _clear_auth_env(monkeypatch)
    config_dir = tmp_path / "config"
    (config_dir / "data").mkdir(parents=True)
    stale_claude = tmp_path / "missing" / "claude"
    codex_cli = _make_cli(tmp_path / "custom-bin" / "codex")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text("{}")
    (config_dir / "data" / "ouroboros.db").write_text("")
    _write_config(config_dir, cli_path=stale_claude, backend="claude")
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)
    monkeypatch.setenv("OUROBOROS_AGENT_RUNTIME", "codex")
    monkeypatch.setenv("OUROBOROS_CODEX_CLI_PATH", str(codex_cli))
    monkeypatch.setenv("OUROBOROS_LLM_BACKEND", "codex")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    result = runner.invoke(app, ["health"])

    assert result.exit_code == 0
    assert f"codex: {codex_cli}" in result.output
    assert str(stale_claude) not in result.output
    assert "codex OAuth file present" in result.output


def test_health_honors_gjc_cli_path_environment_override(monkeypatch, tmp_path: Path) -> None:
    _clear_auth_env(monkeypatch)
    config_dir = tmp_path / "config"
    (config_dir / "data").mkdir(parents=True)
    gjc_cli = _make_cli(tmp_path / "custom-bin" / "gjc")
    (config_dir / "data" / "ouroboros.db").write_text("")
    _write_config(config_dir, backend="gjc", extra_orchestrator={"gjc_cli_path": None})
    _write_credentials(config_dir)
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)
    monkeypatch.setenv("OUROBOROS_GJC_CLI_PATH", str(gjc_cli))

    result = runner.invoke(app, ["health"])

    assert result.exit_code == 0
    assert f"gjc: {gjc_cli}" in result.output


def test_health_honors_effective_backend_configured_cli_when_runtime_env_overrides(
    monkeypatch, tmp_path: Path
) -> None:
    _clear_auth_env(monkeypatch)
    config_dir = tmp_path / "config"
    (config_dir / "data").mkdir(parents=True)
    stale_claude = tmp_path / "missing" / "claude"
    codex_cli = _make_cli(tmp_path / "configured-bin" / "codex")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text("{}")
    (config_dir / "data" / "ouroboros.db").write_text("")
    _write_config(
        config_dir,
        cli_path=stale_claude,
        backend="claude",
        extra_orchestrator={"codex_cli_path": str(codex_cli)},
    )
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)
    monkeypatch.setenv("OUROBOROS_AGENT_RUNTIME", "codex")
    monkeypatch.setenv("OUROBOROS_LLM_BACKEND", "codex")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    result = runner.invoke(app, ["health"])

    assert result.exit_code == 0
    assert f"codex: {codex_cli}" in result.output
    assert str(stale_claude) not in result.output


def test_health_treats_copilot_as_cli_authenticated_not_openai_key_backend(
    monkeypatch, tmp_path: Path
) -> None:
    _clear_auth_env(monkeypatch)
    config_dir = tmp_path / "config"
    (config_dir / "data").mkdir(parents=True)
    copilot_cli = _make_cli(tmp_path / "bin" / "copilot")
    _write_execution_events(config_dir / "data" / "ouroboros.db", ())
    (config_dir / "config.yaml").write_text(
        yaml.dump(
            {
                "orchestrator": {
                    "runtime_backend": "copilot",
                    "copilot_cli_path": str(copilot_cli),
                },
                "llm": {"backend": "copilot"},
                "persistence": {"database_path": "data/ouroboros.db"},
            }
        )
    )
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)

    result = runner.invoke(app, ["health"])

    assert result.exit_code == 0
    assert "copilot uses local CLI authentication" in result.output
    assert "OPENAI_API_KEY" not in result.output


def test_health_accepts_claude_sdk_default_without_configured_cli(
    monkeypatch, tmp_path: Path
) -> None:
    _clear_auth_env(monkeypatch)
    config_dir = tmp_path / "config"
    (config_dir / "data").mkdir(parents=True)
    (config_dir / "data" / "ouroboros.db").write_text("")
    _write_config(config_dir, cli_path=None, backend="claude")
    _write_credentials(config_dir)
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)
    monkeypatch.setattr("shutil.which", lambda _name: None)

    result = runner.invoke(app, ["health"])

    assert result.exit_code == 0
    assert "Runtime backend" in result.output
    assert "claude: SDK default" in result.output


def test_health_reports_malformed_credentials_without_crashing(monkeypatch, tmp_path: Path) -> None:
    _clear_auth_env(monkeypatch)
    config_dir = tmp_path / "config"
    (config_dir / "data").mkdir(parents=True)
    cli = _make_cli(tmp_path / "bin" / "claude")
    (config_dir / "data" / "ouroboros.db").write_text("")
    _write_config(config_dir, cli_path=cli)
    (config_dir / "credentials.yaml").write_text("[]")
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)

    result = runner.invoke(app, ["health"])

    assert result.exit_code == 1
    assert "Credentials" in result.output
    assert "must contain a mapping" in result.output


def test_health_reports_malformed_credentials_providers_without_crashing(
    monkeypatch, tmp_path: Path
) -> None:
    _clear_auth_env(monkeypatch)
    config_dir = tmp_path / "config"
    (config_dir / "data").mkdir(parents=True)
    cli = _make_cli(tmp_path / "bin" / "claude")
    (config_dir / "data" / "ouroboros.db").write_text("")
    _write_config(config_dir, cli_path=cli)
    (config_dir / "credentials.yaml").write_text("providers: []")
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)

    result = runner.invoke(app, ["health"])

    assert result.exit_code == 1
    assert "Credentials" in result.output
    assert "credentials providers must be a mapping" in result.output


def test_health_uses_kiro_cli_default_name(monkeypatch, tmp_path: Path) -> None:
    _clear_auth_env(monkeypatch)
    config_dir = tmp_path / "config"
    (config_dir / "data").mkdir(parents=True)
    (config_dir / "data" / "ouroboros.db").write_text("")
    _write_config(config_dir, cli_path=None, backend="kiro")
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)
    monkeypatch.setenv("OUROBOROS_LLM_BACKEND", "kiro")
    monkeypatch.setattr(
        "shutil.which",
        lambda name: "/opt/bin/kiro-cli" if name == "kiro-cli" else None,
    )

    result = runner.invoke(app, ["health"])

    assert result.exit_code == 0
    assert "kiro: /opt/bin/kiro-cli" in result.output
    assert "kiro CLI not found" not in result.output


def test_health_errors_for_malformed_provider_credentials(monkeypatch, tmp_path: Path) -> None:
    _clear_auth_env(monkeypatch)
    config_dir = tmp_path / "config"
    (config_dir / "data").mkdir(parents=True)
    cli = _make_cli(tmp_path / "bin" / "claude")
    (config_dir / "data" / "ouroboros.db").write_text("")
    _write_config(config_dir, cli_path=cli)
    (config_dir / "credentials.yaml").write_text("providers:\n  anthropic: []\n")
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)

    result = runner.invoke(app, ["health"])

    assert result.exit_code == 1
    assert "Credentials" in result.output
    assert "credentials provider anthropic must be a mapping" in result.output


def test_health_errors_for_unsupported_llm_backend(monkeypatch, tmp_path: Path) -> None:
    _clear_auth_env(monkeypatch)
    config_dir = tmp_path / "config"
    (config_dir / "data").mkdir(parents=True)
    cli = _make_cli(tmp_path / "bin" / "claude")
    (config_dir / "data" / "ouroboros.db").write_text("")
    _write_config(config_dir, cli_path=cli)
    _write_credentials(config_dir)
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)
    monkeypatch.setenv("OUROBOROS_LLM_BACKEND", "not-a-backend")

    result = runner.invoke(app, ["health"])

    assert result.exit_code == 1
    assert "Credentials" in result.output
    assert "Unsupported backend" in result.output
    assert "uses local CLI authentication" not in result.output
