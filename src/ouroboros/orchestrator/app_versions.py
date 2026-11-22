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
after a version stay when it is put back: nothing is deleted.
"""

from __future__ import annotations

from collections.abc import Mapping
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

    Too many files, too many bytes or a file that cannot be read keeps nothing:
    putting part of a version back would leave a mix that was never played.
    """
    files: dict[str, tuple[bytes, int]] = {}
    total = 0
    for root, dirs, names in os.walk(working_dir):
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

    When a write fails part-way, each file written over is put back as it was and
    the error is raised, so the app is not left a mix of two versions. When the
    app's files now cannot all be kept, nothing is written.
    """
    now = keep(working_dir)
    if now is None:
        raise OSError(errno.EFBIG, "the app's files cannot all be kept", str(working_dir))
    changed = [
        (rel, data, mode)
        for rel, (data, mode) in sorted(version.files.items())
        if now.files.get(rel) != (data, mode)
    ]
    written: list[str] = []
    try:
        for rel, data, mode in changed:
            _write(working_dir, rel, data, mode)
            written.append(rel)
    except OSError:
        for rel in written:
            if rel in now.files:
                with contextlib.suppress(OSError):
                    _write(working_dir, rel, *now.files[rel])
        raise
    return len(written)


def _write(working_dir: Path, rel: str, data: bytes, mode: int) -> None:
    path = PurePosixPath(rel)
    fd = open_dir_inside(working_dir, Path(*path.parts[:-1]), create=True)
    try:
        save_in(fd, path.name, data, mode)
    finally:
        os.close(fd)


__all__ = ["MAX_BYTES", "MAX_FILES", "SKIPPED_DIRS", "AppVersion", "keep", "put_back"]
