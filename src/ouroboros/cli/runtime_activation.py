"""Crash-recoverable activation for setup-owned runtime configuration.

The standalone Claude profile deliberately has no Claude MCP-file side effect.
This module owns only ``~/.ouroboros/config.yaml`` and the creation of a
missing ``credentials.yaml``.

All Ouroboros activation writers cooperate through :func:`file_lock`.  The
lock closes the portable check/promote and check/unlink race between those
writers.  A process that ignores the lock cannot be made transactional by the
cross-platform Python filesystem API; generation fingerprints make edits that
are visible at validation or recovery fail closed, but the narrow native rename
interval is deliberately not advertised as non-cooperative CAS.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
import ctypes
from dataclasses import dataclass
import errno
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any, Literal
from uuid import uuid4

import yaml
from yaml.constructor import ConstructorError

from ouroboros.cli.formatters.panels import print_error, print_warning
from ouroboros.core.file_lock import _windows_directory_lease, file_lock
from ouroboros.core.owner_only import fsync_parent_directory as _fsync_parent_directory_path

_ACTIVATION_LOCK_NAME = ".claude-runtime-activation"
_JOURNAL_NAME = ".claude-runtime-activation.journal.json"
_JOURNAL_VERSION = 1


@dataclass(frozen=True)
class _FileSnapshot:
    """One exact, no-follow activation target generation."""

    kind: Literal["missing", "file", "directory", "symlink", "other"]
    mode: int | None = None
    contents: bytes | None = None
    device: int | None = None
    inode: int | None = None
    modified_ns: int | None = None
    changed_ns: int | None = None
    link_count: int | None = None
    owner_id: int | None = None
    group_id: int | None = None
    link_target: str | None = None


@dataclass(frozen=True)
class _PreparedFile:
    """A fsync'd file ready for one same-directory promotion."""

    path: Path
    snapshot: _FileSnapshot


@dataclass(frozen=True)
class _DirectoryAuthority:
    """Pinned no-follow authority for one activation directory."""

    path: Path
    directory_fd: int | None
    parent_directory_fd: int | None
    requested_parent: Path
    directory_identity: tuple[int, int]
    parent_identity: tuple[int, int]


_ACTIVE_DIRECTORY_AUTHORITY: ContextVar[_DirectoryAuthority | None] = ContextVar(
    "active_claude_activation_directory_authority",
    default=None,
)


class _ConcurrentActivationError(OSError):
    """An activation target no longer matches the generation that was read."""


class _PostPublicationError(OSError):
    """A promotion failed after the prepared inode became live."""

    def __init__(
        self,
        message: str,
        path: Path,
        owned_snapshot: _FileSnapshot,
        observed_snapshot: _FileSnapshot,
    ) -> None:
        super().__init__(message)
        self.path = path
        self.owned_snapshot = owned_snapshot
        self.observed_snapshot = observed_snapshot


class _DurabilityError(_PostPublicationError):
    """A replacement was published but its directory sync was not confirmed."""

    def __init__(self, path: Path, published_snapshot: _FileSnapshot) -> None:
        super().__init__(
            f"Could not confirm replacement durability for {path}",
            path,
            published_snapshot,
            published_snapshot,
        )


