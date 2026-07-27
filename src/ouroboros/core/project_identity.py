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
        if path.is_symlink() or not path.is_file():
            return None
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
    raw_git_dir = pointer.removeprefix("gitdir: ")
    if not raw_git_dir or raw_git_dir != raw_git_dir.strip():
        return None
    try:
        # Git pointer records are paths, not shell input: ``~`` is literal.
        git_dir = Path(raw_git_dir)
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


def _git_config_comment_index(value: str) -> int | None:
    """Return the first unquoted Git comment marker in one logical line."""
    in_quotes = False
    escaped = False
    for index, character in enumerate(value):
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if character == '"':
            in_quotes = not in_quotes
            continue
        if not in_quotes and character in "#;":
            return index
    return None


def _git_config_logical_lines(raw_config: str) -> tuple[str, ...] | None:
    """Fold value continuations while rejecting multiline section headers."""
    logical_lines: list[str] = []
    current = ""
    continuation_pending = False
    physical_lines = raw_config.split("\n")
    for index, physical_line in enumerate(physical_lines):
        has_line_feed = index < len(physical_lines) - 1
        if physical_line.endswith("\r"):
            physical_line = physical_line[:-1]
        if "\r" in physical_line:
            return None
        candidate = current + physical_line
        comment_index = _git_config_comment_index(candidate)
        uncommented = candidate if comment_index is None else candidate[:comment_index]
        trailing_backslashes = len(uncommented) - len(uncommented.rstrip("\\"))
        continues = comment_index is None and trailing_backslashes % 2 == 1
        if continues:
            if not has_line_feed:
                return None
            # Git permits continuations for variable values, but explicitly
            # forbids section headers from spanning physical lines.  Check the
            # accumulated candidate before discarding the newline so malformed
            # ``[co\\\nre]`` cannot become a trusted ``[core]`` section.
            if candidate.lstrip(" \t").startswith("["):
                return None
            current = candidate[:-1]
            continuation_pending = True
            continue
        if continuation_pending and candidate.lstrip(" \t").startswith("["):
            return None
        logical_lines.append(candidate)
        current = ""
        continuation_pending = False
    if continuation_pending:
        return None
    return tuple(logical_lines)


def _parse_git_config_section(value: str) -> tuple[str, str] | None:
    """Parse a section header and return its relevant kind plus remainder."""
    in_quotes = False
    escaped = False
    closing_index: int | None = None
    for index, character in enumerate(value[1:], start=1):
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if character == '"':
            in_quotes = not in_quotes
            continue
        if character == "]" and not in_quotes:
            closing_index = index
            break
    if closing_index is None or in_quotes or escaped:
        return None
    section = value[1:closing_index].strip(" \t")
    if not section:
        return None
    section_name = section.split(maxsplit=1)[0]
    if not section_name or any(
        not character.isascii() or not (character.isalnum() or character in "-.")
        for character in section_name
    ):
        return None
    section_suffix = section[len(section_name) :].lstrip(" \t")
    if section_suffix and not (section_suffix.startswith('"') and section_suffix.endswith('"')):
        return None
    folded = section_name.casefold()
    if folded == "core" and not section_suffix:
        kind = "core"
    elif folded == "extensions" and not section_suffix:
        kind = "extensions"
    elif folded in {"include", "includeif"}:
        kind = "include"
    else:
        kind = "other"
    return kind, value[closing_index + 1 :]


def _parse_git_config_value(value: str) -> tuple[bool, str]:
    """Decode Git quotes, comments, and the documented value escapes."""
    decoded: list[tuple[str, bool]] = []
    in_quotes = False
    index = 0
    escapes = {
        "b": "\b",
        "n": "\n",
        "t": "\t",
        '"': '"',
        "\\": "\\",
    }
    while index < len(value):
        character = value[index]
        if not in_quotes and character in "#;":
            break
        if character == '"':
            in_quotes = not in_quotes
            index += 1
            continue
        if character == "\\":
            index += 1
            if index >= len(value) or value[index] not in escapes:
                return False, ""
            decoded.append((escapes[value[index]], True))
            index += 1
            continue
        decoded.append((character, in_quotes))
        index += 1
    if in_quotes:
        return False, ""
    start = 0
    end = len(decoded)
    while start < end and decoded[start][0] in " \t" and not decoded[start][1]:
        start += 1
    while end > start and decoded[end - 1][0] in " \t" and not decoded[end - 1][1]:
        end -= 1
    return True, "".join(character for character, _quoted in decoded[start:end])


def _parse_git_config_assignment(value: str) -> tuple[str, str | None] | None:
    """Parse one Git variable assignment; ``None`` value means boolean true."""
    candidate = value.lstrip(" \t")
    if (
        not candidate
        or candidate[0] in "#;"
        or not candidate[0].isascii()
        or not candidate[0].isalpha()
    ):
        return None
    index = 1
    while (
        index < len(candidate)
        and candidate[index].isascii()
        and (candidate[index].isalnum() or candidate[index] == "-")
    ):
        index += 1
    key = candidate[:index].casefold()
    remainder = candidate[index:].lstrip(" \t")
    if not remainder or remainder[0] in "#;":
        return key, None
    if remainder[0] != "=":
        return None
    valid, parsed_value = _parse_git_config_value(remainder[1:])
    return (key, parsed_value) if valid else None


