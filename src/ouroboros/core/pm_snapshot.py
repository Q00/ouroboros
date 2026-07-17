"""Persistent read-only worktree snapshots for PM brownfield exploration.

Each registered brownfield repo gets one long-lived detached git worktree
under ``~/.ouroboros/pm-snapshots/<name>-<hash>``, pinned to the remote
default branch (``origin/HEAD`` → ``origin/main`` → ``origin/master``,
falling back to the local ``HEAD`` when no remote ref resolves). Starting a
PM interview refreshes the snapshot in place — ``git fetch`` in the source
repo, then a hard reset of the snapshot to the resolved commit — instead of
re-creating it, so exploration always reads the latest remote main no matter
how stale or dirty the developer's local checkout is.

Every operation here is best-effort: any git failure falls back to the
original repo path and must never break the interview flow.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import subprocess

import structlog

from ouroboros.config.loader import load_config
from ouroboros.config.models import OrchestratorConfig
from ouroboros.core.errors import ConfigError
from ouroboros.core.file_lock import file_lock

log = structlog.get_logger()

# Timeouts (seconds). Fetch and worktree checkout can be slow on large repos.
_FETCH_TIMEOUT = 60
_GIT_TIMEOUT = 120


class PMSnapshotError(Exception):
    """Raised internally when a snapshot git operation fails."""


def _orchestrator_config() -> OrchestratorConfig:
    try:
        return load_config().orchestrator
    except (ConfigError, FileNotFoundError):
        return OrchestratorConfig()


def pm_snapshots_enabled() -> bool:
    """Return True when PM brownfield snapshot worktrees are enabled."""
    config = _orchestrator_config()
    return getattr(config, "pm_snapshot_worktrees", True)


def snapshot_root() -> Path:
    """Return the root directory for PM snapshot worktrees."""
    config = _orchestrator_config()
    root = getattr(config, "pm_snapshot_root", "~/.ouroboros/pm-snapshots")
    return Path(root).expanduser()


def _run_git(args: list[str], cwd: Path, *, timeout: int = _GIT_TIMEOUT) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        raise PMSnapshotError(f"git {' '.join(args)}: {exc}") from exc
    if result.returncode != 0:
        raise PMSnapshotError(f"git {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout.strip()


def _resolve_repo_root(path: Path) -> Path:
    top = _run_git(["rev-parse", "--show-toplevel"], path, timeout=10)
    return Path(top).resolve()


def _snapshot_dir(source_root: Path) -> Path:
    """Return the stable per-repo snapshot location.

    The path digest disambiguates repos that share a directory name.
    """
    digest = hashlib.sha1(str(source_root).encode("utf-8")).hexdigest()[:8]
    return snapshot_root() / f"{source_root.name}-{digest}"


def _snapshot_lock_path(source_root: Path) -> Path:
    """Return the stable per-repo refresh lock path."""
    return snapshot_root() / ".locks" / _snapshot_dir(source_root).name


def _resolve_snapshot_ref(repo_root: Path) -> tuple[str, str]:
    """Resolve the ref and commit a snapshot should pin to.

    Fetches ``origin`` first (failure tolerated — offline refreshes pin to
    the last-known remote ref), then tries the remote default branch and
    common fallbacks before settling on the local ``HEAD``.

    Returns:
        Tuple of (ref name, commit sha).
    """
    try:
        _run_git(["fetch", "origin", "--prune", "--quiet"], repo_root, timeout=_FETCH_TIMEOUT)
    except PMSnapshotError as exc:
        log.warning("pm_snapshot.fetch_failed", repo=str(repo_root), error=str(exc))

    candidates: list[str] = []
    try:
        origin_head = _run_git(
            ["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], repo_root, timeout=10
        )
        if origin_head:
            candidates.append(origin_head)
    except PMSnapshotError:
        pass
    candidates.extend(["refs/remotes/origin/main", "refs/remotes/origin/master", "HEAD"])

    for ref in candidates:
        try:
            sha = _run_git(["rev-parse", "--verify", f"{ref}^{{commit}}"], repo_root, timeout=10)
        except PMSnapshotError:
            continue
        return ref, sha

    raise PMSnapshotError(f"no snapshot ref resolvable in {repo_root}")


def _is_linked_worktree_of(snapshot_path: Path, source_root: Path) -> bool:
    """Check that ``snapshot_path`` is a live worktree linked to ``source_root``."""
    if not snapshot_path.is_dir():
        return False
    try:
        common = _run_git(
            ["rev-parse", "--path-format=absolute", "--git-common-dir"],
            snapshot_path,
            timeout=10,
        )
    except PMSnapshotError:
        return False
    return Path(common).resolve().parent == source_root


def _refresh_one(source_root: Path) -> Path:
    """Create or refresh the snapshot worktree for one repo, returning its path."""
    ref, sha = _resolve_snapshot_ref(source_root)
    snapshot_path = _snapshot_dir(source_root)

    if _is_linked_worktree_of(snapshot_path, source_root):
        _run_git(["reset", "--hard", sha], snapshot_path)
        _run_git(["clean", "-fd"], snapshot_path)
        log.info(
            "pm_snapshot.refreshed",
            repo=str(source_root),
            snapshot_path=str(snapshot_path),
            ref=ref,
            sha=sha,
        )
        return snapshot_path

    # Stale directory that is no longer a valid linked worktree — clear it
    # and drop any dangling registration before re-adding.
    if snapshot_path.exists():
        shutil.rmtree(snapshot_path, ignore_errors=True)
        try:
            _run_git(["worktree", "prune"], source_root, timeout=30)
        except PMSnapshotError:
            pass

    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    _run_git(["worktree", "add", "--detach", str(snapshot_path), sha], source_root)
    log.info(
        "pm_snapshot.created",
        repo=str(source_root),
        snapshot_path=str(snapshot_path),
        ref=ref,
        sha=sha,
    )
    return snapshot_path


def refresh_pm_snapshot_worktrees(
    repos: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Point brownfield repos at refreshed snapshot worktrees.

    Returns a new repo list whose ``path`` targets the per-repo snapshot
    worktree (created on first use, hard-reset to the remote default branch
    afterwards), with the original checkout preserved as ``source_path``.
    Repos that cannot be snapshotted (not a git repo, git failure) are
    returned unchanged so the interview proceeds against the live checkout.
    """
    if not repos or not pm_snapshots_enabled():
        return repos

    result: list[dict[str, str]] = []
    for repo in repos:
        source = repo.get("path", "")
        if not source:
            result.append(repo)
            continue
        try:
            source_root = _resolve_repo_root(Path(source).expanduser())
            with file_lock(_snapshot_lock_path(source_root)):
                snapshot_path = _refresh_one(source_root)
        except (PMSnapshotError, OSError, RuntimeError) as exc:
            log.warning("pm_snapshot.refresh_failed", repo=source, error=str(exc))
            result.append(repo)
            continue
        result.append({**repo, "path": str(snapshot_path), "source_path": source})
    return result
