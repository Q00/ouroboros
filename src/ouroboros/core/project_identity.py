"""Deterministic project identity for cross-run read projections.

The identity in this module is attribution metadata only.  It cannot authorize
execution, mutate an EventStore, or declare acceptance.  Project Map consumers
use it to join immutable session-start events that belong to one source
repository while retaining a repository-relative workspace filter.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tempfile
from uuid import NAMESPACE_URL, uuid5
import weakref

PROJECT_ID_PREFIX = "project_"
_MAX_PATH_LENGTH = 4096
_MAX_GIT_OUTPUT_LENGTH = 1_048_576
_GIT_TIMEOUT_SECONDS = 5.0
_GIT_NEUTRAL_HOME = str(Path(Path(__file__).anchor) / ".ouroboros-git-neutral-home")
_MINIMUM_GIT_VERSION = (2, 36, 0)
_MINIMUM_GIT_VERSION_TEXT = ".".join(str(part) for part in _MINIMUM_GIT_VERSION)
_GIT_VERSION_PATTERN = re.compile(
    rb"git version ([0-9]{1,9})\.([0-9]{1,9})(?:\.([0-9]{1,9}))?(?:[. ][^\r\n]*)?\n"
)


class ProjectIdentityError(ValueError):
    """Raised when a project/workspace identity cannot be represented safely."""


class ProjectIdentityUnavailableError(ProjectIdentityError):
    """Raised when the installed Git boundary cannot answer deterministically."""


class ProjectIdentityGitVersionError(ProjectIdentityError):
    """Raised when installed Git cannot implement the identity grammar."""


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


@dataclass(frozen=True, slots=True)
class _LiveDirectoryEvidence:
    """One canonical directory generation observed by the resolver."""

    path: Path
    device: int
    inode: int


@dataclass(frozen=True, slots=True)
class _ResolvedProjectIdentity:
    """Identity plus the checkout and live-directory evidence that proved it."""

    identity: ProjectIdentity
    checkout_root: Path
    directory_evidence: tuple[_LiveDirectoryEvidence, ...]


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


def _run_git_command(*arguments: str) -> bytes:
    """Run one bounded, non-interactive Git command and return complete stdout.

    A nonzero exit is unavailable because Git does not expose a portable
    exit-code distinction between malformed topology and transient repository
    I/O.
    """
    try:
        with tempfile.TemporaryFile() as output:
            completed = subprocess.run(
                ["git", *arguments],
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


def _run_git(start: Path, *arguments: str) -> bytes:
    """Run one Git query scoped to an explicit canonical working directory."""
    return _run_git_command("-C", str(start), *arguments)


def _require_supported_git() -> None:
    """Reject Git versions that lack the unambiguous topology query grammar."""
    output = _run_git_command("--version")
    match = _GIT_VERSION_PATTERN.fullmatch(output)
    if match is None:
        raise ProjectIdentityGitVersionError(
            f"Git {_MINIMUM_GIT_VERSION_TEXT} or newer is required for project identity"
        )
    major, minor, patch = match.groups()
    version = (int(major), int(minor), int(patch or b"0"))
    if version < _MINIMUM_GIT_VERSION:
        found = ".".join(str(part) for part in version)
        raise ProjectIdentityGitVersionError(
            f"Git {_MINIMUM_GIT_VERSION_TEXT} or newer is required for project identity; "
            f"found {found}"
        )


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


def _capture_live_directory(directory: Path) -> _LiveDirectoryEvidence:
    """Capture the filesystem generation behind one canonical directory path."""
    canonical = _canonical_directory(directory, require_exists=True)
    try:
        status = canonical.stat()
    except FileNotFoundError:
        raise ProjectIdentityError("project identity path must be a directory") from None
    except OSError as exc:
        raise ProjectIdentityUnavailableError(
            "project identity filesystem is temporarily unavailable"
        ) from exc
    return _LiveDirectoryEvidence(
        path=canonical,
        device=status.st_dev,
        inode=status.st_ino,
    )


def _resolve_canonical_project_identity(effective: Path) -> _ResolvedProjectIdentity:
    """Resolve one already-canonical cwd and retain its topology evidence."""
    project_root, checkout_root = _project_and_checkout_roots(effective)
    workspace_path = _relative_workspace_path(effective, checkout_root)
    identity = ProjectIdentity.from_root(
        project_root,
        workspace_path=workspace_path,
        require_exists=True,
    )
    return _ResolvedProjectIdentity(
        identity=identity,
        checkout_root=checkout_root,
        directory_evidence=tuple(
            _capture_live_directory(directory)
            for directory in dict.fromkeys((effective, project_root, checkout_root))
        ),
    )


@dataclass(frozen=True, slots=True)
class _TopologyFileEvidence:
    """Generation-plus-content proof for one repo-local topology input."""

    path: Path
    kind: str  # "missing" | "file" | "dir"
    device: int = 0
    inode: int = 0
    mtime_ns: int = 0
    size: int = 0
    content_hash: str | None = None


@dataclass(frozen=True, slots=True, weakref_slot=True)
class PublicationEvidence:
    """Everything needed to accept a publication identity without Git.

    Captured right after a successful direct resolution. ``escalate`` is set
    whenever the checkout shape falls outside the enumerable input closure
    (bare repositories, markerless discovery, config include directives,
    unreadable inputs) — the publication boundary must then run the full
    re-resolution exactly as before. The fast path may accept only when every
    captured input is byte-for-byte stable, so it is fail-closed by
    construction and runs no Git subprocess at all — which vacuously
    satisfies the V1 rule that resolvers validate Git before topology
    queries: there are none.
    """

    identity: ProjectIdentity
    effective_directory: Path
    directory_evidence: tuple[_LiveDirectoryEvidence, ...]
    file_evidence: tuple[_TopologyFileEvidence, ...]
    escalate: bool


# A valid include or includeIf section header must contain these bytes, in
# any position, casing, or after a BOM — so searching the raw content without
# anchors over-approximates Git's config grammar instead of reimplementing
# it. False positives (the substring inside a comment or value) merely
# escalate to the full re-resolution, which is the safe direction.
_CONFIG_INCLUDE_PATTERN = re.compile(rb"\[include", re.IGNORECASE)
_CONFIG_FILE_NAMES = frozenset({"config", "config.worktree"})
_TOPOLOGY_HASH_LIMIT = 1 << 20


def _capture_topology_file(path: Path, *, hash_content: bool) -> _TopologyFileEvidence:
    """Capture one repo-local topology input, raising on unclassifiable shapes."""
    try:
        status = path.lstat()
    except FileNotFoundError:
        return _TopologyFileEvidence(path=path, kind="missing")
    except OSError as exc:
        raise ProjectIdentityUnavailableError("topology input is unreadable") from exc
    if stat.S_ISDIR(status.st_mode):
        return _TopologyFileEvidence(
            path=path,
            kind="dir",
            device=status.st_dev,
            inode=status.st_ino,
            mtime_ns=status.st_mtime_ns,
            size=0,
        )
    if not stat.S_ISREG(status.st_mode):
        raise ProjectIdentityUnavailableError("topology input has an unsupported shape")
    digest: str | None = None
    if hash_content:
        if status.st_size > _TOPOLOGY_HASH_LIMIT:
            raise ProjectIdentityUnavailableError("topology input exceeds its bound")
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise ProjectIdentityUnavailableError("topology input is unreadable") from exc
        if path.name in _CONFIG_FILE_NAMES and _CONFIG_INCLUDE_PATTERN.search(content):
            # Config includes pull unbounded external files into Git's
            # decision; that closure is not enumerable, so never fast-path it.
            raise ProjectIdentityUnavailableError("config includes are outside the closure")
        digest = hashlib.sha256(content).hexdigest()
    return _TopologyFileEvidence(
        path=path,
        kind="file",
        device=status.st_dev,
        inode=status.st_ino,
        mtime_ns=status.st_mtime_ns,
        size=status.st_size,
        content_hash=digest,
    )


_GIT_DIR_CLOSURE_NAMES = ("HEAD", "config", "config.worktree", "commondir", "gitdir")


def _capture_publication_closure(
    effective: Path, project_root: Path, checkout_root: Path
) -> tuple[tuple[_TopologyFileEvidence, ...], bool]:
    """Capture the repo-local input closure of the direct resolver's queries.

    Returns the captured population and an escalate flag. The closure covers
    exactly the inputs the five fixed topology queries consult for the
    simple-checkout shape: every boundary candidate's ``.git`` marker between
    the effective directory and the checkout root, and — when the marker is a
    directory — its ``HEAD``, ``config``, ``config.worktree``, ``commondir``,
    ``gitdir``, and the ``worktrees`` registry with each entry's ``gitdir``
    link. Anything the capture cannot classify escalates.
    """
    population: list[_TopologyFileEvidence] = []
    try:
        marker = checkout_root / ".git"
        marker_evidence = _capture_topology_file(marker, hash_content=True)
        if marker_evidence.kind == "missing":
            # Markerless discovery (bare candidates) is outside the closure.
            return tuple(population), True
        population.append(marker_evidence)
        if marker_evidence.kind == "file":
            # A gitdir-pointer checkout (linked worktree): the pointer content
            # is hashed above, but the pointed-to administrative directory
            # belongs to another tree whose closure we do not enumerate here.
            return tuple(population), True
        git_dir = marker
        commondir: _TopologyFileEvidence | None = None
        for name in _GIT_DIR_CLOSURE_NAMES:
            entry = _capture_topology_file(git_dir / name, hash_content=True)
            population.append(entry)
            if name == "commondir":
                commondir = entry
        if commondir is None or commondir.kind != "missing":
            # A commondir file means this administrative directory belongs to
            # a linked-worktree constellation; keep to the full re-resolution.
            return tuple(population), True
        worktrees = git_dir / "worktrees"
        registry = _capture_topology_file(worktrees, hash_content=False)
        population.append(registry)
        if registry.kind == "dir":
            try:
                entries = sorted(entry.name for entry in worktrees.iterdir())
            except OSError as exc:
                raise ProjectIdentityUnavailableError("topology input is unreadable") from exc
            for entry in entries:
                population.append(
                    _capture_topology_file(worktrees / entry / "gitdir", hash_content=True)
                )
        # Boundary candidates strictly between the effective directory and the
        # checkout root: a new marker appearing there changes the nearest
        # boundary, so their absence is part of the proof.
        for candidate in (effective, *effective.parents):
            if candidate == checkout_root:
                break
            population.append(_capture_topology_file(candidate / ".git", hash_content=True))
        if project_root != checkout_root:
            # The primary worktree was derived through Git; pin its marker too.
            population.append(_capture_topology_file(project_root / ".git", hash_content=True))
    except ProjectIdentityError:
        return tuple(population), True
    return tuple(population), False


_EVIDENCE_SINK: ContextVar[list[PublicationEvidence | None] | None] = ContextVar(
    "publication_evidence_sink", default=None
)


def active_publication_evidence_sink() -> list[PublicationEvidence | None] | None:
    """Return the ambient evidence sink, if a publication scope is active."""
    return _EVIDENCE_SINK.get()


def active_publication_evidence() -> PublicationEvidence | None:
    """Return the evidence the resolver deposited in the active scope, if any."""
    sink = _EVIDENCE_SINK.get()
    if sink is None:
        return None
    return sink[0]


# Evidence is trusted by issuance, not by shape: only the exact objects this
# module created during a successful resolution are registered, keyed by
# object identity and compared with ``is``. A caller-assembled object — even
# one whose captured entries all match the live filesystem — was never
# issued and is refused before any structural check runs. Weak values keep
# the registry from outliving the evidence itself.
_ISSUED_EVIDENCE: weakref.WeakValueDictionary[int, PublicationEvidence] = (
    weakref.WeakValueDictionary()
)


def _evidence_is_resolver_issued(evidence: PublicationEvidence) -> bool:
    return _ISSUED_EVIDENCE.get(id(evidence)) is evidence


@contextmanager
def publication_evidence_sink() -> Iterator[list[PublicationEvidence | None]]:
    """Carry captured publication evidence out of a nested resolution.

    Contract construction runs the resolver deep inside a worker thread; the
    single-element mutable cell is shared by ``asyncio.to_thread`` context
    copies, so evidence recorded there is visible to the caller afterwards.
    The cell holds observations only — captured stat/hash metadata — never
    capability trust, and it is discarded with the scope.
    """
    cell: list[PublicationEvidence | None] = [None]
    token = _EVIDENCE_SINK.set(cell)
    try:
        yield cell
    finally:
        _EVIDENCE_SINK.reset(token)


def resolve_project_identity_for_publication(
    effective_cwd: str | Path,
) -> tuple[ProjectIdentity, PublicationEvidence]:
    """Resolve a direct identity and capture publication-reusable evidence.

    Behaves exactly like :func:`resolve_project_identity`; additionally
    captures the resolver's repo-local input closure so the publication
    boundary can revalidate by metadata alone (#1796 L2). The closure is
    captured both before and after the resolution and the evidence is usable
    only when the two captures are identical, binding the recorded state to
    the exact resolution that produced the identity. When the checkout shape
    falls outside the enumerable closure — or the bracket detects any
    interleaved change — the returned evidence demands escalation and
    publication re-resolves in full, exactly as before.
    """
    effective = _canonical_directory(effective_cwd, require_exists=True)
    pre_files: tuple[_TopologyFileEvidence, ...] = ()
    pre_escalate = True
    pre_marker_root: Path | None = None
    try:
        pre_marker_root, bare_candidate = _nearest_repository_boundary(effective)
    except ProjectIdentityError:
        bare_candidate = None
    if pre_marker_root is not None and bare_candidate is None:
        pre_files, pre_escalate = _capture_publication_closure(
            effective, pre_marker_root, pre_marker_root
        )
    resolved = _resolve_with_evidence(effective)
    project_root = Path(resolved.identity.project_root)
    file_evidence: tuple[_TopologyFileEvidence, ...] = ()
    escalate = True
    if (
        not pre_escalate
        and resolved.checkout_root == pre_marker_root
        and project_root == resolved.checkout_root
    ):
        # The closure was captured before the resolver's queries and is
        # re-captured after them; only a byte-identical pair proves the state
        # the resolver consumed is the state the evidence records. Any
        # interleaved mutation perturbs a stat generation or a content hash
        # and the pair differs, so publication escalates.
        post_files, post_escalate = _capture_publication_closure(
            effective, project_root, resolved.checkout_root
        )
        if not post_escalate and post_files == pre_files:
            file_evidence = post_files
            escalate = False
    evidence = PublicationEvidence(
        identity=resolved.identity,
        effective_directory=effective,
        directory_evidence=resolved.directory_evidence,
        file_evidence=file_evidence,
        escalate=escalate,
    )
    _ISSUED_EVIDENCE[id(evidence)] = evidence
    sink = _EVIDENCE_SINK.get()
    if sink is not None:
        sink[0] = evidence
    return resolved.identity, evidence


def publication_evidence_is_stable(
    evidence: PublicationEvidence, effective_cwd: str | Path
) -> bool:
    """Return True only when every captured publication input is unchanged.

    Only evidence this module itself issued is considered at all — a
    caller-assembled object is refused regardless of its contents, because a
    structural match can prove only the current filesystem state, never that
    Git produced the claimed identity from it. ``effective_cwd`` must
    canonicalize to the directory the evidence was captured from — the proof
    is about that resolution and transfers to no other workspace. Any doubt — an escalating capture, a changed or newly
    unreadable input, a directory generation drift — returns False so the
    caller escalates to the full re-resolution. This function runs no Git
    subprocess.
    """
    if not _evidence_is_resolver_issued(evidence):
        return False
    if evidence.escalate:
        return False
    try:
        effective = _canonical_directory(effective_cwd, require_exists=True)
    except ProjectIdentityError:
        return False
    if effective != evidence.effective_directory:
        return False
    if not _publication_closure_is_complete(evidence):
        return False
    try:
        _revalidate_live_directories(*evidence.directory_evidence)
    except ProjectIdentityError:
        return False
    for expected in evidence.file_evidence:
        try:
            current = _capture_topology_file(
                expected.path, hash_content=expected.content_hash is not None
            )
        except ProjectIdentityError:
            return False
        if current != expected:
            return False
    return True


def _publication_closure_is_complete(evidence: PublicationEvidence) -> bool:
    """Require the evidence to carry the full resolver-shaped closure.

    The session boundary receives evidence as a call argument, so acceptance
    cannot rest on where the object came from — it must rest on what it
    proves. The captured population has to contain every input the closure
    capture records for the simple-checkout shape anchored at exactly this
    identity: the marker directory at the project root, each administrative
    file, the worktrees registry with a link per currently present entry,
    and a pinned-missing marker for every boundary candidate below the root.
    Anything less — including the empty closure — is refused.
    """
    project_root = Path(evidence.identity.project_root)
    try:
        workspace_path = _relative_workspace_path(evidence.effective_directory, project_root)
    except ProjectIdentityError:
        return False
    if workspace_path != evidence.identity.workspace_path:
        return False
    captured = {entry.path: entry for entry in evidence.file_evidence}
    git_dir = project_root / ".git"
    marker = captured.get(git_dir)
    if marker is None or marker.kind != "dir":
        return False
    for name in _GIT_DIR_CLOSURE_NAMES:
        if git_dir / name not in captured:
            return False
    if captured[git_dir / "HEAD"].kind != "file":
        return False
    if captured[git_dir / "commondir"].kind != "missing":
        return False
    registry = captured.get(git_dir / "worktrees")
    if registry is None:
        return False
    if registry.kind == "dir":
        try:
            entries = sorted(entry.name for entry in (git_dir / "worktrees").iterdir())
        except OSError:
            return False
        for entry in entries:
            if git_dir / "worktrees" / entry / "gitdir" not in captured:
                return False
    elif registry.kind != "missing":
        return False
    for candidate in (evidence.effective_directory, *evidence.effective_directory.parents):
        if candidate == project_root:
            break
        boundary = captured.get(candidate / ".git")
        if boundary is None or boundary.kind != "missing":
            return False
    directories = {entry.path for entry in evidence.directory_evidence}
    return evidence.effective_directory in directories and project_root in directories


def _revalidate_live_directories(*expected_population: _LiveDirectoryEvidence) -> None:
    """Require every topology input and owner to remain the same live directory."""
    for expected in dict.fromkeys(expected_population):
        current = _capture_live_directory(expected.path)
        if current != expected:
            raise ProjectIdentityError("project identity path changed during resolution")


def _resolve_with_evidence(effective: Path) -> _ResolvedProjectIdentity:
    """Run the direct resolution pipeline and keep its evidence."""
    effective_evidence = _capture_live_directory(effective)
    _require_supported_git()
    resolved = _resolve_canonical_project_identity(effective)
    _revalidate_live_directories(effective_evidence, *resolved.directory_evidence)
    return resolved


def resolve_project_identity(effective_cwd: str | Path) -> ProjectIdentity:
    """Resolve one deterministic Project Map V1 identity.

    Direct callers are anchored to the nearest Git checkout and, for linked
    worktrees, the provable common source checkout.  Non-Git directories remain
    valid local-first projects and use their canonical cwd as both project and
    workspace root.

    Managed task worktrees must use :func:`resolve_managed_project_identity`;
    this direct resolver cannot attribute a caller-supplied source checkout.
    """
    effective = _canonical_directory(effective_cwd, require_exists=True)
    return _resolve_with_evidence(effective).identity


def _source_project_identity(
    source_root: Path,
    source_workspace: Path,
) -> _ResolvedProjectIdentity:
    """Resolve a managed source only from its Git-proven checkout root."""
    project_root, checkout_root = _project_and_checkout_roots(source_root)
    if source_root != checkout_root:
        raise ManagedProjectScopeError(str(source_workspace), str(source_root))
    try:
        workspace_path = _relative_workspace_path(source_workspace, checkout_root)
    except ProjectIdentityError as exc:
        raise ManagedProjectScopeError(str(source_workspace), str(source_root)) from exc
    identity = ProjectIdentity.from_root(
        project_root,
        workspace_path=workspace_path,
        require_exists=True,
    )
    return _ResolvedProjectIdentity(
        identity=identity,
        checkout_root=checkout_root,
        directory_evidence=tuple(
            _capture_live_directory(directory)
            for directory in dict.fromkeys(
                (source_root, source_workspace, project_root, checkout_root)
            )
        ),
    )


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
    input_evidence = tuple(
        _capture_live_directory(directory)
        for directory in dict.fromkeys(
            (
                canonical_source_root,
                canonical_source_workspace,
                canonical_worktree_root,
                canonical_execution_workspace,
            )
        )
    )
    _require_supported_git()
    source = _source_project_identity(
        canonical_source_root,
        canonical_source_workspace,
    )
    execution = _resolve_canonical_project_identity(canonical_execution_workspace)
    # This declared scope comparison preserves the public error taxonomy only.
    # Acceptance below still requires Git-proven checkout and resolved scope.
    try:
        declared_execution_scope = _relative_workspace_path(
            canonical_execution_workspace,
            canonical_worktree_root,
        )
    except ProjectIdentityError as exc:
        raise ManagedProjectScopeError(
            str(canonical_source_workspace),
            str(canonical_execution_workspace),
        ) from exc
    if source.identity.workspace_path != declared_execution_scope:
        raise ManagedProjectScopeError(
            str(canonical_source_workspace),
            str(canonical_execution_workspace),
        )
    if (
        execution.identity.project_id != source.identity.project_id
        or execution.identity.project_root != source.identity.project_root
    ):
        raise ManagedProjectOwnershipError(source.identity, execution.identity)
    if (
        execution.checkout_root != canonical_worktree_root
        or execution.identity.workspace_path != declared_execution_scope
    ):
        raise ManagedProjectScopeError(
            str(canonical_source_workspace),
            str(canonical_execution_workspace),
        )
    _revalidate_live_directories(
        *input_evidence,
        *source.directory_evidence,
        *execution.directory_evidence,
    )
    return source.identity


__all__ = [
    "PROJECT_ID_PREFIX",
    "ManagedProjectOwnershipError",
    "ManagedProjectScopeError",
    "ProjectIdentity",
    "ProjectIdentityError",
    "ProjectIdentityGitVersionError",
    "ProjectIdentityUnavailableError",
    "project_id_for_root",
    "resolve_managed_project_identity",
    "resolve_project_identity",
]
