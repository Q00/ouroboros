"""Worker process-tree exit and stale git index lock handling on termination."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import signal
import sys
import textwrap
from typing import Any

import pytest

from ouroboros.providers.codex_cli_stream import terminate_runtime_process
from ouroboros.providers.worker_process_tree import (
    capture_worker_tree,
    finish_worker_tree,
    git_index_lock_path,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX process tree semantics")

# A worker that, like Codex, starts its tool command in a new session. The tool
# command optionally holds ``.git/index.lock`` open and optionally ignores
# SIGTERM, so only SIGKILL stops it.
_WORKER = textwrap.dedent(
    """
    import os, signal, subprocess, sys, time
    pid_file, workspace, hold_lock, ignore_term, leader_ignores_term = sys.argv[1:6]
    if leader_ignores_term == "1":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child = (
        "import os, signal, sys, time\\n"
        "pid_file, workspace, hold_lock, ignore_term = sys.argv[1:5]\\n"
        "if ignore_term == '1':\\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\\n"
        "lock = None\\n"
        "if hold_lock == '1':\\n"
        "    path = os.path.join(workspace, '.git', 'index.lock')\\n"
        "    lock = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)\\n"
        "with open(pid_file + '.tmp', 'w') as fh:\\n"
        "    fh.write(str(os.getpid()))\\n"
        "os.replace(pid_file + '.tmp', pid_file)\\n"
        "while True:\\n"
        "    time.sleep(0.05)\\n"
    )
    subprocess.Popen(
        [sys.executable, "-c", child, pid_file, workspace, hold_lock, ignore_term],
        start_new_session=True,
    )
    while True:
        time.sleep(0.05)
    """
)


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def warning(self, event: str, **kwargs: Any) -> None:
        self.events.append((event, kwargs))

    def error(self, event: str, **kwargs: Any) -> None:
        self.events.append((event, kwargs))


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():
        return stat.read_text().rpartition(")")[2].split()[0] not in {"Z", "X"}
    return True


async def _wait_for_file(path: Path, timeout: float = 10.0) -> str:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not path.exists():
        if loop.time() > deadline:
            raise AssertionError(f"{path} was not written")
        await asyncio.sleep(0.02)
    return path.read_text()


async def _spawn_worker(
    tmp_path: Path, *, hold_lock: bool, ignore_term: bool, leader_ignores_term: bool = False
) -> tuple[asyncio.subprocess.Process, int, Path]:
    workspace = tmp_path / "repo"
    (workspace / ".git").mkdir(parents=True)
    pid_file = tmp_path / "child.pid"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _WORKER,
        str(pid_file),
        str(workspace),
        "1" if hold_lock else "0",
        "1" if ignore_term else "0",
        "1" if leader_ignores_term else "0",
        start_new_session=True,
    )
    child_pid = int(await _wait_for_file(pid_file))
    return process, child_pid, workspace


async def _cleanup(pid: int) -> None:
    if _alive(pid):
        os.kill(pid, signal.SIGKILL)


@pytest.mark.asyncio
async def test_terminate_waits_for_descendant_in_its_own_session(tmp_path: Path) -> None:
    """A tool command outside the worker's group is stopped before the helper returns."""
    process, child_pid, _ = await _spawn_worker(tmp_path, hold_lock=False, ignore_term=False)
    try:
        assert os.getpgid(child_pid) != os.getpgid(process.pid)
        confirmed = await terminate_runtime_process(
            process,
            shutdown_timeout=2.0,
            terminate_process_group=True,
            process_group_id=os.getpgid(process.pid),
        )
        assert confirmed is True
        assert process.returncode is not None
        assert not _alive(child_pid)
    finally:
        await _cleanup(child_pid)


@pytest.mark.asyncio
async def test_terminate_escalates_to_sigkill_for_descendant_ignoring_sigterm(
    tmp_path: Path,
) -> None:
    """A descendant that ignores SIGTERM is killed within the bounded window."""
    process, child_pid, _ = await _spawn_worker(tmp_path, hold_lock=False, ignore_term=True)
    try:
        loop = asyncio.get_running_loop()
        started = loop.time()
        confirmed = await terminate_runtime_process(process, shutdown_timeout=0.5)
        assert confirmed is True
        assert not _alive(child_pid)
        assert loop.time() - started < 5.0
    finally:
        await _cleanup(child_pid)


