"""Backend-agnostic git worktree management for mutating task workflows."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
from typing import Any

from ouroboros.config.loader import load_config
from ouroboros.config.models import OrchestratorConfig
from ouroboros.core.errors import ConfigError, OuroborosError
from ouroboros.core.file_lock import file_lock


class WorktreeError(OuroborosError):
    """Raised when task worktree provisioning or validation fails."""


@dataclass(frozen=True, slots=True)
class TaskWorkspace:
    """Resolved workspace for one durable mutating task."""

    durable_id: str
    repo_root: str
    repo_name: str
    original_cwd: str
    effective_cwd: str
    worktree_path: str
    branch: str
    lock_path: str
    lock_owner: dict[str, Any] = field(default_factory=dict)

    def to_progress_dict(self) -> dict[str, Any]:
        """Serialize task workspace metadata for progress persistence."""
        return {
            "durable_id": self.durable_id,
            "repo_root": self.repo_root,
            "repo_name": self.repo_name,
            "original_cwd": self.original_cwd,
            "effective_cwd": self.effective_cwd,
            "worktree_path": self.worktree_path,
            "branch": self.branch,
            "lock_path": self.lock_path,
        }

    @classmethod
    def from_progress_dict(cls, value: object) -> TaskWorkspace | None:
        """Deserialize workspace metadata from session progress."""
        if not isinstance(value, dict):
            return None

        required = {
            "durable_id",
            "repo_root",
            "repo_name",
            "original_cwd",
            "effective_cwd",
            "worktree_path",
            "branch",
            "lock_path",
        }
        if not required.issubset(value):
            return None

        if not all(isinstance(value[key], str) and value[key] for key in required):
            return None

        return cls(
            durable_id=value["durable_id"],
            repo_root=value["repo_root"],
            repo_name=value["repo_name"],
            original_cwd=value["original_cwd"],
            effective_cwd=value["effective_cwd"],
            worktree_path=value["worktree_path"],
            branch=value["branch"],
            lock_path=value["lock_path"],
        )


def _orchestrator_config() -> OrchestratorConfig:
    try:
        return load_config().orchestrator
    except (ConfigError, FileNotFoundError):
        return OrchestratorConfig()


def _worktree_root() -> Path:
    config = _orchestrator_config()
    root = getattr(config, "worktree_root", "~/.ouroboros/worktrees")
    return Path(root).expanduser()


def _stale_after() -> timedelta:
    config = _orchestrator_config()
    minutes = getattr(config, "worktree_lock_stale_after_minutes", 60)
    return timedelta(minutes=minutes)


def _worktrees_enabled() -> bool:
    config = _orchestrator_config()
    return getattr(config, "use_worktrees", True)


def _run_git_process(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        raise WorktreeError(f"Git command failed: {' '.join(args)}", details={"error": str(exc)})


def _run_git(args: list[str], cwd: Path) -> str:
    result = _run_git_process(args, cwd)
    if result.returncode != 0:
        raise WorktreeError(
            f"Git command failed: {' '.join(args)}",
            details={"stdout": result.stdout.strip(), "stderr": result.stderr.strip()},
        )
    return result.stdout.strip()


def _resolve_repo_root(start_path: Path) -> Path:
    path = start_path.expanduser().resolve()
    probe = path if path.is_dir() else path.parent
    repo_root = _run_git(["rev-parse", "--show-toplevel"], probe)
    return Path(repo_root).resolve()


def is_git_repo(start_path: str | Path) -> bool:
    """Return True when the given path resolves inside a git repository."""
    path = Path(start_path).expanduser().resolve()
    probe = path if path.is_dir() else path.parent
    try:
        _run_git(["rev-parse", "--show-toplevel"], probe)
    except WorktreeError:
        return False
    return True


def _try_resolve_repo_root(start_path: str | Path) -> Path | None:
    try:
        return _resolve_repo_root(Path(start_path))
    except WorktreeError:
        return None


def _resolve_common_repo_root(start_path: Path) -> Path:
    path = start_path.expanduser().resolve()
    probe = path if path.is_dir() else path.parent
    common_dir = _run_git(["rev-parse", "--path-format=absolute", "--git-common-dir"], probe)
    return Path(common_dir).resolve().parent


def _relative_subdir(repo_root: Path, cwd: Path) -> Path:
    resolved = cwd.expanduser().resolve()
    probe = resolved if resolved.is_dir() else resolved.parent
    try:
        return probe.relative_to(repo_root)
    except ValueError as exc:
        raise WorktreeError(
            f"{cwd} is not inside repo root {repo_root}",
            details={"cwd": str(cwd), "repo_root": str(repo_root)},
        ) from exc


def _ensure_clean_checkout(repo_root: Path) -> None:
    status = _run_git(["status", "--porcelain"], repo_root)
    if status:
        raise WorktreeError(
            "Cannot start task worktree from a dirty checkout",
            details={"repo_root": str(repo_root)},
        )


def _checkout_is_dirty(repo_root: Path) -> bool:
    return bool(_run_git(["status", "--porcelain"], repo_root))


def _list_worktrees(repo_root: Path) -> dict[str, dict[str, str]]:
    output = _run_git(["worktree", "list", "--porcelain"], repo_root)
    entries: dict[str, dict[str, str]] = {}
    current: dict[str, str] = {}
    current_path: str | None = None

    for line in output.splitlines():
        if not line.strip():
            if current_path is not None:
                entries[current_path] = current
            current = {}
            current_path = None
            continue
        if line.startswith("worktree "):
            current_path = line.split(" ", 1)[1]
            current["worktree"] = current_path
        else:
            key, _, value = line.partition(" ")
            current[key] = value

    if current_path is not None:
        entries[current_path] = current

    return entries


def _branch_exists(repo_root: Path, branch: str) -> bool:
    result = _run_git_process(
        ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        repo_root,
    )
    return result.returncode == 0


def _managed_branch_name(repo_root: Path, durable_id: str) -> str:
    branch = f"ooo/{durable_id}"
    result = _run_git_process(["check-ref-format", "--branch", branch], repo_root)
    if result.returncode != 0:
        raise WorktreeError(
            "Invalid durable task identifier for git worktree",
            details={
                "durable_id": durable_id,
                "branch": branch,
                "stderr": result.stderr.strip(),
            },
        )
    return branch


def _repair_managed_path(repo_root: Path, worktree_path: Path) -> None:
    if not worktree_path.exists():
        return

    known_worktrees = _list_worktrees(repo_root)
    if str(worktree_path) in known_worktrees:
        return

    shutil.rmtree(worktree_path)


def _ensure_worktree(
    repo_root: Path,
    worktree_path: Path,
    branch: str,
    *,
    base_ref: str | None = None,
) -> None:
    worktrees = _list_worktrees(repo_root)
    branch_path = None
    for path, data in worktrees.items():
        ref_name = data.get("branch", "")
        if ref_name.endswith(f"/{branch}"):
            branch_path = Path(path)
            break

    if str(worktree_path) in worktrees:
        return

    _repair_managed_path(repo_root, worktree_path)
    worktree_path.parent.mkdir(parents=True, exist_ok=True)

    if branch_path is not None and branch_path != worktree_path:
        raise WorktreeError(
            "Task branch already checked out in another worktree",
            details={"branch": branch, "existing_path": str(branch_path)},
        )

    if _branch_exists(repo_root, branch):
        _run_git(["worktree", "add", str(worktree_path), branch], repo_root)
        return

    base = base_ref or _run_git(["rev-parse", "HEAD"], repo_root)
    _run_git(["worktree", "add", "-b", branch, str(worktree_path), base], repo_root)


def _lock_path(repo_name: str, durable_id: str) -> Path:
    root = _worktree_root() / ".locks" / repo_name
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{durable_id}.json"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _is_lock_stale(data: dict[str, Any]) -> bool:
    host = str(data.get("host", ""))
    pid = data.get("pid")
    if host == socket.gethostname() and isinstance(pid, int):
        return not _pid_is_alive(pid)

    timestamp = data.get("updated_at") or data.get("created_at")
    if not isinstance(timestamp, str):
        return True

    try:
        seen = datetime.fromisoformat(timestamp)
    except ValueError:
        return True

    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=UTC)

    return datetime.now(UTC) - seen > _stale_after()


def _acquire_lock(lock_path: Path, workspace: TaskWorkspace) -> dict[str, Any]:
    with file_lock(lock_path):
        if lock_path.exists():
            try:
                existing = json.loads(lock_path.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                raise WorktreeError(
                    "Invalid task workspace lock file",
                    details={"lock_path": str(lock_path), "error": str(exc)},
                ) from exc
            if isinstance(existing, dict) and existing and not _is_lock_stale(existing):
                raise WorktreeError(
                    "Task already active",
                    details={
                        "durable_id": workspace.durable_id,
                        "worktree_path": workspace.worktree_path,
                        "owner": existing,
                    },
                )

        owner = {
            "durable_id": workspace.durable_id,
            "branch": workspace.branch,
            "repo_root": workspace.repo_root,
            "worktree_path": workspace.worktree_path,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        lock_path.write_text(json.dumps(owner, indent=2, sort_keys=True))
        return owner


def heartbeat_lock(lock_path: str) -> None:
    """Refresh a task lock timestamp if the current process still owns it."""
    path = Path(lock_path)
    if not path.exists():
        return
    with file_lock(path):
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return
        if payload.get("pid") != os.getpid() or payload.get("host") != socket.gethostname():
            return
        payload["updated_at"] = _now_iso()
        path.write_text(json.dumps(payload, indent=2, sort_keys=True))


def release_lock(lock_path: str) -> None:
    """Release a task lock if this process owns it."""
    path = Path(lock_path)
    if not path.exists():
        return
    with file_lock(path):
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            path.unlink(missing_ok=True)
            return
        if not isinstance(payload, dict):
            path.unlink(missing_ok=True)
            return
        if payload.get("pid") == os.getpid() and payload.get("host") == socket.gethostname():
            path.unlink(missing_ok=True)


def prepare_task_workspace(
    source_cwd: str | Path,
    durable_id: str,
    *,
    allow_dirty: bool = False,
) -> TaskWorkspace:
    """Create or reuse a task worktree and acquire its active lock."""
    source_path = Path(source_cwd).expanduser().resolve()
    repo_root = _resolve_repo_root(source_path)
    if not allow_dirty:
        _ensure_clean_checkout(repo_root)

    repo_name = repo_root.name
    branch = _managed_branch_name(repo_root, durable_id)
    worktree_path = _worktree_root() / repo_name / durable_id
    effective_cwd = worktree_path / _relative_subdir(repo_root, source_path)
    _ensure_worktree(repo_root, worktree_path, branch)

    workspace = TaskWorkspace(
        durable_id=durable_id,
        repo_root=str(repo_root),
        repo_name=repo_name,
        original_cwd=str(source_path if source_path.is_dir() else source_path.parent),
        effective_cwd=str(effective_cwd),
        worktree_path=str(worktree_path),
        branch=branch,
        lock_path=str(_lock_path(repo_name, durable_id)),
    )
    owner = _acquire_lock(Path(workspace.lock_path), workspace)
    return TaskWorkspace(
        durable_id=workspace.durable_id,
        repo_root=workspace.repo_root,
        repo_name=workspace.repo_name,
        original_cwd=workspace.original_cwd,
        effective_cwd=workspace.effective_cwd,
        worktree_path=workspace.worktree_path,
        branch=workspace.branch,
        lock_path=workspace.lock_path,
        lock_owner=owner,
    )


def restore_task_workspace(
    durable_id: str,
    persisted: TaskWorkspace | None,
    *,
    fallback_source_cwd: str | Path | None = None,
    allow_dirty: bool = False,
) -> TaskWorkspace:
    """Restore an existing task worktree or bootstrap it from fallback cwd."""
    if persisted is not None:
        worktree_path = Path(persisted.worktree_path)
        if not worktree_path.exists():
            repo_root = Path(persisted.repo_root)
            _ensure_worktree(repo_root, worktree_path, persisted.branch)
        owner = _acquire_lock(Path(persisted.lock_path), persisted)
        return TaskWorkspace(
            durable_id=persisted.durable_id,
            repo_root=persisted.repo_root,
            repo_name=persisted.repo_name,
            original_cwd=persisted.original_cwd,
            effective_cwd=persisted.effective_cwd,
            worktree_path=persisted.worktree_path,
            branch=persisted.branch,
            lock_path=persisted.lock_path,
            lock_owner=owner,
        )

    if fallback_source_cwd is None:
        raise WorktreeError(
            "Cannot restore task workspace without persisted metadata or source cwd",
            details={"durable_id": durable_id},
        )

    source_dir = Path(fallback_source_cwd).expanduser().resolve()
    root = _worktree_root()
    caller_repo_root = _resolve_repo_root(source_dir)

    repo_matches: list[tuple[Path, Path]] = []
    for match in root.glob(f"*/{durable_id}"):
        worktree_path = match.resolve()
        try:
            match_repo_root = _resolve_common_repo_root(worktree_path)
        except WorktreeError:
            continue
        if match_repo_root == caller_repo_root:
            repo_matches.append((worktree_path, match_repo_root))

    if len(repo_matches) > 1:
        raise WorktreeError(
            "Multiple managed worktrees found for durable task",
            details={
                "durable_id": durable_id,
                "repo_root": str(caller_repo_root),
                "matches": [str(match[0]) for match in repo_matches],
            },
        )

    if len(repo_matches) == 1:
        worktree_path, repo_root = repo_matches[0]
        repo_name = worktree_path.parent.name
        branch = _managed_branch_name(repo_root, durable_id)
        lock = _lock_path(repo_name, durable_id)
        effective_cwd = worktree_path / _relative_subdir(caller_repo_root, source_dir)
        workspace = TaskWorkspace(
            durable_id=durable_id,
            repo_root=str(repo_root),
            repo_name=repo_name,
            original_cwd=str(source_dir),
            effective_cwd=str(effective_cwd),
            worktree_path=str(worktree_path),
            branch=branch,
            lock_path=str(lock),
        )
        owner = _acquire_lock(lock, workspace)
        return TaskWorkspace(
            durable_id=workspace.durable_id,
            repo_root=workspace.repo_root,
            repo_name=workspace.repo_name,
            original_cwd=workspace.original_cwd,
            effective_cwd=workspace.effective_cwd,
            worktree_path=workspace.worktree_path,
            branch=workspace.branch,
            lock_path=workspace.lock_path,
            lock_owner=owner,
        )

    return prepare_task_workspace(fallback_source_cwd, durable_id, allow_dirty=allow_dirty)


def maybe_prepare_task_workspace(
    source_cwd: str | Path,
    durable_id: str,
    *,
    allow_dirty: bool = False,
) -> TaskWorkspace | None:
    """Provision a task workspace only when worktrees are enabled."""
    if not _worktrees_enabled():
        return None
    repo_root = _try_resolve_repo_root(source_cwd)
    if repo_root is None:
        return None
    if allow_dirty and _checkout_is_dirty(repo_root):
        return None
    try:
        return prepare_task_workspace(source_cwd, durable_id, allow_dirty=allow_dirty)
    except WorktreeError as exc:
        if exc.message == "Cannot start task worktree from a dirty checkout":
            return None
        raise


def maybe_restore_task_workspace(
    durable_id: str,
    persisted: TaskWorkspace | None,
    *,
    fallback_source_cwd: str | Path | None = None,
    allow_dirty: bool = False,
) -> TaskWorkspace | None:
    """Restore or bootstrap a task workspace when worktrees are enabled."""
    if persisted is None and not _worktrees_enabled():
        return None
    if persisted is None:
        if fallback_source_cwd is None:
            return None
        if _try_resolve_repo_root(fallback_source_cwd) is None:
            return None
    try:
        return restore_task_workspace(
            durable_id,
            persisted,
            fallback_source_cwd=fallback_source_cwd,
            allow_dirty=allow_dirty,
        )
    except WorktreeError as exc:
        if exc.message == "Cannot start task worktree from a dirty checkout":
            return None
        raise
