"""Bounded exit of a worker's whole process tree, and stale git index lock release.

A CLI worker such as Codex starts each tool command in its own session. A
signal to the worker's process group therefore does not reach those commands,
and a cancelled worker can leave a ``git add`` running in the workspace after
the caller has returned, with ``.git/index.lock`` still present.

This module records the worker's descendants while the worker is still alive.
Termination then waits for every recorded process to exit, and escalates from
SIGTERM to SIGKILL when one does not. It removes ``.git/index.lock`` only when
a member of that tree held the lock open, the tree has exited, the lock file is
unchanged, and no readable live process holds it. In every other case the lock
stays in place and the caller logs a structured warning.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
import contextlib
from dataclasses import dataclass, field
import os
from pathlib import Path
import shutil
import signal
import subprocess
from typing import Any, Protocol

_POLL_INTERVAL_SECONDS = 0.05
_SCAN_TIMEOUT_SECONDS = 5.0
_PROC_ROOT = Path("/proc")


class _WarningLogger(Protocol):
    def warning(self, event: str, **kwargs: Any) -> Any: ...


@dataclass(frozen=True, slots=True)
class _ProcessRecord:
    pid: int
    ppid: int
    pgid: int
    start: str
    zombie: bool


@dataclass(frozen=True, slots=True)
class _LockObservation:
    path: Path
    device: int
    inode: int
    mtime_ns: int
    held_by_tree: bool


@dataclass(slots=True)
class WorkerTree:
    """Processes that belong to one worker, recorded before any signal is sent."""

    leader_pid: int | None = None
    members: dict[int, str] = field(default_factory=dict)
    pgid: int | None = None
    workspace: str | None = None
    lock: _LockObservation | None = None
    scan_failed: bool = False


def _proc_available() -> bool:
    return (_PROC_ROOT / "self" / "stat").is_file()


def _parse_proc_stat(pid: int, raw: str) -> _ProcessRecord | None:
    _, sep, tail = raw.rpartition(")")
    fields = tail.split()
    if not sep or len(fields) < 20:
        return None
    try:
        return _ProcessRecord(
            pid=pid,
            ppid=int(fields[1]),
            pgid=int(fields[2]),
            start=fields[19],
            zombie=fields[0] in {"Z", "X"},
        )
    except ValueError:
        return None


def _scan_proc() -> dict[int, _ProcessRecord] | None:
    records: dict[int, _ProcessRecord] = {}
    try:
        entries = list(os.scandir(_PROC_ROOT))
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            raw = Path(entry.path, "stat").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        record = _parse_proc_stat(int(entry.name), raw)
        if record is not None:
            records[record.pid] = record
    return records


def _scan_ps() -> dict[int, _ProcessRecord] | None:
    try:
        completed = subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid=,pgid=,stat=,lstart="],
            capture_output=True,
            text=True,
            timeout=_SCAN_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    records: dict[int, _ProcessRecord] = {}
    for line in completed.stdout.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        try:
            pid, ppid, pgid = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        records[pid] = _ProcessRecord(
            pid=pid,
            ppid=ppid,
            pgid=pgid,
            start=parts[4].strip(),
            zombie=parts[3].startswith("Z"),
        )
    return records


def _scan_processes() -> dict[int, _ProcessRecord] | None:
    """Return every visible process, or ``None`` when no process table is readable."""
    if os.name != "posix":
        return None
    if _proc_available():
        return _scan_proc()
    return _scan_ps()


def _descendants(snapshot: dict[int, _ProcessRecord], root_pid: int) -> dict[int, str]:
    children: dict[int, list[int]] = {}
    for record in snapshot.values():
        children.setdefault(record.ppid, []).append(record.pid)
    found: dict[int, str] = {}
    pending = [root_pid]
    while pending:
        pid = pending.pop()
        record = snapshot.get(pid)
        if record is None or pid in found:
            continue
        found[pid] = record.start
        pending.extend(children.get(pid, ()))
    return found


def git_index_lock_path(workspace: str | None) -> Path | None:
    """Return the ``index.lock`` path of the git repository that contains *workspace*."""
    if not workspace:
        return None
    start = Path(workspace)
    for candidate in (start, *start.parents):
        dot_git = candidate / ".git"
        if dot_git.is_dir():
            return dot_git / "index.lock"
        if dot_git.is_file():
            try:
                text = dot_git.read_text(encoding="utf-8").strip()
            except OSError:
                return None
            if not text.startswith("gitdir:"):
                return None
            git_dir = Path(text.split(":", 1)[1].strip())
            if not git_dir.is_absolute():
                git_dir = candidate / git_dir
            return git_dir / "index.lock"
    return None


def _proc_fd_holds(pid: int, target: str) -> bool:
    fd_dir = _PROC_ROOT / str(pid) / "fd"
    try:
        fds = list(os.scandir(fd_dir))
    except OSError:
        return False
    for fd in fds:
        with contextlib.suppress(OSError):
            if os.readlink(fd.path) == target:
                return True
    return False


def _lock_holders(path: Path, pids: Iterable[int] | None) -> set[int] | None:
    """Return the pids that hold *path* open, or ``None`` when that cannot be checked.

    With ``pids`` set, only those processes are inspected. Processes whose open
    files are not readable by this user are not reported.
    """
    target = os.path.realpath(path)
    if _proc_available():
        if pids is None:
            try:
                pids = [int(entry.name) for entry in os.scandir(_PROC_ROOT) if entry.name.isdigit()]
            except OSError:
                return None
        return {pid for pid in pids if _proc_fd_holds(pid, target)}
    lsof = shutil.which("lsof")
    if lsof is None:
        return None
    try:
        completed = subprocess.run(
            [lsof, "-t", "--", target],
            capture_output=True,
            text=True,
            timeout=_SCAN_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode == 1 and not completed.stdout.strip():
        holders: set[int] = set()
    elif completed.returncode == 0:
        holders = {int(token) for token in completed.stdout.split() if token.isdigit()}
    else:
        return None
    if pids is not None:
        holders &= set(pids)
    return holders


def _observe_lock(workspace: str | None, members: dict[int, str]) -> _LockObservation | None:
    path = git_index_lock_path(workspace)
    if path is None:
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    holders = _lock_holders(path, members) if members else set()
    return _LockObservation(
        path=path,
        device=stat.st_dev,
        inode=stat.st_ino,
        mtime_ns=stat.st_mtime_ns,
        held_by_tree=bool(holders),
    )


def _capture(
    leader_pid: int | None,
    process_group_id: int | None,
    workspace: str | None,
) -> WorkerTree:
    tree = WorkerTree(leader_pid=leader_pid, workspace=workspace)
    if process_group_id is not None and process_group_id != os.getpgrp():
        tree.pgid = process_group_id
    snapshot = _scan_processes() if (leader_pid is not None or tree.pgid is not None) else None
    # On POSIX a live leader whose tree cannot be read leaves the exit unknown.
    tree.scan_failed = snapshot is None and leader_pid is not None and os.name == "posix"
    if snapshot is not None:
        leader = snapshot.get(leader_pid) if leader_pid is not None else None
        # Only a direct child of this process is a worker we own. This keeps a
        # stale or fake pid from pulling unrelated processes into the tree.
        if leader is not None and leader.ppid == os.getpid():
            tree.members = _descendants(snapshot, leader.pid)
            if tree.pgid is not None:
                for record in snapshot.values():
                    if record.pgid == tree.pgid and not record.zombie:
                        tree.members.setdefault(record.pid, record.start)
        tree.members.pop(os.getpid(), None)
    if not tree.members:
        # Without a verified live leader the group id is unproven: leave the
        # group to the existing killpg handling and do not track it here.
        tree.pgid = None
    tree.lock = _observe_lock(workspace, tree.members)
    return tree


async def capture_worker_tree(
    process: Any,
    *,
    process_group_id: int | None = None,
    workspace: str | None = None,
) -> WorkerTree:
    """Record the worker's process tree and its git index lock before any signal."""
    pid = getattr(process, "pid", None)
    leader_pid = (
        pid if isinstance(pid, int) and getattr(process, "returncode", None) is None else None
    )
    if leader_pid is None and process_group_id is None and workspace is None:
        return WorkerTree()
    # Synchronous on purpose: a cancellation landing here must not skip the
    # termination that follows. One process table read costs milliseconds.
    return _capture(leader_pid, process_group_id, workspace)


