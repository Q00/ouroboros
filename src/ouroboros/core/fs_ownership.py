"""Claim-then-verify primitives for publishing and removing owned artifacts.

A component that installs artifacts into directories it shares with operators
(runtime skill registries, bridge extensions, instruction guides) may replace
or delete only the exact generations it produced. These primitives are the
single security boundary for that contract; callers must not reimplement any
part of the claim/verify/publish sequence.

Threat model and defenses:

* **Stale ownership checks.** A check and a later destructive operation
  cannot be safely separated in time. Every mutation first *claims* the
  existing entry with an atomic rename to an unpredictable sibling name,
  re-validates the claimed generation, and only then deletes or replaces it.
* **Symlinked artifacts.** A symlink planted at the canonical path is
  detected on the claimed entry (rename moves the link itself, never its
  target), so a link can never route a write to its target.
* **Symlinked parent directories.** Mutations run relative to a pinned
  directory descriptor. The caller-supplied *trusted ancestor* itself is
  opened with ``O_NOFOLLOW`` — a symlinked configured root (for example a
  redirected ``GJC_CODING_AGENT_DIR``) is rejected outright — and the chain
  from it down to the artifact's parent is opened one component at a time
  with ``O_NOFOLLOW``, so an operator-controlled symlink such as
  ``<profile>/ouroboros -> /external`` cannot redirect publication or
  removal outside the profile. On platforms without ``*at`` support the
  chain is validated with per-component ``lstat`` best effort instead.
* **Publication clobbering.** The final publish rename is no-replace:
  ``renameat2(RENAME_NOREPLACE)`` on Linux, ``renameatx_np(RENAME_EXCL)``
  on macOS, native no-replace ``rename`` on Windows, and an
  existence-guarded rename as a last resort. A generation recreated at the
  canonical path after ownership validation is preserved and the
  publication fails instead of overwriting it.
* **Restoration clobbering.** When another process recreates the canonical
  path while a claim is held, restoring the claim must not overwrite the
  new generation: both are preserved — the recreated entry stays canonical
  and the claimed generation remains beside it under its claim name.
* **Claim-identity races.** The claimed entry's inode identity is captured
  immediately after the claim rename and re-checked after the ownership
  predicate and again inside the deletion itself, so a writer that swaps
  the claimed sibling between the check and the mutation cannot have an
  unrelated generation deleted on the strength of a stale predicate. The
  final ``unlink``/``rmtree`` syscall itself is the platform's atomicity
  limit; the unpredictable claim name closes the remaining window.
* **Crash and partial state.** A claim name is a durable, self-describing
  intent record (``.{name}.{nonce}.{removing|replacing}``). A process that
  dies mid-transaction leaves the generation under that sibling name; every
  later transaction on the same path — and :func:`recover_owned_claims`
  directly — first reconciles such orphans: a claim whose canonical path is
  absent is restored (no-clobber) so it stays discoverable, and a leftover
  claim beside an occupied canonical path is deleted only when the caller's
  ownership predicate confirms it. Parent-directory ``fsync`` after the
  claim, restoration, and publication is best effort (errors are
  suppressed); on filesystems that lose the rename, recovery simply finds
  the pre-claim state, so crash consistency never depends on the fsync.

This adapts the journaled swap-intent/recovery design of
:mod:`ouroboros.hermes.artifacts` — the claim name *is* the intent record —
without its separate journal files. Fully consolidating hermes and the
fingerprint-gated replacement in :mod:`ouroboros.codex.artifacts` onto these
primitives is a candidate follow-up, not something callers should assume has
happened.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
import ctypes
import errno
import os
from pathlib import Path
import re
import shutil
import stat as stat_module
import tempfile


class UnownedArtifactError(OSError):
    """The target of a publication or removal is not the caller-owned generation."""


_DIR_FD_SUPPORTED = (
    os.name == "posix"
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and os.open in os.supports_dir_fd
    and os.rename in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.mkdir in os.supports_dir_fd
    and os.unlink in os.supports_dir_fd
    and os.stat in os.supports_follow_symlinks
)
_LINK_DIR_FD_SUPPORTED = _DIR_FD_SUPPORTED and os.link in os.supports_dir_fd

_RENAME_NOREPLACE_LINUX = 1  # include/uapi/linux/fs.h RENAME_NOREPLACE
_RENAME_EXCL_DARWIN = 0x00000004  # sys/stdio.h RENAME_EXCL


def _load_rename_no_replace() -> Callable[[int, str, int, str], int] | None:
    """Resolve the platform's atomic no-replace renameat variant, if any."""
    if os.name != "posix":
        return None
    try:
        libc = ctypes.CDLL(None, use_errno=True)
    except OSError:
        return None
    import sys

    if sys.platform == "linux" and hasattr(libc, "renameat2"):
        renameat2 = libc.renameat2

        def _linux(src_fd: int, src: str, dst_fd: int, dst: str) -> int:
            return int(
                renameat2(
                    src_fd,
                    src.encode(),
                    dst_fd,
                    dst.encode(),
                    _RENAME_NOREPLACE_LINUX,
                )
            )

        return _linux
    if sys.platform == "darwin" and hasattr(libc, "renameatx_np"):
        renameatx_np = libc.renameatx_np

        def _darwin(src_fd: int, src: str, dst_fd: int, dst: str) -> int:
            return int(
                renameatx_np(src_fd, src.encode(), dst_fd, dst.encode(), _RENAME_EXCL_DARWIN)
            )

        return _darwin
    return None


