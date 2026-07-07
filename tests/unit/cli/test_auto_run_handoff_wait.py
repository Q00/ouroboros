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
from ouroboros.auto.state import (
    AutoPhase,
    AutoPipelineState,
    AutoResumeCapability,
    AutoStore,
)
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
    """A COMPLETE-but-handoff-only result, the state the wait path acts on.

    Matches what ``AutoPipeline._result()`` actually emits for a COMPLETE
    handoff: ``resume_capability=AutoResumeCapability.NONE`` (because
    ``AutoPipelineState.resume_capability()`` maps COMPLETE -> NONE). The wait
    reconciliation must therefore *upgrade* a blocked-but-resumable outcome to
    RESUME explicitly — it cannot inherit RESUME from the input.
    """
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
        resume_capability=AutoResumeCapability.NONE,
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
        # Genuine failure keeps the non-resumable capability (NOT upgraded).
        assert reconciled.resume_capability is AutoResumeCapability.NONE
    finally:
        await _cancel_manager_tasks(manager)
        await store.close()


def _completed_handoff_state(tmp_path, job_id: str) -> AutoPipelineState:
    """Build the durable state exactly as ``AutoPipeline.run`` leaves it after a
    run handoff: phase COMPLETE, with the persisted run handle."""
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.job_id = job_id
    state.run_handoff_status = RUN_HANDOFF_STARTED_STATUS
    # CREATED -> ... -> RUN -> COMPLETE, matching the pipeline's fresh path.
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.RUN, "run")
    state.transition(AutoPhase.COMPLETE, "execution started for grade A Seed")
    return state


@pytest.mark.asyncio
async def test_wait_blocked_verdict_persists_resumable_durable_state(tmp_path) -> None:
    """A non-success wait verdict must correct the DURABLE state, not just memory.

    The pipeline saved COMPLETE at handoff. After the wait observes a paused
    run, a fresh load from the store (as a later ``ouroboros auto --resume``
    would do) must NOT see COMPLETE/product-complete but a resumable state.
    This is the durability the in-memory-only reconciliation could not provide.
    """
    jobs = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'jobs.db'}")
    manager = JobManager(jobs)
    auto_store = AutoStore(tmp_path / "auto")
    try:

        async def _runner() -> MCPToolResult:
            return MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text="Seed Execution PAUSED"),),
                is_error=False,
                meta={"status": "paused", "success": None},
            )

        started = await manager.start_job(
            job_type="execute", initial_message="queued", runner=_runner()
        )
        state = _completed_handoff_state(tmp_path, started.job_id)
        auto_store.save(state)
        # Durable state starts as COMPLETE (the stale, wrong verdict).
        assert auto_store.load(state.auto_session_id).phase is AutoPhase.COMPLETE

        result = AutoPipelineResult(
            status="complete",
            auto_session_id=state.auto_session_id,
            phase="complete",
            job_id=started.job_id,
            run_handoff_status=RUN_HANDOFF_STARTED_STATUS,
            resume_capability=AutoResumeCapability.NONE,
        )

        reconciled = await _await_run_handoff_terminal(
            result,
            job_manager=manager,
            event_store=jobs,
            quiet=True,
            state=state,
            store=auto_store,
        )

        # In-memory verdict is blocked + resumable.
        assert reconciled.status == "blocked"
        assert reconciled.resume_capability is AutoResumeCapability.RESUME

        # DURABLE state, loaded fresh (new store instance) as --resume would:
        # NOT COMPLETE, and classifies as resumable.
        reloaded = AutoStore(tmp_path / "auto").load(state.auto_session_id)
        assert reloaded.phase is not AutoPhase.COMPLETE
        assert reloaded.phase is AutoPhase.RUN
        assert reloaded.job_id == started.job_id
        assert reloaded.resume_capability() is AutoResumeCapability.RESUME
    finally:
        await _cancel_manager_tasks(manager)
        await jobs.close()


