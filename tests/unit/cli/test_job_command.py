"""Tests for the documented background job CLI surface."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import re

from typer.testing import CliRunner

from ouroboros.core.types import Result
from ouroboros.events.base import BaseEvent
from ouroboros.mcp.errors import MCPToolError
from ouroboros.mcp.job_manager import JobLinks, JobManager, JobStatus
from ouroboros.mcp.tools.job_handlers import JobResultHandler, JobStatusHandler, JobWaitHandler
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.persistence.event_store import EventStore

runner = CliRunner()


class _OkStatusHandler:
    async def handle(self, arguments):
        assert arguments == {"job_id": "job_123", "view": "compact"}
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text="job_123 | running"),),
                meta={"job_id": "job_123", "status": "running"},
            )
        )


class _OkWaitHandler:
    async def handle(self, arguments):
        assert arguments == {
            "job_id": "job_123",
            "cursor": 7,
            "timeout_seconds": 0,
            "view": "summary",
            "stream": "progress",
        }
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text="unchanged cursor=7"),),
                meta={"job_id": "job_123", "status": "running", "cursor": 7},
            )
        )


class _CompletedAutoWaitHandler:
    async def handle(self, arguments):
        assert arguments == {
            "job_id": "job_auto_done",
            "cursor": 0,
            "timeout_seconds": 0,
            "view": "full",
            "stream": "progress",
        }
        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=(
                            "## Job: job_auto_done\n\n"
                            "**Type**: auto\n"
                            "**Status**: completed\n"
                            "**Message**: Job complete\n"
                            "**Cursor**: 42\n"
                            "\n### Result\n"
                            "Use `ouroboros_job_result` to fetch the full terminal output."
                        ),
                    ),
                ),
                meta={
                    "job_id": "job_auto_done",
                    "status": "completed",
                    "cursor": 42,
                    "changed": True,
                    "session_id": "auto_session_done",
                },
            )
        )


class _RunningAutoWaitHandler:
    async def handle(self, arguments):
        assert arguments == {
            "job_id": "job_auto_running",
            "cursor": 0,
            "timeout_seconds": 0,
            "view": "full",
            "stream": "progress",
        }
        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=(
                            "## Job: job_auto_running\n\n"
                            "**Type**: auto\n"
                            "**Status**: running\n"
                            "**Terminal**: false\n"
                            "**Message**: Auto pipeline still running\n"
                            "**Cursor**: 11\n"
                            "\nNo new job-level events during this wait window."
                        ),
                    ),
                ),
                meta={
                    "job_id": "job_auto_running",
                    "status": "running",
                    "cursor": 11,
                    "changed": False,
                    "session_id": "auto_session_running",
                },
            )
        )


class _OkResultHandler:
    async def handle(self, arguments):
        assert arguments == {"job_id": "job_123"}
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text="final auto result"),),
                meta={"job_id": "job_123", "status": "completed"},
            )
        )


class _MissingJobHandler:
    async def handle(self, arguments):
        assert arguments == {"job_id": "missing", "view": "full"}
        return Result.err(MCPToolError("Job not found: missing", tool_name="ouroboros_job_status"))


class _InvalidWaitHandler:
    async def handle(self, arguments):
        assert arguments == {
            "job_id": "job_123",
            "cursor": 0,
            "timeout_seconds": -1,
            "view": "full",
            "stream": "progress",
        }
        return Result.err(
            MCPToolError(
                "timeout_seconds must be a non-negative integer",
                tool_name="ouroboros_job_wait",
            )
        )


def test_documented_job_status_command_exits_zero(monkeypatch) -> None:
    from ouroboros.cli.commands import job as job_command
    from ouroboros.cli.main import app

    monkeypatch.setattr(job_command, "JobStatusHandler", _OkStatusHandler)

    result = runner.invoke(app, ["job", "status", "job_123", "--view", "compact"])

    assert result.exit_code == 0
    assert "job_123 | running" in result.output


def test_documented_job_wait_command_exits_zero(monkeypatch) -> None:
    from ouroboros.cli.commands import job as job_command
    from ouroboros.cli.main import app

    monkeypatch.setattr(job_command, "JobWaitHandler", _OkWaitHandler)

    result = runner.invoke(
        app,
        ["job", "wait", "job_123", "--cursor", "7", "--view", "summary"],
    )

    assert result.exit_code == 0
    assert "unchanged cursor=7" in result.output


def test_detached_auto_job_wait_command_reports_completed_status(monkeypatch) -> None:
    from ouroboros.cli.commands import job as job_command
    from ouroboros.cli.main import app

    monkeypatch.setattr(job_command, "JobWaitHandler", _CompletedAutoWaitHandler)

    result = runner.invoke(app, ["job", "wait", "job_auto_done"])

    assert result.exit_code == 0
    assert "## Job: job_auto_done" in result.output
    assert "**Type**: auto" in result.output
    assert "**Status**: completed" in result.output
    assert "**Cursor**: 42" in result.output
    assert "Use `ouroboros_job_result` to fetch the full terminal output." in result.output


def test_detached_auto_job_wait_command_reports_running_non_terminal_status(
    monkeypatch,
) -> None:
    from ouroboros.cli.commands import job as job_command
    from ouroboros.cli.main import app

    monkeypatch.setattr(job_command, "JobWaitHandler", _RunningAutoWaitHandler)

    result = runner.invoke(app, ["job", "wait", "job_auto_running"])

    assert result.exit_code == 0
    assert "## Job: job_auto_running" in result.output
    assert "**Type**: auto" in result.output
    assert "**Status**: running" in result.output
    assert "**Terminal**: false" in result.output
    assert "**Message**: Auto pipeline still running" in result.output
    assert "**Cursor**: 11" in result.output
    assert "No new job-level events during this wait window." in result.output
    assert "Use `ouroboros_job_result` to fetch the full terminal output." not in result.output


def test_detached_auto_job_status_command_reports_tracked_running_work(
    monkeypatch, tmp_path
) -> None:
    """Verify CLI status reconstructs non-terminal detached auto background work."""
    from ouroboros.cli.commands import job as job_command
    from ouroboros.cli.main import app

    db_url = f"sqlite+aiosqlite:///{tmp_path / 'running-detached-auto-jobs.db'}"
    job_id = "job_auto_running_status"
    auto_session_id = "auto_cli_running_status"

    async def _persist_running_auto_job() -> None:
        store = EventStore(db_url)
        await store.initialize()
        try:
            await store.append(
                BaseEvent(
                    type="mcp.job.created",
                    aggregate_type="job",
                    aggregate_id=job_id,
                    data={
                        "job_type": "auto",
                        "status": JobStatus.QUEUED.value,
                        "message": "Queued detached auto",
                        "links": {
                            "session_id": auto_session_id,
                            "execution_id": None,
                            "lineage_id": None,
                        },
                    },
                )
            )
            await store.append(
                BaseEvent(
                    type="mcp.job.updated",
                    aggregate_type="job",
                    aggregate_id=job_id,
                    data={
                        "status": JobStatus.RUNNING.value,
                        "message": "Running auto",
                        "links": {
                            "session_id": auto_session_id,
                            "execution_id": None,
                            "lineage_id": None,
                        },
                    },
                )
            )
        finally:
            await store.close()

    asyncio.run(_persist_running_auto_job())

    monkeypatch.setattr(
        job_command,
        "JobStatusHandler",
        lambda: JobStatusHandler(event_store=EventStore(db_url)),
    )

    result = runner.invoke(app, ["job", "status", job_id])

    assert result.exit_code == 0
    assert f"## Job: {job_id}" in result.output
    assert "**Type**: auto" in result.output
    assert "**Status**: running" in result.output
    assert "**Terminal**: false" in result.output
    assert "**Status Category**: non_terminal" in result.output
    assert "**Tracking**: detached auto tracked background work" in result.output
    assert "**Message**: Running auto" in result.output
    assert f"**Session ID**: {auto_session_id}" in result.output
    assert "Use `ouroboros_job_result` to fetch the full terminal output." not in result.output


def test_detached_auto_job_wait_stdout_is_deterministic_for_identical_inputs(
    monkeypatch, tmp_path
) -> None:
    from ouroboros.cli.commands import job as job_command
    from ouroboros.cli.main import app

    db_url = f"sqlite+aiosqlite:///{tmp_path / 'detached-auto-stable.db'}"
    job_id = "job_auto_stable"
    auto_session_id = "auto_cli_stable"
    timestamp = datetime(2026, 5, 29, 12, 0, tzinfo=UTC)

    async def _persist_running_auto_job() -> None:
        store = EventStore(db_url)
        await store.initialize()
        try:
            await store.append(
                BaseEvent(
                    id="evt_auto_stable_created",
                    type="mcp.job.created",
                    timestamp=timestamp,
                    aggregate_type="job",
                    aggregate_id=job_id,
                    data={
                        "job_type": "auto",
                        "status": JobStatus.QUEUED.value,
                        "message": "Queued detached auto",
                        "links": {
                            "session_id": auto_session_id,
                            "execution_id": None,
                            "lineage_id": None,
                        },
                    },
                )
            )
            await store.append(
                BaseEvent(
                    id="evt_auto_stable_running",
                    type="mcp.job.updated",
                    timestamp=timestamp,
                    aggregate_type="job",
                    aggregate_id=job_id,
                    data={
                        "status": JobStatus.RUNNING.value,
                        "message": "Running auto",
                        "links": {
                            "session_id": auto_session_id,
                            "execution_id": None,
                            "lineage_id": None,
                        },
                    },
                )
            )
        finally:
            await store.close()

    asyncio.run(_persist_running_auto_job())

    monkeypatch.setattr(
        job_command,
        "JobWaitHandler",
        lambda: JobWaitHandler(event_store=EventStore(db_url)),
    )

    command = ["job", "wait", job_id, "--timeout-seconds", "0"]
    first = runner.invoke(app, command)
    second = runner.invoke(app, command)

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert first.output == second.output
    assert not re.search(r"\b\d{4}-\d{2}-\d{2}[T ][0-9:.+-]", first.output)
    assert "**Created**:" not in first.output
    assert "**Updated**:" not in first.output
    assert first.output == (
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
        f"**Session ID**: {auto_session_id}\n"
    )
    assert f"## Job: {job_id}" in first.output
    assert "**Status**: running" in first.output
    assert "**Terminal**: false" in first.output
    assert "**Status Category**: non_terminal" in first.output
    assert "No new job-level events during this wait window." not in first.output


def test_documented_job_result_command_exits_zero(monkeypatch) -> None:
    from ouroboros.cli.commands import job as job_command
    from ouroboros.cli.main import app

    monkeypatch.setattr(job_command, "JobResultHandler", _OkResultHandler)

    result = runner.invoke(app, ["job", "result", "job_123"])

    assert result.exit_code == 0
    assert "final auto result" in result.output


def test_cli_retrieves_previously_tracked_detached_auto_result(monkeypatch, tmp_path) -> None:
    """Verify CLI result retrieval reconstructs a completed detached auto job."""
    from ouroboros.cli.commands import job as job_command
    from ouroboros.cli.main import app

    db_url = f"sqlite+aiosqlite:///{tmp_path / 'detached-auto-jobs.db'}"

    async def _detached_auto_runner() -> MCPToolResult:
        await asyncio.sleep(0.01)
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
            meta={
                "auto_session_id": "auto_cli_later",
                "completed_at": "2026-05-29T12:00:00+00:00",
            },
        )

    async def _persist_completed_auto_job() -> str:
        store = EventStore(db_url)
        manager = JobManager(store)
        try:
            started = await manager.start_job(
                job_type="auto",
                initial_message="Queued auto",
                runner=_detached_auto_runner(),
                links=JobLinks(session_id="auto_cli_later"),
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

    job_id = asyncio.run(_persist_completed_auto_job())

    monkeypatch.setattr(
        job_command,
        "JobResultHandler",
        lambda: JobResultHandler(event_store=EventStore(db_url)),
    )

    result = runner.invoke(app, ["job", "result", job_id])
    repeated = runner.invoke(app, ["job", "result", job_id])

    assert result.exit_code == 0
    assert repeated.exit_code == 0
    assert repeated.output == result.output
    assert result.output == (
        "detached auto completed result\nstatus=completed\nterminal=true\nartifact=seed.yaml\n"
    )
    assert not re.search(r"\b\d{4}-\d{2}-\d{2}[T ][0-9:.+-]", result.output)
    assert "**Created**:" not in result.output
    assert "**Updated**:" not in result.output


def test_cli_retrieves_failed_detached_auto_result_distinct_from_success(
    monkeypatch, tmp_path
) -> None:
    """Verify documented CLI wait/result surfaces failed detached auto distinctly."""
    from ouroboros.cli.commands import job as job_command
    from ouroboros.cli.main import app

    db_url = f"sqlite+aiosqlite:///{tmp_path / 'failed-detached-auto-jobs.db'}"

    async def _successful_auto_runner() -> MCPToolResult:
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
            meta={"auto_session_id": "auto_cli_success", "status": "completed"},
        )

    async def _failed_auto_runner() -> MCPToolResult:
        return MCPToolResult(
            content=(
                MCPContentItem(
                    type=ContentType.TEXT,
                    text=(
                        "detached auto failed result\n"
                        "status=failed\n"
                        "terminal=true\n"
                        "error=seed gate failed"
                    ),
                ),
            ),
            is_error=True,
            meta={
                "auto_session_id": "auto_cli_failed",
                "status": "failed",
                "error_code": "seed_gate_failed",
            },
        )

    async def _wait_for_status(manager: JobManager, job_id: str, status: JobStatus) -> None:
        deadline = asyncio.get_running_loop().time() + 1
        snapshot = await manager.get_snapshot(job_id)
        while snapshot.status is not status:
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError(
                    f"job {job_id} did not become {status}; last={snapshot.status}"
                )
            await asyncio.sleep(0.01)
            snapshot = await manager.get_snapshot(job_id)

    async def _persist_terminal_auto_jobs() -> tuple[str, str]:
        store = EventStore(db_url)
        manager = JobManager(store)
        try:
            successful = await manager.start_job(
                job_type="auto",
                initial_message="Queued successful auto",
                runner=_successful_auto_runner(),
                links=JobLinks(session_id="auto_cli_success"),
            )
            failed = await manager.start_job(
                job_type="auto",
                initial_message="Queued failed auto",
                runner=_failed_auto_runner(),
                links=JobLinks(session_id="auto_cli_failed"),
            )
            await _wait_for_status(manager, successful.job_id, JobStatus.COMPLETED)
            await _wait_for_status(manager, failed.job_id, JobStatus.FAILED)
            return successful.job_id, failed.job_id
        finally:
            await store.close()

    success_job_id, failed_job_id = asyncio.run(_persist_terminal_auto_jobs())

    monkeypatch.setattr(
        job_command,
        "JobWaitHandler",
        lambda: JobWaitHandler(event_store=EventStore(db_url)),
    )
    monkeypatch.setattr(
        job_command,
        "JobResultHandler",
        lambda: JobResultHandler(event_store=EventStore(db_url)),
    )

    failed_wait = runner.invoke(app, ["job", "wait", failed_job_id])
    success_result = runner.invoke(app, ["job", "result", success_job_id])
    failed_result = runner.invoke(app, ["job", "result", failed_job_id])
    repeated_failed_result = runner.invoke(app, ["job", "result", failed_job_id])

    assert failed_wait.exit_code == 1
    assert f"## Job: {failed_job_id}" in failed_wait.output
    assert "**Type**: auto" in failed_wait.output
    assert "**Status**: failed" in failed_wait.output
    assert "**Terminal**: true" in failed_wait.output
    assert "Use `ouroboros_job_result` to fetch the full terminal output." in failed_wait.output

    assert success_result.exit_code == 0
    assert success_result.output == (
        "detached auto completed result\nstatus=completed\nterminal=true\nartifact=seed.yaml\n"
    )

    assert failed_result.exit_code == 1
    assert repeated_failed_result.exit_code == 1
    assert repeated_failed_result.output == failed_result.output
    assert failed_result.output == (
        "detached auto failed result\nstatus=failed\nterminal=true\nerror=seed gate failed\n"
    )
    assert failed_result.output != success_result.output
    assert "status=completed" not in failed_result.output
    assert "artifact=seed.yaml" not in failed_result.output
    assert not re.search(r"\b\d{4}-\d{2}-\d{2}[T ][0-9:.+-]", failed_result.output)


def test_cli_detached_auto_result_for_running_job_reports_not_ready(monkeypatch, tmp_path) -> None:
    """Verify premature CLI result retrieval emits stable pending guidance."""
    from ouroboros.cli.commands import job as job_command
    from ouroboros.cli.main import app

    db_url = f"sqlite+aiosqlite:///{tmp_path / 'running-detached-auto-result.db'}"
    job_id = "job_auto_result_running"
    auto_session_id = "auto_cli_result_running"

    async def _persist_running_auto_job() -> None:
        store = EventStore(db_url)
        await store.initialize()
        try:
            await store.append(
                BaseEvent(
                    type="mcp.job.created",
                    aggregate_type="job",
                    aggregate_id=job_id,
                    data={
                        "job_type": "auto",
                        "status": JobStatus.QUEUED.value,
                        "message": "Queued detached auto",
                        "links": {
                            "session_id": auto_session_id,
                            "execution_id": None,
                            "lineage_id": None,
                        },
                    },
                )
            )
            await store.append(
                BaseEvent(
                    type="mcp.job.updated",
                    aggregate_type="job",
                    aggregate_id=job_id,
                    data={
                        "status": JobStatus.RUNNING.value,
                        "message": "Running auto",
                        "links": {
                            "session_id": auto_session_id,
                            "execution_id": None,
                            "lineage_id": None,
                        },
                    },
                )
            )
        finally:
            await store.close()

    asyncio.run(_persist_running_auto_job())

    monkeypatch.setattr(
        job_command,
        "JobResultHandler",
        lambda: JobResultHandler(event_store=EventStore(db_url)),
    )

    result = runner.invoke(app, ["job", "result", job_id])
    repeated = runner.invoke(app, ["job", "result", job_id])

    assert result.exit_code == 0
    assert repeated.exit_code == 0
    assert repeated.output == result.output
    assert f"Job result not ready: {job_id}" in result.output
    assert "status=running" in result.output
    assert "terminal=false" in result.output
    assert "detached auto job is still tracked background work" in result.output
    assert f"wait: ouroboros job wait {job_id}" in result.output
    assert f"retrieve after terminal status: ouroboros job result {job_id}" in result.output
    assert f'mcp_wait: ouroboros_job_wait(job_id="{job_id}")' in result.output
    assert f'mcp_result: ouroboros_job_result(job_id="{job_id}")' in result.output


def test_cli_detached_auto_result_with_invalid_job_handle_exits_nonzero(
    monkeypatch, tmp_path
) -> None:
    """Verify CLI result retrieval fails clearly for an unknown detached job handle."""
    from ouroboros.cli.commands import job as job_command
    from ouroboros.cli.main import app

    db_url = f"sqlite+aiosqlite:///{tmp_path / 'missing-detached-auto-result.db'}"

    monkeypatch.setattr(
        job_command,
        "JobResultHandler",
        lambda: JobResultHandler(event_store=EventStore(db_url)),
    )

    result = runner.invoke(app, ["job", "result", "missing_detached_auto"])

    assert result.exit_code == 1
    assert "Job handle not found: missing_detached_auto. Result unavailable." in result.output


def test_cli_detached_auto_result_with_expired_job_handle_returns_persisted_result(
    monkeypatch, tmp_path
) -> None:
    """Verify CLI result retrieval keeps terminal persisted results after handle TTL."""
    from ouroboros.cli.commands import job as job_command
    from ouroboros.cli.main import app

    db_url = f"sqlite+aiosqlite:///{tmp_path / 'expired-detached-auto-result.db'}"
    job_id = "job_auto_result_expired"
    expired_at = datetime.now(UTC) - timedelta(hours=2)

    async def _persist_expired_auto_job() -> None:
        store = EventStore(db_url)
        await store.initialize()
        try:
            await store.append(
                BaseEvent(
                    id="evt_expired_auto_created",
                    type="mcp.job.created",
                    timestamp=expired_at,
                    aggregate_type="job",
                    aggregate_id=job_id,
                    data={
                        "job_type": "auto",
                        "status": JobStatus.QUEUED.value,
                        "message": "Queued detached auto",
                        "links": {
                            "session_id": "auto_cli_result_expired",
                            "execution_id": None,
                            "lineage_id": None,
                        },
                    },
                )
            )
            await store.append(
                BaseEvent(
                    id="evt_expired_auto_completed",
                    type="mcp.job.completed",
                    timestamp=expired_at + timedelta(seconds=1),
                    aggregate_type="job",
                    aggregate_id=job_id,
                    data={
                        "status": JobStatus.COMPLETED.value,
                        "message": "Job complete",
                        "result_text": "stale detached auto result",
                        "result_meta": {"auto_session_id": "auto_cli_result_expired"},
                        "is_error": False,
                    },
                )
            )
        finally:
            await store.close()

    asyncio.run(_persist_expired_auto_job())

    monkeypatch.setattr(
        job_command,
        "JobResultHandler",
        lambda: JobResultHandler(event_store=EventStore(db_url)),
    )

    result = runner.invoke(app, ["job", "result", job_id])

    assert result.exit_code == 0
    assert "stale detached auto result" in result.output
    assert f"Job handle expired: {job_id}" not in result.output


def test_cli_wait_and_status_with_invalid_detached_auto_handle_fail_stably(
    monkeypatch, tmp_path
) -> None:
    """Verify CLI wait/status fail clearly for an unknown detached auto handle."""
    from ouroboros.cli.commands import job as job_command
    from ouroboros.cli.main import app

    db_url = f"sqlite+aiosqlite:///{tmp_path / 'missing-detached-auto-status.db'}"
    invalid_job_id = "missing_detached_auto"

    monkeypatch.setattr(
        job_command,
        "JobStatusHandler",
        lambda: JobStatusHandler(event_store=EventStore(db_url)),
    )
    monkeypatch.setattr(
        job_command,
        "JobWaitHandler",
        lambda: JobWaitHandler(event_store=EventStore(db_url)),
    )

    first_status = runner.invoke(app, ["job", "status", invalid_job_id])
    second_status = runner.invoke(app, ["job", "status", invalid_job_id])
    first_wait = runner.invoke(app, ["job", "wait", invalid_job_id, "--timeout-seconds", "0"])
    second_wait = runner.invoke(app, ["job", "wait", invalid_job_id, "--timeout-seconds", "0"])

    assert first_status.exit_code == 1
    assert second_status.exit_code == 1
    assert first_wait.exit_code == 1
    assert second_wait.exit_code == 1
    assert second_status.output == first_status.output
    assert second_wait.output == first_wait.output
    assert f"Job not found: {invalid_job_id}" in first_status.output
    assert f"Job not found: {invalid_job_id}" in first_wait.output
    assert not re.search(r"\b\d{4}-\d{2}-\d{2}[T ][0-9:.+-]", first_status.output)
    assert not re.search(r"\b\d{4}-\d{2}-\d{2}[T ][0-9:.+-]", first_wait.output)


def test_invalid_job_handle_exits_nonzero(monkeypatch) -> None:
    from ouroboros.cli.commands import job as job_command
    from ouroboros.cli.main import app

    monkeypatch.setattr(job_command, "JobStatusHandler", _MissingJobHandler)

    result = runner.invoke(app, ["job", "status", "missing"])

    assert result.exit_code == 1
    assert "Job not found: missing" in result.output


def test_invalid_job_wait_argument_exits_nonzero(monkeypatch) -> None:
    from ouroboros.cli.commands import job as job_command
    from ouroboros.cli.main import app

    monkeypatch.setattr(job_command, "JobWaitHandler", _InvalidWaitHandler)

    result = runner.invoke(app, ["job", "wait", "job_123", "--timeout-seconds", "-1"])

    assert result.exit_code == 1
    assert "timeout_seconds must be a non-negative integer" in result.output
