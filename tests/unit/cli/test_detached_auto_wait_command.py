"""Focused tests for detached auto wait CLI observability."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
import re

from typer.testing import CliRunner

from ouroboros.events.base import BaseEvent
from ouroboros.mcp.job_manager import JobLinks, JobManager, JobStatus
from ouroboros.mcp.tools.job_handlers import JobResultHandler, JobWaitHandler
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.persistence.event_store import EventStore

runner = CliRunner()


def test_cli_detached_auto_wait_reports_in_progress_background_work(monkeypatch, tmp_path) -> None:
    """Verify the wait command gives stable status for running detached auto work."""
    from ouroboros.cli.commands import job as job_command
    from ouroboros.cli.main import app

    db_url = f"sqlite+aiosqlite:///{tmp_path / 'detached-auto-wait.db'}"
    job_id = "job_auto_wait_running_focus"
    session_id = "auto_session_wait_running_focus"
    timestamp = datetime(2026, 5, 29, 12, 0, tzinfo=UTC)

    async def _persist_running_detached_auto_job() -> None:
        store = EventStore(db_url)
        await store.initialize()
        try:
            await store.append(
                BaseEvent(
                    id="evt_auto_wait_created",
                    type="mcp.job.created",
                    timestamp=timestamp,
                    aggregate_type="job",
                    aggregate_id=job_id,
                    data={
                        "job_type": "auto",
                        "status": JobStatus.QUEUED.value,
                        "message": "Queued detached auto",
                        "links": {
                            "session_id": session_id,
                            "execution_id": None,
                            "lineage_id": None,
                        },
                    },
                )
            )
            await store.append(
                BaseEvent(
                    id="evt_auto_wait_running",
                    type="mcp.job.updated",
                    timestamp=timestamp,
                    aggregate_type="job",
                    aggregate_id=job_id,
                    data={
                        "status": JobStatus.RUNNING.value,
                        "message": "Running auto",
                        "links": {
                            "session_id": session_id,
                            "execution_id": None,
                            "lineage_id": None,
                        },
                    },
                )
            )
        finally:
            await store.close()

    asyncio.run(_persist_running_detached_auto_job())

    monkeypatch.setattr(
        job_command,
        "JobWaitHandler",
        lambda: JobWaitHandler(event_store=EventStore(db_url)),
    )

    command = ["job", "wait", job_id, "--cursor", "2", "--timeout-seconds", "0"]
    result = runner.invoke(app, command)
    repeated = runner.invoke(app, command)

    expected_output = (
        f"## Job: {job_id}\n"
        "\n"
        "**Type**: auto\n"
        "**Status**: running\n"
        "**Terminal**: false\n"
        "**Status Category**: non_terminal\n"
        "**Tracking**: detached auto tracked background work\n"
        "**Message**: Running auto\n"
        "**Cursor**: 2\n"
        "\n"
        "### Links\n"
        f"**Session ID**: {session_id}\n"
        "\n"
        "No new job-level events during this wait window.\n"
    )

    assert result.exit_code == 0
    assert repeated.exit_code == 0
    assert result.output == expected_output
    assert repeated.output == expected_output
    assert "Use `ouroboros_job_result` to fetch the full terminal output." not in result.output
    assert not re.search(r"\b\d{4}-\d{2}-\d{2}[T ][0-9:.+-]", result.output)


def test_cli_detached_auto_wait_invalid_handle_fails_with_actionable_error(
    monkeypatch, tmp_path
) -> None:
    """Verify wait fails stably for an unknown detached auto handle."""
    from ouroboros.cli.commands import job as job_command
    from ouroboros.cli.main import app

    db_url = f"sqlite+aiosqlite:///{tmp_path / 'missing-detached-auto-wait.db'}"
    invalid_job_id = "missing_detached_auto_wait"

    monkeypatch.setattr(
        job_command,
        "JobWaitHandler",
        lambda: JobWaitHandler(event_store=EventStore(db_url)),
    )

    command = ["job", "wait", invalid_job_id, "--timeout-seconds", "0"]
    result = runner.invoke(app, command)
    repeated = runner.invoke(app, command)

    assert result.exit_code == 1
    assert repeated.exit_code == 1
    assert repeated.output == result.output
    assert f"Job not found: {invalid_job_id}. Wait unavailable." in result.output
    assert "Check the job" in result.output
    assert "handle returned by detached auto start" in result.output
    assert "ouroboros job wait" in result.output
    assert invalid_job_id in result.output
    assert "**Status**: completed" not in result.output
    assert "**Terminal**: true" not in result.output
    assert "Use `ouroboros_job_result` to fetch the full terminal output." not in result.output
    assert not re.search(r"\b\d{4}-\d{2}-\d{2}[T ][0-9:.+-]", result.output)


def test_cli_detached_auto_wait_failed_job_exits_nonzero_with_stable_error(
    monkeypatch, tmp_path
) -> None:
    """Verify a failed detached auto wait command is observable and non-zero."""
    from ouroboros.cli.commands import job as job_command
    from ouroboros.cli.main import app

    db_url = f"sqlite+aiosqlite:///{tmp_path / 'failed-detached-auto-wait.db'}"

    async def _failed_auto_runner() -> MCPToolResult:
        return MCPToolResult(
            content=(
                MCPContentItem(
                    type=ContentType.TEXT,
                    text="detached auto failed\nstatus=failed\nterminal=true",
                ),
            ),
            is_error=True,
            meta={
                "auto_session_id": "auto_session_failed_wait_focus",
                "status": "failed",
                "error_code": "seed_gate_failed",
            },
        )

    async def _persist_failed_detached_auto_job() -> str:
        store = EventStore(db_url)
        manager = JobManager(store)
        try:
            started = await manager.start_job(
                job_type="auto",
                initial_message="Queued detached auto",
                runner=_failed_auto_runner(),
                links=JobLinks(session_id="auto_session_failed_wait_focus"),
            )
            deadline = asyncio.get_running_loop().time() + 1
            snapshot = await manager.get_snapshot(started.job_id)
            while snapshot.status is not JobStatus.FAILED:
                if asyncio.get_running_loop().time() >= deadline:
                    raise AssertionError(
                        f"job {started.job_id} did not fail; last={snapshot.status}"
                    )
                await asyncio.sleep(0.01)
                snapshot = await manager.get_snapshot(started.job_id)
            return started.job_id
        finally:
            await store.close()

    job_id = asyncio.run(_persist_failed_detached_auto_job())

    monkeypatch.setattr(
        job_command,
        "JobWaitHandler",
        lambda: JobWaitHandler(event_store=EventStore(db_url)),
    )

    command = ["job", "wait", job_id, "--timeout-seconds", "0"]
    result = runner.invoke(app, command)
    repeated = runner.invoke(app, command)

    assert result.exit_code == 1
    assert repeated.exit_code == 1
    assert repeated.output == result.output
    assert f"## Job: {job_id}" in result.output
    assert "**Type**: auto" in result.output
    assert "**Status**: failed" in result.output
    assert "**Terminal**: true" in result.output
    assert "**Status Category**: terminal" in result.output
    assert "**Message**: Job failed" in result.output
    assert "**Session ID**: auto_session_failed_wait_focus" in result.output
    assert "Use `ouroboros_job_result` to fetch the full terminal output." in result.output
    assert "detached auto tracked background work" not in result.output
    assert "status=completed" not in result.output
    assert not re.search(r"\b\d{4}-\d{2}-\d{2}[T ][0-9:.+-]", result.output)


def test_cli_detached_auto_result_invalid_handle_fails_stably(monkeypatch, tmp_path) -> None:
    """Verify result retrieval fails deterministically for an invalid auto handle."""
    from ouroboros.cli.commands import job as job_command
    from ouroboros.cli.main import app

    db_url = f"sqlite+aiosqlite:///{tmp_path / 'missing-detached-auto-result.db'}"
    invalid_job_id = "missing_detached_auto"

    monkeypatch.setattr(
        job_command,
        "JobResultHandler",
        lambda: JobResultHandler(event_store=EventStore(db_url)),
    )

    command = ["job", "result", invalid_job_id]
    result = runner.invoke(app, command)
    repeated = runner.invoke(app, command)

    expected_message = f"Job handle not found: {invalid_job_id}. Result unavailable."

    assert result.exit_code == 1
    assert repeated.exit_code == 1
    assert repeated.output == result.output
    assert expected_message in result.output
    assert not re.search(r"\b\d{4}-\d{2}-\d{2}[T ][0-9:.+-]", result.output)


def test_cli_detached_auto_result_missing_run_is_actionable_not_terminal_success(
    monkeypatch, tmp_path
) -> None:
    """Independently verify missing detached result retrieval fails usefully."""
    from ouroboros.cli.commands import job as job_command
    from ouroboros.cli.main import app

    db_url = f"sqlite+aiosqlite:///{tmp_path / 'unknown-detached-auto-result.db'}"
    unknown_job_id = "unknown_detached_auto_result"

    monkeypatch.setattr(
        job_command,
        "JobResultHandler",
        lambda: JobResultHandler(event_store=EventStore(db_url)),
    )

    command = ["job", "result", unknown_job_id]
    first = runner.invoke(app, command)
    second = runner.invoke(app, command)

    expected_message = f"Job handle not found: {unknown_job_id}. Result unavailable."

    assert first.exit_code == 1
    assert second.exit_code == 1
    assert second.output == first.output
    assert expected_message in first.output
    assert "Job handle" in first.output
    assert "Result unavailable" in first.output
    assert unknown_job_id in first.output
    assert "**Status**: completed" not in first.output
    assert "**Terminal**: true" not in first.output
    assert "status=completed" not in first.output
    assert "terminal=true" not in first.output
    assert "Use `ouroboros_job_result` to fetch the full terminal output." not in first.output
    assert not re.search(r"\b\d{4}-\d{2}-\d{2}[T ][0-9:.+-]", first.output)


def test_cli_detached_auto_result_returns_completed_artifact(monkeypatch, tmp_path) -> None:
    """Verify the focused CLI result command retrieves completed detached auto output."""
    from ouroboros.cli.commands import job as job_command
    from ouroboros.cli.main import app

    db_url = f"sqlite+aiosqlite:///{tmp_path / 'completed-detached-auto-result.db'}"

    async def _completed_auto_runner() -> MCPToolResult:
        return MCPToolResult(
            content=(
                MCPContentItem(
                    type=ContentType.TEXT,
                    text=(
                        "detached auto completed result\n"
                        "status=completed\n"
                        "terminal=true\n"
                        "artifact=seed.yaml"
                    ),
                ),
            ),
            meta={"auto_session_id": "auto_session_result_focus"},
        )

    async def _persist_completed_detached_auto_job() -> str:
        store = EventStore(db_url)
        manager = JobManager(store)
        try:
            started = await manager.start_job(
                job_type="auto",
                initial_message="Queued detached auto",
                runner=_completed_auto_runner(),
                links=JobLinks(session_id="auto_session_result_focus"),
            )
            deadline = asyncio.get_running_loop().time() + 1
            snapshot = await manager.get_snapshot(started.job_id)
            while snapshot.status is not JobStatus.COMPLETED:
                if asyncio.get_running_loop().time() >= deadline:
                    raise AssertionError(
                        f"job {started.job_id} did not complete; last={snapshot.status}"
                    )
                await asyncio.sleep(0.01)
                snapshot = await manager.get_snapshot(started.job_id)
            return started.job_id
        finally:
            await store.close()

    job_id = asyncio.run(_persist_completed_detached_auto_job())

    monkeypatch.setattr(
        job_command,
        "JobResultHandler",
        lambda: JobResultHandler(event_store=EventStore(db_url)),
    )

    command = ["job", "result", job_id]
    result = runner.invoke(app, command)
    repeated = runner.invoke(app, command)

    expected_output = (
        "detached auto completed result\nstatus=completed\nterminal=true\nartifact=seed.yaml\n"
    )

    assert result.exit_code == 0
    assert repeated.exit_code == 0
    assert result.output == expected_output
    assert repeated.output == expected_output
    assert not re.search(r"\b\d{4}-\d{2}-\d{2}[T ][0-9:.+-]", result.output)


def test_cli_detached_auto_result_returns_stable_failure_error_payload(
    monkeypatch, tmp_path
) -> None:
    """Verify completed failure retrieval exposes a stable minimal error payload."""
    from ouroboros.cli.commands import job as job_command
    from ouroboros.cli.main import app

    db_url = f"sqlite+aiosqlite:///{tmp_path / 'failed-detached-auto-result.db'}"
    error_payload = {
        "status": "failed",
        "terminal": True,
        "error": {
            "code": "seed_gate_failed",
            "message": "Seed gate failed",
        },
    }

    async def _failed_auto_runner() -> MCPToolResult:
        return MCPToolResult(
            content=(
                MCPContentItem(
                    type=ContentType.TEXT,
                    text=json.dumps(error_payload, sort_keys=True),
                ),
            ),
            is_error=True,
            meta={
                "auto_session_id": "auto_session_failed_result_focus",
                "status": "failed",
                "error_code": "seed_gate_failed",
            },
        )

    async def _persist_failed_detached_auto_job() -> str:
        store = EventStore(db_url)
        manager = JobManager(store)
        try:
            started = await manager.start_job(
                job_type="auto",
                initial_message="Queued detached auto",
                runner=_failed_auto_runner(),
                links=JobLinks(session_id="auto_session_failed_result_focus"),
            )
            deadline = asyncio.get_running_loop().time() + 1
            snapshot = await manager.get_snapshot(started.job_id)
            while snapshot.status is not JobStatus.FAILED:
                if asyncio.get_running_loop().time() >= deadline:
                    raise AssertionError(
                        f"job {started.job_id} did not fail; last={snapshot.status}"
                    )
                await asyncio.sleep(0.01)
                snapshot = await manager.get_snapshot(started.job_id)
            return started.job_id
        finally:
            await store.close()

    job_id = asyncio.run(_persist_failed_detached_auto_job())

    monkeypatch.setattr(
        job_command,
        "JobResultHandler",
        lambda: JobResultHandler(event_store=EventStore(db_url)),
    )

    command = ["job", "result", job_id]
    result = runner.invoke(app, command)
    repeated = runner.invoke(app, command)

    assert result.exit_code == 1
    assert repeated.exit_code == 1
    assert repeated.output == result.output
    assert json.loads(result.output) == error_payload
    assert set(json.loads(result.output)) == {"status", "terminal", "error"}
    assert set(json.loads(result.output)["error"]) == {"code", "message"}
    assert "seed_gate_failed" in result.output
    assert "Seed gate failed" in result.output
    assert "status=completed" not in result.output
    assert "artifact=seed.yaml" not in result.output
    assert not re.search(r"\b\d{4}-\d{2}-\d{2}[T ][0-9:.+-]", result.output)