def _live_members(tree: WorkerTree) -> tuple[dict[int, _ProcessRecord], set[int]] | None:
    snapshot = _scan_processes()
    if snapshot is None:
        return None
    live: set[int] = set()
    for pid, start in tree.members.items():
        record = snapshot.get(pid)
        if record is not None and record.start == start and not record.zombie:
            live.add(pid)
    if tree.pgid is not None:
        live.update(
            record.pid
            for record in snapshot.values()
            if record.pgid == tree.pgid and not record.zombie
        )
    live.discard(os.getpid())
    return snapshot, live


def _signal(tree: WorkerTree, sig: int, *, exclude_leader_group: bool) -> None:
    observed = _live_members(tree)
    if observed is None:
        return
    snapshot, live = observed
    for pid in live:
        if exclude_leader_group and (pid == tree.leader_pid or snapshot[pid].pgid == tree.pgid):
            continue
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.kill(pid, sig)


async def signal_worker_tree(tree: WorkerTree, sig: int, *, exclude_leader_group: bool) -> None:
    """Signal every live member, skipping the leader and its group when asked."""
    if not tree.members and tree.pgid is None:
        return
    _signal(tree, sig, exclude_leader_group=exclude_leader_group)


async def wait_for_worker_tree_exit(tree: WorkerTree, *, timeout: float) -> set[int] | None:
    """Wait until no member is alive; return the survivors, or ``None`` if unknown."""
    if tree.scan_failed:
        return None
    if not tree.members and tree.pgid is None:
        return set()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, timeout)
    while True:
        observed = await asyncio.to_thread(_live_members, tree)
        if observed is None:
            return None
        survivors = observed[1]
        remaining = deadline - loop.time()
        if not survivors or remaining <= 0:
            return survivors
        await asyncio.sleep(min(_POLL_INTERVAL_SECONDS, remaining))