_RENAME_NO_REPLACE = _load_rename_no_replace()


class _PinnedParent:
    """One held parent-directory capability for name-relative mutations."""

    __slots__ = ("fd", "path")

    def __init__(self, path: Path, fd: int | None) -> None:
        self.path = path
        self.fd = fd

    def revalidate(self) -> None:
        """Fail closed if the canonical parent path no longer names the pinned inode.

        Ownership predicates and tree builds read through the canonical
        pathname; this check brackets those reads so a parent renamed away and
        replaced by a symlink or substitute directory cannot redirect them.
        """
        if self.fd is None:
            return
        try:
            opened = os.fstat(self.fd)
            current = os.stat(self.path)
        except OSError as exc:
            raise OSError(
                getattr(errno, "ESTALE", errno.EIO),
                "artifact parent directory changed during mutation",
                str(self.path),
            ) from exc
        if not stat_module.S_ISDIR(current.st_mode) or (
            opened.st_dev,
            opened.st_ino,
        ) != (current.st_dev, current.st_ino):
            raise OSError(
                getattr(errno, "ESTALE", errno.EIO),
                "artifact parent directory changed during mutation",
                str(self.path),
            )

    def is_symlink(self, name: str) -> bool:
        if self.fd is None:
            return (self.path / name).is_symlink()
        try:
            return stat_module.S_ISLNK(os.stat(name, dir_fd=self.fd, follow_symlinks=False).st_mode)
        except OSError:
            return False

    def entry_identity(self, name: str) -> tuple[int, int, int] | None:
        """(device, inode, file-type) of one entry without following links."""
        try:
            if self.fd is None:
                entry = os.lstat(self.path / name)
            else:
                entry = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        except OSError:
            return None
        return (entry.st_dev, entry.st_ino, stat_module.S_IFMT(entry.st_mode))

    def remove_entry(
        self, name: str, expected_identity: tuple[int, int, int] | None = None
    ) -> None:
        """Delete one held entry relative to the pinned descriptor.

        With *expected_identity*, the deletion is bound to the exact inode the
        caller validated: an entry that no longer carries that identity raises
        :class:`UnownedArtifactError` instead of being deleted.
        """
        if self.fd is None:
            if expected_identity is not None and self.entry_identity(name) != expected_identity:
                raise UnownedArtifactError(
                    f"claimed generation changed identity before removal: {self.path / name}"
                )
            remove_path(self.path / name)
            return
        try:
            entry = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            if expected_identity is not None:
                raise UnownedArtifactError(
                    f"claimed generation disappeared before removal: {self.path / name}"
                ) from None
            return
        identity = (entry.st_dev, entry.st_ino, stat_module.S_IFMT(entry.st_mode))
        if expected_identity is not None and identity != expected_identity:
            raise UnownedArtifactError(
                f"claimed generation changed identity before removal: {self.path / name}"
            )
        if stat_module.S_ISDIR(entry.st_mode):
            shutil.rmtree(name, dir_fd=self.fd)
        else:
            os.unlink(name, dir_fd=self.fd)

    def make_staging_dir(self, prefix: str) -> str:
        if self.fd is None:
            return Path(tempfile.mkdtemp(prefix=prefix, suffix=".tmp", dir=str(self.path))).name
        name = f"{prefix}{os.urandom(8).hex()}.tmp"
        os.mkdir(name, mode=0o700, dir_fd=self.fd)
        return name

    def lexists(self, name: str) -> bool:
        if self.fd is None:
            return os.path.lexists(self.path / name)
        try:
            os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        except (FileNotFoundError, NotADirectoryError):
            return False
        return True

    def is_directory(self, name: str) -> bool:
        if self.fd is None:
            candidate = self.path / name
            return not candidate.is_symlink() and candidate.is_dir()
        try:
            return stat_module.S_ISDIR(os.stat(name, dir_fd=self.fd, follow_symlinks=False).st_mode)
        except OSError:
            return False

    def replace(self, source_name: str, target_name: str) -> None:
        if self.fd is None:
            os.replace(self.path / source_name, self.path / target_name)
            return
        os.rename(source_name, target_name, src_dir_fd=self.fd, dst_dir_fd=self.fd)

    def rename_no_replace(self, source_name: str, target_name: str) -> None:
        """Atomically rename; raise FileExistsError when the target exists."""
        if self.fd is not None and _RENAME_NO_REPLACE is not None:
            result = _RENAME_NO_REPLACE(self.fd, source_name, self.fd, target_name)
            if result == 0:
                return
            code = ctypes.get_errno()
            if code in (errno.EEXIST, errno.ENOTEMPTY):
                raise FileExistsError(code, os.strerror(code), target_name)
            if code not in (errno.ENOSYS, errno.EINVAL):
                raise OSError(code, os.strerror(code), target_name)
        if os.name == "nt":  # pragma: no cover - Windows rename is natively no-replace
            os.rename(self.path / source_name, self.path / target_name)
            return
        # Last-resort guarded rename: the window between the check and the
        # rename is not atomic, but every supported platform takes one of the
        # atomic branches above.
        if self.lexists(target_name):
            raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), target_name)
        self.replace(source_name, target_name)

    def link_no_clobber(self, source_name: str, target_name: str) -> bool:
        """Atomically link *source_name* to *target_name*; False when it exists."""
        if not _LINK_DIR_FD_SUPPORTED or self.fd is None:
            raise NotImplementedError
        try:
            os.link(
                source_name,
                target_name,
                src_dir_fd=self.fd,
                dst_dir_fd=self.fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            return False
        os.unlink(source_name, dir_fd=self.fd)
        return True

    def create_temp_file(self, prefix: str) -> tuple[int, str]:
        if self.fd is None:
            return tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=str(self.path))
        name = f"{prefix}{os.urandom(8).hex()}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        return os.open(name, flags, 0o600, dir_fd=self.fd), name

    def unlink(self, name: str) -> None:
        with suppress(OSError):
            if self.fd is None:
                (self.path / name).unlink()
            else:
                os.unlink(name, dir_fd=self.fd)

    def fsync(self) -> None:
        if self.fd is None:
            return
        with suppress(OSError):
            os.fsync(self.fd)


