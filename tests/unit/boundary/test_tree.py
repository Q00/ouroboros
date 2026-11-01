"""Byte manifests never follow a link or block on a special file."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys

import pytest

from ouroboros.boundary import tree
from ouroboros.boundary.tree import (
    COPY_ATTEMPTS,
    UNREADABLE,
    CheckoutCopyError,
    copy_checkout,
    tree_manifest,
    unreadable_paths,
)
from ouroboros.core.filesystem_capability import open_directory_anchor


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX named pipes")
def test_a_named_pipe_is_unreadable_and_never_blocks(tmp_path: Path) -> None:
    (tmp_path / "code.py").write_text("x = 1\n")
    os.mkfifo(tmp_path / "pipe")

    manifest = tree_manifest(tmp_path)

    assert manifest["pipe"] == UNREADABLE
    assert unreadable_paths(manifest) == ("pipe",)
    assert manifest["code.py"] != UNREADABLE


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_hashing_a_path_swapped_for_a_link_refuses_it(tmp_path: Path) -> None:
    # A file replaced by a link after it was listed is not read through the link.
    secret = tmp_path / "secret.txt"
    secret.write_text("outside\n")
    swapped = tmp_path / "swapped.py"
    swapped.symlink_to(secret)

    anchor = open_directory_anchor(tmp_path)
    try:
        with pytest.raises(OSError):
            tree._file_sha256(anchor, "swapped.py")
    finally:
        anchor.close()


def test_a_tree_deeper_than_the_walk_can_follow_is_unreadable_not_a_crash(
    tmp_path: Path,
) -> None:
    import inspect

    (tmp_path / "top.py").write_text("x = 1\n")
    deep = tmp_path / "d"
    for _ in range(80):
        deep = deep / "d"
    deep.mkdir(parents=True)
    (deep / "leaf.py").write_text("y = 2\n")
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(len(inspect.stack()) + 40)
    try:
        manifest = tree_manifest(tmp_path)
    finally:
        sys.setrecursionlimit(limit)

    assert manifest["top.py"] != UNREADABLE
    assert unreadable_paths(manifest)
    assert not any(
        path.endswith("leaf.py") and value != UNREADABLE for path, value in manifest.items()
    )


def _copy_error(source: Path, name: str) -> shutil.Error:
    """The error ``shutil.copytree`` reports when ``name`` vanishes after the walk."""
    return shutil.Error(
        [(str(source / name), "", f"[Errno 2] No such file or directory: '{source / name}'")]
    )


def test_a_file_that_vanishes_during_the_copy_is_copied_after_a_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #2808: Git maintenance removes a transient lock between the directory walk
    # and the copy. The first attempt loses that file; the retry walks the tree
    # again and copies what is really there.
    source = tmp_path / "source"
    source.mkdir()
    (source / "kept.py").write_text("kept = True\n")
    (source / "maintenance.lock").write_text("transient\n")
    destination = tmp_path / "copy"
    real_copytree = shutil.copytree
    attempts: list[int] = []

    def racing_copytree(src: Path, dst: Path, **kwargs: object) -> object:
        attempts.append(1)

        def copy_function(from_path: str, to_path: str, **inner: object) -> object:
            if Path(from_path).name == "kept.py" and len(attempts) == 1:
                (source / "maintenance.lock").unlink()
            return shutil.copy2(from_path, to_path, **inner)

        return real_copytree(src, dst, copy_function=copy_function, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(shutil, "copytree", racing_copytree)

    copy_checkout(source, destination)

    assert len(attempts) == 2, "the disturbed copy must be retried, not published or raised"
    assert (destination / "kept.py").read_text() == "kept = True\n"
    assert not (destination / "maintenance.lock").exists()


def test_a_copy_that_keeps_failing_raises_and_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "code.py").write_text("x = 1\n")
    destination = tmp_path / "copy"
    attempts: list[int] = []

    def always_racing(src: object, dst: object, **kwargs: object) -> object:
        attempts.append(1)
        raise _copy_error(source, "maintenance.lock")

    monkeypatch.setattr(shutil, "copytree", always_racing)

    with pytest.raises(CheckoutCopyError):
        copy_checkout(source, destination)

    assert len(attempts) == COPY_ATTEMPTS, "the retry is bounded"
    assert not destination.exists(), "a partial copy must never be published"
    assert list(tmp_path.iterdir()) == [source], "the scratch tree is cleaned up"


def test_a_failed_replacement_keeps_the_previous_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "new.py").write_text("new = True\n")
    destination = tmp_path / "snapshot"
    destination.mkdir()
    (destination / "old.py").write_text("old = True\n")

    def always_racing(src: object, dst: object, **kwargs: object) -> object:
        raise _copy_error(source, "maintenance.lock")

    monkeypatch.setattr(shutil, "copytree", always_racing)

    with pytest.raises(CheckoutCopyError):
        copy_checkout(source, destination, replace=True)

    assert (destination / "old.py").read_text() == "old = True\n"
    assert not (destination / "new.py").exists()


def test_an_existing_destination_is_refused_unless_replacing(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "code.py").write_text("x = 1\n")
    destination = tmp_path / "copy"
    destination.mkdir()

    with pytest.raises(FileExistsError):
        copy_checkout(source, destination)


def test_replacing_publishes_the_new_checkout(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "new.py").write_text("new = True\n")
    destination = tmp_path / "copy"
    destination.mkdir()
    (destination / "old.py").write_text("old = True\n")

    copy_checkout(source, destination, replace=True)

    assert (destination / "new.py").read_text() == "new = True\n"
    assert not (destination / "old.py").exists()


def test_a_copy_that_cannot_start_raises_the_original_error(tmp_path: Path) -> None:
    # A missing source is deterministic: no retry, and the caller keeps the
    # OSError subclass it has always seen (probe_runner reports its name).
    destination = tmp_path / "copy"

    with pytest.raises(FileNotFoundError):
        copy_checkout(tmp_path / "missing", destination)

    assert not destination.exists()
