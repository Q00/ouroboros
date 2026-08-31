"""Harness-observed workspace changes as unforgeable ``files_touched`` support.

The transcript verifier proves ``files_touched`` claims from the runtime
transcript: structured ``Edit``/``Write`` path values, or shell mutation targets
the Bash lease tracker could authenticate. Every runtime encodes tool activity a
little differently, so real work keeps landing outside those shapes — a file
written through a heredoc, a scaffolding command, ``git apply``, a Python
one-liner — and the leaf is rejected as ``FABRICATION_SUSPECTED`` for a file that
demonstrably changed on disk.

This module adds the harness's own observation as a support source. The
dispatcher fingerprints the workspace before the leaf starts and after its
stream ends; a claimed path whose fingerprint changed (or that appeared) during
that window is backed by the harness, not by the leaf's narration. The
observation rides inside the transcript as an :class:`AgentMessage` whose data
carries a :class:`WorkspaceObservation` *instance*. Runtime adapters build
messages from JSON, so a leaf can never manufacture that object — the type check
in :func:`observation_from_message` is the forgery boundary.

Only positive support is granted: a path that did not change stays unsupported,
so a stale file in the workspace still cannot prove that this run touched it,
and a truncated snapshot (workspace over budget) can only withhold support.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path, PurePosixPath
import stat
import time

from ouroboros.orchestrator.adapter import AgentMessage

HARNESS_OBSERVATION_MESSAGE_TYPE = "harness_observation"
HARNESS_OBSERVATION_DATA_KEY = "_harness_workspace_observation"

# Directories that are never part of a leaf's deliverable and can be huge.
_IGNORED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        ".ouroboros",
        "target",
        "dist",
        "build",
        ".next",
        ".turbo",
    }
)
# Snapshot budget. Past either bound the observation is marked truncated and
# proves nothing about paths that were not fingerprinted.
DEFAULT_MAX_ENTRIES = 50_000
DEFAULT_TIME_BUDGET_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshot:
    """Fingerprint of every regular file under a workspace root."""

    root: str
    fingerprints: dict[str, tuple[int, int]] = field(default_factory=dict)
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class WorkspaceObservation:
    """Workspace-relative paths the harness saw change during one leaf run."""

    changed_paths: frozenset[str]
    truncated: bool = False

    def supports_file_claim(self, claim: str) -> bool:
        """Return True when the claimed workspace-relative path changed."""
        normalized = _normalize_relative_path(claim)
        return normalized is not None and normalized in self.changed_paths


def _normalize_relative_path(value: str) -> str | None:
    """Normalize a workspace-relative claim to a comparable POSIX form."""
    stripped = value.strip().replace("\\", "/")
    if not stripped:
        return None
    candidate = PurePosixPath(stripped)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    parts = [part for part in candidate.parts if part not in ("", ".")]
    if not parts:
        return None
    return "/".join(parts).lower()


def snapshot_workspace(
    task_cwd: str | os.PathLike[str] | None,
    *,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    time_budget_seconds: float = DEFAULT_TIME_BUDGET_SECONDS,
) -> WorkspaceSnapshot | None:
    """Fingerprint the workspace; ``None`` when there is no usable root."""
    if task_cwd is None:
        return None
    try:
        root = Path(task_cwd).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if not root.is_dir():
        return None

    fingerprints: dict[str, tuple[int, int]] = {}
    truncated = False
    deadline = time.monotonic() + max(time_budget_seconds, 0.0)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if name not in _IGNORED_DIRECTORY_NAMES)
        if time.monotonic() > deadline:
            truncated = True
            break
        for filename in filenames:
            if len(fingerprints) >= max_entries or time.monotonic() > deadline:
                truncated = True
                break
            full = os.path.join(dirpath, filename)
            try:
                stat_result = os.lstat(full)
            except OSError:
                continue
            if not stat.S_ISREG(stat_result.st_mode):
                # Symlinks and special files are not deliverables the leaf wrote.
                continue
            relative = os.path.relpath(full, root).replace(os.sep, "/")
            fingerprints[relative.lower()] = (stat_result.st_size, stat_result.st_mtime_ns)
        if truncated:
            break
    return WorkspaceSnapshot(root=str(root), fingerprints=fingerprints, truncated=truncated)


def diff_workspace_snapshots(
    before: WorkspaceSnapshot | None,
    after: WorkspaceSnapshot | None,
) -> WorkspaceObservation | None:
    """Return the paths that appeared or changed between two snapshots."""
    if before is None or after is None or before.root != after.root:
        return None
    changed: set[str] = set()
    for path, fingerprint in after.fingerprints.items():
        prior = before.fingerprints.get(path)
        if prior == fingerprint:
            continue
        if prior is None and before.truncated:
            # A truncated pre-snapshot may simply have run out of budget before
            # reaching this path; its absence there is uncertainty, not proof
            # that the leaf created it during this window.
            continue
        changed.add(path)
    return WorkspaceObservation(
        changed_paths=frozenset(changed),
        truncated=before.truncated or after.truncated,
    )


def build_observation_message(observation: WorkspaceObservation) -> AgentMessage:
    """Wrap an observation as a transcript message the verifier can read."""
    count = len(observation.changed_paths)
    suffix = " (snapshot truncated)" if observation.truncated else ""
    return AgentMessage(
        type=HARNESS_OBSERVATION_MESSAGE_TYPE,
        content=f"Harness observed {count} changed workspace file(s){suffix}",
        data={HARNESS_OBSERVATION_DATA_KEY: observation},
    )


def observation_from_message(message: AgentMessage) -> WorkspaceObservation | None:
    """Return the harness observation carried by *message*, if genuine.

    The instance check is the forgery boundary: runtime adapters deserialize
    JSON into plain dicts and lists, so only harness code can place a
    :class:`WorkspaceObservation` here.
    """
    if message.type != HARNESS_OBSERVATION_MESSAGE_TYPE:
        return None
    candidate = message.data.get(HARNESS_OBSERVATION_DATA_KEY)
    if isinstance(candidate, WorkspaceObservation):
        return candidate
    return None


def is_harness_observation_message(message: AgentMessage) -> bool:
    """Return True for a genuine harness observation message."""
    return observation_from_message(message) is not None


def insert_observation_message(
    messages: list[AgentMessage], observation: WorkspaceObservation
) -> None:
    """Append the observation to the transcript.

    Always an append: repositioning existing entries would shift the indices
    that mid-stream bookkeeping (e.g. session-signal delivery slices) captured
    earlier. The verifier excludes the leaf's terminal self-report by identity
    (the last final message), not by position, so an observation appended after
    the final result is still counted as support.
    """
    messages.append(build_observation_message(observation))


__all__ = [
    "HARNESS_OBSERVATION_DATA_KEY",
    "HARNESS_OBSERVATION_MESSAGE_TYPE",
    "WorkspaceObservation",
    "WorkspaceSnapshot",
    "build_observation_message",
    "diff_workspace_snapshots",
    "insert_observation_message",
    "is_harness_observation_message",
    "observation_from_message",
    "snapshot_workspace",
]
