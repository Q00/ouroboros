"""No-follow directory capabilities for security-sensitive path checks.

A ``NoFollowDirectoryChain`` holds one descriptor per directory, each opened
by name from the one before it with ``O_NOFOLLOW | O_DIRECTORY``, so no link
anywhere in the chain is followed. Every effect on a file goes through the
held leaf descriptor (``dir_fd``), and a result is returned only after the
chain is confirmed to still name the directories it holds (``postvalidate``):
a directory swapped for a link, or moved, while the chain is held makes the
operation fail instead of reading or writing somewhere else. Where the
platform has no ``O_NOFOLLOW`` or ``dir_fd`` support every operation fails
closed; nothing falls back to following a path.
"""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass, field
from enum import StrEnum
import hashlib
import os
from pathlib import Path
import stat

DirectoryChainFingerprint = tuple[tuple[str, int, int, int], ...]

_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
_CHUNK = 1024 * 1024


def _directory_identity(value: os.stat_result) -> tuple[int, int, int]:
    return (value.st_dev, value.st_ino, value.st_mode)


def nofollow_directory_capabilities_available() -> bool:
    """Return whether this platform can perform fail-closed dirfd traversal."""
    return (
        hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and os.open in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.stat in os.supports_follow_symlinks
    )


def _held_effects_available() -> bool:
    """Whether listing, reading, creating and unlinking through held dirfds is possible."""
    return (
        nofollow_directory_capabilities_available()
        and all(function in os.supports_dir_fd for function in (os.mkdir, os.unlink, os.readlink))
        and os.listdir in os.supports_fd
    )


def _canonical_name(component: str) -> bool:
    return component not in {"", ".", ".."} and Path(component).name == component


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


class HeldPathChanged(OSError):
    """A held directory or a file created through it no longer has the name it was opened by."""


@dataclass(frozen=True, slots=True)
class RegularFile:
    """A regular file read through a held chain: its identity and its bytes."""

    device: int
    inode: int
    size: int
    data: bytes


