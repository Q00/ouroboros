"""``wait_for_change`` folds only new job rows on a changed tick.

Before this change a changed tick called ``get_snapshot`` and replayed the
job's whole stream from rowid 0. It now resumes the cached fold (#2328) and
falls back to that full replay whenever the fold is not provably equal to it,
so the returned snapshot is unchanged. The origin/main implementation is kept
below as the reference.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import random
from typing import Any

import pytest

from ouroboros.events.base import BaseEvent
from ouroboros.mcp import job_manager as job_manager_module
from ouroboros.mcp.job_manager import JobManager, JobSnapshot
from ouroboros.mcp.job_snapshot import fold_job_events
from ouroboros.persistence.event_store import EventStore

JOB_ID = "job_wait_tail"
T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


# Verbatim copy of JobManager.wait_for_change on origin/main (592c667f).
async def _reference_wait_for_change(
    self: JobManager,
    job_id: str,
    *,
    cursor: int = 0,
    timeout_seconds: int = 10,
) -> tuple[JobSnapshot, bool]:
    """Wait until the job aggregate receives a new event."""
    await self._ensure_initialized()
    deadline = asyncio.get_running_loop().time() + timeout_seconds

    while True:
        events, new_cursor = await self._event_store.get_events_after("job", job_id, cursor)
        if events:
            snapshot = await self.get_snapshot(job_id)
            return replace(snapshot, cursor=new_cursor), True

        snapshot = await self._get_snapshot_tail(job_id)
        if snapshot.is_terminal or asyncio.get_running_loop().time() >= deadline:
            return snapshot, False

        await asyncio.sleep(0.5)


def _job_event(event_type: str, data: dict[str, Any], at: datetime) -> BaseEvent:
    return BaseEvent(
        type=event_type, aggregate_type="job", aggregate_id=JOB_ID, data=data, timestamp=at
    )


def _created(at: datetime = T0) -> BaseEvent:
    return _job_event(
        "mcp.job.created",
        {
            "job_type": "execute_seed",
            "status": "running",
            "message": "created",
            "links": {},
            # A live owner keeps the dead-owner reconcilers out of the picture.
            **job_manager_module.current_persisted_process_owner(),
        },
        at,
    )


def _update(index: int, at: datetime, status: str = "running") -> BaseEvent:
    return _job_event(
        "mcp.job.updated",
        {
            "status": status,
            "message": f"update {index}",
            "result_meta": {"index": index},
            "links": {"execution_id": f"exec-{index}"} if index % 3 == 0 else {},
        },
        at,
    )


class _CursorSpy:
    def __init__(self, store: EventStore) -> None:
        self.job_cursors: list[int] = []
        self._original = store.get_events_after

    async def __call__(
        self, aggregate_type: str, aggregate_id: str, last_row_id: int = 0, **kwargs: Any
    ) -> tuple[list[BaseEvent], int]:
        if aggregate_type == "job":
            self.job_cursors.append(last_row_id)
        return await self._original(aggregate_type, aggregate_id, last_row_id, **kwargs)


@pytest.fixture
async def store(tmp_path):
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'jobs.db'}")
    await event_store.initialize()
    yield event_store
    await event_store.close()


async def _full_replay(store: EventStore, cursor: int) -> JobSnapshot:
    return replace(await JobManager(store).get_snapshot(JOB_ID), cursor=cursor)


class TestReplayEquivalentFlag:
    def test_full_fold_and_in_order_resumes_stay_equivalent(self) -> None:
        fold = fold_job_events(None, [_created(), _update(0, T0 + timedelta(seconds=1))], cursor=2)
        assert fold.replay_equivalent is True
        fold = fold_job_events(fold, [], cursor=2)
        assert fold.replay_equivalent is True
        later = [_update(2, T0 + timedelta(seconds=3)), _update(1, T0 + timedelta(seconds=2))]
        fold = fold_job_events(fold, later, cursor=4)
        assert fold.replay_equivalent is True

    @pytest.mark.parametrize("offset", [timedelta(seconds=-1), timedelta(0)])
    def test_resumed_row_not_after_folded_rows_clears_flag_for_good(self, offset) -> None:
        fold = fold_job_events(None, [_created(), _update(0, T0 + timedelta(seconds=5))], cursor=2)
        stale = _update(1, T0 + timedelta(seconds=5) + offset)
        fold = fold_job_events(fold, [_update(2, T0 + timedelta(seconds=9)), stale], cursor=4)
        assert fold.replay_equivalent is False
        fold = fold_job_events(fold, [_update(3, T0 + timedelta(seconds=20))], cursor=5)
        assert fold.replay_equivalent is False


class TestWaitForChangeTail:
    async def test_warm_changed_ticks_never_read_from_row_zero(self, store: EventStore) -> None:
        await store.append(_created())
        manager = JobManager(store)
        cursor = (await manager.get_snapshot(JOB_ID)).cursor
        spy = _CursorSpy(store)
        store.get_events_after = spy  # type: ignore[method-assign]
        try:
            for index in range(5):
                await store.append(_update(index, T0 + timedelta(seconds=index + 1)))
                reads_before = len(spy.job_cursors)
                snapshot, changed = await manager.wait_for_change(
                    JOB_ID, cursor=cursor, timeout_seconds=0
                )
                tick_reads = spy.job_cursors[reads_before:]
                assert changed is True
                assert snapshot.message == f"update {index}"
                # Two job reads per tick (change probe + tail), none from row 0.
                assert tick_reads == [cursor, cursor]
                assert snapshot == await _full_replay(store, snapshot.cursor)
                cursor = snapshot.cursor
        finally:
            store.get_events_after = spy._original  # type: ignore[method-assign]

    async def test_changed_tick_discards_pending_receipt_like_the_full_replay(
        self, store: EventStore
    ) -> None:
        await store.append(_created())
        current = JobManager(store)
        reference = JobManager(store)
        managers = (current, reference)
        for manager in managers:
            await manager.get_snapshot(JOB_ID)
            manager.pending_receipts.remember(job_id=JOB_ID, worker_pid=4242, timeout_seconds=1.0)
        cursor = await store.get_current_rowid()

        await current.wait_for_change(JOB_ID, cursor=cursor, timeout_seconds=0)
        await _reference_wait_for_change(reference, JOB_ID, cursor=cursor, timeout_seconds=0)
        # An idle tick never discarded the receipt, and still does not.
        assert all(manager.pending_receipts.get(JOB_ID) is not None for manager in managers)

        await store.append(_update(1, T0 + timedelta(seconds=1)))
        await current.wait_for_change(JOB_ID, cursor=cursor, timeout_seconds=0)
        await _reference_wait_for_change(reference, JOB_ID, cursor=cursor, timeout_seconds=0)
        assert all(manager.pending_receipts.get(JOB_ID) is None for manager in managers)

    async def test_late_commit_of_an_earlier_stamp_falls_back_to_full_replay(
        self, store: EventStore
    ) -> None:
        """The #2328 divergence: the tail would apply the older row last."""
        await store.append(_created())
        manager = JobManager(store)
        await manager.get_snapshot(JOB_ID)
        await store.append(_update(2, T0 + timedelta(seconds=2)))
        await manager._get_snapshot_tail(JOB_ID)  # monitor tick folds the newer row
        cursor = await store.get_current_rowid()
        await store.append(_update(1, T0 + timedelta(seconds=1)))  # stamped first, committed last
        spy = _CursorSpy(store)
        store.get_events_after = spy  # type: ignore[method-assign]
        try:
            snapshot, changed = await manager.wait_for_change(
                JOB_ID, cursor=cursor, timeout_seconds=0
            )
        finally:
            store.get_events_after = spy._original  # type: ignore[method-assign]

        assert changed is True
        assert snapshot.message == "update 2"
        assert snapshot == await _full_replay(store, snapshot.cursor)
        assert 0 in spy.job_cursors
        # The full replay re-seeded the cache, so the next changed tick is cheap again.
        assert manager._fold_cache[JOB_ID].replay_equivalent is True

    @pytest.mark.parametrize("seed", range(12))
    async def test_matches_reference_under_interleaved_appends(
        self, store: EventStore, seed: int
    ) -> None:
        """Script appends (in order, older-stamped, tied), monitor ticks and waits.

        One manager runs the new ``wait_for_change``; a second runs the
        origin/main reference. Both observe the same rows and must return the
        same ``(snapshot, changed)`` at every wait; changed ticks must also equal
        a fresh full replay.
        """
        rng = random.Random(seed)
        await store.append(_created())
        current = JobManager(store)
        reference = JobManager(store)
        newest = T0 + timedelta(seconds=1)
        client_cursor = 0
        for step in range(40):
            action = rng.random()
            if action < 0.45:
                if rng.random() < 0.3:
                    at = newest - timedelta(milliseconds=rng.randint(0, 3))
                else:
                    newest = newest + timedelta(milliseconds=rng.randint(1, 5))
                    at = newest
                status = "cancel_requested" if rng.random() < 0.1 else "running"
                await store.append(_update(step, at, status))
            elif action < 0.65:
                assert await current._get_snapshot_tail(JOB_ID) == (
                    await reference._get_snapshot_tail(JOB_ID)
                )
            else:
                if rng.random() < 0.2:
                    client_cursor = rng.randint(0, client_cursor)
                got = await current.wait_for_change(JOB_ID, cursor=client_cursor, timeout_seconds=0)
                want = await _reference_wait_for_change(
                    reference, JOB_ID, cursor=client_cursor, timeout_seconds=0
                )
                assert got == want, (seed, step)
                snapshot, changed = got
                if changed:
                    assert snapshot == await _full_replay(store, snapshot.cursor)
                client_cursor = snapshot.cursor