class _CommittedCleanupError(OSError):
    """Activation committed, but best-effort recovery-artifact cleanup failed."""


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects every duplicate mapping key."""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    loader.flatten_mapping(node)
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _authority_operand(path: Path) -> tuple[str | Path, int | None]:
    """Return a pathname anchored to the active pinned directory when possible."""
    authority = _ACTIVE_DIRECTORY_AUTHORITY.get()
    if (
        authority is not None
        and authority.directory_fd is not None
        and path.parent == authority.path
    ):
        return path.name, authority.directory_fd
    return path, None


def _require_active_directory_binding() -> None:
    """Fail if the pinned config generation is no longer the live pathname."""
    authority = _ACTIVE_DIRECTORY_AUTHORITY.get()
    if authority is None:
        raise _ConcurrentActivationError("Activation directory authority is unavailable")
    try:
        selected_parent = os.stat(authority.path.parent, follow_symlinks=False)
        requested_parent = os.stat(authority.requested_parent)
        _require_real_directory(selected_parent, authority.path.parent)
        _require_real_directory(requested_parent, authority.requested_parent)
        if (
            _directory_identity(selected_parent) != authority.parent_identity
            or _directory_identity(requested_parent) != authority.parent_identity
        ):
            raise _ConcurrentActivationError(
                f"Activation parent changed: {authority.requested_parent}"
            )

        if authority.directory_fd is None or authority.parent_directory_fd is None:
            current = os.stat(authority.path, follow_symlinks=False)
            _require_real_directory(current, authority.path)
            if _directory_identity(current) != authority.directory_identity:
                raise _ConcurrentActivationError(f"Activation directory changed: {authority.path}")
            return

        opened_parent = os.fstat(authority.parent_directory_fd)
        current = os.stat(
            authority.path.name,
            dir_fd=authority.parent_directory_fd,
            follow_symlinks=False,
        )
        opened = os.fstat(authority.directory_fd)
        _require_real_directory(opened_parent, authority.path.parent)
        _require_real_directory(current, authority.path)
        _require_real_directory(opened, authority.path)
        if (
            _directory_identity(opened_parent) != authority.parent_identity
            or _directory_identity(current) != authority.directory_identity
            or _directory_identity(opened) != authority.directory_identity
        ):
            raise _ConcurrentActivationError(f"Activation directory changed: {authority.path}")
    except FileNotFoundError as exc:
        raise _ConcurrentActivationError(
            f"Activation directory binding disappeared: {authority.path}"
        ) from exc


def fsync_parent_directory(file_path: Path) -> bool:
    """Flush the active pinned directory, falling back to the shared path helper."""
    _operand, directory_fd = _authority_operand(file_path)
    if directory_fd is None:
        return _fsync_parent_directory_path(file_path)
    try:
        os.fsync(directory_fd)
    except OSError as error:
        return error.errno in (errno.EINVAL, errno.ENOTSUP)
    return True


def _read_regular_snapshot(path: Path, initial_stat: os.stat_result) -> _FileSnapshot:
    """Read a regular file through one no-follow descriptor generation."""
    flags = os.O_RDONLY
    for optional_flag in ("O_CLOEXEC", "O_NONBLOCK", "O_NOFOLLOW"):
        flags |= getattr(os, optional_flag, 0)
    operand, directory_fd = _authority_operand(path)
    descriptor = os.open(operand, flags, dir_fd=directory_fd)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise _ConcurrentActivationError(f"Activation target changed type: {path}")
        if (before.st_dev, before.st_ino) != (initial_stat.st_dev, initial_stat.st_ino):
            raise _ConcurrentActivationError(f"Activation target changed identity: {path}")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        after = os.fstat(descriptor)
        before_generation = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
            before.st_nlink,
            before.st_uid,
            before.st_gid,
        )
        after_generation = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
            after.st_nlink,
            after.st_uid,
            after.st_gid,
        )
        if after_generation != before_generation:
            raise _ConcurrentActivationError(f"Activation target changed while read: {path}")
        return _FileSnapshot(
            kind="file",
            mode=stat.S_IMODE(before.st_mode),
            contents=b"".join(chunks),
            device=before.st_dev,
            inode=before.st_ino,
            modified_ns=before.st_mtime_ns,
            changed_ns=before.st_ctime_ns,
            link_count=before.st_nlink,
            owner_id=before.st_uid,
            group_id=before.st_gid,
        )
    finally:
        os.close(descriptor)


def _snapshot_target(path: Path) -> _FileSnapshot:
    """Snapshot without following links or opening special files."""
    operand, directory_fd = _authority_operand(path)
    try:
        stat_result = os.stat(operand, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return _FileSnapshot(kind="missing")
    mode = stat.S_IMODE(stat_result.st_mode)
    if stat.S_ISREG(stat_result.st_mode):
        return _read_regular_snapshot(path, stat_result)
    if stat.S_ISLNK(stat_result.st_mode):
        return _FileSnapshot(
            kind="symlink",
            mode=mode,
            owner_id=stat_result.st_uid,
            group_id=stat_result.st_gid,
            link_target=os.readlink(operand, dir_fd=directory_fd),
        )
    if stat.S_ISDIR(stat_result.st_mode):
        return _FileSnapshot(
            kind="directory",
            mode=mode,
            device=stat_result.st_dev,
            inode=stat_result.st_ino,
            owner_id=stat_result.st_uid,
            group_id=stat_result.st_gid,
        )
    return _FileSnapshot(
        kind="other",
        mode=mode,
        owner_id=stat_result.st_uid,
        group_id=stat_result.st_gid,
    )


def _require_snapshot(path: Path, expected: _FileSnapshot) -> _FileSnapshot:
    current = _snapshot_target(path)
    if current != expected:
        raise _ConcurrentActivationError(f"Activation target changed concurrently: {path}")
    return current


def _require_file_target(path: Path, snapshot: _FileSnapshot) -> None:
    if snapshot.kind == "missing":
        return
    if snapshot.kind != "file":
        raise ValueError(f"{path.name} must be a regular file, not {snapshot.kind}")
    if snapshot.link_count != 1:
        raise ValueError(f"{path.name} must not be hard-linked")


def _yaml_mapping(path: Path, snapshot: _FileSnapshot) -> dict[str, object]:
    if snapshot.kind != "file" or snapshot.contents is None:
        raise ValueError(f"{path.name} is not a regular file")
    try:
        loaded = yaml.load(snapshot.contents.decode("utf-8"), Loader=_UniqueKeyLoader)
    except (UnicodeDecodeError, yaml.YAMLError, RecursionError) as exc:
        raise ValueError(f"Could not parse {path.name}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ValueError(f"Invalid non-mapping {path.name} contents")
    if not all(isinstance(key, str) for key in loaded):
        raise ValueError(f"Invalid non-string key in {path.name}")
    return loaded


def _snapshot_from_stat(contents: bytes, result: os.stat_result) -> _FileSnapshot:
    return _FileSnapshot(
        kind="file",
        mode=stat.S_IMODE(result.st_mode),
        contents=contents,
        device=result.st_dev,
        inode=result.st_ino,
        modified_ns=result.st_mtime_ns,
        changed_ns=result.st_ctime_ns,
        link_count=result.st_nlink,
        owner_id=result.st_uid,
        group_id=result.st_gid,
    )


def _validate_prepared_mode(
    path: Path,
    prepared_mode: int,
    *,
    requested_mode: int,
    preserve_exact_mode: bool,
) -> None:
    """Validate POSIX mode bits where the platform reports them reliably."""
    if os.name == "nt":
        # NTFS security is enforced by ACLs; CPython reports writable files as
        # 0o666 regardless of the requested mode because POSIX group/other
        # bits do not exist. Treating those synthetic bits as a violation
        # makes every Windows activation fail before publication.
        return
    if preserve_exact_mode and prepared_mode != requested_mode:
        raise OSError(f"Could not preserve file mode for {path}")
    if prepared_mode & ~requested_mode:
        raise OSError(f"Prepared file mode exceeds the secure default for {path}")


def _prepare_file(
    path: Path,
    contents: bytes,
    *,
    requested_mode: int,
    preserve_exact_mode: bool,
    requested_owner: tuple[int, int] | None = None,
    contains_secret: bool = False,
) -> _PreparedFile:
    """Create and fsync a new file, preserving supported metadata exactly."""
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    operand, directory_fd = _authority_operand(path)
    descriptor = os.open(operand, flags, requested_mode, dir_fd=directory_fd)
    written_length = 0
    try:
        if preserve_exact_mode and hasattr(os, "fchmod"):
            os.fchmod(descriptor, requested_mode)
        if requested_owner is not None:
            if hasattr(os, "fchown"):
                os.fchown(descriptor, requested_owner[0], requested_owner[1])
            owner = os.fstat(descriptor)
            if (owner.st_uid, owner.st_gid) != requested_owner:
                raise OSError(f"Could not preserve file ownership for {path}")
        view = memoryview(contents)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError(f"Could not prepare replacement for {path}")
            written_length += written
            view = view[written:]
        os.fsync(descriptor)
        prepared = os.fstat(descriptor)
        if not stat.S_ISREG(prepared.st_mode) or prepared.st_nlink != 1:
            raise OSError(f"Could not prepare regular replacement for {path}")
        _validate_prepared_mode(
            path,
            stat.S_IMODE(prepared.st_mode),
            requested_mode=requested_mode,
            preserve_exact_mode=preserve_exact_mode,
        )
        if requested_owner is not None and (prepared.st_uid, prepared.st_gid) != requested_owner:
            raise OSError(f"Could not preserve file ownership for {path}")
        return _PreparedFile(path, _snapshot_from_stat(contents, prepared))
    except BaseException:
        failed_prepared: _PreparedFile | None = None
        try:
            failed_stat = os.fstat(descriptor)
            failed_prepared = _PreparedFile(
                path,
                _snapshot_from_stat(contents[:written_length], failed_stat),
            )
        except OSError:
            # Without a descriptor-derived identity there is no safe pathname
            # cleanup.  Leave the stage for recovery rather than guessing.
            pass
        os.close(descriptor)
        descriptor = -1
        if failed_prepared is not None:
            _discard_prepared(failed_prepared, contains_secret=contains_secret)
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _new_staging_path(target: Path, generation: str, label: str) -> Path:
    return target.parent / f".claude-runtime-activation.{generation}.{label}.stage"


def _new_claim_path(target: Path, generation: str, label: str) -> Path:
    return target.parent / f".claude-runtime-activation.{generation}.{label}.claim"


def _rename_if_absent(source: Path, destination: Path) -> None:
    """Atomically move ``source`` without ever replacing ``destination``."""
    if os.name == "nt":  # pragma: no cover - exercised on Windows
        # MoveFileEx without MOVEFILE_REPLACE_EXISTING is the behavior exposed
        # by os.rename on Windows.
        os.rename(source, destination)
        return

    libc = ctypes.CDLL(None, use_errno=True)
    source_operand, source_directory_fd = _authority_operand(source)
    destination_operand, destination_directory_fd = _authority_operand(destination)
    source_bytes = os.fsencode(source_operand)
    destination_bytes = os.fsencode(destination_operand)
    result: int
    if sys.platform == "darwin":
        rename_exclusive = 0x00000004  # RENAME_EXCL
        renameatx_np = libc.renameatx_np
        renameatx_np.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameatx_np.restype = ctypes.c_int
        result = renameatx_np(
            -2 if source_directory_fd is None else source_directory_fd,
            source_bytes,
            -2 if destination_directory_fd is None else destination_directory_fd,
            destination_bytes,
            rename_exclusive,
        )
    elif sys.platform.startswith("linux"):
        rename_noreplace = 1
        try:
            renameat2 = libc.renameat2
        except AttributeError as exc:  # pragma: no cover - old libc
            raise OSError(
                errno.ENOTSUP,
                "atomic no-replace rename is unavailable",
                str(destination),
            ) from exc
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        result = renameat2(
            -100 if source_directory_fd is None else source_directory_fd,
            source_bytes,
            -100 if destination_directory_fd is None else destination_directory_fd,
            destination_bytes,
            rename_noreplace,
        )
    else:  # pragma: no cover - fail closed on unimplemented POSIX variants
        raise OSError(
            errno.ENOTSUP,
            "atomic no-replace rename is unavailable",
            str(destination),
        )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), str(destination))


def _restore_claim_if_absent(claim: Path, target: Path) -> bool:
    """Atomically move a claimed inode back without overwriting a newer target."""
    claimed = _snapshot_target(claim)
    if claimed.kind != "file":
        raise _ConcurrentActivationError(f"Recovery claim is not a regular file: {claim}")
    try:
        _rename_if_absent(claim, target)
    except FileExistsError:
        return False
    _claim_restoration_checkpoint("linked", target)
    if not fsync_parent_directory(target):
        raise OSError(f"Could not confirm claimed generation restoration: {target}")
    _claim_restoration_checkpoint("durable", target)
    return True


def _claim_restoration_checkpoint(_phase: str, _target: Path) -> None:
    """Test seam around atomic claim restoration and its directory durability."""


def _artifact_cleanup_checkpoint(_path: Path) -> None:
    """Test seam immediately before an artifact is atomically quarantined."""


def _remove_owned_artifact(
    path: Path,
    expected_guard: object,
    *,
    durable: bool = True,
) -> Path | None:
    """Retire only the exact owned generation at ``path``.

    Portable filesystems do not expose unlink-if-inode.  Consequently even an
    unpredictable quarantine name has a snapshot-to-unlink race against a
    same-UID directory observer.  We therefore never path-unlink here: the
    owned inode is moved to an unpredictable retired tombstone.  A last-moment
    foreign replacement is moved, identified, and restored without changing
    its inode, contents, or mode.
    """
    current = _snapshot_target(path)
    if current.kind == "missing":
        return None
    quarantine = path.parent / f".{path.name}.{uuid4().hex}.retired"
    _artifact_cleanup_checkpoint(path)
    _rename_if_absent(path, quarantine)
    quarantined = _snapshot_target(quarantine)
    if not _matches_guard(quarantined, expected_guard, strict=False):
        if not _restore_claim_if_absent(quarantine, path):
            raise _ConcurrentActivationError(
                f"Changed cleanup target and its replacement were both preserved: {path}"
            )
        raise _ConcurrentActivationError(f"Refusing to remove a changed artifact: {path}")
    # Do not unlink the retired name.  There is no portable conditional unlink,
    # and retaining a small setup-owned tombstone is safer than deleting a
    # foreign inode inserted after the guard snapshot.
    if durable and not fsync_parent_directory(path):
        raise OSError(f"Could not confirm owned artifact cleanup: {path}")
    return quarantine


def _retire_owned_secret(
    path: Path,
    expected: _FileSnapshot,
    *,
    durable: bool = True,
    allow_multiple_links: bool = False,
) -> None:
    """Move an exact setup-owned secret name into durable operator quarantine.

    Portable filesystems cannot prove that a same-UID observer did not create
    a hardlink immediately before a destructive inode mutation.  Credential
    cleanup therefore never truncates, overwrites, or path-unlinks an inode.
    It retires only the guarded name and retains the bytes in an unpredictable
    owner-only tombstone for explicit human recovery or disposal.  If link
    topology or any other guarded property changes, name retirement restores
    the observed generation and fails closed without altering its bytes.
    """
    if (
        expected.kind != "file"
        or expected.contents is None
        or expected.link_count is None
        or expected.link_count < 1
        or (expected.link_count != 1 and not allow_multiple_links)
    ):
        raise _ConcurrentActivationError(f"Secret artifact is not a regular file: {path}")
    retired = _remove_owned_artifact(path, _owned_guard(expected), durable=durable)
    if retired is not None:
        print_warning(
            "Secure credential scrubbing cannot be proven on this filesystem; "
            f"retained setup-owned bytes for explicit recovery at {retired}"
        )


def _retire_guarded_secret(
    path: Path,
    expected_guard: object,
    *,
    durable: bool = True,
) -> None:
    """Retire only a secret generation that matches its durable journal guard."""
    current = _snapshot_target(path)
    if current.kind == "missing":
        return
    if not _matches_guard(current, expected_guard, strict=False):
        raise _ConcurrentActivationError(f"Secret artifact changed concurrently: {path}")
    _retire_owned_secret(path, current, durable=durable)


def _discard_prepared(
    prepared: _PreparedFile,
    *,
    contains_secret: bool = False,
) -> None:
    """Best-effort retirement that never mutates or path-unlinks an inode."""
    try:
        if contains_secret:
            _retire_owned_secret(prepared.path, prepared.snapshot, durable=True)
        else:
            _remove_owned_artifact(
                prepared.path,
                _owned_guard(prepared.snapshot),
                durable=False,
            )
    except OSError:
        # A changed stage has already been restored to its original pathname.
        # The activation failure remains the primary error.
        pass


def _promotion_checkpoint(_operation: str, _target: Path) -> None:
    """Test seam immediately before a live target is claimed or published."""


def _post_publication_checkpoint(_target: Path) -> None:
    """Test seam after no-replace publication and before validation returns."""


def _promote_prepared_under_lock(
    target: Path,
    prepared: _PreparedFile,
    expected: _FileSnapshot,
    claim: Path,
) -> _FileSnapshot:
    """Promote one prepared generation while the cooperative lock is held.

    The caller owns the cross-platform activation lock for the entire snapshot,
    validation, and promotion interval.  This is the portable guarded-CAS
    boundary for Ouroboros writers; an external writer that ignores that lock
    is detected by the before/after generation checks but is not promised
    impossible at the exact OS rename instruction.
    """
    _require_snapshot(prepared.path, prepared.snapshot)
    if expected.kind == "file":
        _promotion_checkpoint("claim", target)
        _rename_if_absent(target, claim)
        claimed = _snapshot_target(claim)
        _claim_publication_checkpoint(target)
        if not _same_owned_generation(claimed, expected):
            if not _restore_claim_if_absent(claim, target):
                raise _ConcurrentActivationError(
                    f"Changed target was claimed and a newer generation now occupies {target}; "
                    f"preserved the claimed generation at {claim}"
                )
            raise _ConcurrentActivationError(
                f"Activation target changed before guarded promotion: {target}"
            )
    elif expected.kind != "missing":
        raise ValueError(f"Cannot promote over unsupported target kind: {expected.kind}")

    try:
        _promotion_checkpoint("publish", target)
        _rename_if_absent(prepared.path, target)
    except BaseException:
        if expected.kind == "file" and _snapshot_target(claim).kind == "file":
            _restore_claim_if_absent(claim, target)
        raise
    try:
        _post_publication_checkpoint(target)
        published = _snapshot_target(target)
    except BaseException as exc:
        try:
            observed = _snapshot_target(target)
        except OSError:
            observed = prepared.snapshot
        raise _PostPublicationError(
            f"Could not validate published generation: {target}",
            target,
            prepared.snapshot,
            observed,
        ) from exc
    if not _matches_guard(published, _owned_guard(prepared.snapshot), strict=False):
        raise _PostPublicationError(
            f"Prepared generation changed after publication: {target}",
            target,
            prepared.snapshot,
            published,
        )
    try:
        durable = fsync_parent_directory(target)
    except BaseException as exc:
        raise _PostPublicationError(
            f"Could not validate replacement durability: {target}",
            target,
            published,
            published,
        ) from exc
    if not durable:
        raise _DurabilityError(target, published)
    return published


def _claim_publication_checkpoint(_target: Path) -> None:
    """Test seam immediately after a live target is moved into its claim."""


def _atomic_write_text_if_current_matches(
    path: Path,
    content: str,
    expected: _FileSnapshot,
    *,
    mode: int,
) -> _FileSnapshot:
    """Prepare and promote one exact generation under the activation lock."""
    staging = _new_staging_path(path, uuid4().hex, path.stem)
    claim = _new_claim_path(path, uuid4().hex, path.stem)
    prepared = _prepare_file(
        staging,
        content.encode("utf-8"),
        requested_mode=mode,
        preserve_exact_mode=expected.kind == "file",
        requested_owner=(expected.owner_id, expected.group_id)
        if expected.kind == "file"
        and expected.owner_id is not None
        and expected.group_id is not None
        else None,
    )
    try:
        published = _promote_prepared_under_lock(path, prepared, expected, claim)
        # This helper is used only for a one-file rollback write.  Its old
        # generation is no longer needed once the replacement is durable.
        claimed = _snapshot_target(claim)
        if claimed.kind == "file":
            try:
                _remove_owned_artifact(claim, _owned_guard(claimed))
            except OSError as exc:
                raise _DurabilityError(path, published) from exc
        return published
    finally:
        _discard_prepared(prepared)


def _same_owned_generation(left: _FileSnapshot, right: _FileSnapshot) -> bool:
    return (
        left.kind == right.kind == "file"
        and left.device == right.device
        and left.inode == right.inode
        and left.mode == right.mode
        and left.owner_id == right.owner_id
        and left.group_id == right.group_id
        and left.link_count == right.link_count
        and left.contents == right.contents
    )


def _same_owned_inode_and_contents(left: _FileSnapshot, right: _FileSnapshot) -> bool:
    """Match one setup-owned inode while allowing only link-count growth."""
    return (
        left.kind == right.kind == "file"
        and left.device == right.device
        and left.inode == right.inode
        and left.mode == right.mode
        and left.owner_id == right.owner_id
        and left.group_id == right.group_id
        and left.contents == right.contents
        and left.link_count is not None
        and right.link_count is not None
        and left.link_count >= right.link_count
    )


def _digest(contents: bytes | None) -> str | None:
    return hashlib.sha256(contents).hexdigest() if contents is not None else None


def _strict_guard(snapshot: _FileSnapshot) -> dict[str, object]:
    return {
        "kind": snapshot.kind,
        "mode": snapshot.mode,
        "device": snapshot.device,
        "inode": snapshot.inode,
        "modified_ns": snapshot.modified_ns,
        "changed_ns": snapshot.changed_ns,
        "link_count": snapshot.link_count,
        "owner_id": snapshot.owner_id,
        "group_id": snapshot.group_id,
        "sha256": _digest(snapshot.contents),
    }


def _owned_guard(snapshot: _FileSnapshot) -> dict[str, object]:
    return {
        "kind": snapshot.kind,
        "mode": snapshot.mode,
        "device": snapshot.device,
        "inode": snapshot.inode,
        "owner_id": snapshot.owner_id,
        "group_id": snapshot.group_id,
        "link_count": snapshot.link_count,
        "sha256": _digest(snapshot.contents),
    }


def _matches_guard(snapshot: _FileSnapshot, guard: object, *, strict: bool) -> bool:
    if not isinstance(guard, dict):
        return False
    expected = _strict_guard(snapshot) if strict else _owned_guard(snapshot)
    return guard == expected


def _write_journal(
    path: Path,
    payload: dict[str, object],
    expected: _FileSnapshot,
) -> _FileSnapshot:
    _require_snapshot(path, expected)
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    staging = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    prepared = _prepare_file(
        staging,
        encoded,
        requested_mode=0o600,
        preserve_exact_mode=False,
    )
    try:
        _require_snapshot(path, expected)
        _journal_publication_checkpoint(path)
        _rename_if_absent(staging, path)
        published = _snapshot_target(path)
        if not _matches_guard(published, _owned_guard(prepared.snapshot), strict=False):
            raise _ConcurrentActivationError("Activation journal generation changed during write")
        if not fsync_parent_directory(path):
            raise _DurabilityError(path, published)
        return published
    finally:
        _discard_prepared(prepared)


def _load_journal(path: Path) -> tuple[dict[str, object], _FileSnapshot]:
    snapshot = _snapshot_target(path)
    _require_file_target(path, snapshot)
    if snapshot.kind != "file" or snapshot.contents is None:
        raise ValueError("Activation recovery journal is not a regular file")

    def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate activation journal key: {key}")
            result[key] = value
        return result

    try:
        loaded = json.loads(snapshot.contents, object_pairs_hook=_unique_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"Invalid activation recovery journal: {exc}") from exc
    if not isinstance(loaded, dict) or loaded.get("version") != _JOURNAL_VERSION:
        raise ValueError("Unsupported activation recovery journal")
    return loaded, snapshot


def _journal_publication_checkpoint(_path: Path) -> None:
    """Test seam immediately before destination-absent journal publication."""


def _journal_stage_path(config_dir: Path, journal: dict[str, object], key: str) -> Path:
    raw_name = journal.get(key)
    generation = journal.get("generation")
    label_by_key = {
        "config_stage": "config",
        "credentials_stage": "credentials",
        "config_claim": "config-original",
        "credentials_claim": "credentials-original",
        "config_rollback_claim": "config-rollback",
        "credentials_rollback_claim": "credentials-rollback",
    }
    label = label_by_key.get(key)
    if (
        not isinstance(raw_name, str)
        or not isinstance(generation, str)
        or len(generation) != 32
        or any(character not in "0123456789abcdef" for character in generation)
        or label is None
    ):
        raise ValueError("Incomplete activation recovery journal")
    suffix = "stage" if key.endswith("_stage") else "claim"
    expected_name = f".claude-runtime-activation.{generation}.{label}.{suffix}"
    if raw_name != expected_name:
        raise ValueError("Unsafe activation recovery artifact name")
    return config_dir / raw_name


def _discard_owned_target(
    path: Path,
    expected_guard: object,
    rollback_claim: Path,
    *,
    contains_secret: bool = False,
) -> None:
    """Retire only an owned live generation via atomic claim-and-inspect."""
    _promotion_checkpoint("rollback-claim", path)
    _rename_if_absent(path, rollback_claim)
    claimed = _snapshot_target(rollback_claim)
    if not _matches_guard(claimed, expected_guard, strict=False):
        if not _restore_claim_if_absent(rollback_claim, path):
            raise _ConcurrentActivationError(
                f"Changed target and a newer replacement were both preserved: {path}"
            )
        raise _ConcurrentActivationError(f"Refusing to remove a changed generation: {path}")
    if contains_secret:
        _retire_owned_secret(rollback_claim, claimed)
    else:
        _remove_owned_artifact(rollback_claim, expected_guard)


def _cleanup_journal_artifacts(
    config_dir: Path,
    journal_path: Path,
    journal: dict[str, object],
    journal_snapshot: _FileSnapshot,
) -> None:
    creates_credentials = journal.get("creates_credentials")
    if not isinstance(creates_credentials, bool):
        raise ValueError("Invalid activation recovery journal credentials state")
    artifact_specs = (
        ("credentials_stage", "credentials_published"),
        ("config_stage", "config_published"),
        ("credentials_rollback_claim", "credentials_published"),
        ("config_rollback_claim", "config_published"),
        ("credentials_claim", "credentials_original_owned"),
        ("config_claim", "config_original_owned"),
    )
    resolved: dict[str, Path] = {}
    for name_key, guard_key in artifact_specs:
        stage = _journal_stage_path(config_dir, journal, name_key)
        resolved[name_key] = stage
        current = _snapshot_target(stage)
        if current.kind == "missing":
            continue
        if not creates_credentials and name_key.startswith("credentials_"):
            raise _ConcurrentActivationError(
                f"Unexpected credential artifact for preserved credentials: {stage}"
            )
        guard = journal.get(guard_key)
        if not _matches_guard(current, guard, strict=False):
            raise _ConcurrentActivationError(f"Recovery artifact changed concurrently: {stage}")

    # Disposable stage/published-rollback names go first.  The journal and
    # original-generation claims remain live until this retirement is durable,
    # so a failure can still roll the activation back without losing config.
    for name_key in (
        "credentials_stage",
        "config_stage",
        "credentials_rollback_claim",
        "config_rollback_claim",
    ):
        path = resolved[name_key]
        guard_key = dict(artifact_specs)[name_key]
        if name_key in {"credentials_stage", "credentials_rollback_claim"}:
            _retire_guarded_secret(
                path,
                journal.get(guard_key),
                durable=False,
            )
        else:
            current = _snapshot_target(path)
            if current.kind != "missing":
                _remove_owned_artifact(path, journal.get(guard_key), durable=False)
    if not fsync_parent_directory(journal_path):
        raise OSError("Could not confirm disposable activation cleanup durability")

    # Past this point config is the durable commit.  Cleanup failures must not
    # enter rollback because original claims may already be gone.
    try:
        for name_key in ("credentials_claim", "config_claim"):
            path = resolved[name_key]
            current = _snapshot_target(path)
            if current.kind != "missing":
                guard_key = dict(artifact_specs)[name_key]
                _remove_owned_artifact(path, journal.get(guard_key), durable=False)
        if not fsync_parent_directory(journal_path):
            raise OSError("Could not confirm original-claim cleanup durability")
        _remove_owned_artifact(
            journal_path,
            _owned_guard(journal_snapshot),
            durable=False,
        )
        if not fsync_parent_directory(journal_path):
            raise OSError("Could not confirm activation journal cleanup durability")
    except OSError as exc:
        raise _CommittedCleanupError(str(exc)) from exc


def _recover_interrupted_activation(config_dir: Path) -> None:
    """Recover the only partial state: fresh credentials before config commit."""
    journal_path = config_dir / _JOURNAL_NAME
    if _snapshot_target(journal_path).kind == "missing":
        return
    journal, journal_snapshot = _load_journal(journal_path)
    config_path = config_dir / "config.yaml"
    credentials_path = config_dir / "credentials.yaml"
    config_claim = _journal_stage_path(config_dir, journal, "config_claim")
    credentials_rollback_claim = _journal_stage_path(
        config_dir, journal, "credentials_rollback_claim"
    )
    config_now = _snapshot_target(config_path)
    credentials_now = _snapshot_target(credentials_path)
    config_is_original = _matches_guard(
        config_now, journal.get("config_original"), strict=True
    ) or _matches_guard(
        config_now,
        journal.get("config_original_owned"),
        strict=False,
    )
    config_is_published = _matches_guard(config_now, journal.get("config_published"), strict=False)
    credentials_is_original = _matches_guard(
        credentials_now, journal.get("credentials_original"), strict=True
    ) or _matches_guard(
        credentials_now,
        journal.get("credentials_original_owned"),
        strict=False,
    )
    credentials_is_published = _matches_guard(
        credentials_now, journal.get("credentials_published"), strict=False
    )
    raw_creates_credentials = journal.get("creates_credentials")
    if not isinstance(raw_creates_credentials, bool):
        raise ValueError("Invalid activation recovery journal credentials state")
    creates_credentials = raw_creates_credentials

    # A crash can occur after atomically claiming the old config and before
    # conditionally publishing the new one.  Restore that exact claimed inode
    # only if no newer operator generation occupies the destination.
    claimed_config = _snapshot_target(config_claim)
    if (
        config_now.kind == "missing"
        and claimed_config.kind == "file"
        and _matches_guard(
            claimed_config,
            journal.get("config_original_owned"),
            strict=False,
        )
    ):
        if not _restore_claim_if_absent(config_claim, config_path):
            raise _ConcurrentActivationError(
                "A newer config generation prevented interrupted-claim recovery"
            )
        config_now = _snapshot_target(config_path)
        config_is_original = True

    if config_is_published and (credentials_is_published or not creates_credentials):
        # Config is the commit point.  A crash after its atomic promotion but
        # before journal cleanup is a committed activation, not partial state.
        _cleanup_journal_artifacts(config_dir, journal_path, journal, journal_snapshot)
        return
    if config_is_original and (credentials_is_original or not creates_credentials):
        _cleanup_journal_artifacts(config_dir, journal_path, journal, journal_snapshot)
        return
    if config_is_original and creates_credentials and credentials_is_published:
        _discard_owned_target(
            credentials_path,
            journal.get("credentials_published"),
            credentials_rollback_claim,
            contains_secret=True,
        )
        _cleanup_journal_artifacts(config_dir, journal_path, journal, journal_snapshot)
        return
    raise _ConcurrentActivationError(
        "Interrupted activation overlaps a non-cooperative config/credential generation; "
        "preserved all files and the recovery journal"
    )


def _rollback_file(
    path: Path,
    original: _FileSnapshot,
    expected_current: _FileSnapshot | None,
    *,
    original_claim: Path,
    rollback_claim: Path,
    retire_created_secret: bool = False,
) -> bool:
    if expected_current is None:
        return True
    try:
        _promotion_checkpoint("rollback-claim", path)
        _rename_if_absent(path, rollback_claim)
        current = _snapshot_target(rollback_claim)
        if not _same_owned_generation(current, expected_current):
            if not _restore_claim_if_absent(rollback_claim, path):
                raise _ConcurrentActivationError(
                    f"Changed rollback target and its replacement were both preserved: {path}"
                )
            raise _ConcurrentActivationError(f"Activation rollback generation changed: {path}")
        if original.kind == "missing":
            # The rollback claim is the activation-owned published generation.
            if retire_created_secret:
                _retire_owned_secret(
                    rollback_claim,
                    current,
                    allow_multiple_links=True,
                )
            else:
                _remove_owned_artifact(rollback_claim, _owned_guard(current))
            return True
        if original.kind != "file" or original.contents is None or original.mode is None:
            raise OSError(f"Cannot restore unsupported activation target: {path}")
        claimed_original = _snapshot_target(original_claim)
        if not _same_owned_generation(claimed_original, original):
            raise _ConcurrentActivationError(f"Original rollback claim changed: {original_claim}")
        if not _restore_claim_if_absent(original_claim, path):
            raise _ConcurrentActivationError(f"Newer generation prevented rollback: {path}")
        _remove_owned_artifact(rollback_claim, _owned_guard(current))
        return True
    except OSError as exc:
        print_warning(f"Preserved current {path.name}; activation rollback was incomplete: {exc}")
        return False


@dataclass(frozen=True)
class _CreatedDirectory:
    """Identity used to prove that a just-created directory stayed selected.

    Directories are deliberately retained on activation failure. POSIX and
    Windows expose no portable rmdir-if-inode primitive, so pathname cleanup
    would reintroduce the foreign-victim deletion race this authority prevents.
    """

    name: str
    device: int
    inode: int


def _directory_authority_checkpoint(_phase: str, _path: Path) -> None:
    """Test seam around no-follow activation-directory creation and binding."""


def _directory_identity(result: os.stat_result) -> tuple[int, int]:
    return result.st_dev, result.st_ino


def _require_real_directory(result: os.stat_result, path: Path) -> None:
    if not stat.S_ISDIR(result.st_mode):
        raise ValueError(f"Activation directory must be a real directory: {path}")


def _select_activation_parent(parent: Path) -> tuple[Path, tuple[int, int]]:
    """Resolve one requested home parent and prove the selected generation.

    The user-visible home may itself be a POSIX symlink or Windows junction.
    Descendant mutations use the resolved physical parent, while the original
    binding remains part of the active authority and is revalidated throughout
    activation.  The selected directory and the requested path must identify
    the same generation before any lockfile or configuration mutation.
    """
    selected = parent.resolve(strict=True)
    _directory_authority_checkpoint("parent-resolved", parent)
    selected_result = os.stat(selected, follow_symlinks=False)
    requested_result = os.stat(parent)
    _require_real_directory(selected_result, selected)
    _require_real_directory(requested_result, parent)
    selected_identity = _directory_identity(selected_result)
    if _directory_identity(requested_result) != selected_identity:
        raise _ConcurrentActivationError(f"Activation parent changed: {parent}")
    return selected, selected_identity


def _require_selected_parent_binding(
    requested: Path,
    selected: Path,
    expected_identity: tuple[int, int],
) -> None:
    """Revalidate both names for a previously selected parent generation."""
    selected_result = os.stat(selected, follow_symlinks=False)
    requested_result = os.stat(requested)
    _require_real_directory(selected_result, selected)
    _require_real_directory(requested_result, requested)
    if (
        _directory_identity(selected_result) != expected_identity
        or _directory_identity(requested_result) != expected_identity
    ):
        raise _ConcurrentActivationError(f"Activation parent changed: {requested}")


def _fsync_directory_descriptor(descriptor: int, path: Path) -> None:
    """Confirm one directory-entry mutation when the filesystem supports it."""
    try:
        os.fsync(descriptor)
    except OSError as error:
        if error.errno not in (errno.EINVAL, errno.ENOTSUP):
            raise OSError(f"Could not confirm activation directory durability: {path}") from error


def _open_directory_at(parent_fd: int, name: str, path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(name, flags, dir_fd=parent_fd)
    try:
        opened = os.fstat(descriptor)
        _directory_authority_checkpoint("opened", path)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        _require_real_directory(opened, path)
        _require_real_directory(current, path)
        if _directory_identity(opened) != _directory_identity(current):
            raise _ConcurrentActivationError(f"Activation directory changed: {path}")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_or_create_directory_at(
    parent_fd: int,
    name: str,
    path: Path,
) -> tuple[int, _CreatedDirectory | None]:
    """Select and pin one real directory before any descendant mutation."""
    try:
        return _open_directory_at(parent_fd, name, path), None
    except FileNotFoundError:
        _directory_authority_checkpoint("before-create", path)
        try:
            os.mkdir(name, mode=0o777, dir_fd=parent_fd)
        except FileExistsError as exc:
            raise _ConcurrentActivationError(
                f"Activation directory appeared concurrently: {path}"
            ) from exc
        created = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        _require_real_directory(created, path)
        created_directory = _CreatedDirectory(name, created.st_dev, created.st_ino)
        _directory_authority_checkpoint("created-snapshotted", path)
        descriptor = _open_directory_at(parent_fd, name, path)
        if _directory_identity(os.fstat(descriptor)) != (
            created_directory.device,
            created_directory.inode,
        ):
            os.close(descriptor)
            raise _ConcurrentActivationError(f"Created activation directory changed: {path}")
        _fsync_directory_descriptor(parent_fd, path.parent)
        return descriptor, created_directory


@contextmanager
def _activation_directory_authority(config_dir: Path) -> Iterator[Path]:
    """Resolve the home parent, then pin a no-follow activation-directory leaf."""
    requested_parent = config_dir.parent
    selected_parent, parent_identity = _select_activation_parent(requested_parent)
    selected_config_dir = selected_parent / config_dir.name
    lock_target = selected_parent / _ACTIVATION_LOCK_NAME

    if os.name == "nt":  # pragma: no cover - exercised on Windows
        # Resolve a junction-backed home before acquiring leases. The lock
        # authority then pins the physical parent, and a second lease on the
        # non-reparse config-directory leaf prevents rename/delete topology
        # swaps while descendant operations remain path based on Windows.
        before = _snapshot_target(selected_config_dir)
        if before.kind not in {"missing", "directory"}:
            raise ValueError(f"Activation directory must be real: {selected_config_dir}")
        with file_lock(lock_target, stable_parent_authority=True):
            _require_selected_parent_binding(
                requested_parent,
                selected_parent,
                parent_identity,
            )
            created: _FileSnapshot | None = None
            if before.kind == "missing":
                _directory_authority_checkpoint("before-create", selected_config_dir)
                try:
                    selected_config_dir.mkdir()
                except FileExistsError as exc:
                    raise _ConcurrentActivationError(
                        f"Activation directory appeared concurrently: {selected_config_dir}"
                    ) from exc
                created = _snapshot_target(selected_config_dir)
                if created.kind != "directory":
                    raise _ConcurrentActivationError(
                        f"Created activation directory changed: {selected_config_dir}"
                    )
                _directory_authority_checkpoint("created-snapshotted", selected_config_dir)
            with _windows_directory_lease(selected_config_dir):
                selected = _snapshot_target(selected_config_dir)
                if selected.kind != "directory":
                    raise _ConcurrentActivationError(
                        f"Activation directory changed: {selected_config_dir}"
                    )
                if before.kind == "directory" and selected != before:
                    raise _ConcurrentActivationError(
                        f"Activation directory changed: {selected_config_dir}"
                    )
                if created is not None and selected != created:
                    raise _ConcurrentActivationError(
                        f"Created activation directory changed: {selected_config_dir}"
                    )
                if selected.device is None or selected.inode is None:
                    raise _ConcurrentActivationError(
                        f"Activation directory identity unavailable: {selected_config_dir}"
                    )
                for child in (selected_config_dir / "data", selected_config_dir / "logs"):
                    child_before = _snapshot_target(child)
                    if child_before.kind == "missing":
                        child.mkdir()
                    elif child_before.kind != "directory":
                        raise ValueError(f"Activation directory must be real: {child}")
                token = _ACTIVE_DIRECTORY_AUTHORITY.set(
                    _DirectoryAuthority(
                        selected_config_dir,
                        None,
                        None,
                        requested_parent,
                        (selected.device, selected.inode),
                        parent_identity,
                    )
                )
                try:
                    _require_active_directory_binding()
                    yield selected_config_dir
                    _require_active_directory_binding()
                finally:
                    _ACTIVE_DIRECTORY_AUTHORITY.reset(token)
        return

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    parent_fd = os.open(selected_parent, flags)
    config_fd = -1
    child_descriptors: list[int] = []
    try:
        opened_parent = os.fstat(parent_fd)
        _require_real_directory(opened_parent, selected_parent)
        if _directory_identity(opened_parent) != parent_identity:
            raise _ConcurrentActivationError(f"Activation parent changed: {requested_parent}")
        _require_selected_parent_binding(
            requested_parent,
            selected_parent,
            parent_identity,
        )
        with file_lock(
            lock_target,
            parent_fd=parent_fd,
            stable_parent_authority=True,
        ):
            _require_selected_parent_binding(
                requested_parent,
                selected_parent,
                parent_identity,
            )
            config_fd, _created_config = _open_or_create_directory_at(
                parent_fd, selected_config_dir.name, selected_config_dir
            )
            for child_name in ("data", "logs"):
                child_fd, _created_child = _open_or_create_directory_at(
                    config_fd,
                    child_name,
                    selected_config_dir / child_name,
                )
                child_descriptors.append(child_fd)
            directory_identity = _directory_identity(os.fstat(config_fd))
            token = _ACTIVE_DIRECTORY_AUTHORITY.set(
                _DirectoryAuthority(
                    selected_config_dir,
                    config_fd,
                    parent_fd,
                    requested_parent,
                    directory_identity,
                    parent_identity,
                )
            )
            try:
                _require_active_directory_binding()
                yield selected_config_dir
                _require_active_directory_binding()
            finally:
                _ACTIVE_DIRECTORY_AUTHORITY.reset(token)
    finally:
        for descriptor in reversed(child_descriptors):
            os.close(descriptor)
        if config_fd >= 0:
            os.close(config_fd)
        os.close(parent_fd)


def _publication_checkpoint(_name: str) -> None:
    """Test seam for deterministic process-death recovery checks."""


def activate_claude_runtime(
    claude_path: str,
    *,
    runtime_backend: Literal["claude", "claude_mcp"] = "claude",
) -> Path | None:
    """Activate one Claude profile plus credentials, or fail closed."""
    from ouroboros.config.models import (
        CredentialsConfig,
        OuroborosConfig,
        get_config_dir,
        get_default_config,
        get_default_credentials,
    )

    config_dir_candidate = get_config_dir()
    requested_config_path = config_dir_candidate / "config.yaml"
    try:
        with _activation_directory_authority(config_dir_candidate) as config_dir:
            _require_active_directory_binding()
            _recover_interrupted_activation(config_dir)
            config_path = config_dir / "config.yaml"
            credentials_path = config_dir / "credentials.yaml"
            config_generation = _snapshot_target(config_path)
            credentials_generation = _snapshot_target(credentials_path)
            _require_file_target(config_path, config_generation)
            _require_file_target(credentials_path, credentials_generation)

            config = (
                get_default_config().model_dump(mode="json")
                if config_generation.kind == "missing"
                else _yaml_mapping(config_path, config_generation)
            )
            if config_generation.kind == "file":
                OuroborosConfig.model_validate(config)
            credentials = (
                get_default_credentials().model_dump(mode="json")
                if credentials_generation.kind == "missing"
                else _yaml_mapping(credentials_path, credentials_generation)
            )
            if credentials_generation.kind == "file":
                CredentialsConfig.model_validate(credentials)

            orchestrator = config.get("orchestrator")
            if orchestrator is None:
                orchestrator = {}
                config["orchestrator"] = orchestrator
            if not isinstance(orchestrator, dict):
                raise ValueError("Invalid non-mapping 'orchestrator' section in config.yaml")
            orchestrator["runtime_backend"] = runtime_backend
            orchestrator["cli_path"] = claude_path
            llm = config.get("llm")
            if llm is None:
                llm = {}
                config["llm"] = llm
            if not isinstance(llm, dict):
                raise ValueError("Invalid non-mapping 'llm' section in config.yaml")
            llm["backend"] = "claude"
            OuroborosConfig.model_validate(config)
            config_content = yaml.dump(config, default_flow_style=False, sort_keys=False)
            credentials_content = yaml.dump(credentials, default_flow_style=False, sort_keys=False)

            generation = uuid4().hex
            config_stage = _new_staging_path(config_path, generation, "config")
            credentials_stage = _new_staging_path(credentials_path, generation, "credentials")
            config_claim = _new_claim_path(config_path, generation, "config-original")
            credentials_claim = _new_claim_path(
                credentials_path, generation, "credentials-original"
            )
            config_rollback_claim = _new_claim_path(config_path, generation, "config-rollback")
            credentials_rollback_claim = _new_claim_path(
                credentials_path, generation, "credentials-rollback"
            )
            for reserved_claim in (
                config_claim,
                credentials_claim,
                config_rollback_claim,
                credentials_rollback_claim,
            ):
                if _snapshot_target(reserved_claim).kind != "missing":
                    raise _ConcurrentActivationError(
                        f"Activation claim path is already occupied: {reserved_claim}"
                    )
            prepared_files: list[_PreparedFile] = []
            prepared_credentials: _PreparedFile | None = None
            try:
                prepared_config = _prepare_file(
                    config_stage,
                    config_content.encode(),
                    requested_mode=(
                        config_generation.mode if config_generation.mode is not None else 0o644
                    ),
                    preserve_exact_mode=config_generation.kind == "file",
                    requested_owner=(config_generation.owner_id, config_generation.group_id)
                    if config_generation.kind == "file"
                    and config_generation.owner_id is not None
                    and config_generation.group_id is not None
                    else None,
                )
                prepared_files.append(prepared_config)
                if credentials_generation.kind == "missing":
                    prepared_credentials = _prepare_file(
                        credentials_stage,
                        credentials_content.encode(),
                        requested_mode=0o600,
                        preserve_exact_mode=False,
                        contains_secret=True,
                    )
                    prepared_files.append(prepared_credentials)
                if not fsync_parent_directory(config_stage):
                    raise OSError("Could not confirm prepared activation durability")
            except BaseException:
                for prepared in prepared_files:
                    _discard_prepared(
                        prepared,
                        contains_secret=prepared is prepared_credentials,
                    )
                raise

            journal_path = config_dir / _JOURNAL_NAME
            # Both staged contents and their directory entries are durable
            # before this journal becomes durable.  Only then may credentials
            # be published.  Config is published last and is the commit point.
            journal = {
                "version": _JOURNAL_VERSION,
                "generation": generation,
                "creates_credentials": credentials_generation.kind == "missing",
                "config_original": _strict_guard(config_generation),
                "config_original_owned": _owned_guard(config_generation),
                "config_published": _owned_guard(prepared_config.snapshot),
                "credentials_original": _strict_guard(credentials_generation),
                "credentials_original_owned": _owned_guard(credentials_generation),
                "credentials_published": _owned_guard(
                    prepared_credentials.snapshot
                    if prepared_credentials is not None
                    else credentials_generation
                ),
                "config_stage": config_stage.name,
                "credentials_stage": credentials_stage.name,
                "config_claim": config_claim.name,
                "credentials_claim": credentials_claim.name,
                "config_rollback_claim": config_rollback_claim.name,
                "credentials_rollback_claim": credentials_rollback_claim.name,
            }
            try:
                _require_active_directory_binding()
                journal_snapshot = _write_journal(
                    journal_path,
                    journal,
                    _FileSnapshot(kind="missing"),
                )
            except BaseException:
                for prepared in prepared_files:
                    _discard_prepared(
                        prepared,
                        contains_secret=prepared is prepared_credentials,
                    )
                raise

            config_written: _FileSnapshot | None = None
            credentials_written: _FileSnapshot | None = None
            try:
                if credentials_generation.kind == "missing":
                    if prepared_credentials is None:
                        raise OSError("Missing prepared credentials generation")
                    _require_active_directory_binding()
                    credentials_written = _promote_prepared_under_lock(
                        credentials_path,
                        prepared_credentials,
                        credentials_generation,
                        credentials_claim,
                    )
                    _publication_checkpoint("credentials")
                else:
                    _require_snapshot(credentials_path, credentials_generation)
                _require_active_directory_binding()
                config_written = _promote_prepared_under_lock(
                    config_path,
                    prepared_config,
                    config_generation,
                    config_claim,
                )
                _publication_checkpoint("config")
                expected_credentials = credentials_written or credentials_generation
                _require_snapshot(credentials_path, expected_credentials)
                _require_snapshot(config_path, config_written)
                _require_active_directory_binding()
                _cleanup_journal_artifacts(
                    config_dir,
                    journal_path,
                    journal,
                    journal_snapshot,
                )
                return requested_config_path
            except _CommittedCleanupError as exc:
                print_warning(
                    "Claude configuration committed; recovery-artifact cleanup "
                    f"will be retried on the next invocation: {exc}"
                )
                return requested_config_path
            except BaseException as exc:
                if isinstance(exc, _PostPublicationError):
                    rollback_snapshot = (
                        exc.observed_snapshot
                        if _same_owned_inode_and_contents(
                            exc.observed_snapshot,
                            exc.owned_snapshot,
                        )
                        else exc.owned_snapshot
                    )
                    if exc.path == config_path:
                        config_written = rollback_snapshot
                    elif exc.path == credentials_path:
                        credentials_written = rollback_snapshot
                config_restored = _rollback_file(
                    config_path,
                    config_generation,
                    config_written,
                    original_claim=config_claim,
                    rollback_claim=config_rollback_claim,
                )
                credentials_restored = _rollback_file(
                    credentials_path,
                    credentials_generation,
                    credentials_written,
                    original_claim=credentials_claim,
                    rollback_claim=credentials_rollback_claim,
                    retire_created_secret=True,
                )
                # Rollback may already have restored both original generations.
                # Let the durable journal verify that fact and clean only owned
                # artifacts.  If an external writer intervened it is preserved.
                if config_restored and credentials_restored:
                    _cleanup_journal_artifacts(
                        config_dir,
                        journal_path,
                        journal,
                        journal_snapshot,
                    )
                else:
                    _recover_interrupted_activation(config_dir)
                raise
    except (OSError, ValueError, RecursionError) as exc:
        print_error(f"Claude setup activation failed; aborting safely: {exc}")
        return None