@pytest.mark.asyncio
async def test_stale_lock_held_by_killed_descendant_is_removed(tmp_path: Path) -> None:
    """The worker's own lock, left by a SIGKILLed git-like child, is released."""
    process, child_pid, workspace = await _spawn_worker(tmp_path, hold_lock=True, ignore_term=True)
    lock = workspace / ".git" / "index.lock"
    logger = _Recorder()
    try:
        assert lock.exists()
        confirmed = await terminate_runtime_process(
            process,
            shutdown_timeout=0.5,
            logger=logger,
            log_namespace="test_runtime",
            workspace=str(workspace),
        )
        assert confirmed is True
        assert not _alive(child_pid)
        assert not lock.exists()
        assert logger.events[-1] == (
            "test_runtime.stale_git_index_lock_removed",
            {"path": str(lock), "reason": "worker_tree_exited"},
        )
    finally:
        await _cleanup(child_pid)


@pytest.mark.asyncio
async def test_lock_not_held_by_worker_is_left_with_warning(tmp_path: Path) -> None:
    """A lock that existed without a worker holder is never deleted."""
    process, child_pid, workspace = await _spawn_worker(
        tmp_path, hold_lock=False, ignore_term=False
    )
    lock = workspace / ".git" / "index.lock"
    lock.write_text("")
    logger = _Recorder()
    try:
        await terminate_runtime_process(
            process,
            shutdown_timeout=1.0,
            logger=logger,
            log_namespace="test_runtime",
            workspace=str(workspace),
        )
        assert lock.exists()
        assert logger.events[-1] == (
            "test_runtime.stale_git_index_lock_left",
            {"path": str(lock), "reason": "not_held_by_worker"},
        )
    finally:
        await _cleanup(child_pid)


@pytest.mark.asyncio
async def test_lock_also_held_by_unrelated_process_is_left(tmp_path: Path) -> None:
    """After the tree exits, a lock some other live process holds stays in place."""
    process, child_pid, workspace = await _spawn_worker(tmp_path, hold_lock=True, ignore_term=True)
    lock = workspace / ".git" / "index.lock"
    ready = tmp_path / "holder.ready"
    holder = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import sys, time\n"
        "fh = open(sys.argv[1], 'rb')\n"
        "open(sys.argv[2], 'w').close()\n"
        "time.sleep(60)\n",
        str(lock),
        str(ready),
    )
    logger = _Recorder()
    try:
        await _wait_for_file(ready)
        await terminate_runtime_process(
            process,
            shutdown_timeout=0.5,
            logger=logger,
            log_namespace="test_runtime",
            workspace=str(workspace),
        )
        assert not _alive(child_pid)
        assert _alive(holder.pid)
        assert lock.exists()
        assert logger.events[-1] == (
            "test_runtime.stale_git_index_lock_left",
            {"path": str(lock), "reason": "held_by_live_process"},
        )
    finally:
        holder.kill()
        await holder.wait()
        await _cleanup(child_pid)


@pytest.mark.asyncio
async def test_second_cancel_during_termination_still_waits_for_tree(tmp_path: Path) -> None:
    """A cancel that lands while the worker is being stopped kills and awaits the tree."""
    process, child_pid, workspace = await _spawn_worker(
        tmp_path, hold_lock=True, ignore_term=True, leader_ignores_term=True
    )
    lock = workspace / ".git" / "index.lock"
    try:
        task = asyncio.create_task(
            terminate_runtime_process(process, shutdown_timeout=5.0, workspace=str(workspace))
        )
        await asyncio.sleep(0.3)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not _alive(child_pid)
        assert not lock.exists()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        await _cleanup(child_pid)


@pytest.mark.asyncio
async def test_pid_that_is_not_our_child_is_never_adopted() -> None:
    """A stale or fake pid must not pull unrelated processes into the tree."""

    class _Foreign:
        pid = os.getppid()
        returncode = None

    tree = await capture_worker_tree(_Foreign())
    assert tree.members == {}


@pytest.mark.asyncio
async def test_unverified_process_group_is_never_adopted_or_signalled() -> None:
    """An exited or fake leader must not turn a group id into kill targets."""
    bystander = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(60)", start_new_session=True
    )

    class _Exited:
        pid = os.getppid()
        returncode = 0

    try:
        pgid = os.getpgid(bystander.pid)
        tree = await capture_worker_tree(_Exited(), process_group_id=pgid)
        assert tree.members == {}
        assert tree.pgid is None
        assert await finish_worker_tree(
            tree, started_at=0.0, shutdown_timeout=0.1, logger=None, log_namespace=None
        )
        assert _alive(bystander.pid)
    finally:
        bystander.kill()
        await bystander.wait()


def test_git_index_lock_path_follows_worktree_gitdir_file(tmp_path: Path) -> None:
    """A linked worktree's ``.git`` file points at its own index lock."""
    git_dir = tmp_path / "main" / ".git" / "worktrees" / "wt"
    git_dir.mkdir(parents=True)
    worktree = tmp_path / "wt"
    (worktree / "pkg").mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n")

    assert git_index_lock_path(str(worktree / "pkg")) == git_dir / "index.lock"
