"""Deterministic project identity for cross-run read projections.

The identity in this module is attribution metadata only.  It cannot authorize
execution, mutate an EventStore, or declare acceptance.  Project Map consumers
use it to join immutable session-start events that belong to one source
repository while retaining a repository-relative workspace filter.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from uuid import NAMESPACE_URL, uuid5

PROJECT_ID_PREFIX = "project_"
_MAX_PATH_LENGTH = 4096
_MAX_GIT_POINTER_LENGTH = 4096


class ProjectIdentityError(ValueError):
    """Raised when a project/workspace identity cannot be represented safely."""


def _canonical_directory(value: str | Path) -> Path:
    if not isinstance(value, (str, Path)):
        raise ProjectIdentityError("project identity requires a non-empty path")
    raw_value = str(value)
    if not raw_value.strip() or len(raw_value) > _MAX_PATH_LENGTH or "\x00" in raw_value:
        raise ProjectIdentityError("project identity path exceeds its bound")
    try:
        resolved = Path(value).expanduser().resolve(strict=False)
        if resolved.exists() and not resolved.is_dir():
            raise ProjectIdentityError("project identity path must be a directory")
    except ProjectIdentityError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise ProjectIdentityError("project identity path cannot be canonicalized") from exc
    if len(str(resolved)) > _MAX_PATH_LENGTH:
        raise ProjectIdentityError("project identity path exceeds its bound")
    return resolved


def project_id_for_root(project_root: str | Path) -> str:
    """Return ``project_`` plus the full UUIDv5 hex for a canonical root.

    ``NAMESPACE_URL`` and the canonical absolute path string are the complete
    public V1 algorithm.  Repository moves/clones intentionally produce a new
    identity; portable remote-based identity is outside Project Map V1.
    """
    canonical_root = str(_canonical_directory(project_root))
    return f"{PROJECT_ID_PREFIX}{uuid5(NAMESPACE_URL, canonical_root).hex}"


def _normalize_workspace_path(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_PATH_LENGTH:
        raise ProjectIdentityError("workspace_path must be a bounded non-empty string")
    if "\\" in value or "\x00" in value:
        raise ProjectIdentityError("workspace_path must use canonical POSIX segments")
    parsed = PurePosixPath(value)
    normalized = str(parsed)
    if parsed.is_absolute() or ".." in parsed.parts:
        raise ProjectIdentityError("workspace_path must stay relative to the project root")
    if normalized != value or (value != "." and "." in parsed.parts):
        raise ProjectIdentityError("workspace_path must be canonical")
    return value


@dataclass(frozen=True, slots=True)
class ProjectIdentity:
    """Canonical project join key plus repository-relative workspace scope."""

    project_id: str
    project_root: str
    workspace_path: str

    def __post_init__(self) -> None:
        canonical_root = str(_canonical_directory(self.project_root))
        if self.project_root != canonical_root:
            raise ProjectIdentityError("project_root must be canonical")
        if self.project_id != project_id_for_root(canonical_root):
            raise ProjectIdentityError("project_id does not match project_root")
        _normalize_workspace_path(self.workspace_path)

    @classmethod
    def from_root(
        cls,
        project_root: str | Path,
        *,
        workspace_path: str = ".",
    ) -> ProjectIdentity:
        """Construct a validated identity from an explicit source root."""
        canonical_root = str(_canonical_directory(project_root))
        return cls(
            project_id=project_id_for_root(canonical_root),
            project_root=canonical_root,
            workspace_path=_normalize_workspace_path(workspace_path),
        )

    def to_event_data(self) -> dict[str, str]:
        """Return the additive ``orchestrator.session.started`` anchor fields."""
        return {
            "project_id": self.project_id,
            "project_root": self.project_root,
            "workspace_path": self.workspace_path,
        }

    def to_workspace_data(self) -> dict[str, str]:
        """Return the existing nested execution-contract workspace identity."""
        return {
            "project_root": self.project_root,
            "workspace_path": self.workspace_path,
        }


def _nearest_git_checkout_root(start: Path) -> Path | None:
    for candidate in (start, *start.parents):
        marker = candidate / ".git"
        if marker.is_dir() or marker.is_file():
            return candidate
    return None


def _read_bounded_first_line(path: Path) -> str | None:
    try:
        with path.open(encoding="utf-8", errors="strict") as stream:
            value = stream.readline(_MAX_GIT_POINTER_LENGTH + 1)
    except (OSError, UnicodeError):
        return None
    if len(value) > _MAX_GIT_POINTER_LENGTH:
        return None
    return value.strip()


def _linked_worktree_source_root(checkout_root: Path) -> Path:
    """Resolve a linked worktree to its primary source checkout when provable.

    A normal repository has a ``.git`` directory and is already its source
    root.  A linked worktree has a ``.git`` pointer to a per-worktree gitdir;
    that directory's bounded ``commondir`` pointer resolves to the primary
    ``.git`` directory.  Submodules also use ``.git`` files but normally have
    no ``commondir`` file, so they correctly remain their own project root.
    Malformed metadata degrades conservatively to the active checkout root.
    """
    marker = checkout_root / ".git"
    if not marker.is_file():
        return checkout_root

    pointer = _read_bounded_first_line(marker)
    if pointer is None or not pointer.startswith("gitdir: "):
        return checkout_root
    raw_git_dir = pointer.removeprefix("gitdir: ").strip()
    if not raw_git_dir:
        return checkout_root
    try:
        git_dir = Path(raw_git_dir).expanduser()
        if not git_dir.is_absolute():
            git_dir = checkout_root / git_dir
        git_dir = git_dir.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return checkout_root

    raw_common_dir = _read_bounded_first_line(git_dir / "commondir")
    if not raw_common_dir:
        return checkout_root
    try:
        common_dir = Path(raw_common_dir).expanduser()
        if not common_dir.is_absolute():
            common_dir = git_dir / common_dir
        common_dir = common_dir.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return checkout_root
    if common_dir.name != ".git" or not common_dir.is_dir():
        return checkout_root
    worktrees_dir = common_dir / "worktrees"
    try:
        git_dir_relative = git_dir.relative_to(worktrees_dir)
    except ValueError:
        return checkout_root
    if len(git_dir_relative.parts) != 1 or not git_dir.is_dir():
        return checkout_root
    raw_checkout_marker = _read_bounded_first_line(git_dir / "gitdir")
    if not raw_checkout_marker:
        return checkout_root
    try:
        checkout_marker = Path(raw_checkout_marker).expanduser()
        if not checkout_marker.is_absolute():
            checkout_marker = git_dir / checkout_marker
        backlink_matches = checkout_marker.resolve(strict=False) == marker.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return checkout_root
    if not backlink_matches:
        return checkout_root
    return common_dir.parent.resolve(strict=False)


def _relative_workspace_path(workspace: Path, checkout_root: Path) -> str:
    try:
        relative = workspace.relative_to(checkout_root)
    except ValueError as exc:
        raise ProjectIdentityError("workspace path is outside its canonical checkout root") from exc
    return _normalize_workspace_path(relative.as_posix() or ".")


def resolve_project_identity(
    effective_cwd: str | Path,
    *,
    source_root: str | Path | None = None,
    source_workspace: str | Path | None = None,
) -> ProjectIdentity:
    """Resolve one deterministic Project Map V1 identity.

    Direct callers are anchored to the nearest Git checkout and, for linked
    worktrees, the provable common source checkout.  Non-Git directories remain
    valid local-first projects and use their canonical cwd as both project and
    workspace root.

    Managed task worktrees pass ``source_root`` and ``source_workspace`` from
    their durable :class:`TaskWorkspace`; this prevents the generated worktree
    path from splitting one source project into a new project on every run.
    """
    effective = _canonical_directory(effective_cwd)
    if source_root is not None:
        root = _canonical_directory(source_root)
        workspace = _canonical_directory(source_workspace or source_root)
        workspace_path = _relative_workspace_path(workspace, root)
        return ProjectIdentity.from_root(root, workspace_path=workspace_path)

    checkout_root = _nearest_git_checkout_root(effective)
    if checkout_root is None:
        return ProjectIdentity.from_root(effective)
    source = _linked_worktree_source_root(checkout_root)
    workspace_path = _relative_workspace_path(effective, checkout_root)
    return ProjectIdentity.from_root(source, workspace_path=workspace_path)


__all__ = [
    "PROJECT_ID_PREFIX",
    "ProjectIdentity",
    "ProjectIdentityError",
    "project_id_for_root",
    "resolve_project_identity",
]
