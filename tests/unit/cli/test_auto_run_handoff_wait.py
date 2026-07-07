"""Tests for the direct ``ouroboros auto`` run-handoff wait behaviour.

Regression coverage for the live smoke-test bug where a direct CLI
``ouroboros auto`` started the execute run-handoff as a detached in-process
job and then exited, so ``asyncio.run`` teardown cancelled the job at ~200ms
and the seed was never executed. The fix makes the CLI wait for the job to
reach a terminal state (default) and reconciles the run verdict onto the
result, while ``--no-wait`` preserves fire-and-forget detach behaviour.
"""

from __future__ import annotations

import asyncio
import re

import pytest
from typer.testing import CliRunner

from ouroboros.auto.handoff_contract import RUN_HANDOFF_STARTED_STATUS
from ouroboros.auto.pipeline import AutoPipelineResult
from ouroboros.auto.state import AutoResumeCapability
from ouroboros.cli.commands import auto as auto_command
from ouroboros.cli.commands.auto import _await_run_handoff_terminal
from ouroboros.cli.main import app
from ouroboros.mcp.job_manager import JobManager, JobStatus
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.persistence.event_store import EventStore

runner = CliRunner()


def _plain(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


async def _cancel_manager_tasks(manager: JobManager) -> None:
    tasks = [
        *manager._tasks.values(),  # noqa: SLF001
        *manager._runner_tasks.values(),  # noqa: SLF001
        *manager._monitors.values(),  # noqa: SLF001
    ]
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _handoff_only_result(job_id: str) -> AutoPipelineResult:
    """A COMPLETE-but-handoff-only result, the state the wait path acts on."""
    return AutoPipelineResult(
        status="complete",
        auto_session_id="auto_wait",
        phase="complete",
        grade="A",
        seed_path="/tmp/seed.yaml",
        job_id=job_id,
        execution_id="exec_wait",
        run_session_id="orch_wait",
        run_handoff_status=RUN_HANDOFF_STARTED_STATUS,
        resume_capability=AutoResumeCapability.RESUME,
    )


def _terminal_run_result() -> MCPToolResult:
    return MCPToolResult(
        content=(MCPContentItem(type=ContentType.TEXT, text="run receipt: 2/2 ACs satisfied"),),
        is_error=False,
        meta={"status": "completed", "success": True},
    )


@pytest.mark.asyncio
async def test_wait_drives_run_job_to_completion_without_cancellation(tmp_path) -> None:
    """A fast completing job is awaited to COMPLETED — never cancelled on exit."""
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'jobs.db'}")
    manager = JobManager(store)
    try:

        async def _runner() -> MCPToolResult:
            return _terminal_run_result()

        started = await manager.start_job(
            job_type="execute", initial_message="queued", runner=_runner()
        )
        result = _handoff_only_result(started.job_id)

        reconciled = await _await_run_handoff_terminal(
            result,
            job_manager=manager,
            event_store=store,
            quiet=True,
        )

        # Run verdict is projected onto the auto result.
        assert reconciled.status == "complete"
        assert reconciled.execution_job_status == JobStatus.COMPLETED.value
        assert reconciled.resume_capability is AutoResumeCapability.NONE

        # The job reached a genuine COMPLETED terminal, not CANCELLED.
        snapshot = await manager.get_snapshot(started.job_id)
        assert snapshot.status is JobStatus.COMPLETED

        # No cancellation event was ever written for this job.
        events, _cursor = await store.get_events_after("job", started.job_id, 0)
        assert not any(event.type == "mcp.job.cancelled" for event in events)
    finally:
        await _cancel_manager_tasks(manager)
        await store.close()


@pytest.mark.asyncio
async def test_wait_reflects_failed_run_job_verdict(tmp_path) -> None:
    """A failing run job flips the auto result to ``failed`` so exit code is 1."""
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'jobs.db'}")
    manager = JobManager(store)
    try:

        async def _runner() -> MCPToolResult:
            raise RuntimeError("boom in executor")

        started = await manager.start_job(
            job_type="execute", initial_message="queued", runner=_runner()
        )
        result = _handoff_only_result(started.job_id)

        reconciled = await _await_run_handoff_terminal(
            result,
            job_manager=manager,
            event_store=store,
            quiet=True,
        )

        assert reconciled.status == "failed"
        assert reconciled.execution_job_status == JobStatus.FAILED.value
        assert reconciled.blocker and "boom in executor" in reconciled.blocker
    finally:
        await _cancel_manager_tasks(manager)
        await store.close()


@pytest.mark.asyncio
async def test_wait_is_noop_when_job_not_owned(tmp_path) -> None:
    """An unknown/plugin-dispatch job handle leaves the result untouched."""
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'jobs.db'}")
    manager = JobManager(store)
    try:
        result = _handoff_only_result("job_not_in_this_manager")
        reconciled = await _await_run_handoff_terminal(
            result,
            job_manager=manager,
            event_store=store,
            quiet=True,
        )
        assert reconciled is result
    finally:
        await _cancel_manager_tasks(manager)
        await store.close()


@pytest.mark.asyncio
async def test_wait_is_noop_without_job_handle(tmp_path) -> None:
    """No job_id (plugin dispatch) => nothing to wait on, result unchanged."""
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'jobs.db'}")
    manager = JobManager(store)
    try:
        result = AutoPipelineResult(
            status="complete",
            auto_session_id="auto_wait",
            phase="complete",
            run_handoff_status=RUN_HANDOFF_STARTED_STATUS,
        )
        reconciled = await _await_run_handoff_terminal(
            result,
            job_manager=manager,
            event_store=store,
            quiet=True,
        )
        assert reconciled is result
    finally:
        await _cancel_manager_tasks(manager)
        await store.close()


def test_default_cli_invocation_requests_wait() -> None:
    """Without --no-wait, the CLI asks ``_run_auto`` to wait for the run job."""
    captured: dict[str, object] = {}

    async def fake_run_auto(**kwargs: object) -> AutoPipelineResult:
        captured.update(kwargs)
        return _handoff_only_result("job_default")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(auto_command, "_run_auto", fake_run_auto)
        result = runner.invoke(app, ["auto", "safe wait goal"])

    assert result.exit_code == 0
    assert captured["wait"] is True


def test_no_wait_flag_detaches_and_prints_honest_notice() -> None:
    """--no-wait passes wait=False and warns the run won't survive exit."""
    captured: dict[str, object] = {}

    async def fake_run_auto(**kwargs: object) -> AutoPipelineResult:
        captured.update(kwargs)
        return _handoff_only_result("job_detached")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(auto_command, "_run_auto", fake_run_auto)
        result = runner.invoke(app, ["auto", "safe detach goal", "--no-wait"])

    assert result.exit_code == 0
    assert captured["wait"] is False
    output = _plain(result.output)
    assert "will NOT survive process exit" in output
    assert "job_detached" in output
