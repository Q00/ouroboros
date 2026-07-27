"""Deterministic project identity for cross-run read projections.

The identity in this module is attribution metadata only.  It cannot authorize
execution, mutate an EventStore, or declare acceptance.  Project Map consumers
use it to join immutable session-start events that belong to one source
repository while retaining a repository-relative workspace filter.
"""

from __future__ import annotations

from configparser import Error as ConfigError
from configparser import RawConfigParser
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from uuid import NAMESPACE_URL, uuid5

PROJECT_ID_PREFIX = "project_"
_MAX_PATH_LENGTH = 4096
_MAX_GIT_POINTER_LENGTH = 4096
_MAX_GIT_CONFIG_LENGTH = 65_536


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


def _read_bounded_utf8(path: Path, *, max_bytes: int) -> str | None:
    """Return one complete, bounded UTF-8 file or ``None`` when untrusted."""
    try:
        with path.open("rb") as stream:
            raw_value = stream.read(max_bytes + 1)
    except OSError:
        return None
    if len(raw_value) > max_bytes or b"\x00" in raw_value:
        return None
    try:
        return raw_value.decode("utf-8", errors="strict")
    except UnicodeError:
        return None


def _read_bounded_record(path: Path) -> str | None:
    """Read one complete Git pointer record with an optional final newline."""
    value = _read_bounded_utf8(path, max_bytes=_MAX_GIT_POINTER_LENGTH)
    if value is None:
        return None
    if value.endswith("\r\n"):
        value = value[:-2]
    elif value.endswith(("\r", "\n")):
        value = value[:-1]
    if not value or "\r" in value or "\n" in value or value != value.strip():
        return None
    return value


def _git_pointer_target(checkout_root: Path) -> Path | None:
    """Return the bounded gitdir target named by a checkout's gitfile."""
    marker = checkout_root / ".git"
    if not marker.is_file():
        return None
    pointer = _read_bounded_record(marker)
    if pointer is None or not pointer.startswith("gitdir: "):
        return None
    raw_git_dir = pointer.removeprefix("gitdir: ").strip()
    if not raw_git_dir:
        return None
    try:
        git_dir = Path(raw_git_dir).expanduser()
        if not git_dir.is_absolute():
            git_dir = checkout_root / git_dir
        git_dir = git_dir.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None
    return git_dir if git_dir.is_dir() else None


@dataclass(frozen=True, slots=True)
class _GitCoreConfig:
    bare: bool
    worktree: Path | None


def _read_git_core_config(git_dir: Path) -> _GitCoreConfig | None:
    """Read only the bounded core fields needed to prove a common-dir root."""
    raw_config = _read_bounded_utf8(
        git_dir / "config",
        max_bytes=_MAX_GIT_CONFIG_LENGTH,
    )
    if raw_config is None:
        return None
    parser = RawConfigParser(interpolation=None, strict=False, allow_no_value=True)
    try:
        parser.read_string(raw_config)
        core_section = next(
            (section for section in parser.sections() if section.casefold() == "core"),
            None,
        )
        if core_section is None:
            return None
        bare = parser.getboolean(core_section, "bare", fallback=None)
        if bare is None:
            return None
        raw_worktree = parser.get(core_section, "worktree", fallback=None)
    except (ConfigError, ValueError):
        return None
    if raw_worktree is None:
        return _GitCoreConfig(bare=bare, worktree=None)
    if not isinstance(raw_worktree, str) or not raw_worktree.strip():
        return None
    try:
        worktree = Path(raw_worktree.strip()).expanduser()
        if not worktree.is_absolute():
            worktree = git_dir / worktree
        worktree = _canonical_directory(worktree)
    except ProjectIdentityError:
        return None
    return _GitCoreConfig(bare=bare, worktree=worktree)


def _common_git_source_root(common_dir: Path) -> Path | None:
    """Return one source identity root positively owned by a common gitdir."""
    core = _read_git_core_config(common_dir)
    if core is not None and core.bare:
        if core.worktree is not None:
            return None
        if (
            _read_bounded_record(common_dir / "HEAD")
            and (common_dir / "objects").is_dir()
            and (common_dir / "refs").is_dir()
        ):
            return common_dir
        return None

    if common_dir.name == ".git":
        normal_checkout = common_dir.parent
        try:
            if (normal_checkout / ".git").resolve(strict=False) == common_dir:
                return normal_checkout.resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            return None

    if core is None:
        return None
    if core.worktree is not None:
        return core.worktree if _git_pointer_target(core.worktree) == common_dir else None

    # ``git init --separate-git-dir`` does not persist the primary worktree
    # path.  Its non-bare common directory is therefore the only stable root
    # that both the primary gitfile and every linked worktree can prove.
    if (
        _read_bounded_record(common_dir / "HEAD")
        and (common_dir / "objects").is_dir()
        and (common_dir / "refs").is_dir()
    ):
        return common_dir
    return None


def _linked_worktree_source_root(checkout_root: Path) -> Path:
    """Resolve a gitfile checkout to one common source root when provable.

    A normal repository has a ``.git`` directory and is already its source
    root.  A linked worktree has a ``.git`` pointer to a per-worktree gitdir;
    that directory's bounded ``commondir`` pointer and backlink prove shared
    ownership.  Standard repositories use the primary checkout; submodules
    use their configured worktree; repositories created with an external Git
    directory and bare repositories that own worktrees use the validated common
    directory because no primary worktree exists for peers to recover.
    Malformed metadata degrades conservatively to the active checkout root.
    """
    marker = checkout_root / ".git"
    if not marker.is_file():
        return checkout_root

    git_dir = _git_pointer_target(checkout_root)
    if git_dir is None:
        return checkout_root

    raw_common_dir = _read_bounded_record(git_dir / "commondir")
    if not raw_common_dir:
        return _common_git_source_root(git_dir) or checkout_root
    try:
        common_dir = Path(raw_common_dir).expanduser()
        if not common_dir.is_absolute():
            common_dir = git_dir / common_dir
        common_dir = common_dir.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return checkout_root
    if not common_dir.is_dir():
        return checkout_root
    worktrees_dir = common_dir / "worktrees"
    try:
        git_dir_relative = git_dir.relative_to(worktrees_dir)
    except ValueError:
        return checkout_root
    if len(git_dir_relative.parts) != 1 or not git_dir.is_dir():
        return checkout_root
    raw_checkout_marker = _read_bounded_record(git_dir / "gitdir")
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
    return _common_git_source_root(common_dir) or checkout_root


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
        checkout_root = _canonical_directory(source_root)
        workspace = _canonical_directory(source_workspace or source_root)
        workspace_path = _relative_workspace_path(workspace, checkout_root)
        project_root = _linked_worktree_source_root(checkout_root)
        return ProjectIdentity.from_root(project_root, workspace_path=workspace_path)

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
