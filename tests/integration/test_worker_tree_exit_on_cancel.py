"""Cancelling a Codex worker job ends only after the worker's whole tree has exited.

Codex starts tool commands such as ``git add`` in their own sessions. Before
this regression test, cancelling the worker signalled only the Codex process
group, so the job turned terminal (and ``ouroboros auto`` returned) while the
tool command still ran and held ``.git/index.lock`` in the workspace.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

from ouroboros.auto.adapters import _wait_for_job_terminal
from ouroboros.mcp.job_manager import JobManager
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime
from ouroboros.persistence.event_store import EventStore
from ouroboros.providers import worker_process_tree

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX process tree semantics")

# Fake ``codex``: emits one JSONL event, then starts a git-like tool command in
# a new session. The tool command takes the index lock, ignores SIGTERM, and
# runs until killed, so only SIGKILL stops it and its lock stays behind.
_FAKE_CODEX = textwrap.dedent(
    """
    import json, os, subprocess, sys, time
    if "exec" not in sys.argv[1:]:
        # Version probes run from the caller's directory; never start the tool.
        print("codex-cli 0.0.0-test")
        sys.exit(0)
    pid_file = os.environ["FAKE_TOOL_PID_FILE"]
    lock_path = os.environ["FAKE_TOOL_LOCK_PATH"]
    tool = (
        "import os, signal, sys, time\\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\\n"
        "fd = os.open(sys.argv[2], os.O_CREAT | os.O_EXCL | os.O_WRONLY)\\n"
        "with open(sys.argv[1] + '.tmp', 'w') as fh:\\n"
        "    fh.write(str(os.getpid()))\\n"
        "os.replace(sys.argv[1] + '.tmp', sys.argv[1])\\n"
        "while True:\\n"
        "    time.sleep(0.05)\\n"
    )
    print(json.dumps({"type": "thread.started", "thread_id": "fake-thread"}), flush=True)
    subprocess.Popen([sys.executable, "-c", tool, pid_file, lock_path], start_new_session=True)
    while True:
        time.sleep(0.05)
    """
)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():
        return stat.read_text().rpartition(")")[2].split()[0] not in {"Z", "X"}
    return True


def _own_pids() -> list[int]:
    if Path("/proc/self/stat").is_file():
        pids = []
        for entry in os.scandir("/proc"):
            if entry.name.isdigit():
                try:
                    if os.stat(entry.path).st_uid == os.getuid():
                        pids.append(int(entry.name))
                except OSError:
                    continue
        return pids
    out = subprocess.run(
        ["ps", "-U", str(os.getuid()), "-o", "pid="], capture_output=True, text=True, check=True
    ).stdout
    return [int(token) for token in out.split()]


@pytest.mark.asyncio
async def test_cancelled_codex_job_turns_terminal_only_after_tool_command_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "testbed"
    (workspace / ".git").mkdir(parents=True)
    lock = workspace / ".git" / "index.lock"
    pid_file = tmp_path / "tool.pid"
    cli = tmp_path / "codex"
    cli.write_text(f"#!{sys.executable}\n{_FAKE_CODEX}", encoding="utf-8")
    cli.chmod(0o755)
    monkeypatch.setenv("FAKE_TOOL_PID_FILE", str(pid_file))
    monkeypatch.setenv("FAKE_TOOL_LOCK_PATH", str(lock))
    # A non-root CI user cannot read other users' open files, so the real
    # all-process holder check fails closed. Use this user's processes as the
    # whole table, as root in a container (the A59 setting) would see it.
    real_holders = worker_process_tree._lock_holders
    monkeypatch.setattr(
        worker_process_tree,
        "_lock_holders",
        lambda path, pids: real_holders(path, _own_pids() if pids is None else pids),
    )

    runtime = CodexCliRuntime(cli_path=str(cli), cwd=str(workspace))
    runtime._process_shutdown_timeout_seconds = 0.5
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'events.db'}")
    manager = JobManager(store)

    async def _runner() -> MCPToolResult:
        async for _message in runtime.execute_task("edit serializer.py"):
            pass
        return MCPToolResult(
            content=(MCPContentItem(type=ContentType.TEXT, text="finished"),),
            is_error=False,
        )

    tool_pid: int | None = None
    try:
        await store.initialize()
        started = await manager.start_job(
            job_type="execute_seed",
            initial_message="queued",
            runner=_runner(),
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 15
        while not pid_file.exists():
            assert loop.time() < deadline, "fake tool command did not start"
            await asyncio.sleep(0.02)
        tool_pid = int(pid_file.read_text())
        assert lock.exists()

        await manager.cancel_job(started.job_id)
        meta = await asyncio.wait_for(_wait_for_job_terminal(manager, started.job_id), timeout=30)

        # What auto waits on is terminal; at that moment the tree must be gone
        # and the workspace must not be locked.
        assert meta["status"] == "cancelled"
        assert not _alive(tool_pid)
        assert not lock.exists()
    finally:
        if tool_pid is not None and _alive(tool_pid):
            os.kill(tool_pid, 9)
        await store.close()