def _parse_git_config_values(
    raw_config: str,
) -> dict[tuple[str, str], str | None] | None:
    """Return identity-relevant later-wins values from one bounded config."""
    logical_lines = _git_config_logical_lines(raw_config)
    if logical_lines is None:
        return None
    current_section: str | None = None
    relevant_values: dict[tuple[str, str], str | None] = {}
    for logical_line in logical_lines:
        candidate = logical_line.lstrip(" \t")
        if not candidate or candidate[0] in "#;":
            continue
        if candidate.startswith("["):
            parsed_section = _parse_git_config_section(candidate)
            if parsed_section is None:
                return None
            current_section, candidate = parsed_section
            if current_section == "include":
                # Includes escape the bounded file and cannot prove identity.
                return None
            candidate = candidate.lstrip(" \t")
            if not candidate or candidate[0] in "#;":
                continue
        if current_section is None:
            return None
        assignment = _parse_git_config_assignment(candidate)
        if assignment is None:
            return None
        key, parsed_value = assignment
        if current_section == "core" and key in {"bare", "worktree"}:
            relevant_values[("core", key)] = parsed_value
        elif current_section == "extensions" and key == "worktreeconfig":
            relevant_values[("extensions", key)] = parsed_value
    return relevant_values


def _parse_git_boolean(value: str | None) -> bool | None:
    if value is None:
        return True
    normalized = value.casefold()
    if not normalized:
        return False
    if normalized in {"true", "yes", "on", "1"}:
        return True
    if normalized in {"false", "no", "off", "0"}:
        return False
    return None


def _read_git_core_config(git_dir: Path) -> _GitCoreConfig | None:
    """Read bounded common and applicable main-worktree core configuration."""
    raw_config = _read_bounded_utf8(
        git_dir / "config",
        max_bytes=_MAX_GIT_CONFIG_LENGTH,
    )
    if raw_config is None:
        return None
    common_values = _parse_git_config_values(raw_config)
    if common_values is None:
        return None

    worktree_config_enabled = False
    worktree_config_key = ("extensions", "worktreeconfig")
    if worktree_config_key in common_values:
        parsed_extension = _parse_git_boolean(common_values[worktree_config_key])
        if parsed_extension is None:
            return None
        worktree_config_enabled = parsed_extension

    core_values = {
        key: value for (section, key), value in common_values.items() if section == "core"
    }
    if worktree_config_enabled:
        # For the main worktree, ``git rev-parse --git-path
        # config.worktree`` names ``<common-dir>/config.worktree``.  Git reads
        # it after the common config, so its core values override earlier
        # values.  A missing file is a valid empty overlay; a present symlink,
        # non-file, oversized, malformed, or including file cannot prove
        # ownership and fails closed.
        worktree_config_path = git_dir / "config.worktree"
        try:
            if worktree_config_path.is_symlink():
                return None
            worktree_config_exists = worktree_config_path.exists()
        except OSError:
            return None
        if worktree_config_exists:
            raw_worktree_config = _read_bounded_utf8(
                worktree_config_path,
                max_bytes=_MAX_GIT_CONFIG_LENGTH,
            )
            if raw_worktree_config is None:
                return None
            worktree_values = _parse_git_config_values(raw_worktree_config)
            if worktree_values is None:
                return None
            core_values.update(
                {
                    key: value
                    for (section, key), value in worktree_values.items()
                    if section == "core"
                }
            )

    if "bare" not in core_values:
        return None
    bare = _parse_git_boolean(core_values["bare"])
    if bare is None:
        return None
    if "worktree" not in core_values:
        return _GitCoreConfig(bare=bare, worktree=None)
    raw_worktree = core_values["worktree"]
    if raw_worktree is None or not raw_worktree:
        return None
    try:
        # Git stores ``core.worktree`` verbatim.  Relative values, including
        # leading ``~`` components, are resolved from the Git directory and
        # never through the process environment.
        worktree = Path(raw_worktree)
        if not worktree.is_absolute():
            worktree = git_dir / worktree
        worktree = _canonical_directory(worktree)
    except (OSError, ProjectIdentityError, RuntimeError, ValueError):
        return None
    return _GitCoreConfig(bare=bare, worktree=worktree)


def _direct_gitfile_source_root(checkout_root: Path, git_dir: Path) -> Path | None:
    """Return a direct gitfile owner only when the target names it explicitly."""
    core = _read_git_core_config(git_dir)
    if core is None or core.bare or core.worktree != checkout_root:
        return None
    return checkout_root if _git_pointer_target(checkout_root) == git_dir else None


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

    # An explicit, positively proven owner outranks directory naming.  An
    # external common directory may itself be named ``.git`` without being the
    # metadata directory of its parent checkout.
    if core is not None and core.worktree is not None:
        return core.worktree if _git_pointer_target(core.worktree) == common_dir else None

    if common_dir.name == ".git":
        normal_checkout = common_dir.parent
        try:
            if (normal_checkout / ".git").resolve(strict=False) == common_dir:
                return normal_checkout.resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            return None

    if core is None:
        return None

    # Without an explicit core.worktree, only callers that already proved a
    # common-dir worktree record and backlink may use this stable common root.
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
        return _direct_gitfile_source_root(checkout_root, git_dir) or checkout_root
    try:
        common_dir = Path(raw_common_dir)
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
        checkout_marker = Path(raw_checkout_marker)
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
        workspace = _canonical_directory(
            source_root if source_workspace is None else source_workspace
        )
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