@pytest.mark.asyncio
async def test_wait_success_keeps_durable_state_complete(tmp_path) -> None:
    """Genuine success must leave the durable state COMPLETE (fast-path intact)."""
    jobs = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'jobs.db'}")
    manager = JobManager(jobs)
    auto_store = AutoStore(tmp_path / "auto")
    try:

        async def _runner() -> MCPToolResult:
            return MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text="2/2 ACs satisfied"),),
                is_error=False,
                meta={"status": "completed", "success": True},
            )

        started = await manager.start_job(
            job_type="execute", initial_message="queued", runner=_runner()
        )
        state = _completed_handoff_state(tmp_path, started.job_id)
        auto_store.save(state)

        result = AutoPipelineResult(
            status="complete",
            auto_session_id=state.auto_session_id,
            phase="complete",
            job_id=started.job_id,
            run_handoff_status=RUN_HANDOFF_STARTED_STATUS,
            resume_capability=AutoResumeCapability.NONE,
        )

        reconciled = await _await_run_handoff_terminal(
            result,
            job_manager=manager,
            event_store=jobs,
            quiet=True,
            state=state,
            store=auto_store,
        )

        assert reconciled.status == "complete"
        reloaded = AutoStore(tmp_path / "auto").load(state.auto_session_id)
        assert reloaded.phase is AutoPhase.COMPLETE
    finally:
        await _cancel_manager_tasks(manager)
        await jobs.close()


@pytest.mark.asyncio
async def test_wait_paused_run_is_not_complete_and_stays_resumable(tmp_path) -> None:
    """JobStatus.COMPLETED + result_meta status 'paused' must NOT read as success.

    ``ExecuteSeedHandler`` maps ``SessionStatus.PAUSED`` (e.g. usage-limit
    pause) to a job result of ``status="paused", success=None`` while the JOB
    itself completes. The complete-product resume gate blocks paused runs; the
    CLI wait path must preserve that contract: paused surfaces as blocked with
    the run handle and resume capability preserved — never complete.
    """
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'jobs.db'}")
    manager = JobManager(store)
    try:

        async def _runner() -> MCPToolResult:
            return MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text="Seed Execution PAUSED"),),
                is_error=False,
                meta={"status": "paused", "success": None},
            )

        started = await manager.start_job(
            job_type="execute", initial_message="queued", runner=_runner()
        )
        result = _handoff_only_result(started.job_id)
        # Production input: a COMPLETE handoff carries NONE, so a passing
        # RESUME assertion below proves an explicit upgrade, not inheritance.
        assert result.resume_capability is AutoResumeCapability.NONE

        reconciled = await _await_run_handoff_terminal(
            result,
            job_manager=manager,
            event_store=store,
            quiet=True,
        )

        # The JOB reached COMPLETED, but the EXECUTION paused.
        snapshot = await manager.get_snapshot(started.job_id)
        assert snapshot.status is JobStatus.COMPLETED
        assert snapshot.result_meta.get("status") == "paused"

        # NOT successful completion.
        assert reconciled.status == "blocked"
        assert reconciled.blocker is not None and "paused" in reconciled.blocker
        # Upgraded to RESUME (from the NONE input) with the run handle intact.
        assert reconciled.resume_capability is AutoResumeCapability.RESUME
        assert reconciled.job_id == started.job_id
        assert reconciled.execution_id == "exec_wait"
        assert reconciled.run_session_id == "orch_wait"
    finally:
        await _cancel_manager_tasks(manager)
        await store.close()


@pytest.mark.asyncio
async def test_wait_completed_job_without_success_meta_is_not_complete(tmp_path) -> None:
    """Allowlist parity: unknown terminal meta must not pass as run success."""
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'jobs.db'}")
    manager = JobManager(store)
    try:

        async def _runner() -> MCPToolResult:
            return MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text="weird terminal"),),
                is_error=False,
                meta={"status": "unknown"},
            )

        started = await manager.start_job(
            job_type="execute", initial_message="queued", runner=_runner()
        )
        result = _handoff_only_result(started.job_id)
        assert result.resume_capability is AutoResumeCapability.NONE

        reconciled = await _await_run_handoff_terminal(
            result,
            job_manager=manager,
            event_store=store,
            quiet=True,
        )

        assert reconciled.status == "blocked"
        assert reconciled.blocker is not None
        assert "without confirming terminal run success" in reconciled.blocker
        # Upgraded from the NONE input — allowlist-blocked runs are resumable.
        assert reconciled.resume_capability is AutoResumeCapability.RESUME
    finally:
        await _cancel_manager_tasks(manager)
        await store.close()


