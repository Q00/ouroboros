"""Deterministic project identity for cross-run read projections.

The identity in this module is attribution metadata only.  It cannot authorize
execution, mutate an EventStore, or declare acceptance.  Project Map consumers
use it to join immutable session-start events that belong to one source
repository while retaining a repository-relative workspace filter.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import subprocess
import tempfile
from uuid import NAMESPACE_URL, uuid5

PROJECT_ID_PREFIX = "project_"
_MAX_PATH_LENGTH = 4096
_MAX_GIT_OUTPUT_LENGTH = 65_536
_GIT_TIMEOUT_SECONDS = 5.0
_GIT_NEUTRAL_HOME = str(Path(Path(__file__).anchor) / ".ouroboros-git-neutral-home")


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
    """Return the nearest checkout marker without interpreting Git metadata."""
    for candidate in (start, *start.parents):
        marker = candidate / ".git"
        try:
            # Even a malformed child marker is a discovery boundary.  If Git
            # rejects it, attribution remains scoped to this child instead of
            # silently inheriting an ancestor repository.
            marker.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            return candidate
        return candidate
    return None


def _git_environment() -> dict[str, str]:
    """Return a stable environment without caller-supplied Git overrides."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "HOME": _GIT_NEUTRAL_HOME,
            "LC_ALL": "C",
        }
    )
    return environment


def _run_git(start: Path, *arguments: str) -> bytes | None:
    """Run one bounded, non-interactive Git query and return complete stdout."""
    try:
        with tempfile.TemporaryFile() as output:
            completed = subprocess.run(
                ["git", "-C", str(start), *arguments],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.DEVNULL,
                timeout=_GIT_TIMEOUT_SECONDS,
                env=_git_environment(),
                shell=False,
            )
            if completed.returncode != 0 or output.tell() > _MAX_GIT_OUTPUT_LENGTH:
                return None
            output.seek(0)
            return output.read()
    except (OSError, subprocess.SubprocessError):
        return None


def _git_path(output: bytes | None) -> Path | None:
    """Decode one newline-terminated Git path without trimming valid spaces."""
    if output is None or not output.endswith(b"\n"):
        return None
    record = output[:-1]
    if not record or b"\n" in record or b"\r" in record or b"\x00" in record:
        return None
    try:
        return _canonical_directory(record.decode("utf-8", errors="strict"))
    except (UnicodeError, ProjectIdentityError):
        return None


def _git_dir_argument(checkout_root: Path | None) -> tuple[str, ...]:
    if checkout_root is None:
        return ()
    return (f"--git-dir={checkout_root / '.git'}",)


def _git_head_is_valid(git_dir: Path) -> bool:
    """Ask Git to validate either a symbolic/unborn or detached HEAD."""
    git_dir_argument = f"--git-dir={git_dir}"
    symbolic_head = _run_git(
        git_dir,
        git_dir_argument,
        "symbolic-ref",
        "--quiet",
        "HEAD",
    )
    if symbolic_head is not None:
        return True
    detached_head = _run_git(
        git_dir,
        git_dir_argument,
        "rev-parse",
        "--verify",
        "--quiet",
        "HEAD^{object}",
    )
    return detached_head is not None


