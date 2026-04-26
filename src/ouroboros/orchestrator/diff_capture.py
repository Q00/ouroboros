"""Per-AC git-diff capture helpers for serial compounding mode (Phase 2 / Q2).

Pure synchronous helpers used by :class:`SerialCompoundingExecutor` to surround
each AC body with ``git stash create`` boundaries and populate
:attr:`ACPostmortem.diff_summary` with a truncated ``git diff --stat`` between
the pre- and post-AC snapshots.

Design notes (see ``docs/brainstorm/phase-2-q2-diff-capture-design.md``):

- ``git stash create`` is the *primary* per-AC boundary mechanism. It
  produces an unreferenced commit SHA without modifying ``.git/refs/stash``
  or the working tree, so nothing accumulates across thousands of ACs.
  [[INVARIANT: git stash create produces an unreferenced SHA without
  modifying .git/refs/stash, so stash list stays empty across runs]]
- ``git stash create`` returns empty stdout on a clean tree.  The
  SerialCompoundingExecutor's commit-per-AC workflow leaves clean trees
  at AC boundaries, so the helpers fall back to ``git rev-parse HEAD`` —
  also a valid tree-ish for ``git diff --stat`` — so committed-only
  changes are still captured.
  [[INVARIANT: capture falls back to git rev-parse HEAD when git stash
  create returns empty (clean tree)]]
- All git subprocess calls use ``check=False`` and a 5s timeout.  Every
  failure (no ``.git/``, missing binary, timeout, non-zero exit) results in
  an empty ``diff_summary`` plus a structured warning log — the AC
  continues and the run is unaffected.
  [[INVARIANT: diff capture failures never propagate — diff_summary
  becomes "" on every error path]]
- Diff capture is **SerialCompoundingExecutor-only**.  ParallelACExecutor
  does NOT call into this module, so parallel-mode prompts and event
  streams remain byte-identical pre/post Q2.
  [[INVARIANT: parallel mode prompts are byte-identical pre/post Q2
  because diff capture is SerialCompoundingExecutor-only]]
- For ACs that touch tracked files in a real git workspace, the resulting
  ``diff_summary`` is non-empty and contains at least one ``--stat`` line
  plus the summary footer.
  [[INVARIANT: ACPostmortem.diff_summary is non-empty for ACs that
  modify tracked files in a git workspace]]
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

from ouroboros.observability.logging import get_logger

log = get_logger(__name__)

# Subprocess timeout for every ``git`` call we make (seconds).  Tight on
# purpose — diff capture is best-effort, never a run-blocker.
_GIT_TIMEOUT_SECONDS = 5.0

# Defaults for the truncation knobs.  Configurable via env vars so dogfood
# runs can dial them in without code changes.
_DEFAULT_FILE_CAP = 20
_DEFAULT_CHAR_BUDGET = 4000

# Marker appended when ``char_budget`` truncation kicks in.  Kept terse so
# it doesn't eat budget itself.
_TRUNCATED_MARKER = "[truncated]"


def _diff_capture_enabled() -> bool:
    """Return True when diff capture should run, False otherwise.

    Reads ``OUROBOROS_DIFF_CAPTURE_ENABLED`` at call time (mirrors the
    pattern in ``serial_executor._get_min_reliability``).  Treats
    ``"false"``, ``"0"``, and the empty string as disabled (case-insensitive).
    Anything else — including the default of ``"true"`` — enables capture.
    """
    raw = os.environ.get("OUROBOROS_DIFF_CAPTURE_ENABLED", "true").strip().lower()
    return raw not in {"false", "0", ""}


def _resolve_file_cap(file_cap: int | None) -> int:
    """Resolve the effective file cap.

    ``None`` means "caller did not specify — consult
    ``OUROBOROS_DIFF_SUMMARY_FILE_CAP`` and fall back to
    :data:`_DEFAULT_FILE_CAP`."  Any explicit int (including a value
    equal to the default constant) is honored as a hard override.
    Invalid env-var values fall back silently.
    """
    if file_cap is not None:
        return file_cap
    raw = os.environ.get("OUROBOROS_DIFF_SUMMARY_FILE_CAP", "").strip()
    if raw:
        try:
            parsed = int(raw)
            if parsed > 0:
                return parsed
        except ValueError:
            pass
    return _DEFAULT_FILE_CAP


def _resolve_char_budget(char_budget: int | None) -> int:
    """Resolve the effective char budget (mirror of :func:`_resolve_file_cap`).

    ``None`` defers to env or default; any explicit int wins.
    """
    if char_budget is not None:
        return char_budget
    raw = os.environ.get("OUROBOROS_DIFF_SUMMARY_CHAR_BUDGET", "").strip()
    if raw:
        try:
            parsed = int(raw)
            if parsed > 0:
                return parsed
        except ValueError:
            pass
    return _DEFAULT_CHAR_BUDGET


def _is_git_repo(workspace_root: Path) -> bool:
    """Return True iff ``workspace_root`` contains a ``.git`` entry.

    Worktrees use a regular file rather than a directory, so we accept either.
    """
    git_path = workspace_root / ".git"
    return git_path.exists()


def _resolve_head(workspace_root: Path, *, phase: str) -> str | None:
    """Resolve HEAD to a commit SHA, returning ``None`` on any failure.

    Used as a fallback when ``git stash create`` returns empty stdout
    (clean tree).  ``git rev-parse HEAD`` is also a valid tree-ish for
    ``git diff --stat``, so the downstream diff is unchanged.

    Failures are logged at WARNING with ``fallback="head"`` so structured
    log queries can distinguish stash-path failures from HEAD-path
    failures.

    Args:
        workspace_root: Directory to run ``git`` from.
        phase: ``"pre_snapshot"`` or ``"post_snapshot"`` (for telemetry).

    Returns:
        The HEAD SHA, or ``None`` on any failure (no HEAD, broken repo,
        timeout, etc.).
    """
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(workspace_root),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        log.warning(
            "serial_executor.diff_capture.failed",
            reason="git_binary_missing",
            phase=phase,
            fallback="head",
        )
        return None
    except subprocess.TimeoutExpired:
        log.warning(
            "serial_executor.diff_capture.failed",
            reason="timeout",
            phase=phase,
            fallback="head",
        )
        return None
    except OSError as exc:  # pragma: no cover — defensive belt
        log.warning(
            "serial_executor.diff_capture.failed",
            reason="os_error",
            phase=phase,
            fallback="head",
            error=str(exc),
        )
        return None

    if completed.returncode != 0:
        log.warning(
            "serial_executor.diff_capture.failed",
            reason=f"head_lookup_exit_{completed.returncode}",
            phase=phase,
            fallback="head",
            stderr=(completed.stderr or "")[:200],
        )
        return None

    sha = (completed.stdout or "").strip()
    if not sha:
        log.warning(
            "serial_executor.diff_capture.failed",
            reason="head_lookup_empty",
            phase=phase,
            fallback="head",
        )
        return None
    return sha


def capture_pre_ac_snapshot(workspace_root: Path) -> str | None:
    """Run ``git stash create`` in ``workspace_root``.

    Returns the resulting 40-char SHA on success, or ``None`` on every
    failure mode (no ``.git/``, missing git binary, timeout, non-zero exit).
    Failures emit structured warning logs but do **not** raise.

    Args:
        workspace_root: Directory to invoke ``git stash create`` from.

    Returns:
        The stash SHA, or ``None`` on any failure.
    """
    if not _is_git_repo(workspace_root):
        log.warning(
            "serial_executor.diff_capture.skipped",
            reason="not_a_git_repo",
            workspace_root=str(workspace_root),
        )
        return None

    try:
        completed = subprocess.run(
            ["git", "stash", "create"],
            cwd=str(workspace_root),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        log.warning(
            "serial_executor.diff_capture.failed",
            reason="git_binary_missing",
            workspace_root=str(workspace_root),
        )
        return None
    except subprocess.TimeoutExpired:
        log.warning(
            "serial_executor.diff_capture.failed",
            reason="timeout",
            phase="pre_snapshot",
            workspace_root=str(workspace_root),
        )
        return None
    except OSError as exc:  # pragma: no cover — defensive belt
        log.warning(
            "serial_executor.diff_capture.failed",
            reason="os_error",
            phase="pre_snapshot",
            error=str(exc),
            workspace_root=str(workspace_root),
        )
        return None

    if completed.returncode != 0:
        log.warning(
            "serial_executor.diff_capture.failed",
            reason=f"stash_create_exit_{completed.returncode}",
            phase="pre_snapshot",
            stderr=(completed.stderr or "")[:200],
        )
        return None

    sha = (completed.stdout or "").strip()
    # ``git stash create`` exits 0 with empty stdout when the working tree
    # is clean.  The orchestrator's commit-per-AC workflow leaves clean
    # trees at AC boundaries, so fall back to HEAD — also a valid tree-ish
    # for downstream ``git diff --stat`` — to capture committed changes.
    if not sha:
        log.debug(
            "serial_executor.diff_capture.head_fallback",
            phase="pre_snapshot",
            reason="stash_create_empty_sha",
        )
        return _resolve_head(workspace_root, phase="pre_snapshot")
    return sha


def _capture_post_snapshot(workspace_root: Path) -> str | None:
    """Internal: run ``git stash create`` for the post-AC snapshot.

    Mirrors :func:`capture_pre_ac_snapshot` but does NOT log the
    ``not_a_git_repo`` "skipped" event — by the time we reach the post
    snapshot the pre snapshot already succeeded, so the repo exists.
    """
    try:
        completed = subprocess.run(
            ["git", "stash", "create"],
            cwd=str(workspace_root),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        log.warning(
            "serial_executor.diff_capture.failed",
            reason="git_binary_missing",
            phase="post_snapshot",
        )
        return None
    except subprocess.TimeoutExpired:
        log.warning(
            "serial_executor.diff_capture.failed",
            reason="timeout",
            phase="post_snapshot",
        )
        return None
    except OSError as exc:  # pragma: no cover — defensive belt
        log.warning(
            "serial_executor.diff_capture.failed",
            reason="os_error",
            phase="post_snapshot",
            error=str(exc),
        )
        return None

    if completed.returncode != 0:
        log.warning(
            "serial_executor.diff_capture.failed",
            reason=f"stash_create_exit_{completed.returncode}",
            phase="post_snapshot",
            stderr=(completed.stderr or "")[:200],
        )
        return None

    sha = (completed.stdout or "").strip()
    if not sha:
        # Clean tree at AC end (e.g. agent committed everything).  Fall
        # back to HEAD so the diff still covers committed changes.
        log.debug(
            "serial_executor.diff_capture.head_fallback",
            phase="post_snapshot",
            reason="stash_create_empty_sha",
        )
        return _resolve_head(workspace_root, phase="post_snapshot")
    return sha


def _churn_for_stat_line(line: str) -> int:
    """Estimate insertions+deletions for one ``--stat`` line.

    ``git diff --stat`` lines look like::

        path/to/foo.py    | 23 +++++++-------
        path/to/bar.py    |  8 ----

    The number after the ``|`` is the total churn (additions + deletions).
    Falls back to counting ``+`` and ``-`` in the histogram column if the
    leading number isn't parseable.  Returns 0 on anything unrecognized so
    such lines sort last under a stable sort.
    """
    if "|" not in line:
        return 0
    _, _, after = line.partition("|")
    after = after.strip()
    if not after:
        return 0
    # First whitespace-separated token is the churn count for textual files;
    # binary-file lines look like "Bin 1234 -> 5678 bytes" — fall back to the
    # ``+`` / ``-`` histogram count there.
    head, _, _ = after.partition(" ")
    try:
        return int(head)
    except ValueError:
        return after.count("+") + after.count("-")


def _truncate_stat(raw_stat: str, *, file_cap: int, char_budget: int) -> str:
    """Apply file-cap and char-budget truncation to a ``git diff --stat`` blob.

    Strategy:

    1. Split into per-file lines plus the trailing summary line ("N files
       changed, M insertions(+), L deletions(-)").
    2. If file-line count > ``file_cap``, sort by churn descending (stable),
       keep the top ``file_cap``, append ``... and K more files``.
    3. Re-assemble; if total length > ``char_budget``, truncate at
       ``char_budget`` and append :data:`_TRUNCATED_MARKER`, *preserving*
       the summary footer (it's small and authoritative).
    """
    text = raw_stat.rstrip("\n")
    if not text:
        return ""

    lines = text.split("\n")
    # The summary line is the last non-empty line and contains "changed,".
    summary_line = ""
    file_lines: list[str] = []
    if lines and "changed," in lines[-1]:
        summary_line = lines[-1].strip()
        file_lines = [ln for ln in lines[:-1] if ln.strip()]
    else:
        # No recognizable summary line — treat all lines as file rows.
        file_lines = [ln for ln in lines if ln.strip()]

    # Step 1: file-cap truncation.
    truncated_more_line = ""
    total_files = len(file_lines)
    if total_files > file_cap:
        # Pair each original index with its churn for a stable sort by churn desc.
        # Filter by index (not value) so byte-identical rows can't sneak past the
        # cap — `git diff --stat` rows are normally unique per path, but a future
        # format change or contrived test could produce collisions.
        scored = [(idx, _churn_for_stat_line(ln)) for idx, ln in enumerate(file_lines)]
        # Stable sort: primary key churn desc, secondary original index asc.
        scored.sort(key=lambda t: (-t[1], t[0]))
        kept_indices = {idx for idx, _ in scored[:file_cap]}
        # Restore original document order for the kept lines.
        file_lines = [ln for idx, ln in enumerate(file_lines) if idx in kept_indices]
        truncated_more_line = f"... and {total_files - file_cap} more files"

    parts: list[str] = list(file_lines)
    if truncated_more_line:
        parts.append(truncated_more_line)
    if summary_line:
        parts.append(summary_line)

    assembled = "\n".join(parts)

    # Step 2: char-budget hard cap.  Always preserve the summary line.
    if len(assembled) <= char_budget:
        return assembled

    # Reserve room for "\n<summary>\n<marker>" so the footer is not lost.
    footer_block = ""
    if summary_line:
        footer_block = "\n" + summary_line
    marker_block = "\n" + _TRUNCATED_MARKER
    overhead = len(footer_block) + len(marker_block)

    # Tight-budget fallback: when the footer+marker alone busts the budget,
    # there's no room for body content.  Return whatever subset fits,
    # preferring summary > marker, and HARD-CAP at char_budget so the
    # contract is never violated.
    if overhead >= char_budget:
        fallback_parts = [p for p in (summary_line, _TRUNCATED_MARKER) if p]
        return "\n".join(fallback_parts)[:char_budget]

    # Body gets whatever's left of the budget.  Without a summary, we still
    # reserve room for the marker.
    body_budget = max(0, char_budget - overhead)
    body_source_parts = list(file_lines)
    if truncated_more_line:
        body_source_parts.append(truncated_more_line)
    body_source = "\n".join(body_source_parts)
    body = body_source[:body_budget].rstrip("\n")
    return body + footer_block + marker_block


def compute_diff_summary(
    pre_sha: str | None,
    workspace_root: Path,
    *,
    file_cap: int | None = None,
    char_budget: int | None = None,
) -> str:
    """Compute a truncated ``git diff --stat`` between ``pre_sha`` and a fresh post snapshot.

    Returns the empty string on any of the following:

    * ``pre_sha`` is ``None`` (pre-snapshot capture failed / no repo).
    * ``OUROBOROS_DIFF_CAPTURE_ENABLED`` is ``"false"``, ``"0"``, or empty.
    * The post snapshot fails (any reason).
    * Pre and post SHAs are identical (no-op AC).
    * ``git diff`` itself fails (any reason).

    Args:
        pre_sha: SHA returned by :func:`capture_pre_ac_snapshot`.
        workspace_root: Directory where the AC ran.
        file_cap: Keep at most this many file rows by churn.  ``None`` (the
            default) defers to ``OUROBOROS_DIFF_SUMMARY_FILE_CAP`` and then to
            :data:`_DEFAULT_FILE_CAP`.  Any explicit int (including a value
            equal to the default constant) is honored as a hard override.
        char_budget: Hard cap on the returned string length.  ``None`` (the
            default) defers to ``OUROBOROS_DIFF_SUMMARY_CHAR_BUDGET`` and then
            to :data:`_DEFAULT_CHAR_BUDGET`.  Any explicit int wins.

    Returns:
        Truncated ``--stat`` text, or ``""`` on any failure / disabled / no-op.
    """
    if not _diff_capture_enabled():
        return ""
    if pre_sha is None:
        return ""

    post_sha = _capture_post_snapshot(workspace_root)
    if not post_sha:
        return ""

    if post_sha == pre_sha:
        log.debug(
            "serial_executor.diff_capture.no_op",
            workspace_root=str(workspace_root),
        )
        return ""

    try:
        completed = subprocess.run(
            ["git", "diff", "--stat", pre_sha, post_sha],
            cwd=str(workspace_root),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        log.warning(
            "serial_executor.diff_capture.failed",
            reason="git_binary_missing",
            phase="diff",
        )
        return ""
    except subprocess.TimeoutExpired:
        log.warning(
            "serial_executor.diff_capture.failed",
            reason="timeout",
            phase="diff",
        )
        return ""
    except OSError as exc:  # pragma: no cover — defensive belt
        log.warning(
            "serial_executor.diff_capture.failed",
            reason="os_error",
            phase="diff",
            error=str(exc),
        )
        return ""

    if completed.returncode != 0:
        log.warning(
            "serial_executor.diff_capture.failed",
            reason=f"diff_exit_{completed.returncode}",
            phase="diff",
            stderr=(completed.stderr or "")[:200],
        )
        return ""

    raw = completed.stdout or ""
    effective_file_cap = _resolve_file_cap(file_cap)
    effective_char_budget = _resolve_char_budget(char_budget)
    return _truncate_stat(raw, file_cap=effective_file_cap, char_budget=effective_char_budget)


__all__ = ["capture_pre_ac_snapshot", "compute_diff_summary"]
