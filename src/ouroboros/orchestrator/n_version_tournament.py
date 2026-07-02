"""N-version tournament scaffolding (PR-X X3, opt-in, NOT live-wired).

The idea: an AC that has already burned its one cross-harness redispatch (X1) and
failed *again* can, when ``execution.n_version_tournament`` is on, be dispatched
to up to two additional distinct runtimes **in parallel**, each in its own
isolated git worktree of the run workspace. First result whose verification
passes wins; loser worktrees are discarded; the winner's changes are applied back
to the main workspace.

Status — deliberately scaffolding, not a live trigger. This module ships the
mechanically-testable primitives as pure functions plus a worktree manager:

* :func:`plan_tournament` — pick up to N distinct alternative runtimes (pure).
* :class:`RunWorktreeManager` — create/cleanup *isolated* git worktrees so no
  contestant ever shares a dirty workspace.
* :func:`select_tournament_winner` — first contestant whose verification passed
  wins, deterministically (pure).
* :func:`export_worktree_diff` / :func:`apply_diff_to_workspace` — the simplest
  robust winner-apply mechanism: capture the winner worktree's ``git diff`` and
  ``git apply`` it onto the main workspace.

Wiring the live per-AC trigger into ``parallel_executor`` is intentionally left
out: it is deeply entangled with the executor's per-AC dispatch internals and the
concurrently-edited retry area, and cannot be exercised safely without real
runtimes. Shipping the tested primitives here — and stopping short of the live
trigger — is the honest split (see PR-X X3 guidance).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
import tempfile

from ouroboros.observability.logging import get_logger
from ouroboros.orchestrator.runtime_picker import (
    RuntimeBackendRef,
    pick_alternative_runtime,
)

log = get_logger(__name__)

# Contest at most this many *additional* runtimes in parallel (X3 says "up to 2").
DEFAULT_MAX_CONTESTANTS = 2


@dataclass(frozen=True, slots=True)
class TournamentEntry:
    """One contestant's outcome in the N-version tournament."""

    backend: RuntimeBackendRef
    passed: bool
    worktree_path: str | None = None
    summary: str | None = None


def plan_tournament(
    failed_backend: RuntimeBackendRef,
    *,
    exclude: set[str] | None = None,
    weights: Mapping[str, float] | None = None,
    max_contestants: int = DEFAULT_MAX_CONTESTANTS,
) -> tuple[RuntimeBackendRef, ...]:
    """Pick up to ``max_contestants`` distinct alternative runtimes to contest.

    Pure and deterministic: repeatedly calls :func:`pick_alternative_runtime`,
    growing the exclusion set each round so every contestant is a distinct
    backend. Returns fewer than ``max_contestants`` (possibly zero) when the
    machine simply does not have enough installed alternatives.
    """
    if max_contestants <= 0:
        return ()
    chosen: list[RuntimeBackendRef] = []
    excluded: set[str] = set(exclude or set())
    excluded.add(failed_backend)
    for _ in range(max_contestants):
        candidate = pick_alternative_runtime(failed_backend, exclude=excluded, weights=weights)
        if candidate is None:
            break
        chosen.append(candidate)
        excluded.add(candidate)
    return tuple(chosen)


def select_tournament_winner(
    entries: Sequence[TournamentEntry],
) -> TournamentEntry | None:
    """Return the first contestant whose verification passed, or ``None``.

    "First" is by the given sequence order, so callers that want a
    fastest-wins race should append entries in completion order, while callers
    that want a deterministic priority should append in priority order.
    """
    for entry in entries:
        if entry.passed:
            return entry
    return None


class RunWorktreeManager:
    """Create and clean up isolated git worktrees of a run workspace.

    Each contestant gets its own worktree checked out at the workspace's current
    ``HEAD`` — never a shared, possibly-dirty directory. Worktrees live under a
    private temp root and are force-removed on cleanup so a crashed contestant
    never leaves the main repo with a dangling worktree registration.
    """

    def __init__(self, workspace: Path | str) -> None:
        self._workspace = Path(workspace).expanduser().resolve()
        self._root = Path(tempfile.mkdtemp(prefix="ooo-nversion-"))
        self._created: list[Path] = []

    @property
    def workspace(self) -> Path:
        """The main run workspace these worktrees branch from."""
        return self._workspace

    def _git(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd or self._workspace),
            capture_output=True,
            text=True,
            check=True,
        )

    def create(self, label: str) -> Path:
        """Add an isolated worktree for ``label`` at the workspace HEAD."""
        safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in label) or "wt"
        path = self._root / safe
        # --detach keeps each contestant on a detached HEAD so no branch name
        # collides across contestants sharing the same base commit.
        self._git("worktree", "add", "--detach", str(path), "HEAD")
        self._created.append(path)
        return path

    def cleanup(self, path: Path) -> None:
        """Force-remove a single worktree, ignoring already-gone paths."""
        try:
            self._git("worktree", "remove", "--force", str(path))
        except subprocess.CalledProcessError as exc:
            log.debug("n_version.worktree_remove_failed", path=str(path), error=str(exc))
        if path in self._created:
            self._created.remove(path)

    def cleanup_all(self) -> None:
        """Remove every worktree created by this manager and prune the temp root."""
        for path in list(self._created):
            self.cleanup(path)
        try:
            self._git("worktree", "prune")
        except subprocess.CalledProcessError as exc:
            log.debug("n_version.worktree_prune_failed", error=str(exc))
        shutil.rmtree(self._root, ignore_errors=True)

    def __enter__(self) -> RunWorktreeManager:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.cleanup_all()


def export_worktree_diff(worktree_path: Path | str) -> str:
    """Return the winner worktree's working-tree diff against HEAD.

    Includes untracked files via ``--`` intent-to-add is out of scope for the
    scaffold; we capture tracked changes, which is the common case for an AC that
    edits existing files. Returns an empty string on any git error.
    """
    path = Path(worktree_path)
    try:
        result = subprocess.run(
            ["git", "diff", "HEAD"],
            cwd=str(path),
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        log.debug("n_version.export_diff_failed", path=str(path), error=str(exc))
        return ""
    return result.stdout


def apply_diff_to_workspace(workspace: Path | str, diff: str) -> bool:
    """Apply a captured winner diff onto the main workspace via ``git apply``.

    Returns ``True`` when the patch applied cleanly. A no-op empty diff counts as
    success (nothing to apply). Any conflict/error returns ``False`` so the caller
    keeps the main workspace untouched rather than half-applied.
    """
    if not diff.strip():
        return True
    try:
        subprocess.run(
            ["git", "apply", "--whitespace=nowarn", "-"],
            cwd=str(Path(workspace)),
            input=diff,
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        log.debug("n_version.apply_diff_failed", error=str(exc))
        return False
    return True


__all__ = [
    "DEFAULT_MAX_CONTESTANTS",
    "RunWorktreeManager",
    "TournamentEntry",
    "apply_diff_to_workspace",
    "export_worktree_diff",
    "plan_tournament",
    "select_tournament_winner",
]