def _git_project_root(start: Path, checkout_root: Path | None = None) -> Path | None:
    """Ask Git for the primary worktree (or bare common directory)."""
    git_dir_argument = _git_dir_argument(checkout_root)
    common_output = _run_git(
        start,
        *git_dir_argument,
        "rev-parse",
        "--path-format=absolute",
        "--git-common-dir",
    )
    common_dir = _git_path(common_output)
    if common_dir is None:
        return None
    common_bare = _run_git(
        common_dir,
        f"--git-dir={common_dir}",
        "rev-parse",
        "--is-bare-repository",
    )
    if common_bare not in {b"true\n", b"false\n"}:
        return None

    output = _run_git(
        start,
        *git_dir_argument,
        "worktree",
        "list",
        "--porcelain",
        "-z",
    )
    if output is None:
        return None
    prefix = b"worktree "
    try:
        worktrees = tuple(
            _canonical_directory(record.removeprefix(prefix).decode("utf-8", errors="strict"))
            for record in output.split(b"\x00")
            if record.startswith(prefix)
        )
    except (UnicodeError, ProjectIdentityError):
        return None
    if not worktrees:
        return None
    if common_bare == b"true\n":
        active_is_common = start == common_dir or common_dir in start.parents
        owned = checkout_root is None or checkout_root in worktrees or active_is_common
        return common_dir if owned and _git_head_is_valid(common_dir) else None

    main_worktree = worktrees[0]
    # ``worktree list`` identifies the primary checkout, while rev-parse asks
    # Git's own config parser to apply an explicit ``core.worktree`` owner.
    # It fails for a bare primary, where the worktree-list path is the desired
    # stable common-directory identity.
    top_level = _run_git(
        main_worktree,
        f"--git-dir={common_dir}",
        "rev-parse",
        "--path-format=absolute",
        "--show-toplevel",
    )
    project_root = main_worktree if top_level is None else _git_path(top_level)
    if project_root is None:
        return None
    # Git accepting an explicit --git-dir does not prove that the active
    # checkout owns it.  Membership must come from Git's worktree population or
    # from Git's own configured top-level decision (an explicit core.worktree).
    if checkout_root is not None and checkout_root not in (*worktrees, project_root):
        return None
    return project_root


def _active_repository_is_bare(start: Path, checkout_root: Path | None) -> bool:
    output = _run_git(
        start,
        *_git_dir_argument(checkout_root),
        "rev-parse",
        "--is-bare-repository",
    )
    return output == b"true\n"


def _project_and_checkout_roots(start: Path) -> tuple[Path, Path]:
    """Resolve Git-owned topology, conservatively falling back to one checkout."""
    checkout_root = _nearest_git_checkout_root(start)
    # A markerless bare repository may live inside an ordinary checkout. Ask
    # Git about the active directory before adopting an ancestor marker.
    if checkout_root != start and _active_repository_is_bare(start, None):
        active_git_dir = _git_path(
            _run_git(start, "rev-parse", "--path-format=absolute", "--absolute-git-dir")
        )
        if active_git_dir is not None and (
            active_git_dir == start or active_git_dir in start.parents
        ):
            bare_root = _git_project_root(start)
            return (bare_root, bare_root) if bare_root is not None else (start, start)
    project_root = _git_project_root(start, checkout_root)
    if project_root is None:
        fallback = checkout_root or start
        return fallback, fallback
    # A bare repository can itself be named ``.git``; in that case filesystem
    # marker discovery sees its parent, while Git correctly identifies the bare
    # directory as both the project and checkout root.
    if _active_repository_is_bare(start, checkout_root):
        return project_root, project_root
    if checkout_root is not None:
        return project_root, checkout_root
    top_level = _run_git(
        start,
        *_git_dir_argument(checkout_root),
        "rev-parse",
        "--path-format=absolute",
        "--show-toplevel",
    )
    if top_level is None:
        return start, start
    decoded_top_level = _git_path(top_level)
    return (project_root, decoded_top_level) if decoded_top_level is not None else (start, start)


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
        workspace = _canonical_directory(
            source_root if source_workspace is None else source_workspace
        )
        workspace_path = _relative_workspace_path(workspace, checkout_root)
        project_root, _ = _project_and_checkout_roots(checkout_root)
        return ProjectIdentity.from_root(project_root, workspace_path=workspace_path)

    source, checkout_root = _project_and_checkout_roots(effective)
    workspace_path = _relative_workspace_path(effective, checkout_root)
    return ProjectIdentity.from_root(source, workspace_path=workspace_path)


__all__ = [
    "PROJECT_ID_PREFIX",
    "ProjectIdentity",
    "ProjectIdentityError",
    "project_id_for_root",
    "resolve_project_identity",
]
