"""Copies of an app's files, so the completion pass can put back a version it played.

A fix or repair worker can leave an app looking worse than it found it. In the
benchmark behind this module, one fix rewrote a renderer for a context its
shaders did not fit, and the galaxy the first play had shown became a thin band.
A repair then raised the particle count until the galaxy was one white slab, and
nothing played the app after that repair. The pass keeps each version it plays,
in memory, and writes back an earlier one when it looks better than the latest
(:mod:`ouroboros.orchestrator.completion_pass`).

Only regular files are kept; a symlink is neither followed nor kept. Each write
goes through a directory reached from the app's directory without following a
symlink (:func:`ouroboros.orchestrator.playtest.open_dir_inside`). Files made
after a version stay when it is put back: nothing is deleted. When a write
fails part-way, the app is returned to how it was before the put-back.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import contextlib
from dataclasses import dataclass
import errno
import os
from pathlib import Path, PurePosixPath
import stat

from ouroboros.orchestrator.playtest import open_dir_inside, save_in

# Not the app: the pass's own files, version control, installed packages, caches.
SKIPPED_DIRS = frozenset(
    {".ouroboros", ".git", "node_modules", "__pycache__", ".venv", "venv", ".pytest_cache"}
)
MAX_FILES = 2000
MAX_BYTES = 32 * 1024 * 1024
_READ_FLAGS = os.O_RDONLY | int(getattr(os, "O_NOFOLLOW", 0))


@dataclass(frozen=True)
class AppVersion:
    """An app's regular files: path relative to its directory -> (content, mode)."""

    files: Mapping[str, tuple[bytes, int]]


def keep(working_dir: Path) -> AppVersion | None:
    """The app's regular files now, or ``None`` when they cannot all be kept.

    Too many files, too many bytes, or a file or directory that cannot be read
    keeps nothing: putting part of a version back would leave a mix that was
    never played.
    """
    try:
        return _keep(working_dir)
    except OSError:  # a directory the walk could not read
        return None


def _raise(error: OSError) -> None:
    raise error


def _keep(working_dir: Path) -> AppVersion | None:
    files: dict[str, tuple[bytes, int]] = {}
    total = 0
    for root, dirs, names in os.walk(working_dir, onerror=_raise):
        dirs[:] = sorted(d for d in dirs if d not in SKIPPED_DIRS)
        for name in sorted(names):
            path = Path(root) / name
            try:
                if not stat.S_ISREG(os.lstat(path).st_mode):
                    continue
                fd = os.open(path, _READ_FLAGS)
            except OSError:
                return None
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    continue
                with os.fdopen(os.dup(fd), "rb") as handle:
                    data = handle.read(MAX_BYTES - total + 1)
            except OSError:
                return None
            finally:
                os.close(fd)
            total += len(data)
            if len(files) >= MAX_FILES or total > MAX_BYTES:
                return None
            files[path.relative_to(working_dir).as_posix()] = (data, stat.S_IMODE(info.st_mode))
    return AppVersion(files)


def put_back(working_dir: Path, version: AppVersion) -> int:
    """Write ``version``'s files back where they differ now; the number written.

    When a write fails part-way, each file written over is put back as it was,
    each file and directory the put-back made is removed, and the error is
    raised, so the app is not left a mix of two versions. Nothing is written,
    and an error is raised, when the app's files now cannot all be kept, or when
    a path the version holds is now a link, or a directory on its way is not a
    directory: writing would replace what is there.
    """
    now = keep(working_dir)
    if now is None:
        raise OSError(errno.EFBIG, "the app's files cannot all be kept", str(working_dir))
    changed = [
        (rel, data, mode)
        for rel, (data, mode) in sorted(version.files.items())
        if now.files.get(rel) != (data, mode)
    ]
    for rel, _, _ in changed:
        _check_way(working_dir, rel)
    made_dirs = _missing_dirs(working_dir, [rel for rel, _, _ in changed])
    written: list[str] = []
    try:
        for rel, data, mode in changed:
            _write(working_dir, rel, data, mode)
            written.append(rel)
    except OSError:
        for rel in reversed(written):
            with contextlib.suppress(OSError):
                if rel in now.files:
                    _write(working_dir, rel, *now.files[rel])
                else:
                    _remove(working_dir, rel, os.unlink)
        for rel in made_dirs:  # deepest first; one that is not empty stays
            with contextlib.suppress(OSError):
                _remove(working_dir, rel, os.rmdir)
        raise
    return len(written)


def _check_way(working_dir: Path, rel: str) -> None:
    """Raise when something other than a directory, or than a file at the end, is on ``rel``'s way."""
    parts = PurePosixPath(rel).parts
    for depth in range(1, len(parts) + 1):
        path = working_dir.joinpath(*parts[:depth])
        try:
            mode = os.lstat(path).st_mode
        except FileNotFoundError:
            return
        fits = stat.S_ISREG(mode) if depth == len(parts) else stat.S_ISDIR(mode)
        if not fits:
            raise OSError(errno.EEXIST, "a put-back would replace this", str(path))


def _missing_dirs(working_dir: Path, rels: list[str]) -> list[str]:
    """The directories on the way to ``rels`` that do not exist now, deepest first."""
    missing: set[str] = set()
    for rel in rels:
        parts = PurePosixPath(rel).parts[:-1]
        for depth in range(1, len(parts) + 1):
            prefix = PurePosixPath(*parts[:depth]).as_posix()
            if prefix not in missing and not os.path.lexists(working_dir / prefix):
                missing.add(prefix)
    return sorted(missing, key=lambda rel: (-len(PurePosixPath(rel).parts), rel))


def _remove(working_dir: Path, rel: str, remove: Callable[..., None]) -> None:
    path = PurePosixPath(rel)
    fd = open_dir_inside(working_dir, Path(*path.parts[:-1]))
    try:
        remove(path.name, dir_fd=fd)
    finally:
        os.close(fd)


def _write(working_dir: Path, rel: str, data: bytes, mode: int) -> None:
    path = PurePosixPath(rel)
    fd = open_dir_inside(working_dir, Path(*path.parts[:-1]), create=True)
    try:
        save_in(fd, path.name, data, mode)
    finally:
        os.close(fd)


__all__ = ["MAX_BYTES", "MAX_FILES", "SKIPPED_DIRS", "AppVersion", "keep", "put_back"]