@dataclass(slots=True)
class NoFollowDirectoryChain:
    """Held directory fds plus the lexical parent-to-child bindings they prove.

    A chain from ``descend`` borrows its parent's descriptors (the first
    ``_borrowed``) and closes only its own; the parent must outlive it.
    """

    _directory_fds: list[int]
    _component_names: tuple[str, ...]
    _borrowed: int = 0
    _root_name: str = field(default=os.sep)

    @property
    def leaf_fd(self) -> int:
        """Return the final directory capability in the chain."""
        if not self._directory_fds:
            msg = "no-follow directory chain is closed"
            raise OSError(msg)
        return self._directory_fds[-1]

    @property
    def descriptor_count(self) -> int:
        """Return the number of directory descriptors currently owned."""
        return len(self._directory_fds)

    def matches_opened_directories(self, other: NoFollowDirectoryChain) -> bool:
        """Check that two held traversals opened the same directory identities."""
        if self._component_names != other._component_names or len(self._directory_fds) != len(
            other._directory_fds
        ):
            return False
        try:
            return all(
                _directory_identity(os.fstat(left_fd)) == _directory_identity(os.fstat(right_fd))
                for left_fd, right_fd in zip(
                    self._directory_fds,
                    other._directory_fds,
                    strict=True,
                )
            )
        except (OSError, RuntimeError, ValueError):
            return False

    def matches_opened_prefix(self, other: NoFollowDirectoryChain) -> bool:
        """Check that this chain is the identical held prefix of another walk."""
        prefix_length = len(self._directory_fds)
        if self._component_names != other._component_names[
            : len(self._component_names)
        ] or prefix_length > len(other._directory_fds):
            return False
        try:
            return all(
                _directory_identity(os.fstat(left_fd)) == _directory_identity(os.fstat(right_fd))
                for left_fd, right_fd in zip(
                    self._directory_fds,
                    other._directory_fds[:prefix_length],
                    strict=True,
                )
            )
        except (OSError, RuntimeError, ValueError):
            return False

    def fingerprint(self) -> DirectoryChainFingerprint:
        """Snapshot component names and opened identities for durable replay."""
        if len(self._directory_fds) != len(self._component_names) + 1:
            msg = "no-follow directory chain is closed or malformed"
            raise OSError(msg)
        names = (self._root_name, *self._component_names)
        return tuple(
            (name, identity.st_dev, identity.st_ino, identity.st_mode)
            for name, directory_fd in zip(names, self._directory_fds, strict=True)
            for identity in (os.fstat(directory_fd),)
        )

    def postvalidate(self) -> bool:
        """Confirm every held child is still named by its held parent."""
        if len(self._directory_fds) != len(self._component_names) + 1:
            return False
        try:
            return all(
                _directory_identity(os.stat(name, dir_fd=parent_fd, follow_symlinks=False))
                == _directory_identity(os.fstat(child_fd))
                for parent_fd, child_fd, name in zip(
                    self._directory_fds[:-1],
                    self._directory_fds[1:],
                    self._component_names,
                    strict=True,
                )
            )
        except (OSError, RuntimeError, ValueError):
            return False

    def descend(self, name: str) -> NoFollowDirectoryChain:
        """The chain extended by the child directory ``name``, opened without following it."""
        if not _canonical_name(name):
            msg = "directory capability components must be canonical names"
            raise ValueError(msg)
        child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=self.leaf_fd)
        return NoFollowDirectoryChain(
            [*self._directory_fds, child_fd],
            (*self._component_names, name),
            len(self._directory_fds),
            self._root_name,
        )

    def names(self) -> list[str]:
        """The names in the leaf directory, sorted."""
        return sorted(os.listdir(self.leaf_fd))

    def status(self, name: str) -> os.stat_result:
        """The status of ``name`` in the leaf directory itself (a link is not followed)."""
        return os.stat(name, dir_fd=self.leaf_fd, follow_symlinks=False)

    def read_link(self, name: str) -> str:
        """The target text of the link ``name`` in the leaf directory."""
        return os.readlink(name, dir_fd=self.leaf_fd)

    def _open_regular(self, name: str) -> int:
        if not _canonical_name(name):
            msg = "file capability names must be canonical names"
            raise ValueError(msg)
        descriptor = os.open(name, _READ_FLAGS, dir_fd=self.leaf_fd)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise OSError(f"not a regular file: {name}")
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    def _confirm_read(self, name: str, descriptor: int) -> os.stat_result:
        """The held file is still ``name`` in the leaf, and every held directory is still named."""
        held = os.fstat(descriptor)
        named = os.stat(name, dir_fd=self.leaf_fd, follow_symlinks=False)
        if not _same_inode(held, named) or not self.postvalidate():
            raise OSError(f"the path of {name} changed while it was read")
        return held

    def read_regular_file(self, name: str) -> RegularFile:
        """Read the regular file ``name`` in the leaf directory; ``OSError`` for anything else.

        A link, a special file, or a file or directory moved while it was
        read is refused.
        """
        descriptor = self._open_regular(name)
        try:
            chunks = []
            while chunk := os.read(descriptor, _CHUNK):
                chunks.append(chunk)
            held = self._confirm_read(name, descriptor)
        finally:
            os.close(descriptor)
        return RegularFile(held.st_dev, held.st_ino, held.st_size, b"".join(chunks))

    def hash_regular_file(self, name: str) -> str:
        """SHA-256 of the regular file ``name`` in the leaf, under ``read_regular_file``'s rules."""
        descriptor = self._open_regular(name)
        try:
            digest = hashlib.sha256()
            while chunk := os.read(descriptor, _CHUNK):
                digest.update(chunk)
            self._confirm_read(name, descriptor)
        finally:
            os.close(descriptor)
        return digest.hexdigest()

    def create_exclusive(self, name: str, data: bytes, *, mode: int = 0o600) -> bool:
        """Create ``name`` in the leaf holding exactly ``data``; ``False`` if ``name`` exists.

        The file is created exclusively (``O_EXCL``, never through a link),
        written and synced; ``True`` is returned only if ``name`` then still
        names the inode written here and the chain still names its
        directories (``HeldPathChanged`` otherwise). On any failure the file is
        removed, but only while ``name`` is still the inode this call created,
        and the error is raised.
        """
        if not _canonical_name(name):
            msg = "file capability names must be canonical names"
            raise ValueError(msg)
        try:
            descriptor = os.open(name, _CREATE_FLAGS, mode, dir_fd=self.leaf_fd)
        except FileExistsError:
            return False
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(descriptor, view) :]
            os.fsync(descriptor)
            # Success only while ``name`` still is the inode written here and
            # every held directory is still named by its parent.
            if not _same_inode(os.fstat(descriptor), self.status(name)):
                raise HeldPathChanged(f"{name} was replaced while it was published")
            if not self.postvalidate():
                raise HeldPathChanged(f"the directory of {name} moved while it was published")
        except BaseException:
            self._unlink_created(name, descriptor)
            raise
        finally:
            os.close(descriptor)
        return True

    def _unlink_created(self, name: str, descriptor: int) -> None:
        try:
            if _same_inode(
                os.fstat(descriptor), os.stat(name, dir_fd=self.leaf_fd, follow_symlinks=False)
            ):
                os.unlink(name, dir_fd=self.leaf_fd)
        except OSError:
            pass

    def close(self) -> None:
        """Release all held capabilities; repeated calls are safe."""
        directory_fds, self._directory_fds = self._directory_fds, []
        for directory_fd in reversed(directory_fds[self._borrowed :]):
            try:
                os.close(directory_fd)
            except OSError:
                pass


