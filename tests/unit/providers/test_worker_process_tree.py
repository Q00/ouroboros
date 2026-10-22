"""Worker process-tree exit and stale git index lock handling on termination."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import signal
import subprocess
import sys
import textwrap
from typing import Any

import pytest

from ouroboros.providers import worker_process_tree
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


@pytest.fixture
def own_process_view(monkeypatch: pytest.MonkeyPatch) -> None:
    """Treat this user's processes as the whole table, as for root in a container.

    A non-root CI user cannot read other users' open files, so the real
    all-process holder check fails closed there. These tests exercise the
    removal and holder decisions over the processes they control.
    """
    real = worker_process_tree._lock_holders

    def _view(path: Path, pids: Any) -> set[int] | None:
        return real(path, _own_pids() if pids is None else pids)

    monkeypatch.setattr(worker_process_tree, "_lock_holders", _view)


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
@pytest.mark.usefixtures("own_process_view")
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
@pytest.mark.usefixtures("own_process_view")
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
@pytest.mark.usefixtures("own_process_view")
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


def _observe(path: Path) -> Any:
    stat = path.stat()
    return worker_process_tree._LockObservation(
        path=path,
        device=stat.st_dev,
        inode=stat.st_ino,
        mtime_ns=stat.st_mtime_ns,
        held_by_tree=True,
    )


def test_replaced_lock_is_put_back_untouched(tmp_path: Path) -> None:
    """A different file at the lock name goes back under the name, unchanged."""
    lock = tmp_path / "index.lock"
    lock.write_text("old")
    observation = _observe(lock)
    lock.unlink()
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.close(fd)
    replacement_inode = lock.stat().st_ino

    outcome = worker_process_tree._claim_verify_remove(lock, observation)

    assert outcome == ("left", "lock_replaced")
    assert lock.stat().st_ino == replacement_inode
    assert sorted(p.name for p in tmp_path.iterdir()) == ["index.lock"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("own_process_view")
async def test_lock_opened_after_tree_exit_is_put_back(tmp_path: Path) -> None:
    """Holders are checked on the claimed file, so a late opener keeps its lock."""
    lock = tmp_path / "index.lock"
    lock.write_text("")
    observation = _observe(lock)
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
    try:
        await _wait_for_file(ready)
        outcome = worker_process_tree._claim_verify_remove(lock, observation)
        assert outcome == ("left", "held_by_live_process")
        assert lock.stat().st_ino == observation.inode
        assert sorted(p.name for p in tmp_path.iterdir()) == ["holder.ready", "index.lock"]
    finally:
        holder.kill()
        await holder.wait()


@pytest.mark.asyncio
@pytest.mark.usefixtures("own_process_view")
async def test_lock_held_through_a_hard_link_is_put_back(tmp_path: Path) -> None:
    """A holder is matched by the open file, not by the path it was opened through."""
    lock = tmp_path / "index.lock"
    lock.write_text("")
    alias = tmp_path / "alias"
    os.link(lock, alias)
    observation = _observe(lock)
    ready = tmp_path / "holder.ready"
    holder = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import sys, time\n"
        "fh = open(sys.argv[1], 'rb')\n"
        "open(sys.argv[2], 'w').close()\n"
        "time.sleep(60)\n",
        str(alias),
        str(ready),
    )
    try:
        await _wait_for_file(ready)
        outcome = worker_process_tree._claim_verify_remove(lock, observation)
        assert outcome == ("left", "held_by_live_process")
        assert lock.stat().st_ino == observation.inode
    finally:
        holder.kill()
        await holder.wait()


@pytest.mark.asyncio
async def test_unavailable_process_scan_reports_unconfirmed_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a readable process table the helper never claims the tree exited."""
    process, child_pid, workspace = await _spawn_worker(tmp_path, hold_lock=True, ignore_term=False)
    lock = workspace / ".git" / "index.lock"
    monkeypatch.setattr(worker_process_tree, "_scan_processes", lambda: None)
    logger = _Recorder()
    try:
        confirmed = await terminate_runtime_process(
            process,
            shutdown_timeout=0.5,
            logger=logger,
            log_namespace="test_runtime",
            workspace=str(workspace),
        )
        assert confirmed is False
        assert (
            "test_runtime.worker_tree_exit_unconfirmed",
            {"pids": [], "reason": "process_scan_unavailable"},
        ) in logger.events
        assert lock.exists()
        assert logger.events[-1] == (
            "test_runtime.stale_git_index_lock_left",
            {"path": str(lock), "reason": "not_held_by_worker"},
        )
    finally:
        await _cleanup(child_pid)


@pytest.mark.skipif(os.name == "posix" and os.geteuid() == 0, reason="root sees every process")
def test_incomplete_holder_view_leaves_the_lock(tmp_path: Path) -> None:
    """When some processes cannot be inspected, the lock is never removed."""
    lock = tmp_path / "index.lock"
    lock.write_text("")
    observation = _observe(lock)

    outcome = worker_process_tree._claim_verify_remove(lock, observation)

    assert outcome == ("left", "holder_check_unavailable")
    assert lock.stat().st_ino == observation.inode
    assert sorted(p.name for p in tmp_path.iterdir()) == ["index.lock"]