@pytest.mark.asyncio
async def test_wait_deadline_cancels_wedged_job_and_reports_bounded_verdict(tmp_path) -> None:
    """A never-terminal execute job must not outlive the pipeline deadline.

    The default wait is bounded by the same top-level ``--timeout`` budget the
    pipeline advertises: on expiry the job is cancelled cleanly (cancellation
    event written) and the CLI reports blocked/timeout with the resume handle
    preserved — never complete, never waiting past the bound.
    """
    import time as time_module

    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'jobs.db'}")
    manager = JobManager(store)
    try:
        started_running = asyncio.Event()

        async def _wedged_runner() -> MCPToolResult:
            started_running.set()
            await asyncio.sleep(3600)  # never terminates within the test
            raise AssertionError("unreachable")

        started = await manager.start_job(
            job_type="execute", initial_message="queued", runner=_wedged_runner()
        )
        await asyncio.wait_for(started_running.wait(), timeout=5)
        result = _handoff_only_result(started.job_id)
        assert result.resume_capability is AutoResumeCapability.NONE

        wait_started = time_module.monotonic()
        reconciled = await asyncio.wait_for(
            _await_run_handoff_terminal(
                result,
                job_manager=manager,
                event_store=store,
                quiet=True,
                deadline_at=time_module.monotonic() + 0.5,
            ),
            # Outer guard: bound (0.5s) + cancel grace (5s) + margin. The wait
            # must return on its own well within this.
            timeout=15,
        )
        elapsed = time_module.monotonic() - wait_started

        # Returned within the bound (deadline + cancel grace), not wedged.
        assert elapsed < 10

        # NOT complete: blocked/timeout verdict with resume guidance.
        assert reconciled.status == "blocked"
        assert reconciled.blocker is not None
        assert "deadline" in reconciled.blocker
        assert "--resume auto_wait" in reconciled.blocker
        # Upgraded from the NONE input — a deadline-cancelled run is resumable.
        assert reconciled.resume_capability is AutoResumeCapability.RESUME
        assert reconciled.job_id == started.job_id

        # The job was cancelled cleanly via the existing cancel path.
        snapshot = await manager.get_snapshot(started.job_id)
        assert snapshot.status in {JobStatus.CANCELLED, JobStatus.CANCEL_REQUESTED}
        for _ in range(100):
            snapshot = await manager.get_snapshot(started.job_id)
            if snapshot.is_terminal:
                break
            await asyncio.sleep(0.05)
        assert snapshot.status is JobStatus.CANCELLED
        events, _cursor = await store.get_events_after("job", started.job_id, 0)
        assert any(event.type == "mcp.job.cancelled" for event in events)
    finally:
        await _cancel_manager_tasks(manager)
        await store.close()


def test_deadline_blocked_result_exit_code_is_nonzero() -> None:
    """CLI exit code is non-zero for the bounded deadline/blocked verdict."""
    from dataclasses import replace as dc_replace

    async def fake_run_auto(**_kwargs: object) -> AutoPipelineResult:
        return dc_replace(
            _handoff_only_result("job_wedged"),
            status="blocked",
            blocker="run wait deadline exhausted (top-level pipeline --timeout budget)",
        )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(auto_command, "_run_auto", fake_run_auto)
        result = runner.invoke(app, ["auto", "safe wedged goal"])

    assert result.exit_code == 1
    output = _plain(result.output)
    assert "deadline" in output


def test_paused_run_exit_code_reflects_non_complete() -> None:
    """CLI exit code is non-zero when the waited run reconciles to paused/blocked."""
    from dataclasses import replace as dc_replace

    async def fake_run_auto(**_kwargs: object) -> AutoPipelineResult:
        return dc_replace(
            _handoff_only_result("job_paused"),
            status="blocked",
            blocker="run execution paused before completion; resume the paused run",
        )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(auto_command, "_run_auto", fake_run_auto)
        result = runner.invoke(app, ["auto", "safe paused goal"])

    assert result.exit_code == 1
    output = _plain(result.output)
    assert "paused" in output


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
