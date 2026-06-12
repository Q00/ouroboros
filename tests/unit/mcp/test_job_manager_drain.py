"""Tests for JobManager.drain — shutdown-time terminalization of live jobs.

Without an explicit drain, job tasks are killed by ``asyncio.run`` teardown
*after* ``EventStore.close()``, so their terminal appends fail with
``PersistenceError`` and the rows stay RUNNING forever — manufacturing the
dead-owner zombie rows the #1373 reconciler then has to repair.
"""

from __future__ import annotations

import asyncio

from ouroboros.mcp import job_manager as job_manager_module
from ouroboros.mcp.job_manager import JobLinks, JobManager, JobStatus
from ouroboros.persistence.event_store import EventStore


def _build_store(tmp_path) -> EventStore:
    db_path = tmp_path / "jobs.db"
    return EventStore(f"sqlite+aiosqlite:///{db_path}")


async def _start_blocked_job(manager: JobManager, *, session_id: str | None = None):
    started = asyncio.Event()

    async def _runner() -> str:
        started.set()
        await asyncio.sleep(3600)
        return "never"

    snapshot = await manager.start_job(
        job_type="test_drain",
        initial_message="blocked",
        runner=_runner(),
        links=JobLinks(session_id=session_id),
    )
    await asyncio.wait_for(started.wait(), timeout=2.0)
    return snapshot


class TestJobManagerDrain:
    async def test_drain_persists_interrupted_before_store_closes(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)
        await store.initialize()
        try:
            snapshot = await _start_blocked_job(manager)

            drained = await manager.drain(grace_seconds=2.0)

            assert drained == 1
            final = await manager.get_snapshot(snapshot.job_id)
            assert final.status is JobStatus.INTERRUPTED
            assert final.result_meta.get("interrupted_from_shutdown") is True
        finally:
            await store.close()

    async def test_drain_writes_interrupted_not_cancelled(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)
        await store.initialize()
        try:
            snapshot = await _start_blocked_job(manager)

            await manager.drain(grace_seconds=2.0)

            events, _ = await store.get_events_after("job", snapshot.job_id, last_row_id=0)
            types = [event.type for event in events]
            assert "mcp.job.interrupted" in types
            assert "mcp.job.cancelled" not in types
        finally:
            await store.close()

    async def test_drain_skips_jobs_owned_by_live_external_holder(
        self, tmp_path, monkeypatch
    ) -> None:
        """A live heartbeat holder in another process owns the terminal state."""
        store = _build_store(tmp_path)
        manager = JobManager(store)
        await store.initialize()
        try:
            snapshot = await _start_blocked_job(manager, session_id="sess_external")
            monkeypatch.setattr(job_manager_module, "is_holder_alive", lambda _session_id: True)
            monkeypatch.setattr(
                job_manager_module,
                "is_owned_by_current_process",
                lambda _session_id: False,
            )

            await manager.drain(grace_seconds=2.0)

            final = await manager.get_snapshot(snapshot.job_id)
            assert not final.is_terminal, (
                "drain must not terminalize a job whose live external holder "
                "is the progress authority"
            )
        finally:
            await store.close()

    async def test_drain_terminalizes_wedged_job_directly(self, tmp_path) -> None:
        """A runner that swallows cancellation still gets a terminal row."""
        store = _build_store(tmp_path)
        manager = JobManager(store)
        await store.initialize()
        try:
            started = asyncio.Event()

            async def _stubborn_runner() -> str:
                started.set()
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    pass  # swallows the drain's cancel...
                await asyncio.sleep(3600)  # ...and keeps running past the grace
                return "never"

            snapshot = await manager.start_job(
                job_type="test_drain_wedged",
                initial_message="wedged",
                runner=_stubborn_runner(),
            )
            await asyncio.wait_for(started.wait(), timeout=2.0)

            await manager.drain(grace_seconds=0.2)

            final = await manager.get_snapshot(snapshot.job_id)
            assert final.status is JobStatus.INTERRUPTED
        finally:
            for task in [*manager._tasks.values(), *manager._runner_tasks.values()]:
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                *manager._tasks.values(),
                *manager._runner_tasks.values(),
                return_exceptions=True,
            )
            await store.close()

    async def test_drain_with_no_jobs_is_noop(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)
        await store.initialize()
        try:
            assert await manager.drain(grace_seconds=0.1) == 0
        finally:
            await store.close()

    async def test_user_cancel_still_writes_cancelled_when_not_draining(self, tmp_path) -> None:
        """The draining branch must not change normal cancel semantics."""
        store = _build_store(tmp_path)
        manager = JobManager(store)
        await store.initialize()
        try:
            snapshot = await _start_blocked_job(manager)
            runner = manager._runner_tasks[snapshot.job_id]
            job_task = manager._tasks[snapshot.job_id]
            runner.cancel()
            await asyncio.gather(job_task, return_exceptions=True)

            final = await manager.get_snapshot(snapshot.job_id)
            assert final.status is JobStatus.CANCELLED
        finally:
            await store.close()


class TestCleanupExpiredJobs:
    async def test_cleanup_evicts_recovery_locks_and_terminalized_markers(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)
        await store.initialize()
        try:

            async def _quick() -> str:
                return "done"

            snapshot = await manager.start_job(
                job_type="test_ttl", initial_message="quick", runner=_quick()
            )
            job_task = manager._tasks.get(snapshot.job_id)
            if job_task is not None:
                await asyncio.gather(job_task, return_exceptions=True)
            # Simulate registry residue that the TTL sweep must also evict.
            manager._recovery_locks[snapshot.job_id] = asyncio.Lock()
            manager._monitor_terminalized_jobs.add(snapshot.job_id)

            from datetime import timedelta

            removed = await manager.cleanup_expired_jobs(ttl=timedelta(seconds=0))

            assert removed == 1
            assert snapshot.job_id not in manager._recovery_locks
            assert snapshot.job_id not in manager._monitor_terminalized_jobs
        finally:
            await store.close()