def _release_lock(tree: WorkerTree, tree_exited: bool) -> tuple[str, str] | None:
    path = git_index_lock_path(tree.workspace)
    if path is None or not path.exists():
        return None
    observation = tree.lock
    if observation is None or not observation.held_by_tree:
        return "left", "not_held_by_worker"
    if not tree_exited:
        return "left", "worker_tree_alive"
    try:
        stat = path.stat()
    except OSError:
        return None
    if (stat.st_dev, stat.st_ino, stat.st_mtime_ns) != (
        observation.device,
        observation.inode,
        observation.mtime_ns,
    ):
        return "left", "lock_replaced"
    return _claim_verify_remove(path, observation)


def _put_back(claimed: Path, path: Path) -> str | None:
    """Return the claimed file to its name; report where it stays if the name is taken."""
    try:
        os.link(claimed, path)
    except OSError:
        return claimed.name
    with contextlib.suppress(OSError):
        os.unlink(claimed)
    return None


def _claim_verify_remove(path: Path, observation: _LockObservation) -> tuple[str, str] | None:
    """Claim the lock name first, then verify the claimed file, then delete it.

    The rename moves whatever holds the name to a private path in one step, so
    the identity and holder checks below apply to the exact file that would be
    deleted, and no later process can reach it by the lock name. Git creates a
    lock with ``O_EXCL`` and never adopts an existing file, so a replacement is
    always a new inode. Any file that fails a check goes back under its name, or
    stays under the claimed name (reported) when the name was taken meanwhile.
    """
    claimed = path.with_name(f"{path.name}.ouroboros-stale-{os.getpid()}-{os.urandom(4).hex()}")
    try:
        os.rename(path, claimed)
    except FileNotFoundError:
        return None
    except OSError:
        return "left", "claim_failed"
    try:
        stat = os.lstat(claimed)
        same = (stat.st_dev, stat.st_ino, stat.st_mtime_ns) == (
            observation.device,
            observation.inode,
            observation.mtime_ns,
        )
    except OSError:
        same = False
    reason: str | None = None
    if not same:
        reason = "lock_replaced"
    else:
        holders = _lock_holders(claimed, None)
        if holders is None:
            reason = "holder_check_unavailable"
        elif holders:
            reason = "held_by_live_process"
    if reason is None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(claimed)
        return "removed", "worker_tree_exited"
    kept_at = _put_back(claimed, path)
    return "left", reason if kept_at is None else f"{reason}_kept_at:{kept_at}"