def open_nofollow_directory_chain(
    absolute_directory: str | os.PathLike[str],
    *,
    relative_components: tuple[str, ...] = (),
    create_missing: bool = False,
) -> NoFollowDirectoryChain:
    """Open an absolute directory from trusted ``/`` through every component.

    With ``create_missing`` a missing component is created in its held
    parent (``mkdir`` through the parent descriptor) and then opened like
    any other, so a link planted in its place is still refused.
    """
    if not nofollow_directory_capabilities_available() or (
        create_missing and not _held_effects_available()
    ):
        msg = "no-follow dirfd traversal is unavailable"
        raise OSError(msg)
    absolute = Path(os.path.abspath(absolute_directory))
    if absolute.anchor != os.sep:
        msg = "no-follow capability traversal requires an absolute POSIX path"
        raise ValueError(msg)
    components = (*absolute.parts[1:], *relative_components)
    if any(
        component in {"", ".", ".."} or Path(component).name != component
        for component in components
    ):
        msg = "directory capability components must be canonical names"
        raise ValueError(msg)

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_fds: list[int] = []
    try:
        current_fd = os.open(os.sep, flags)
        directory_fds.append(current_fd)
        for component in components:
            if create_missing:
                try:
                    os.mkdir(component, 0o777, dir_fd=current_fd)
                except FileExistsError:
                    pass
            current_fd = os.open(component, flags, dir_fd=current_fd)
            directory_fds.append(current_fd)
        return NoFollowDirectoryChain(directory_fds, tuple(components))
    except BaseException:
        for directory_fd in reversed(directory_fds):
            try:
                os.close(directory_fd)
            except OSError:
                pass
        raise


def open_directory_anchor(directory: str | os.PathLike[str]) -> NoFollowDirectoryChain:
    """Hold ``directory`` itself as the root of a chain that ``descend`` extends.

    The caller names the anchor (a checkout copy it created, the process's
    working directory); only what lies below it is opened without following
    links. ``OSError`` when held traversal is unavailable on this platform.
    """
    if not _held_effects_available():
        msg = "no-follow dirfd traversal is unavailable"
        raise OSError(msg)
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    return NoFollowDirectoryChain([descriptor], (), 0, os.fspath(directory))


class CheckoutFileRefusal(StrEnum):
    """Why ``resolve_checkout_file`` did not prove a path to be a regular checkout file."""

    UNAVAILABLE = "nofollow_unavailable"
    NOT_CHECKOUT_RELATIVE = "not_checkout_relative"
    ROOT_UNREADABLE = "root_unreadable"
    MISSING = "missing"
    LINK = "link"
    NOT_DIRECTORY = "not_directory"
    NOT_REGULAR = "not_regular"
    UNREADABLE = "unreadable"
    MOVED = "moved"


def _refusal_of(
    chain: NoFollowDirectoryChain, name: str, *, directory: bool
) -> CheckoutFileRefusal | None:
    try:
        status = chain.status(name)
    except FileNotFoundError:
        return CheckoutFileRefusal.MISSING
    except OSError:
        return CheckoutFileRefusal.UNREADABLE
    if stat.S_ISLNK(status.st_mode):
        return CheckoutFileRefusal.LINK
    if directory and not stat.S_ISDIR(status.st_mode):
        return CheckoutFileRefusal.NOT_DIRECTORY
    if not directory and not stat.S_ISREG(status.st_mode):
        return CheckoutFileRefusal.NOT_REGULAR
    return None


def resolve_checkout_file(
    root: str | os.PathLike[str], relative: str
) -> RegularFile | CheckoutFileRefusal:
    """Prove ``relative`` (POSIX, below ``root``) names a regular file, and read it.

    Every directory of the path and the file itself are opened by name from
    the held ``root`` without following a link; the bytes are returned only
    if the whole chain still names what was opened once they are read. Any
    other outcome is a ``CheckoutFileRefusal``, never an exception.
    """
    if not _held_effects_available():
        return CheckoutFileRefusal.UNAVAILABLE
    parts = tuple(relative.split("/")) if isinstance(relative, str) else ()
    if not parts or not all(_canonical_name(part) for part in parts):
        return CheckoutFileRefusal.NOT_CHECKOUT_RELATIVE
    with ExitStack() as held:
        try:
            chain = open_directory_anchor(root)
        except OSError:
            return CheckoutFileRefusal.ROOT_UNREADABLE
        held.callback(chain.close)
        for name in parts[:-1]:
            refusal = _refusal_of(chain, name, directory=True)
            if refusal is not None:
                return refusal
            try:
                chain = chain.descend(name)
            except OSError:
                return CheckoutFileRefusal.MOVED
            held.callback(chain.close)
        refusal = _refusal_of(chain, parts[-1], directory=False)
        if refusal is not None:
            return refusal
        try:
            return chain.read_regular_file(parts[-1])
        except PermissionError:
            return CheckoutFileRefusal.UNREADABLE
        except OSError:
            return CheckoutFileRefusal.MOVED
    return CheckoutFileRefusal.UNREADABLE  # pragma: no cover - ExitStack never suppresses
