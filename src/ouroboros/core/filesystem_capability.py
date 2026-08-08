"""No-follow directory capabilities for security-sensitive path checks."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

DirectoryChainFingerprint = tuple[tuple[str, int, int, int], ...]


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


@dataclass(slots=True)
class NoFollowDirectoryChain:
    """Held directory fds plus the lexical parent-to-child bindings they prove."""

    _directory_fds: list[int]
    _component_names: tuple[str, ...]

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
        names = (os.sep, *self._component_names)
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

    def close(self) -> None:
        """Release all held capabilities; repeated calls are safe."""
        directory_fds, self._directory_fds = self._directory_fds, []
        for directory_fd in reversed(directory_fds):
            try:
                os.close(directory_fd)
            except OSError:
                pass


def open_nofollow_directory_chain(
    absolute_directory: str | os.PathLike[str],
    *,
    relative_components: tuple[str, ...] = (),
) -> NoFollowDirectoryChain:
    """Open an absolute directory from trusted ``/`` through every component."""
    if not nofollow_directory_capabilities_available():
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
