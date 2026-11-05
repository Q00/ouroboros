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


def test_a_failed_promotion_keeps_the_previous_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #2808 review round 2: the promotion that replaces a published snapshot must
    # not delete it before the new copy is known to be in place.
    source = tmp_path / "source"
    source.mkdir()
    (source / "new.py").write_text("new = True\n")
    destination = tmp_path / "snapshot"
    destination.mkdir()
    (destination / "old.py").write_text("old = True\n")

    real_replace = os.replace

    def failing_promotion(src: object, dst: object) -> None:
        if str(src).endswith(".copying") and Path(dst) == destination:
            raise OSError("promotion failed")
        real_replace(src, dst)

    monkeypatch.setattr(tree.os, "replace", failing_promotion)

    with pytest.raises(OSError, match="promotion failed"):
        copy_checkout(source, destination, replace=True)

    assert (destination / "old.py").read_text() == "old = True\n", "the baseline survives"
    assert not (destination / "new.py").exists()
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == [], "no scratch left"


def test_a_failed_rename_aside_leaves_no_scratch_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #2808 review round 3: the first publication rename can fail too; the completed
    # scratch copy must not be left behind when it does.
    source = tmp_path / "source"
    source.mkdir()
    (source / "new.py").write_text("new = True\n")
    destination = tmp_path / "snapshot"
    destination.mkdir()
    (destination / "old.py").write_text("old = True\n")

    real_replace = os.replace

    def failing_first_rename(src: object, dst: object) -> None:
        if Path(src) == destination:
            raise OSError("rename aside failed")
        real_replace(src, dst)

    monkeypatch.setattr(tree.os, "replace", failing_first_rename)

    with pytest.raises(OSError, match="rename aside failed"):
        copy_checkout(source, destination, replace=True)

    assert (destination / "old.py").read_text() == "old = True\n", "the baseline survives"
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == [], "no scratch left"


def test_a_failed_first_publication_leaves_no_scratch_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #2808 review round 4: the same cleanup has to cover the path where there is no
    # destination to replace yet.
    source = tmp_path / "source"
    source.mkdir()
    (source / "code.py").write_text("x = 1\n")
    destination = tmp_path / "snapshot"

    def failing_replace(src: object, dst: object) -> None:
        raise OSError("first publication failed")

    monkeypatch.setattr(tree.os, "replace", failing_replace)

    with pytest.raises(OSError, match="first publication failed"):
        copy_checkout(source, destination)

    assert not destination.exists()
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == [], "no scratch left"


@pytest.mark.parametrize("sibling_name", [".copy.copying", ".copy.previous"])
def test_copy_preserves_unowned_sibling_data(tmp_path: Path, sibling_name: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "new.py").write_text("new\n")
    destination = tmp_path / "copy"
    destination.mkdir()
    (destination / "old.py").write_text("old\n")
    sibling = tmp_path / sibling_name
    sibling.mkdir()
    (sibling / "user.txt").write_text("keep\n")

    copy_checkout(source, destination, replace=True)

    assert (sibling / "user.txt").read_text() == "keep\n"
    assert (destination / "new.py").read_text() == "new\n"


@pytest.mark.parametrize("late_kind", ["directory", "file", "symlink"])
def test_a_destination_created_during_copy_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, late_kind: str
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "new.py").write_text("new\n")
    destination = tmp_path / "copy"
    real_copytree = shutil.copytree

    def create_destination(src: Path, dst: Path, **kwargs: object) -> object:
        result = real_copytree(src, dst, **kwargs)
        if late_kind == "directory":
            destination.mkdir()
            (destination / "user.txt").write_text("keep\n")
        elif late_kind == "file":
            destination.write_text("keep\n")
        else:
            destination.symlink_to("missing-target")
        return result

    monkeypatch.setattr(shutil, "copytree", create_destination)

    with pytest.raises(FileExistsError):
        copy_checkout(source, destination)

    if late_kind == "directory":
        assert (destination / "user.txt").read_text() == "keep\n"
    elif late_kind == "file":
        assert destination.read_text() == "keep\n"
    else:
        assert destination.is_symlink()
        assert os.readlink(destination) == "missing-target"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["copy", "source"]


def test_interleaved_copies_do_not_remove_each_others_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "new.py").write_text("new\n")
    destination = tmp_path / "copy"
    real_copytree = shutil.copytree
    nested = False

    def interleaved_copy(src: Path, dst: Path, **kwargs: object) -> object:
        nonlocal nested
        result = real_copytree(src, dst, **kwargs)
        if not nested:
            nested = True
            copy_checkout(source, destination)
        return result

    monkeypatch.setattr(shutil, "copytree", interleaved_copy)

    with pytest.raises(FileExistsError):
        copy_checkout(source, destination)

    assert (destination / "new.py").read_text() == "new\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["copy", "source"]


def test_failed_rollback_keeps_a_recoverable_previous_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "new.py").write_text("new\n")
    destination = tmp_path / "copy"
    destination.mkdir()
    (destination / "old.py").write_text("keep\n")
    real_replace = os.replace
    recovery_paths: list[Path] = []

    def fail_publication_and_rollback(src: object, dst: object) -> None:
        if Path(src) == destination:
            recovery_paths.append(Path(dst))
            real_replace(src, dst)
        else:
            raise OSError("publication and rollback failed")

    monkeypatch.setattr(tree.os, "replace", fail_publication_and_rollback)

    with pytest.raises(CheckoutCopyError) as raised:
        copy_checkout(source, destination, replace=True)

    previous = recovery_paths[0]
    assert str(previous) in str(raised.value)
    assert (previous / "old.py").read_text() == "keep\n"
    assert not destination.exists()
    assert sorted(previous.parent.iterdir()) == [previous]


def test_copy_creates_missing_destination_parents(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "code.py").write_text("code\n")
    destination = tmp_path / "missing" / "parents" / "copy"

    copy_checkout(source, destination)

    assert (destination / "code.py").read_text() == "code\n"
    assert list(destination.parent.iterdir()) == [destination]
