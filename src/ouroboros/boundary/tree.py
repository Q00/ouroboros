"""Byte-level manifests of checkout trees for protected-byte checks.

A manifest maps every workspace-relative file path to the SHA-256 of its bytes
(or to ``symlink:<target>`` for a symbolic link). Paths whose components match
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

DEFAULT_UNPROTECTED_NAMES: frozenset[str] = frozenset(
    {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".hypothesis"}
)

_CHUNK = 1024 * 1024


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def tree_manifest(
    root: Path,
    *,
    unprotected_names: Iterable[str] = DEFAULT_UNPROTECTED_NAMES,
) -> dict[str, str]:
    """Return ``{relative_path: digest}`` for every protected file under ``root``."""
    skip = frozenset(unprotected_names)
    base = root.resolve()
    manifest: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        current = Path(dirpath)
        kept: list[str] = []
        for name in dirnames:
            if name in skip:
                continue
            full = current / name
            if full.is_symlink():
                manifest[full.relative_to(base).as_posix()] = f"symlink:{os.readlink(full)}"
                continue
            kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            if name in skip:
                continue
            full = current / name
            relative = full.relative_to(base).as_posix()
            if full.is_symlink():
                manifest[relative] = f"symlink:{os.readlink(full)}"
            elif full.is_file():
                manifest[relative] = _file_sha256(full)
    return manifest


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
    """Copy a checkout, preserving symlinks, into a new ``destination``."""
    shutil.copytree(source, destination, symlinks=True)
