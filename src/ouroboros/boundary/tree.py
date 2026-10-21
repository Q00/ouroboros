"""Byte-level manifests of checkout trees for protected-byte checks.

A manifest maps every workspace-relative file path to the SHA-256 of its bytes
(or to ``symlink:<target>`` for a symbolic link). A file or directory that
cannot be read, and anything that is neither a regular file, a directory nor a
symbolic link (a named pipe, a socket, a device), maps to ``UNREADABLE``: the
manifest is still computed, and a caller that must copy or judge the tree
sees which paths it cannot use (``unreadable_paths``). Paths whose components match
an unprotected name (by default version-control metadata and regenerable
interpreter or tool caches) are left out: running a test legitimately rewrites
them, and flagging them would make every run look like a mutation.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import hashlib
from pathlib import Path
import shutil
import stat

from ouroboros.core.filesystem_capability import NoFollowDirectoryChain, open_directory_anchor

DEFAULT_UNPROTECTED_NAMES: frozenset[str] = frozenset(
    {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".hypothesis"}
)

UNREADABLE = "unreadable"


def _file_sha256(directory: NoFollowDirectoryChain, name: str) -> str:
    """SHA-256 of the regular file ``name`` in a held directory (``hash_regular_file``).

    A name swapped for a link, a pipe or a device, or a directory of its
    path swapped or moved while it is read, raises ``OSError`` (the caller
    records it as ``UNREADABLE``); outside bytes are never hashed.
    """
    return directory.hash_regular_file(name)


def _walk(
    directory: NoFollowDirectoryChain, prefix: str, skip: frozenset[str], manifest: dict[str, str]
) -> None:
    """Record every entry below a held directory, opening each child through it."""
    try:
        names = directory.names()
    except OSError:
        manifest[prefix.rstrip("/") or "."] = UNREADABLE
        return
    for name in names:
        if name in skip:
            continue
        relative = prefix + name
        try:
            mode = directory.status(name).st_mode
            if stat.S_ISLNK(mode):
                manifest[relative] = f"symlink:{directory.read_link(name)}"
            elif stat.S_ISDIR(mode):
                child = directory.descend(name)
                try:
                    _walk(child, relative + "/", skip, manifest)
                finally:
                    child.close()
            elif stat.S_ISREG(mode):
                manifest[relative] = _file_sha256(directory, name)
            else:
                manifest[relative] = UNREADABLE
        except (OSError, RecursionError):
            # A tree deeper than the walk (or the descriptors) can hold is
            # recorded as unreadable, never a crash of the whole manifest.
            manifest[relative] = UNREADABLE


def tree_manifest(
    root: Path,
    *,
    unprotected_names: Iterable[str] = DEFAULT_UNPROTECTED_NAMES,
) -> dict[str, str]:
    """Return ``{relative_path: digest}`` for every protected file under ``root``.

    ``root`` is held once (``open_directory_anchor``) and everything below it
    is opened by name through held directory descriptors, never through a
    rebuilt path, so a directory swapped for a link while the tree is read
    cannot lead outside it. Where held traversal is unavailable the whole
    tree is ``UNREADABLE`` (``{".": UNREADABLE}``).
    """
    skip = frozenset(unprotected_names)
    try:
        anchor = open_directory_anchor(root.resolve())
    except FileNotFoundError:
        return {}
    except OSError:
        return {".": UNREADABLE}
    manifest: dict[str, str] = {}
    try:
        _walk(anchor, "", skip, manifest)
    finally:
        anchor.close()
    return manifest


def unreadable_paths(manifest: Mapping[str, str]) -> tuple[str, ...]:
    """Paths of ``manifest`` whose bytes could not be read (``UNREADABLE``)."""
    return tuple(sorted(path for path, value in manifest.items() if value == UNREADABLE))


def manifest_digest(manifest: Mapping[str, str]) -> str:
    """Return one SHA-256 over a manifest, independent of insertion order."""
    digest = hashlib.sha256()
    for path in sorted(manifest):
        digest.update(path.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(manifest[path].encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def tree_digest(
    root: Path,
    *,
    unprotected_names: Iterable[str] = DEFAULT_UNPROTECTED_NAMES,
) -> str:
    """Return the manifest digest of ``root``; the identity of a checkout artifact."""
    return manifest_digest(tree_manifest(root, unprotected_names=unprotected_names))


def changed_paths(before: Mapping[str, str], after: Mapping[str, str]) -> tuple[str, ...]:
    """Return paths present in ``before`` that are modified or missing in ``after``."""
    return tuple(sorted(path for path, value in before.items() if after.get(path) != value))


def added_paths(before: Mapping[str, str], after: Mapping[str, str]) -> tuple[str, ...]:
    """Return paths present in ``after`` but not in ``before``."""
    return tuple(sorted(set(after) - set(before)))


def copy_checkout(source: Path, destination: Path) -> None:
    """Copy a checkout, preserving symlinks, into a new ``destination``.

    ``__pycache__`` is not copied: it is outside the protected digest, so a
    planted ``.pyc`` matching a source file's mtime and size could otherwise
    run in place of the reviewed source.
    """
    shutil.copytree(
        source, destination, symlinks=True, ignore=shutil.ignore_patterns("__pycache__")
    )
