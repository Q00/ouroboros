"""Byte manifests never follow a link or block on a special file."""

from __future__ import annotations

import os
from pathlib import Path
import sys

import pytest

from ouroboros.boundary import tree
from ouroboros.boundary.tree import UNREADABLE, tree_manifest, unreadable_paths
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
