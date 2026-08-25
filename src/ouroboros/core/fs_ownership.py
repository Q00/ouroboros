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
  directory descriptor. The chain from a caller-supplied *trusted ancestor*
  down to the artifact's parent is opened one component at a time with
  ``O_NOFOLLOW``, so an operator-controlled symlink such as
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

Related, deliberately separate machinery: :mod:`ouroboros.hermes.artifacts`
implements a heavier journaled variant of the same idea (swap-intent files
and restart recovery), and :mod:`ouroboros.codex.artifacts` carries its own
fingerprint-gated replacement. Consolidating those onto these primitives is a
candidate follow-up, not something callers should assume has happened.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
import ctypes
import errno
import os
from pathlib import Path
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


def _nofollow_components(parent: Path, trusted_ancestor: Path | None) -> tuple[Path, list[str]]:
    """Split *parent* into a trusted base and the components to walk no-follow."""
    if trusted_ancestor is None:
        return parent.parent, [parent.name]
    ancestor = Path(trusted_ancestor)
    try:
        relative = parent.relative_to(ancestor)
    except ValueError:
        return parent.parent, [parent.name]
    return ancestor, [part for part in relative.parts if part not in ("", ".")]


def _symlink_component_error(component: Path) -> OSError:
    return OSError(
        errno.ELOOP,
        "refusing to operate through a symlinked artifact parent component",
        str(component),
    )


@contextmanager
def _pinned_parent(
    parent: Path,
    *,
    trusted_ancestor: Path | None,
    create: bool,
) -> Iterator[_PinnedParent]:
    """Pin *parent*, walking every component below the trusted ancestor no-follow."""
    base, components = _nofollow_components(parent, trusted_ancestor)
    if not _DIR_FD_SUPPORTED:
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
    fd = os.open(base, flags)
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
            return parent.link_no_clobber(claimed_name, name)
        except FileNotFoundError:
            return False
        except OSError:
            pass  # fall through to the rename below
    try:
        parent.rename_no_replace(claimed_name, name)
    except (FileNotFoundError, FileExistsError):
        return False
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
            try:
                parent.replace(path.name, claimed_name)
            except FileNotFoundError:
                return False
            try:
                owned = not claimed.is_symlink() and is_owned(claimed)
            except BaseException:
                _restore_claimed(parent, claimed_name, path.name)
                raise
            if not owned:
                _restore_claimed(parent, claimed_name, path.name)
                return False
            remove_path(claimed)
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

    _publish_owned(path, _build, is_owned=is_owned, trusted_ancestor=trusted_ancestor)


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
        staging = Path(
            tempfile.mkdtemp(prefix=f".{target_name}.", suffix=".tmp", dir=str(parent.path))
        )
        try:
            build(staging)
        except BaseException:
            remove_path(staging)
            raise
        return staging.name

    _publish_owned(path, _build, is_owned=is_owned, trusted_ancestor=trusted_ancestor)


def _publish_owned(
    path: Path,
    build: Callable[[_PinnedParent, str], str],
    *,
    is_owned: Callable[[Path], bool],
    trusted_ancestor: Path | None,
) -> None:
    with _pinned_parent(path.parent, trusted_ancestor=trusted_ancestor, create=True) as parent:
        claimed_name: str | None = None
        if parent.lexists(path.name):
            claimed_name = _claim_name(path.name, "replacing")
            claimed = path.with_name(claimed_name)
            parent.replace(path.name, claimed_name)
            try:
                owned = not claimed.is_symlink() and is_owned(claimed)
            except BaseException:
                _restore_claimed(parent, claimed_name, path.name)
                raise
            if not owned:
                _restore_claimed(parent, claimed_name, path.name)
                raise UnownedArtifactError(f"preserved user-managed file at {path}")
        staged_name = build(parent, path.name)
        try:
            parent.rename_no_replace(staged_name, path.name)
        except FileExistsError as exc:
            remove_path(path.with_name(staged_name))
            if claimed_name is not None:
                _restore_claimed(parent, claimed_name, path.name)
            raise UnownedArtifactError(
                f"canonical path was recreated during publication: {path}"
            ) from exc
        except BaseException:
            remove_path(path.with_name(staged_name))
            if claimed_name is not None:
                _restore_claimed(parent, claimed_name, path.name)
            raise
        if claimed_name is not None:
            remove_path(path.with_name(claimed_name))
        parent.fsync()