def _nofollow_components(
    parent: Path, trusted_ancestor: Path | None
) -> tuple[Path, list[str], bool]:
    """Split *parent* into a base, the components to walk no-follow, and whether
    the base is a caller-declared trusted root that must itself be pinned."""
    if trusted_ancestor is None:
        return parent.parent, [parent.name], False
    ancestor = Path(trusted_ancestor)
    try:
        relative = parent.relative_to(ancestor)
    except ValueError:
        return parent.parent, [parent.name], False
    return ancestor, [part for part in relative.parts if part not in ("", ".")], True


def _symlink_component_error(component: Path) -> OSError:
    return OSError(
        errno.ELOOP,
        "refusing to operate through a symlinked artifact parent component",
        str(component),
    )


def _symlink_trusted_root_error(root: Path) -> OSError:
    return OSError(
        errno.ELOOP,
        "refusing to operate under a symlinked trusted root",
        str(root),
    )


@contextmanager
def _pinned_parent(
    parent: Path,
    *,
    trusted_ancestor: Path | None,
    create: bool,
) -> Iterator[_PinnedParent]:
    """Pin *parent*, walking every component below the trusted ancestor no-follow.

    A caller-declared trusted root is itself opened ``O_NOFOLLOW``: a symlink
    planted at (or configured as) that root is rejected instead of silently
    redirecting every mutation into its target.
    """
    base, components, base_is_trusted_root = _nofollow_components(parent, trusted_ancestor)
    if not _DIR_FD_SUPPORTED:
        if base_is_trusted_root and base.is_symlink():  # pragma: no cover - non-posix
            raise _symlink_trusted_root_error(base)
        current = base
        for name in components:  # pragma: no cover - non-posix best effort
            current = current / name
            if current.is_symlink():
                raise _symlink_component_error(current)
            if create:
                current.mkdir(exist_ok=True)
        yield _PinnedParent(path=parent, fd=None)
        return

    if create:
        base.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    nofollow_flags = flags | os.O_NOFOLLOW
    try:
        fd = os.open(base, nofollow_flags if base_is_trusted_root else flags)
    except OSError as exc:
        if base_is_trusted_root and exc.errno in (errno.ELOOP, errno.EMLINK, errno.ENOTDIR):
            raise _symlink_trusted_root_error(base) from exc
        raise
    try:
        walked = base
        for name in components:
            walked = walked / name
            if create:
                with suppress(FileExistsError):
                    os.mkdir(name, mode=0o755, dir_fd=fd)
            try:
                child = os.open(name, nofollow_flags, dir_fd=fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.EMLINK, errno.ENOTDIR):
                    raise _symlink_component_error(walked) from exc
                raise
            os.close(fd)
            fd = child
        yield _PinnedParent(path=parent, fd=fd)
    finally:
        os.close(fd)


