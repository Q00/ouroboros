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
  robust winner-apply mechanism: capture the winner worktree's
  ``git diff --binary`` and ``git apply`` it onto the main workspace. Uses
  ``--binary`` for full binary content transfer, ``-z`` for NUL-safe untracked
  file listing, ``--`` before pathnames, and rejects rc=1 with no patch. A
  failed export is reported as ``None`` (distinct from an empty ``b""`` diff) so
  a git error can never be mistaken for "the winner changed nothing".

Wiring the live per-AC trigger into ``parallel_executor`` is intentionally left
out: it is deeply entangled with the executor's per-AC dispatch internals and the
concurrently-edited retry area, and cannot be exercised safely without real
runtimes. Shipping the tested primitives here — and stopping short of the live
trigger — is the honest split (see PR-X X3 guidance).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import os
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

# Wall-clock ceiling for every git subprocess this module runs. Worktree
# add/remove is materially slower than the ``rev-parse``-style probes elsewhere
# in the codebase (``core.git_workflow`` uses 5s), so this matches the 30s used
# by ``core.worktree`` for the same class of operation. Without a bound, a
# wedged git (e.g. ``index.lock`` contention with a concurrent generation) hangs
# the tournament forever — including teardown, which leaks worktrees and the
# temp root.
GIT_COMMAND_TIMEOUT_SECONDS = 30

# Git failures that must never abort an in-progress cleanup sequence.
# ``TimeoutExpired`` is *not* a ``CalledProcessError`` subclass, so it has to be
# named explicitly or a timed-out teardown step would skip the rest of cleanup.
_GIT_CLEANUP_ERRORS = (
    subprocess.CalledProcessError,
    subprocess.TimeoutExpired,
    OSError,
)


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
            timeout=GIT_COMMAND_TIMEOUT_SECONDS,
        )

    def create(self, label: str) -> Path:
        """Add an isolated worktree for ``label`` at the workspace HEAD.

        Raises ``CalledProcessError``/``TimeoutExpired``/``OSError`` when git
        cannot produce the worktree: a contestant with no isolated checkout must
        fail loudly rather than silently share the main workspace.
        """
        safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in label) or "wt"
        path = self._root / safe
        # --detach keeps each contestant on a detached HEAD so no branch name
        # collides across contestants sharing the same base commit.
        try:
            self._git("worktree", "add", "--detach", str(path), "HEAD")
        except _GIT_CLEANUP_ERRORS:
            # A failed or timed-out ``worktree add`` may still have left a
            # partial checkout and a registration behind. Track it so cleanup
            # force-removes and prunes it instead of leaking it.
            self._created.append(path)
            raise
        self._created.append(path)
        return path

    def cleanup(self, path: Path) -> None:
        """Force-remove a single worktree, ignoring already-gone paths.

        Never raises: cleanup is best-effort so one wedged worktree cannot
        abandon the rest of the teardown sequence.
        """
        try:
            self._git("worktree", "remove", "--force", str(path))
        except _GIT_CLEANUP_ERRORS as exc:
            log.warning("n_version.worktree_remove_failed", path=str(path), error=str(exc))
        if path in self._created:
            self._created.remove(path)

    def cleanup_all(self) -> None:
        """Remove every worktree created by this manager and prune the temp root.

        The temp root is removed in a ``finally`` so a hung or failing git step
        can never leak the ``mkdtemp`` directory. ``git worktree prune`` runs
        AFTER the ``rmtree`` so that stale worktree registrations left behind
        by a removed directory are cleaned up.
        """
        try:
            for path in list(self._created):
                self.cleanup(path)
        finally:
            shutil.rmtree(self._root, ignore_errors=True)
            try:
                self._git("worktree", "prune")
            except _GIT_CLEANUP_ERRORS as exc:
                log.warning("n_version.worktree_prune_failed", error=str(exc))

    def __enter__(self) -> RunWorktreeManager:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.cleanup_all()


