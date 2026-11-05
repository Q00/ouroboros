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
import os
from pathlib import Path
import shutil
import stat
import tempfile

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


COPY_ATTEMPTS = 3
"""How many times ``copy_checkout`` retries a copy that a live writer disturbed."""


class CheckoutCopyError(OSError):
    """A checkout could not be copied consistently; nothing partial was published."""


def _discard(path: Path) -> None:
    """Remove ``path`` whether it is a tree, a file or a link (a missing path is fine)."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _publish_over(scratch: Path, destination: Path) -> None:
    """Move ``scratch`` onto an existing ``destination`` without losing it on failure.

    Deleting the published checkout first would make a failed promotion cost the
    baseline it was replacing. The old copy is renamed aside instead, put back if
    the promotion raises, and only then removed — so a failed promotion leaves the
    previous checkout in place. Discarding ``scratch`` is the caller's job, so every
    publication failure is cleaned up in one place.
    """
    previous = scratch.parent / ".previous"
    os.replace(destination, previous)
    try:
        os.replace(scratch, destination)
    except OSError:
        try:
            os.replace(previous, destination)
        except OSError as restore:
            raise CheckoutCopyError(
                f"could not promote {scratch} to {destination}; "
                f"the previous checkout is still at {previous}"
            ) from restore
        raise
    _discard(previous)


def copy_checkout(
    source: Path, destination: Path, *, attempts: int = COPY_ATTEMPTS, replace: bool = False
) -> None:
    """Copy a checkout, preserving symlinks, into ``destination``.

    ``__pycache__`` is not copied: it is outside the protected digest, so a
    planted ``.pyc`` matching a source file's mtime and size could otherwise
    run in place of the reviewed source.

    A checkout may be written to while it is copied: Git maintenance removes a
    transient lock file between the directory walk and the copy, which
    ``shutil.copytree`` reports as ``shutil.Error`` (#2808). The copy is built in
    a scratch tree in a unique sibling directory and moved into place only
    once it is complete, so a partial tree is never published as a baseline.
    Such a copy is retried ``attempts`` times and then raises ``CheckoutCopyError`` instead of leaking
    ``shutil.Error``. An ``OSError`` is not retried: it means the copy could not
    start (a missing source, an unwritable parent), and it reaches the caller
    with the type it always had.

    ``destination`` must not exist unless ``replace`` is set; even then it is
    replaced only after the new copy completed, so a failed copy leaves the
    previous checkout in place.
    """
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    if (destination.exists() or destination.is_symlink()) and not replace:
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
    scratch = staging / ".copying"
    previous = staging / ".previous"
    last: shutil.Error | None = None
    try:
        for _ in range(attempts):
            try:
                shutil.copytree(
                    source, scratch, symlinks=True, ignore=shutil.ignore_patterns("__pycache__")
                )
            except shutil.Error as exc:
                last = exc
                _discard(scratch)
                continue
            if destination.exists() or destination.is_symlink():
                if not replace:
                    raise FileExistsError(destination)
                _publish_over(scratch, destination)
            else:
                os.replace(scratch, destination)
            return
        raise CheckoutCopyError(
            f"could not copy the checkout {source} to {destination} in {attempts} attempts"
        ) from last
    finally:
        _discard(scratch)
        # A failed rollback leaves the baseline at the path reported by _publish_over.
        if not (previous.exists() or previous.is_symlink()):
            _discard(staging)