@pytest.mark.asyncio
async def test_live_leader_missing_from_scan_reports_unconfirmed_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial process table that omits the live worker never confirms exit."""
    process, child_pid, _ = await _spawn_worker(tmp_path, hold_lock=False, ignore_term=False)
    real_scan = worker_process_tree._scan_processes

    def _partial() -> Any:
        snapshot = real_scan()
        if snapshot is not None:
            snapshot.pop(process.pid, None)
        return snapshot

    monkeypatch.setattr(worker_process_tree, "_scan_processes", _partial)
    logger = _Recorder()
    try:
        confirmed = await terminate_runtime_process(
            process, shutdown_timeout=0.5, logger=logger, log_namespace="test_runtime"
        )
        assert confirmed is False
        assert (
            "test_runtime.worker_tree_exit_unconfirmed",
            {"pids": [], "reason": "process_scan_unavailable"},
        ) in logger.events
    finally:
        await _cleanup(child_pid)


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="Linux /proc descriptor check")
def test_uninspectable_descriptor_is_unknown_not_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A descriptor that cannot be stat'ed makes the holder answer unknown."""
    lock = tmp_path / "index.lock"
    lock.write_text("")
    stat = lock.stat()
    real_stat = os.stat

    def _denied(path: Any, *args: Any, **kwargs: Any) -> Any:
        if str(path).startswith(f"/proc/{os.getpid()}/fd/"):
            raise PermissionError(path)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(worker_process_tree.os, "stat", _denied)
    assert worker_process_tree._proc_fd_holds(os.getpid(), (stat.st_dev, stat.st_ino)) is None


@pytest.mark.asyncio
async def test_live_member_missing_from_later_scan_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recorded member that a later scan omits while it lives is never counted gone."""
    process, child_pid, _ = await _spawn_worker(tmp_path, hold_lock=False, ignore_term=False)
    try:
        tree = await capture_worker_tree(process)
        assert child_pid in tree.members
        real_scan = worker_process_tree._scan_processes

        def _partial() -> Any:
            snapshot = real_scan()
            if snapshot is not None:
                snapshot.pop(child_pid, None)
            return snapshot

        monkeypatch.setattr(worker_process_tree, "_scan_processes", _partial)
        assert await worker_process_tree.wait_for_worker_tree_exit(tree, timeout=0.1) is None
    finally:
        process.kill()
        await process.wait()
        await _cleanup(child_pid)


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="Linux /proc descriptor check")
def test_hidden_fd_directory_of_live_process_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """A live process whose fd directory is missing is unknown, not a non-holder."""
    real_scandir = os.scandir

    def _hidden(path: Any) -> Any:
        if str(path) == f"/proc/{os.getpid()}/fd":
            raise FileNotFoundError(path)
        return real_scandir(path)

    monkeypatch.setattr(worker_process_tree.os, "scandir", _hidden)
    assert worker_process_tree._proc_fd_holds(os.getpid(), (0, 0)) is None


@pytest.mark.skipif(not Path("/proc/self/stat").is_file(), reason="Linux /proc scan")
def test_unreadable_stat_of_live_process_makes_scan_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live process whose stat file is missing is not read as an exit."""
    target = f"/proc/{os.getpid()}/stat"
    real_read_text = Path.read_text

    def _missing(self: Path, *args: Any, **kwargs: Any) -> str:
        if str(self) == target:
            raise FileNotFoundError(target)
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _missing)
    assert worker_process_tree._scan_proc() is None


@pytest.mark.asyncio
async def test_descendant_missed_by_first_scan_is_still_stopped_and_awaited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The freeze rescans until the tree is complete, so a missed child is found."""
    process, child_pid, _ = await _spawn_worker(tmp_path, hold_lock=False, ignore_term=True)
    real_scan = worker_process_tree._scan_processes
    calls = {"n": 0}

    def _first_misses_child() -> Any:
        calls["n"] += 1
        snapshot = real_scan()
        if snapshot is not None and calls["n"] == 1:
            snapshot.pop(child_pid, None)
        return snapshot

    monkeypatch.setattr(worker_process_tree, "_scan_processes", _first_misses_child)
    try:
        confirmed = await terminate_runtime_process(process, shutdown_timeout=0.5)
        assert confirmed is True
        assert not _alive(child_pid)
    finally:
        await _cleanup(child_pid)


@pytest.mark.asyncio
async def test_child_forked_by_live_member_during_wait_is_adopted(tmp_path: Path) -> None:
    """A member that forks from its SIGTERM handler and stays alive is followed."""
    pid_file = tmp_path / "late.pid"
    code = (
        "import os, signal, subprocess, sys, time\n"
        "def on_term(*_):\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "    p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],"
        " start_new_session=True)\n"
        "    open(sys.argv[1], 'w').write(str(p.pid))\n"
        "signal.signal(signal.SIGTERM, on_term)\n"
        "while True:\n"
        "    time.sleep(0.05)\n"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", code, str(pid_file), start_new_session=True
    )
    await asyncio.sleep(0.5)
    late_pid: int | None = None
    try:
        confirmed = await terminate_runtime_process(process, shutdown_timeout=1.0)
        late_pid = int(pid_file.read_text())
        assert confirmed is True
        assert not _alive(late_pid)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        if late_pid is not None:
            await _cleanup(late_pid)


def test_git_index_lock_path_follows_worktree_gitdir_file(tmp_path: Path) -> None:
    """A linked worktree's ``.git`` file points at its own index lock."""
    git_dir = tmp_path / "main" / ".git" / "worktrees" / "wt"
    git_dir.mkdir(parents=True)
    worktree = tmp_path / "wt"
    (worktree / "pkg").mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n")

    assert git_index_lock_path(str(worktree / "pkg")) == git_dir / "index.lock"