def export_worktree_diff(worktree_path: Path | str) -> bytes | None:
    """Return the winner worktree's working-tree diff against HEAD.

    Captures both tracked changes (``git diff --binary HEAD``) and untracked new
    files (detected via ``git ls-files -z --others --exclude-standard``, then
    each diffed with ``git diff --binary --no-index -- /dev/null <file>``).
    This ensures a winner that only creates new files — including binary files —
    still produces a usable patch.

    The patch and NUL-delimited pathname listing remain bytes throughout so
    arbitrary valid Git path bytes and non-UTF-8 file content round-trip without
    implicit text decoding. Pathnames are converted with :func:`os.fsdecode`
    only when passed back to the operating system; its surrogateescape behavior
    preserves undecodable bytes on POSIX.

    The ``--binary`` flag generates full binary content in the patch so that
    ``git apply`` can reconstruct binary files rather than emitting an
    inapplicable "Binary files differ" marker.

    Untracked file listing uses ``-z`` (NUL-terminated) output to safely handle
    filenames that Git would otherwise C-quote (non-ASCII, tabs, newlines,
    quotes). Each pathname is passed after ``--`` to prevent interpretation as
    an option.

    Returns ``None`` when git could not be consulted at all, and ``b""`` only
    when git succeeded and genuinely reported no changes. The distinction
    matters: a failed export means the winning contestant's work has *not* been
    captured, so treating it as "nothing to apply" would silently discard that
    work while reporting a successful merge.
    """
    path = Path(worktree_path)
    try:
        result = subprocess.run(
            ["git", "diff", "--binary", "HEAD"],
            cwd=str(path),
            capture_output=True,
            check=True,
            timeout=GIT_COMMAND_TIMEOUT_SECONDS,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        log.error("n_version.export_diff_failed", path=str(path), error=str(exc))
        return None

    diff_output = result.stdout

    # Git emits raw pathname bytes with -z. Decode only at the filesystem
    # boundary so os.fsdecode/os.fsencode can round-trip undecodable bytes via
    # surrogateescape instead of applying strict stdout text decoding.
    try:
        untracked_result = subprocess.run(
            ["git", "ls-files", "-z", "--others", "--exclude-standard"],
            cwd=str(path),
            capture_output=True,
            check=True,
            timeout=GIT_COMMAND_TIMEOUT_SECONDS,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        log.error("n_version.export_untracked_list_failed", path=str(path), error=str(exc))
        return None

    untracked_files = [
        os.fsdecode(raw_path) for raw_path in untracked_result.stdout.split(b"\0") if raw_path
    ]

    for ufile in untracked_files:
        try:
            # git diff --no-index exits with code 1 when there are differences,
            # which is the expected case here (comparing /dev/null to a new file).
            # --binary ensures binary content is encoded in the patch. ``--``
            # prevents option-like pathnames from being interpreted as options.
            file_diff_result = subprocess.run(
                ["git", "diff", "--binary", "--no-index", "--", "/dev/null", ufile],
                cwd=str(path),
                capture_output=True,
                check=False,
                timeout=GIT_COMMAND_TIMEOUT_SECONDS,
            )
            # Exit code 1 means differences found (expected), while 0 means no
            # diff. Every other status, including negative signal return codes,
            # means Git did not complete the export successfully.
            if file_diff_result.returncode not in (0, 1):
                log.error(
                    "n_version.export_untracked_diff_failed",
                    path=str(path),
                    file=repr(ufile),
                    stderr=file_diff_result.stderr.decode(errors="backslashreplace"),
                )
                return None
            # Reject rc=1 with no patch output: this indicates git could not
            # read the file (e.g. a broken symlink or permission error) rather
            # than a real difference. Treating it as empty would silently lose
            # the file.
            if file_diff_result.returncode == 1 and not file_diff_result.stdout.strip():
                log.error(
                    "n_version.export_untracked_diff_empty_patch",
                    path=str(path),
                    file=repr(ufile),
                    stderr=file_diff_result.stderr.decode(errors="backslashreplace"),
                    reason="git diff --no-index returned rc=1 but produced no patch",
                )
                return None
            if file_diff_result.stdout:
                diff_output += file_diff_result.stdout
        except (subprocess.TimeoutExpired, OSError) as exc:
            log.error(
                "n_version.export_untracked_diff_failed",
                path=str(path),
                file=repr(ufile),
                error=str(exc),
            )
            return None

    return diff_output


def apply_diff_to_workspace(workspace: Path | str, diff: bytes | None) -> bool:
    """Apply a captured winner diff onto the main workspace via ``git apply``.

    Returns ``True`` when the patch applied cleanly. A genuinely empty diff
    (``b""``) counts as success — there is nothing to apply. A ``None`` diff
    means :func:`export_worktree_diff` failed and the winner's changes were never
    captured; that is a hard failure, never a no-op success. Any conflict/error
    returns ``False`` so the caller keeps the main workspace untouched rather
    than half-applied.
    """
    if diff is None:
        log.error(
            "n_version.apply_diff_missing",
            workspace=str(workspace),
            reason="winner diff export failed; refusing to report a no-op merge as success",
        )
        return False
    if not diff.strip():
        return True
    try:
        subprocess.run(
            ["git", "apply", "--whitespace=nowarn", "-"],
            cwd=str(Path(workspace)),
            input=diff,
            capture_output=True,
            check=True,
            timeout=GIT_COMMAND_TIMEOUT_SECONDS,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        log.error("n_version.apply_diff_failed", workspace=str(workspace), error=str(exc))
        return False
    return True


__all__ = [
    "DEFAULT_MAX_CONTESTANTS",
    "GIT_COMMAND_TIMEOUT_SECONDS",
    "RunWorktreeManager",
    "TournamentEntry",
    "apply_diff_to_workspace",
    "export_worktree_diff",
    "plan_tournament",
    "select_tournament_winner",
]
