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
import stat
import subprocess
import tempfile
from uuid import NAMESPACE_URL, uuid5

PROJECT_ID_PREFIX = "project_"
_MAX_PATH_LENGTH = 4096
_MAX_GIT_OUTPUT_LENGTH = 1_048_576
_GIT_TIMEOUT_SECONDS = 5.0
_GIT_NEUTRAL_HOME = str(Path(Path(__file__).anchor) / ".ouroboros-git-neutral-home")


class ProjectIdentityError(ValueError):
    """Raised when a project/workspace identity cannot be represented safely."""


class ProjectIdentityUnavailableError(ProjectIdentityError):
    """Raised when the installed Git boundary cannot answer deterministically."""


class ManagedProjectScopeError(ProjectIdentityError):
    """Raised when source and execution workspaces select different relative scopes."""

    def __init__(self, source_workspace: str, execution_workspace: str) -> None:
        super().__init__("managed source and execution workspace scopes do not match")
        self.source_workspace = source_workspace
        self.execution_workspace = execution_workspace


class ManagedProjectOwnershipError(ProjectIdentityError):
    """Raised when a generated checkout does not belong to its durable source."""

    def __init__(
        self,
        source_identity: ProjectIdentity,
        execution_identity: ProjectIdentity,
    ) -> None:
        super().__init__("managed worktree does not belong to its source project")
        self.source_identity = source_identity
        self.execution_identity = execution_identity


def _canonical_directory(value: str | Path, *, require_exists: bool = False) -> Path:
    if not isinstance(value, (str, Path)):
        raise ProjectIdentityError("project identity requires a non-empty path")
    raw_value = str(value)
    if not raw_value.strip() or len(raw_value) > _MAX_PATH_LENGTH or "\x00" in raw_value:
        raise ProjectIdentityError("project identity path exceeds its bound")
    try:
        resolved = Path(value).expanduser().resolve(strict=False)
        try:
            mode = resolved.stat().st_mode
        except FileNotFoundError:
            if require_exists:
                raise ProjectIdentityError("project identity path must be a directory") from None
        except OSError as exc:
            raise ProjectIdentityUnavailableError(
                "project identity filesystem is temporarily unavailable"
            ) from exc
        else:
            if not stat.S_ISDIR(mode):
                raise ProjectIdentityError("project identity path must be a directory")
    except ProjectIdentityError:
        raise
    except OSError as exc:
        raise ProjectIdentityUnavailableError(
            "project identity filesystem is temporarily unavailable"
        ) from exc
    except (RuntimeError, ValueError) as exc:
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
        require_exists: bool = False,
    ) -> ProjectIdentity:
        """Construct a validated identity from an explicit source root."""
        canonical_root = str(_canonical_directory(project_root, require_exists=require_exists))
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


def _nearest_repository_boundary(start: Path) -> tuple[Path | None, Path | None]:
    """Return the nearest checkout marker or bare shape, without parsing either."""
    for candidate in (start, *start.parents):
        marker = candidate / ".git"
        try:
            # Even a malformed child marker is a discovery boundary.  If Git
            # rejects it, attribution remains scoped to this child instead of
            # silently inheriting an ancestor repository.
            marker.lstat()
        except FileNotFoundError:
            pass
        except OSError:
            return candidate, None
        else:
            return candidate, None
        try:
            (candidate / "HEAD").stat()
            (candidate / "objects").stat()
            (candidate / "refs").stat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ProjectIdentityUnavailableError(
                "bare repository discovery is temporarily unavailable"
            ) from exc
        return None, candidate
    return None, None


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


