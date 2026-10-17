"""Workspace evidence recorded with every judged AC attempt.

``execution.ac.attempt_judged`` is emitted once per outer worker attempt,
retries included. Without the workspace state at that moment, nobody can tell
afterwards whether a later attempt changed or damaged work an earlier one had
right. This module adds the diff identity and the verify-gate verdict to that
event: the ``git diff --stat`` against ``HEAD``, the untracked files, and one
SHA-256 over the full diff plus the untracked files' contents. It only reads
the repository (no index or object writes) and adds nothing outside a Git
work tree.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import hashlib
import os
from pathlib import Path
import subprocess
from typing import Any

from ouroboros.orchestrator.verify_gate_outcome import _mapping_has_bounded_keys

#: Keys this module may add to an ``execution.ac.attempt_judged`` payload.
ATTEMPT_EVIDENCE_KEYS = frozenset(
    {"verify_passed", "verify_cause", "verify_no_signal", "workspace_diff"}
)

_GIT_TIMEOUT_SECONDS = 10
_DIFF_STAT_CHARS = 4000
_UNTRACKED_LISTED = 50


async def attempt_workspace_evidence(executor: Any, result: Any) -> dict[str, object]:
    """Return event fields describing the workspace and verdict after ``result``."""
    evidence: dict[str, object] = {}
    outcome = getattr(result, "verify_gate_outcome", None)
    if outcome is not None:
        evidence["verify_passed"] = outcome.passed
        evidence["verify_cause"] = outcome.cause
        evidence["verify_no_signal"] = outcome.no_signal
    cwd = (
        getattr(executor, "_task_cwd", None)
        or getattr(getattr(executor, "_adapter", None), "working_directory", None)
        or os.getcwd()
    )
    diff = await asyncio.to_thread(workspace_diff_evidence, Path(cwd))
    if diff:
        evidence["workspace_diff"] = diff
    return evidence


def judgment_envelope_ok(data: Mapping[str, Any], required: frozenset[str]) -> bool:
    """Whether a judged-attempt payload has ``required`` keys plus only evidence keys."""
    return _mapping_has_bounded_keys(data, required=required, optional=ATTEMPT_EVIDENCE_KEYS)


def workspace_diff_evidence(cwd: Path) -> dict[str, object] | None:
    """Diff stat, untracked files and a content hash of ``cwd`` against ``HEAD``."""
    head = _git(cwd, "rev-parse", "HEAD")
    if head is None:
        return None
    stat = _git(cwd, "diff", "HEAD", "--stat", "--find-renames")
    diff = _git_bytes(cwd, "diff", "HEAD", "--binary", "--find-renames")
    untracked_text = _git(cwd, "ls-files", "--others", "--exclude-standard", "-z")
    if stat is None or diff is None or untracked_text is None:
        return None
    untracked = [path for path in untracked_text.split("\0") if path]
    digest = hashlib.sha256(diff)
    for path in untracked:
        digest.update(b"\0untracked\0" + path.encode("utf-8", "surrogateescape") + b"\0")
        try:
            digest.update((cwd / path).read_bytes())
        except OSError:
            digest.update(b"\0unreadable\0")
    return {
        "head": head.strip(),
        "diff_stat": stat.strip()[:_DIFF_STAT_CHARS],
        "diff_bytes": len(diff),
        "untracked_count": len(untracked),
        "untracked": untracked[:_UNTRACKED_LISTED],
        "sha256": digest.hexdigest(),
    }


def _git(cwd: Path, *args: str) -> str | None:
    output = _git_bytes(cwd, *args)
    return None if output is None else output.decode("utf-8", "replace")


def _git_bytes(cwd: Path, *args: str) -> bytes | None:
    try:
        completed = subprocess.run(  # noqa: S603 - fixed git argv, no shell
            ("git", "-C", str(cwd), *args),
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout if completed.returncode == 0 else None


__all__ = [
    "ATTEMPT_EVIDENCE_KEYS",
    "attempt_workspace_evidence",
    "judgment_envelope_ok",
    "workspace_diff_evidence",
]