def _claim_name(name: str, operation: str) -> str:
    return f".{name}.{os.urandom(8).hex()}.{operation}"


_CLAIM_NAME_PATTERN = re.compile(r"^\.(?P<name>.+)\.[0-9a-f]{16}\.(?:removing|replacing)$")


def find_orphaned_claims(parent: Path) -> tuple[str, ...]:
    """Canonical entry names that have interrupted claim siblings under *parent*.

    A claim sibling left behind by a crashed transaction encodes the canonical
    name it was renamed from; discovery uses this to notice managed state that
    is currently hidden under a claim name.
    """
    try:
        entries = os.listdir(parent)
    except OSError:
        return ()
    names = {
        match.group("name") for entry in entries if (match := _CLAIM_NAME_PATTERN.match(entry))
    }
    return tuple(sorted(names))


def _claim_siblings(parent: _PinnedParent, name: str) -> list[str]:
    pattern = re.compile(rf"^\.{re.escape(name)}\.[0-9a-f]{{16}}\.(?:removing|replacing)$")
    try:
        entries = os.listdir(parent.fd if parent.fd is not None else parent.path)
    except OSError:
        return []
    return sorted(entry for entry in entries if pattern.match(entry))


def _recover_claims(parent: _PinnedParent, path: Path, is_owned: Callable[[Path], bool]) -> bool:
    """Reconcile interrupted claim siblings of *path* left by a crashed transaction.

    A claim whose canonical path is absent is restored (no-clobber) so the
    generation stays discoverable; a leftover claim beside an occupied
    canonical path is deleted only when *is_owned* confirms it. Unattributable
    claims are left in place.
    """
    changed = False
    for claim_name in _claim_siblings(parent, path.name):
        claim_identity = parent.entry_identity(claim_name)
        if claim_identity is None:
            continue
        parent.revalidate()
        if not parent.lexists(path.name):
            if _restore_claimed(parent, claim_name, path.name):
                changed = True
                continue
        claimed = path.with_name(claim_name)
        try:
            parent.revalidate()
            owned = (
                claim_identity[2] != stat_module.S_IFLNK
                and is_owned(claimed)
                and parent.entry_identity(claim_name) == claim_identity
            )
            parent.revalidate()
        except OSError:
            continue
        if owned:
            with suppress(OSError):
                parent.remove_entry(claim_name, expected_identity=claim_identity)
                changed = True
    if changed:
        parent.fsync()
    return changed


def recover_owned_claims(
    path: Path,
    *,
    is_owned: Callable[[Path], bool],
    trusted_ancestor: Path | None = None,
) -> bool:
    """Reconcile interrupted claim state beside *path*; True when anything changed.

    Every publish/remove transaction on a path also runs this reconciliation
    first, so calling it explicitly is only needed for discovery flows that
    must observe recovered state without mutating the artifact.
    """
    try:
        with _pinned_parent(path.parent, trusted_ancestor=trusted_ancestor, create=False) as parent:
            return _recover_claims(parent, path, is_owned)
    except FileNotFoundError:
        return False