def _run_git(start: Path, *arguments: str) -> bytes:
    """Run one bounded, non-interactive Git query and return complete stdout.

    A nonzero exit is unavailable because Git does not expose a portable
    exit-code distinction between malformed topology and transient repository
    I/O.
    """
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
            if completed.returncode != 0:
                raise ProjectIdentityUnavailableError(
                    "Git query failed before topology could be proven"
                )
            if output.tell() > _MAX_GIT_OUTPUT_LENGTH:
                raise ProjectIdentityUnavailableError("Git query output exceeds its bound")
            output.seek(0)
            return output.read()
    except ProjectIdentityUnavailableError:
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProjectIdentityUnavailableError("Git query is temporarily unavailable") from exc


def _git_path(output: bytes) -> Path:
    """Decode one Git-owned path, removing only its final record terminator."""
    if not output.endswith(b"\n"):
        raise ProjectIdentityUnavailableError("Git path output is not representable")
    record = output[:-1]
    if not record or b"\x00" in record:
        raise ProjectIdentityUnavailableError("Git path output is not representable")
    try:
        return _canonical_directory(record.decode("utf-8", errors="strict"), require_exists=True)
    except (UnicodeError, ProjectIdentityError) as exc:
        raise ProjectIdentityUnavailableError("Git path output is not representable") from exc


def _git_dir_argument(checkout_root: Path | None) -> tuple[str, ...]:
    if checkout_root is None:
        return ()
    return (f"--git-dir={checkout_root / '.git'}",)


def _git_head_is_valid(git_dir: Path) -> bool:
    """Ask Git to validate either a symbolic/unborn or detached HEAD."""
    git_dir_argument = f"--git-dir={git_dir}"
    try:
        _run_git(
            git_dir,
            git_dir_argument,
            "symbolic-ref",
            "--quiet",
            "HEAD",
        )
        return True
    except ProjectIdentityUnavailableError:
        # Detached HEAD is the one expected non-symbolic shape. Its independent
        # object proof must succeed; a second nonzero remains unavailable.
        _run_git(
            git_dir,
            git_dir_argument,
            "rev-parse",
            "--verify",
            "--quiet",
            "HEAD^{object}",
        )
        return True


def _git_project_root(
    start: Path,
    checkout_root: Path | None = None,
    *,
    git_dir: Path | None = None,
) -> Path | None:
    """Ask Git for the primary worktree (or bare common directory)."""
    git_dir_argument = (
        (f"--git-dir={git_dir}",) if git_dir is not None else _git_dir_argument(checkout_root)
    )
    common_output = _run_git(
        start,
        *git_dir_argument,
        "rev-parse",
        "--path-format=absolute",
        "--git-common-dir",
    )
    common_dir = _git_path(common_output)
    common_bare = _run_git(
        common_dir,
        f"--git-dir={common_dir}",
        "rev-parse",
        "--is-bare-repository",
    )
    if common_bare not in {b"true\n", b"false\n"}:
        raise ProjectIdentityUnavailableError("Git bare-state output is not representable")

    output = _run_git(
        start,
        *git_dir_argument,
        "worktree",
        "list",
        "--porcelain",
        "-z",
    )
    prefix = b"worktree "
    try:
        worktrees = tuple(
            _canonical_directory(record.removeprefix(prefix).decode("utf-8", errors="strict"))
            for record in output.split(b"\x00")
            if record.startswith(prefix)
        )
    except (UnicodeError, ProjectIdentityError) as exc:
        raise ProjectIdentityUnavailableError("Git worktree output is not representable") from exc
    if not worktrees or not worktrees[0].is_dir():
        raise ProjectIdentityUnavailableError("Git worktree output is not representable")
    if common_bare == b"true\n":
        # A marker proves only registered membership; markerless discovery
        # proves only exact common-directory ownership. Ancestry is not evidence.
        owned = (
            checkout_root in worktrees
            if checkout_root is not None
            else git_dir is not None and start == git_dir == common_dir
        )
        return common_dir if owned and _git_head_is_valid(common_dir) else None

    main_worktree = worktrees[0]
    # ``worktree list`` identifies the primary checkout, while rev-parse asks
    # Git's own config parser to apply an explicit ``core.worktree`` owner.
    top_level = _run_git(
        main_worktree,
        f"--git-dir={common_dir}",
        "rev-parse",
        "--path-format=absolute",
        "--show-toplevel",
    )
    project_root = _git_path(top_level)
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
    if output not in {b"true\n", b"false\n"}:
        raise ProjectIdentityUnavailableError("Git bare-state output is not representable")
    return output == b"true\n"