async def release_stale_git_index_lock(
    tree: WorkerTree,
    *,
    tree_exited: bool,
    logger: _WarningLogger | None,
    log_namespace: str | None,
) -> bool:
    """Remove the worker's own stale ``index.lock``; warn when it has to stay."""
    if tree.workspace is None:
        return False
    outcome = _release_lock(tree, tree_exited)
    if outcome is None:
        return False
    action, reason = outcome
    if logger is not None:
        logger.warning(
            f"{log_namespace}.stale_git_index_lock_{action}",
            path=str(git_index_lock_path(tree.workspace)),
            reason=reason,
        )
    return action == "removed"


async def finish_worker_tree(
    tree: WorkerTree,
    *,
    started_at: float,
    shutdown_timeout: float,
    logger: _WarningLogger | None,
    log_namespace: str | None,
) -> bool:
    """Wait for the recorded tree after the leader is handled; escalate, then report.

    The SIGTERM phase shares one ``shutdown_timeout`` window that began at
    ``started_at``. Survivors then get SIGKILL and one more bounded wait.
    Returns ``True`` only when every member is confirmed gone.
    """
    loop = asyncio.get_running_loop()
    cancelled: asyncio.CancelledError | None = None
    survivors: set[int] | None = None
    try:
        survivors = await wait_for_worker_tree_exit(
            tree, timeout=started_at + shutdown_timeout - loop.time()
        )
        if survivors:
            await signal_worker_tree(tree, signal.SIGKILL, exclude_leader_group=False)
            survivors = await wait_for_worker_tree_exit(tree, timeout=shutdown_timeout)
    except asyncio.CancelledError as exc:
        # Cancelled mid-wait: kill what is left, wait once more, and still
        # report and release the lock before the cancellation propagates.
        cancelled = exc
        await signal_worker_tree(tree, signal.SIGKILL, exclude_leader_group=False)
        with contextlib.suppress(asyncio.CancelledError):
            survivors = await wait_for_worker_tree_exit(tree, timeout=shutdown_timeout)
    tree_exited = survivors is not None and not survivors
    if not tree_exited and logger is not None:
        logger.warning(
            f"{log_namespace}.worker_tree_exit_unconfirmed",
            pids=sorted(survivors or ()),
            reason="process_scan_unavailable" if survivors is None else "survivors",
        )
    await release_stale_git_index_lock(
        tree, tree_exited=tree_exited, logger=logger, log_namespace=log_namespace
    )
    if cancelled is not None:
        raise cancelled
    return tree_exited


__all__ = [
    "WorkerTree",
    "capture_worker_tree",
    "finish_worker_tree",
    "git_index_lock_path",
    "release_stale_git_index_lock",
    "signal_worker_tree",
    "wait_for_worker_tree_exit",
]