def remove_path(path: Path) -> None:
    """Remove a file, symlink, or directory tree."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def restore_claimed(claimed: Path, path: Path) -> bool:
    """Put a claimed generation back unless the canonical path was recreated.

    Returns True when restored. When another process recreated *path* while
    the claim was held, restoring would clobber that new generation, so both
    are preserved instead: the recreated entry stays canonical and the
    claimed one remains beside it under its claim name.
    """
    try:
        with _pinned_parent(path.parent, trusted_ancestor=None, create=False) as parent:
            return _restore_claimed(parent, claimed.name, path.name)
    except FileNotFoundError:
        return False


def _restore_claimed(parent: _PinnedParent, claimed_name: str, name: str) -> bool:
    if _LINK_DIR_FD_SUPPORTED and parent.fd is not None and not parent.is_directory(claimed_name):
        try:
            restored = parent.link_no_clobber(claimed_name, name)
        except FileNotFoundError:
            return False
        except OSError:
            pass  # fall through to the rename below
        else:
            if restored:
                parent.fsync()
            return restored
    try:
        parent.rename_no_replace(claimed_name, name)
    except (FileNotFoundError, FileExistsError):
        return False
    parent.fsync()
    return True


def claim_and_remove_owned(
    path: Path,
    *,
    is_owned: Callable[[Path], bool],
    trusted_ancestor: Path | None = None,
) -> bool:
    """Atomically claim *path* and delete it only if the claimed generation is owned.

    Returns False without touching anything when *path* is missing, and
    restores the claimed generation (no-clobber) when it is a symlink or
    fails the ownership re-check — the concurrent-replacement case.
    """
    claimed_name = _claim_name(path.name, "removing")
    claimed = path.with_name(claimed_name)
    try:
        with _pinned_parent(path.parent, trusted_ancestor=trusted_ancestor, create=False) as parent:
            _recover_claims(parent, path, is_owned)
            try:
                parent.replace(path.name, claimed_name)
            except FileNotFoundError:
                return False
            parent.fsync()
            claimed_identity = parent.entry_identity(claimed_name)
            try:
                parent.revalidate()
                owned = (
                    claimed_identity is not None
                    and claimed_identity[2] != stat_module.S_IFLNK
                    and is_owned(claimed)
                    and parent.entry_identity(claimed_name) == claimed_identity
                )
                parent.revalidate()
            except BaseException:
                _restore_claimed(parent, claimed_name, path.name)
                raise
            if not owned:
                _restore_claimed(parent, claimed_name, path.name)
                return False
            try:
                parent.remove_entry(claimed_name, expected_identity=claimed_identity)
            except UnownedArtifactError:
                # The claimed sibling no longer carries the validated identity;
                # deleting it would destroy a generation nobody attributed.
                _restore_claimed(parent, claimed_name, path.name)
                return False
            except OSError:
                # A transient removal failure must not strand the generation
                # under an undiscoverable claim name: restore the canonical
                # route so discovery still sees it and a retry can succeed.
                _restore_claimed(parent, claimed_name, path.name)
                raise
            parent.fsync()
            return True
    except FileNotFoundError:
        return False


def publish_owned_file(
    path: Path,
    content: str,
    *,
    is_owned: Callable[[Path], bool],
    mode: int | None = None,
    trusted_ancestor: Path | None = None,
    require_existing: bool = False,
) -> None:
    """Atomically publish one owned file generation.

    Any existing entry is claimed (renamed aside) and re-validated before the
    replacement, and the final rename is no-replace — an operator file,
    symlink, or generation recreated after the caller's own checks is
    preserved untouched and the publication fails with
    :class:`UnownedArtifactError` instead of replacing it.
    """

    def _build(parent: _PinnedParent, target_name: str) -> str:
        fd, temp_name = parent.create_temp_file(prefix=f".{target_name}.")
        try:
            if mode is not None and hasattr(os, "fchmod"):
                os.fchmod(fd, mode)
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            with suppress(OSError):
                os.close(fd)
            parent.unlink(temp_name)
            raise
        return temp_name

    _publish_owned(
        path,
        _build,
        is_owned=is_owned,
        trusted_ancestor=trusted_ancestor,
        require_existing=require_existing,
    )


def publish_owned_tree(
    path: Path,
    build: Callable[[Path], None],
    *,
    is_owned: Callable[[Path], bool],
    trusted_ancestor: Path | None = None,
) -> None:
    """Atomically publish one owned directory-tree generation.

    *build* receives a staging directory created beside *path* and must fill
    it with the complete new generation. The staged tree then replaces the
    canonical path under the same claim/verify/no-replace discipline as
    :func:`publish_owned_file`.
    """

    def _build(parent: _PinnedParent, target_name: str) -> str:
        staging_name = parent.make_staging_dir(prefix=f".{target_name}.")
        try:
            build(parent.path / staging_name)
        except BaseException:
            parent.remove_entry(staging_name)
            raise
        return staging_name

    _publish_owned(path, _build, is_owned=is_owned, trusted_ancestor=trusted_ancestor)


def publish_owned_entry(
    path: Path,
    build: Callable[[Path], None],
    *,
    is_owned: Callable[[Path], bool],
    trusted_ancestor: Path | None = None,
    require_existing: bool = False,
) -> None:
    """Atomically publish one owned generation of arbitrary topology.

    *build* receives a nonexistent staging path beside *path* and must create
    the complete entry there — a file, a directory tree, or a symlink. The
    staged entry then replaces the canonical path under the same
    claim/verify/no-replace discipline as :func:`publish_owned_file`; rollback
    flows use this to restore pre-transaction snapshots without a separate
    check-then-restore sequence.
    """

    def _build(parent: _PinnedParent, target_name: str) -> str:
        staged_name = f".{target_name}.{os.urandom(8).hex()}.tmp"
        try:
            build(parent.path / staged_name)
        except BaseException:
            with suppress(OSError):
                parent.remove_entry(staged_name)
            raise
        if not parent.lexists(staged_name):
            raise OSError(errno.ENOENT, "entry build produced no staged generation", str(path))
        return staged_name

    _publish_owned(
        path,
        _build,
        is_owned=is_owned,
        trusted_ancestor=trusted_ancestor,
        require_existing=require_existing,
    )


def _publish_owned(
    path: Path,
    build: Callable[[_PinnedParent, str], str],
    *,
    is_owned: Callable[[Path], bool],
    trusted_ancestor: Path | None,
    require_existing: bool = False,
) -> None:
    with _pinned_parent(path.parent, trusted_ancestor=trusted_ancestor, create=True) as parent:
        _recover_claims(parent, path, is_owned)
        claimed_name: str | None = None
        claimed_identity: tuple[int, int, int] | None = None
        if parent.lexists(path.name):
            claimed_name = _claim_name(path.name, "replacing")
            claimed = path.with_name(claimed_name)
            parent.replace(path.name, claimed_name)
            parent.fsync()
            claimed_identity = parent.entry_identity(claimed_name)
            try:
                parent.revalidate()
                owned = (
                    claimed_identity is not None
                    and claimed_identity[2] != stat_module.S_IFLNK
                    and is_owned(claimed)
                    and parent.entry_identity(claimed_name) == claimed_identity
                )
                parent.revalidate()
            except BaseException:
                _restore_claimed(parent, claimed_name, path.name)
                raise
            if not owned:
                _restore_claimed(parent, claimed_name, path.name)
                raise UnownedArtifactError(f"preserved user-managed file at {path}")
        elif require_existing:
            raise UnownedArtifactError(f"artifact disappeared before publication: {path}")
        try:
            staged_name = build(parent, path.name)
        except BaseException:
            if claimed_name is not None:
                _restore_claimed(parent, claimed_name, path.name)
            raise
        try:
            parent.revalidate()
            parent.rename_no_replace(staged_name, path.name)
        except FileExistsError as exc:
            parent.remove_entry(staged_name)
            if claimed_name is not None:
                _restore_claimed(parent, claimed_name, path.name)
            raise UnownedArtifactError(
                f"canonical path was recreated during publication: {path}"
            ) from exc
        except BaseException:
            parent.remove_entry(staged_name)
            if claimed_name is not None:
                _restore_claimed(parent, claimed_name, path.name)
            raise
        if claimed_name is not None:
            with suppress(UnownedArtifactError):
                # An identity change means the claimed sibling is no longer the
                # generation this transaction validated; leave it for recovery.
                parent.remove_entry(claimed_name, expected_identity=claimed_identity)
        parent.fsync()
