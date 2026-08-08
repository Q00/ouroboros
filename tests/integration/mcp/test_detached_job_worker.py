"""Process-boundary acceptance tests for durable MCP background jobs."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import platform
import subprocess
import sys
import textwrap
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ouroboros.mcp.detached_jobs import (
    DetachedJobAcceptanceTimeout,
    DetachedJobRequest,
    decode_tool_error_rejection,
    encode_tool_error_rejection,
    launch_detached_job,
    status_path_for,
    write_private_json,
)
from ouroboros.mcp.errors import MCPToolError
from ouroboros.mcp.job_manager import JobLinks, JobManager, JobStatus
from ouroboros.mcp.server.adapter import create_ouroboros_server
from ouroboros.mcp.tools.background import start_background_tool_job
from ouroboros.orchestrator.heartbeat import is_process_identity_alive
from ouroboros.orchestrator.persisted_process_identity import (
    persisted_process_owner_alive,
)
from ouroboros.persistence import lineage_claims
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


def test_detached_tool_error_rejection_round_trips_only_typed_fields() -> None:
    error = MCPToolError(
        "seed rejected",
        server_name="ouroboros-mcp",
        tool_name="ouroboros_evolve_step",
        error_code="invalid_seed",
        is_retriable=True,
        details={"lineage_id": "lin_1", "reasons": ["syntax"]},
    )

    payload = encode_tool_error_rejection(error)
    restored = decode_tool_error_rejection(payload)

    assert type(restored) is MCPToolError
    assert restored.message == error.message
    assert restored.server_name == error.server_name
    assert restored.tool_name == error.tool_name
    assert restored.error_code == error.error_code
    assert restored.is_retriable is error.is_retriable
    assert restored.details == error.details


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update(protocol="python.exception"),
        lambda payload: payload.update(exception_type="builtins.SystemExit"),
        lambda payload: payload.update(version=2),
        lambda payload: payload.update(message={"not": "text"}),
        lambda payload: payload.update(server_name=7),
        lambda payload: payload.update(tool_name=["ouroboros_evolve_step"]),
        lambda payload: payload.update(error_code=500),
        lambda payload: payload.update(is_retriable="false"),
        lambda payload: payload.update(details=["not", "an", "object"]),
    ],
)
def test_detached_tool_error_rejection_rejects_malformed_artifacts(mutate) -> None:  # type: ignore[no-untyped-def]
    payload = encode_tool_error_rejection(MCPToolError("seed rejected"))
    mutate(payload)

    with pytest.raises(ValueError):
        decode_tool_error_rejection(payload)


def test_detached_tool_error_rejection_rejects_non_json_details() -> None:
    error = MCPToolError("seed rejected", details={"unsafe": object()})

    with pytest.raises(ValueError, match="JSON-safe"):
        encode_tool_error_rejection(error)


@pytest.mark.asyncio
@pytest.mark.parametrize("spoof", ["job_id", "worker_pid", "request_tool_name"])
async def test_detached_launcher_fails_closed_and_cleans_spoofed_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spoof: str,
) -> None:
    """A swapped or malformed artifact is never reconstructed as a trusted error."""
    from ouroboros.mcp import detached_jobs

    database_url = f"sqlite+aiosqlite:///{tmp_path / 'events.db'}"
    store = EventStore(database_url)
    manager = JobManager(store, durable_jobs=True)
    job_id = await manager.allocate_job_id()
    state_dir = tmp_path / "detached-jobs"
    monkeypatch.setattr(detached_jobs, "detached_jobs_dir", lambda: state_dir)

    class FakeProcess:
        pid = 4242

        @staticmethod
        def poll() -> int:
            return 0

    def fake_spawn(request_path: Path, *, cwd: str) -> FakeProcess:
        del cwd
        payload = {
            "state": "rejected",
            "worker_pid": FakeProcess.pid,
            "job_id": job_id,
            "request_tool_name": "ouroboros_start_evolve_step",
            "rejection": encode_tool_error_rejection(MCPToolError("seed rejected")),
        }
        if spoof == "job_id":
            payload["job_id"] = "job_swapped"
        elif spoof == "worker_pid":
            payload["worker_pid"] = 9999
        else:
            payload["request_tool_name"] = "ouroboros_start_execute_seed"
        write_private_json(status_path_for(request_path), payload, exclusive=False)
        return FakeProcess()

    monkeypatch.setattr(detached_jobs, "_spawn_worker", fake_spawn)
    request = DetachedJobRequest(
        job_id=job_id,
        tool_name="ouroboros_start_evolve_step",
        arguments={"lineage_id": "lin_spoof"},
        database_url=database_url,
        cwd=str(Path.cwd()),
    )

    try:
        with pytest.raises(RuntimeError, match="Detached worker rejection|Malformed"):
            await launch_detached_job(
                job_manager=manager,
                event_store=store,
                request=request,
            )
        assert manager._reserved_job_ids == set()
        assert not state_dir.joinpath(f"{job_id}.json").exists()
        assert not state_dir.joinpath(f"{job_id}.status.json").exists()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_production_detached_evolve_rejects_malformed_seed_before_job_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default file-backed server returns the worker's typed preclaim error."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OUROBOROS_DASHBOARD", "0")
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'events.db'}"
    store = EventStore(database_url)
    server = create_ouroboros_server(
        event_store=store,
        opencode_mode="subprocess",
    )
    lineage_id = "lin_detached_malformed_seed"

    try:
        result = await server.call_tool(
            "ouroboros_start_evolve_step",
            {
                "lineage_id": lineage_id,
                "seed_content": "goal: [unterminated",
                "execute": False,
            },
        )

        assert result.is_err
        assert type(result.error) is MCPToolError
        assert result.error.message.startswith("Failed to parse seed_content:")
        assert result.error.server_name is None
        assert result.error.tool_name == "ouroboros_evolve_step"
        assert result.error.error_code is None
        assert result.error.is_retriable is False
        assert result.error.details == {}
        assert server.job_manager._reserved_job_ids == set()
        assert await store.query_events(event_type="mcp.job.created") == []

        handler_claim = await lineage_claims.observe(
            store,
            scope="evolve-handler",
            lineage_id=lineage_id,
        )
        core_claim = await lineage_claims.observe(
            store,
            scope="evolve-core",
            lineage_id=lineage_id,
        )
        assert handler_claim is not None and handler_claim.completed
        assert core_claim is None

        state_dir = tmp_path / "home" / ".ouroboros" / "detached-jobs"
        assert list(state_dir.glob("*")) == []
    finally:
        await server.shutdown()


@pytest.mark.asyncio
async def test_slow_acceptance_returns_structured_status_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live slow worker yields a status-check receipt, never a parse-only message."""

    class FakeManager:
        durable_jobs_enabled = True

        async def allocate_job_id(self) -> str:
            return "job_acceptance_pending"

        def claim_forced_inline_allocation(self, _job_id: str) -> bool:
            return False

    async def fake_launch_detached_job(**_kwargs):  # type: ignore[no-untyped-def]
        raise DetachedJobAcceptanceTimeout(
            job_id="job_acceptance_pending",
            worker_pid=4242,
            timeout_seconds=0.1,
        )

    monkeypatch.setattr(
        "ouroboros.mcp.tools.background.launch_detached_job",
        fake_launch_detached_job,
    )
    event_store = SimpleNamespace(
        database_url="sqlite+aiosqlite:///events.db",
        supports_cross_process_workers=True,
    )

    with pytest.raises(MCPToolError) as exc_info:
        await start_background_tool_job(
            job_manager=FakeManager(),  # type: ignore[arg-type]
            event_store=event_store,  # type: ignore[arg-type]
            job_type="execute_seed",
            intent="execute_seed",
            process_scope="execute_seed:exec_1",
            initial_message="Queued seed execution",
            links=JobLinks(execution_id="exec_1"),
            work_fn=AsyncMock(),
            cancelled_text="cancelled",
            detached_tool_name="ouroboros_start_execute_seed",
            detached_arguments={"seed_content": "goal: test"},
        )

    error = exc_info.value
    assert error.error_code == "detached_job_acceptance_pending"
    assert error.details == {
        "status": "acceptance_pending",
        "job_id": "job_acceptance_pending",
        "worker_pid": 4242,
        "startup_timeout_seconds": 0.1,
        "worker_continues": True,
        "retry_start_allowed": False,
        "status_check": {
            "tool": "ouroboros_job_status",
            "arguments": {"job_id": "job_acceptance_pending"},
        },
    }


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
        if platform.system() == "Linux":
            owner_identity = created.data.get("owner_identity")
            assert isinstance(owner_identity, dict)
            assert owner_identity["pid"] == owner_pid
            assert persisted_process_owner_alive(created.data) is True
        else:
            assert "owner_identity" not in created.data

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
