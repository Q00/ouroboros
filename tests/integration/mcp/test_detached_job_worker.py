"""Process-boundary acceptance tests for durable MCP background jobs."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

from ouroboros.mcp.job_manager import JobManager, JobStatus
from ouroboros.orchestrator.heartbeat import is_process_identity_alive
from ouroboros.persistence.event_store import EventStore

_LAUNCH_PARENT = textwrap.dedent(
    """
    import asyncio
    import os
    from pathlib import Path
    import sys

    from ouroboros.mcp.detached_jobs import DetachedJobRequest, launch_detached_job
    from ouroboros.mcp.job_manager import JobManager
    from ouroboros.persistence.event_store import EventStore

    async def main():
        database_url, cwd, delay, tool_name = sys.argv[1:]
        store = EventStore(database_url)
        manager = JobManager(store, durable_jobs=True)
        job_id = await manager.allocate_job_id()
        argument_name = (
            "nested_delay_seconds"
            if tool_name == "__detached_nested_probe__"
            else "delay_seconds"
        )
        snapshot = await launch_detached_job(
            job_manager=manager,
            event_store=store,
            request=DetachedJobRequest(
                job_id=job_id,
                tool_name=tool_name,
                arguments={argument_name: float(delay)},
                database_url=database_url,
                cwd=cwd,
            ),
        )
        print(f"{os.getpid()} {snapshot.job_id}", flush=True)
        await store.close()

    asyncio.run(main())
    """
)


async def _wait_terminal(
    manager: JobManager,
    job_id: str,
    *,
    timeout: float = 10.0,
):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        snapshot = await manager.get_snapshot(job_id)
        if snapshot.is_terminal:
            return snapshot
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"job {job_id} did not become terminal")
        await asyncio.sleep(0.05)


def _spawn_accepting_parent(
    *,
    database_url: str,
    cwd: Path,
    home: Path,
    delay: float,
) -> tuple[int, str]:
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "OUROBOROS_DASHBOARD": "0",
        }
    )
    completed = subprocess.run(  # noqa: S603 - fixed interpreter/test program
        [
            sys.executable,
            "-c",
            _LAUNCH_PARENT,
            database_url,
            str(cwd),
            str(delay),
            "__detached_probe__",
        ],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    parent_pid, job_id = completed.stdout.strip().split()
    return int(parent_pid), job_id


def _spawn_nested_accepting_parent(
    *,
    database_url: str,
    cwd: Path,
    home: Path,
    delay: float,
) -> tuple[int, str]:
    env = os.environ.copy()
    env.update({"HOME": str(home), "OUROBOROS_DASHBOARD": "0"})
    completed = subprocess.run(  # noqa: S603 - fixed interpreter/test program
        [
            sys.executable,
            "-c",
            _LAUNCH_PARENT,
            database_url,
            str(cwd),
            str(delay),
            "__detached_nested_probe__",
        ],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    parent_pid, job_id = completed.stdout.strip().split()
    return int(parent_pid), job_id


@pytest.mark.asyncio
async def test_detached_job_survives_accepting_process_exit(tmp_path: Path) -> None:
    """The worker, not the exited MCP-like parent, owns terminal delivery."""
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'events.db'}"
    parent_pid, job_id = _spawn_accepting_parent(
        database_url=database_url,
        cwd=Path.cwd(),
        home=tmp_path / "home",
        delay=3.0,
    )

    store = EventStore(database_url)
    manager = JobManager(store)
    try:
        snapshot = await manager.get_snapshot(job_id)
        events = await store.replay("job", job_id)
        created = events[0]
        owner_pid = created.data["owner_pid"]
        owner_start_time = created.data.get("owner_start_time")

        assert owner_pid != parent_pid
        assert snapshot.status in {JobStatus.QUEUED, JobStatus.RUNNING}
        assert is_process_identity_alive(owner_pid, owner_start_time)

        terminal = await _wait_terminal(manager, job_id)
        assert terminal.status == JobStatus.COMPLETED
        assert terminal.result_text == "detached probe complete"
        final_events = await store.replay("job", job_id)
        assert all(event.type != "mcp.job.interrupted" for event in final_events)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_external_controller_cancels_detached_owner(tmp_path: Path) -> None:
    """A later MCP process delivers CANCEL_REQUESTED to the owning worker."""
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'events.db'}"
    _parent_pid, job_id = _spawn_accepting_parent(
        database_url=database_url,
        cwd=Path.cwd(),
        home=tmp_path / "home",
        delay=30.0,
    )

    store = EventStore(database_url)
    controller = JobManager(store)
    try:
        requested = await controller.cancel_job(job_id)
        assert requested.status in {JobStatus.CANCEL_REQUESTED, JobStatus.CANCELLED}

        terminal = await _wait_terminal(controller, job_id)
        assert terminal.status == JobStatus.CANCELLED
        events = await store.replay("job", job_id)
        assert any(event.type == "mcp.job.cancelled" for event in events)
        assert all(event.type != "mcp.job.interrupted" for event in events)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_nested_handoff_gets_independent_durable_owner(tmp_path: Path) -> None:
    """Auto/run-style nested handoffs outlive the top-level worker."""
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'events.db'}"
    _parent_pid, outer_job_id = _spawn_nested_accepting_parent(
        database_url=database_url,
        cwd=Path.cwd(),
        home=tmp_path / "home",
        delay=4.0,
    )

    store = EventStore(database_url)
    manager = JobManager(store)
    try:
        outer = await _wait_terminal(manager, outer_job_id)
        assert outer.status == JobStatus.COMPLETED
        nested_job_id = outer.result_meta["nested_job_id"]
        assert isinstance(nested_job_id, str)

        outer_events = await store.replay("job", outer_job_id)
        nested_events = await store.replay("job", nested_job_id)
        outer_owner = outer_events[0].data["owner_pid"]
        nested_owner = nested_events[0].data["owner_pid"]
        assert nested_owner != outer_owner

        nested = await manager.get_snapshot(nested_job_id)
        assert nested.status in {JobStatus.QUEUED, JobStatus.RUNNING}
        assert is_process_identity_alive(
            nested_owner,
            nested_events[0].data.get("owner_start_time"),
        )

        terminal = await _wait_terminal(manager, nested_job_id)
        assert terminal.status == JobStatus.COMPLETED
        assert terminal.result_text == "detached probe complete"
    finally:
        await store.close()