def _project_and_checkout_roots(start: Path) -> tuple[Path, Path]:
    """Resolve Git-owned topology, conservatively falling back to one checkout."""
    checkout_root, bare_candidate = _nearest_repository_boundary(start)
    if checkout_root is None and bare_candidate is None:
        return start, start
    # Bind markerless bare validation to the exact candidate.  Git discovery
    # from ``start`` may otherwise skip malformed child metadata and inherit an
    # enclosing checkout.
    if bare_candidate is not None:
        bare_root = _git_project_root(bare_candidate, git_dir=bare_candidate)
        if bare_root is None:
            raise ProjectIdentityUnavailableError("Git rejected bare repository topology")
        return bare_root, bare_root
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
    decoded_top_level = _git_path(top_level)
    return project_root, decoded_top_level


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
    effective = _canonical_directory(effective_cwd, require_exists=True)
    _run_git(Path(Path(__file__).anchor), "--version")
    if source_root is not None:
        checkout_root = _canonical_directory(source_root, require_exists=True)
        workspace = _canonical_directory(
            source_root if source_workspace is None else source_workspace,
            require_exists=True,
        )
        workspace_path = _relative_workspace_path(workspace, checkout_root)
        project_root, _ = _project_and_checkout_roots(checkout_root)
        return ProjectIdentity.from_root(project_root, workspace_path=workspace_path)

    source, checkout_root = _project_and_checkout_roots(effective)
    workspace_path = _relative_workspace_path(effective, checkout_root)
    return ProjectIdentity.from_root(source, workspace_path=workspace_path)


def resolve_managed_project_identity(
    execution_workspace: str | Path,
    *,
    source_root: str | Path,
    source_workspace: str | Path,
    worktree_root: str | Path,
) -> ProjectIdentity:
    """Revalidate one managed source/execution pair as a single identity."""
    canonical_source_root = _canonical_directory(source_root, require_exists=True)
    canonical_source_workspace = _canonical_directory(source_workspace, require_exists=True)
    canonical_worktree_root = _canonical_directory(worktree_root, require_exists=True)
    canonical_execution_workspace = _canonical_directory(
        execution_workspace,
        require_exists=True,
    )
    try:
        source_scope = _relative_workspace_path(
            canonical_source_workspace,
            canonical_source_root,
        )
        execution_scope = _relative_workspace_path(
            canonical_execution_workspace,
            canonical_worktree_root,
        )
    except ProjectIdentityError as exc:
        raise ManagedProjectScopeError(
            str(canonical_source_workspace),
            str(canonical_execution_workspace),
        ) from exc
    if source_scope != execution_scope:
        raise ManagedProjectScopeError(
            str(canonical_source_workspace),
            str(canonical_execution_workspace),
        )

    source_identity = resolve_project_identity(
        canonical_execution_workspace,
        source_root=canonical_source_root,
        source_workspace=canonical_source_workspace,
    )
    execution_identity = resolve_project_identity(canonical_execution_workspace)
    if execution_identity != source_identity:
        raise ManagedProjectOwnershipError(source_identity, execution_identity)
    return source_identity


__all__ = [
    "PROJECT_ID_PREFIX",
    "ManagedProjectOwnershipError",
    "ManagedProjectScopeError",
    "ProjectIdentity",
    "ProjectIdentityError",
    "ProjectIdentityUnavailableError",
    "project_id_for_root",
    "resolve_managed_project_identity",
    "resolve_project_identity",
]
