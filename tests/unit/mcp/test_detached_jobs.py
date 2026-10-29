"""Platform launch contracts for durable background workers."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from ouroboros.mcp import detached_jobs
from ouroboros.mcp.errors import MCPToolError
from ouroboros.mcp.job_manager import JobManager
from ouroboros.persistence.event_store import EventStore, sqlite_database_url

_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_CONSOLE = 0x00000010


@pytest.mark.parametrize("platform_name", ["nt", "posix"])
def test_spawn_worker_preserves_launch_contract_with_platform_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform_name: str
) -> None:
    request_path = tmp_path / "worker requests" / "request.json"
    cwd = str(request_path.parent)
    parent_env = {"PATH": "inherited-search-path", "OUROBOROS_DETACHED_JOB_WORKER": "0"}
    process = SimpleNamespace(pid=4242, wait=MagicMock(return_value=0))
    popen = MagicMock(return_value=process)

    # Replace only this module's references: changing os.name globally would
    # make pathlib and pytest select a foreign platform implementation.
    monkeypatch.setattr(
        detached_jobs, "os", SimpleNamespace(name=platform_name, environ=parent_env)
    )
    monkeypatch.setattr(
        detached_jobs,
        "subprocess",
        SimpleNamespace(
            Popen=popen,
            DEVNULL=subprocess.DEVNULL,
            CREATE_NEW_PROCESS_GROUP=_CREATE_NEW_PROCESS_GROUP,
            CREATE_NO_WINDOW=_CREATE_NO_WINDOW,
            DETACHED_PROCESS=_DETACHED_PROCESS,
            CREATE_NEW_CONSOLE=_CREATE_NEW_CONSOLE,
        ),
    )
    monkeypatch.setattr(detached_jobs, "threading", SimpleNamespace(Thread=MagicMock()))

    spawned = detached_jobs._spawn_worker(request_path, cwd=cwd)

    assert spawned is process
    popen.assert_called_once()
    args, kwargs = popen.call_args
    assert args == ([sys.executable, "-m", "ouroboros.mcp.detached_worker", str(request_path)],)
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stdout"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.DEVNULL
    assert kwargs["cwd"] == cwd
    assert kwargs["close_fds"] is True
    assert kwargs["env"] == {**parent_env, "OUROBOROS_DETACHED_JOB_WORKER": "1"}
    assert kwargs["env"] is not parent_env
    assert parent_env["OUROBOROS_DETACHED_JOB_WORKER"] == "0"

    if platform_name == "nt":
        flags = kwargs["creationflags"]
        assert flags == _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW
        assert not flags & (_DETACHED_PROCESS | _CREATE_NEW_CONSOLE)
        assert "start_new_session" not in kwargs
    else:
        assert kwargs["start_new_session"] is True
        assert "creationflags" not in kwargs


def _rejection(*, pid: object = 4242, token: object = "a" * 64) -> dict:
    return {
        "state": "rejected",
        "worker_pid": pid,
        "job_id": "job_token_test",
        "request_tool_name": "ouroboros_start_evolve_step",
        "rejection": detached_jobs.encode_tool_error_rejection(MCPToolError("seed rejected")),
        "launch_token": token,
    }


@pytest.mark.parametrize("platform_name", ["nt", "posix"])
@pytest.mark.parametrize("status_pid", [4242, 9999])
def test_rejection_launch_identity_preserves_platform_pid_rules(
    monkeypatch: pytest.MonkeyPatch, platform_name: str, status_pid: int
) -> None:
    monkeypatch.setattr(detached_jobs, "os", SimpleNamespace(name=platform_name))
    arguments = {
        "job_id": "job_token_test",
        "request_tool_name": "ouroboros_start_evolve_step",
        "worker_pid": 4242,
        "launch_token": "a" * 64,
    }
    if platform_name == "posix" and status_pid != 4242:
        with pytest.raises(RuntimeError, match="unexpected worker pid"):
            detached_jobs._preacceptance_rejection(_rejection(pid=status_pid), **arguments)
    else:
        error = detached_jobs._preacceptance_rejection(_rejection(pid=status_pid), **arguments)
        assert isinstance(error, MCPToolError)
        assert error.message == "seed rejected"


@pytest.mark.parametrize("platform_name", ["nt", "posix"])
@pytest.mark.parametrize("token", [None, "", "b" * 64, "x" * 64, "a" * 63, True, 7])
def test_rejection_refuses_missing_stale_or_malformed_launch_identity(
    monkeypatch: pytest.MonkeyPatch, platform_name: str, token: object
) -> None:
    monkeypatch.setattr(detached_jobs, "os", SimpleNamespace(name=platform_name))
    status = _rejection(token=token)
    if token is None:
        del status["launch_token"]
    with pytest.raises(RuntimeError, match="Detached worker rejection"):
        detached_jobs._preacceptance_rejection(
            status,
            job_id="job_token_test",
            request_tool_name="ouroboros_start_evolve_step",
            worker_pid=4242,
            launch_token="a" * 64,
        )


@pytest.mark.parametrize("platform_name", ["nt", "posix"])
@pytest.mark.parametrize("status_pid", [4242, 9999])
def test_legacy_rejection_helper_still_requires_exact_pid(
    monkeypatch: pytest.MonkeyPatch, platform_name: str, status_pid: int
) -> None:
    monkeypatch.setattr(detached_jobs, "os", SimpleNamespace(name=platform_name))
    status = _rejection(pid=status_pid)
    del status["launch_token"]
    arguments = {
        "job_id": "job_token_test",
        "request_tool_name": "ouroboros_start_evolve_step",
        "worker_pid": 4242,
    }
    if status_pid != 4242:
        with pytest.raises(RuntimeError, match="unexpected worker pid"):
            detached_jobs._preacceptance_rejection(status, **arguments)
    else:
        assert (
            detached_jobs._preacceptance_rejection(status, **arguments).message == "seed rejected"
        )


@pytest.mark.parametrize("pid", [True, 0, -1, "4242", None])
def test_matching_launch_token_does_not_accept_invalid_worker_pid(pid: object) -> None:
    with pytest.raises(RuntimeError, match="invalid worker pid"):
        detached_jobs._preacceptance_rejection(
            _rejection(pid=pid),
            job_id="job_token_test",
            request_tool_name="ouroboros_start_evolve_step",
            worker_pid=4242,
            launch_token="a" * 64,
        )


@pytest.mark.parametrize("token", ["", "x" * 64, "a" * 63, "a" * 65, True, 7, [], {}])
def test_request_rejects_malformed_launch_token(token: object, tmp_path: Path) -> None:
    payload = {
        "job_id": "job_token_test",
        "tool_name": "ouroboros_start_evolve_step",
        "arguments": {},
        "database_url": "sqlite+aiosqlite:///test.db",
        "cwd": str(tmp_path),
        "launch_token": token,
    }
    with pytest.raises(ValueError, match="invalid launch token"):
        detached_jobs.DetachedJobRequest.from_json(payload)


@pytest.mark.asyncio
async def test_each_launch_replaces_caller_token_and_keeps_it_out_of_public_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EventStore(sqlite_database_url(tmp_path / "events.db"))
    manager = JobManager(store, durable_jobs=True)
    monkeypatch.setattr(detached_jobs, "detached_jobs_dir", lambda: tmp_path / "requests")
    tokens = []
    process = SimpleNamespace(pid=4242, poll=lambda: 0)

    def reject(request_path: Path, *, cwd: str):
        payload = json.loads(request_path.read_text(encoding="utf-8"))
        token = payload["launch_token"]
        tokens.append(token)
        status = _rejection(token=token)
        status["job_id"] = payload["job_id"]
        detached_jobs.write_private_json(
            detached_jobs.status_path_for(request_path), status, exclusive=False
        )
        return process

    monkeypatch.setattr(detached_jobs, "_spawn_worker", reject)
    try:
        for _ in range(2):
            request = detached_jobs.DetachedJobRequest(
                job_id=await manager.allocate_job_id(),
                tool_name="ouroboros_start_evolve_step",
                arguments={},
                database_url=store.database_url,
                cwd=str(tmp_path),
                launch_token="f" * 64,
            )
            with pytest.raises(MCPToolError, match="seed rejected") as raised:
                await detached_jobs.launch_detached_job(
                    job_manager=manager, event_store=store, request=request
                )
            assert request.launch_token == "f" * 64
            assert raised.value.details == {}
            assert tokens[-1] not in str(raised.value)
        assert len(set(tokens)) == 2
        assert all(token != "f" * 64 for token in tokens)
        assert manager._reserved_job_ids == set()
        assert list((tmp_path / "requests").glob("*")) == []
    finally:
        await store.close()
