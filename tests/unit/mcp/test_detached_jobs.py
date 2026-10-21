"""Platform launch contracts for durable background workers."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from ouroboros.mcp import detached_jobs

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
