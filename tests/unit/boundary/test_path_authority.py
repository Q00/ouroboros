"""Publication, tree hashing and target resolution never follow a link anywhere in a path.

Each probe swaps or plants a link in a directory of the path (not only at
the leaf): the publication parent, a manifest directory swapped just before
its file is opened, a code object naming a checkout file that does not exist.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sys
import types
from typing import Any

import pytest

from ouroboros.boundary import harness
from ouroboros.boundary.package import (
    CheckPackageError,
    publish_exact,
    seal_package,
    write_package_record,
)
from ouroboros.boundary.tree import UNREADABLE, tree_manifest
from ouroboros.core.filesystem_capability import RegularFile, resolve_checkout_file

from .conftest import build_package

posix_links = pytest.mark.skipif(sys.platform == "win32", reason="POSIX links and dirfds")


# ---------------------------------------------------------------- publication


@posix_links
def test_a_linked_publication_parent_never_redirects_publication(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    store = tmp_path / "store"
    store.mkdir()
    (store / "packages").symlink_to(outside, target_is_directory=True)

    with pytest.raises(CheckPackageError):
        publish_exact(store / "packages" / "record.json", b"{}")
    assert list(outside.iterdir()) == []


@posix_links
def test_a_linked_store_ancestor_never_redirects_a_record(tmp_path: Path, seed) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "store").symlink_to(outside, target_is_directory=True)

    with pytest.raises(CheckPackageError):
        write_package_record(seal_package(build_package(seed)), tmp_path / "store" / "packages")
    assert list(outside.iterdir()) == []


def test_publication_creates_missing_store_directories(tmp_path: Path) -> None:
    target = tmp_path / "store" / "a" / "b" / "record.json"
    assert publish_exact(target, b"abc") == target
    assert target.read_bytes() == b"abc"


# ---------------------------------------------------------------- tree hashing


@posix_links
def test_a_parent_swapped_for_a_link_before_the_file_open_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "mod.py").write_text("inside\n")
    (root / "keep.py").write_text("keep\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "mod.py").write_text("outside bytes\n")

    real_open = os.open
    swapped: list[bool] = []

    def swapping(path: Any, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if os.fspath(path).endswith("mod.py") and not swapped:
            swapped.append(True)
            (root / "pkg").rename(tmp_path / "moved")
            (root / "pkg").symlink_to(outside, target_is_directory=True)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swapping)
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {swapping})

    manifest = tree_manifest(root)

    assert swapped
    assert manifest["pkg/mod.py"] == UNREADABLE
    assert manifest["pkg/mod.py"] != hashlib.sha256(b"outside bytes\n").hexdigest()
    # The rest of the tree is still hashed: the refusal is the swap's, not a fallback.
    assert manifest["keep.py"] == hashlib.sha256(b"keep\n").hexdigest()


@posix_links
def test_a_linked_directory_is_recorded_as_a_link_and_never_entered(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("outside\n")
    (root / "vendor").symlink_to(outside, target_is_directory=True)
    (root / "mod.py").write_text("x = 1\n")

    manifest = tree_manifest(root)

    assert manifest == {
        "vendor": f"symlink:{outside}",
        "mod.py": hashlib.sha256(b"x = 1\n").hexdigest(),
    }


# ---------------------------------------------------------------- target resolution


def _function_from(filename: Path | str) -> types.FunctionType:
    def target() -> int:
        return 1

    return types.FunctionType(target.__code__.replace(co_filename=str(filename)), {})


def test_a_callable_whose_code_names_a_missing_checkout_file_is_not_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    ghost = _function_from(Path(os.getcwd()) / "ghost.py")
    with pytest.raises(harness._Missing):
        harness._require_inside_checkout("ghost.target", ghost, None)


@pytest.mark.parametrize("filename", ["<string>", "relative.py"])
def test_a_callable_without_a_checkout_file_is_not_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "relative.py").write_text("x = 1\n")
    with pytest.raises(harness._Missing):
        harness._require_inside_checkout("m.target", _function_from(filename), None)


@posix_links
def test_a_checkout_file_resolves_and_a_link_anywhere_in_its_path_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "checkout"
    (checkout / "pkg").mkdir(parents=True)
    (checkout / "pkg" / "mod.py").write_text("def target():\n    return 1\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "mod.py").write_text("def target():\n    return 2\n")
    (checkout / "linked_file.py").symlink_to(outside / "mod.py")
    (checkout / "linked_dir").symlink_to(outside, target_is_directory=True)
    monkeypatch.chdir(checkout)
    cwd = Path(os.getcwd())

    harness._require_inside_checkout("pkg.mod.target", _function_from(cwd / "pkg/mod.py"), None)
    for name in ("linked_file.py", "linked_dir/mod.py"):
        with pytest.raises(harness._Missing):
            harness._require_inside_checkout("m.target", _function_from(cwd / name), None)
    with pytest.raises(harness._Missing):
        harness._require_inside_checkout("m.target", _function_from(outside / "mod.py"), None)


@posix_links
def test_the_harness_proof_agrees_with_the_core_checkout_file_primitive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The target process cannot import ouroboros; its proof must decide
    # exactly as core.filesystem_capability.resolve_checkout_file does.
    checkout = tmp_path / "checkout"
    (checkout / "pkg").mkdir(parents=True)
    (checkout / "pkg" / "mod.py").write_text("x = 1\n")
    (checkout / "top.py").write_text("x = 1\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "mod.py").write_text("x = 2\n")
    (checkout / "linked.py").symlink_to(outside / "mod.py")
    (checkout / "linked_dir").symlink_to(outside, target_is_directory=True)
    os.mkfifo(checkout / "pipe")
    monkeypatch.chdir(checkout)
    cwd = os.getcwd()
    for relative in (
        "pkg/mod.py",
        "top.py",
        "linked.py",
        "linked_dir/mod.py",
        "pkg",
        "pipe",
        "ghost.py",
        "top.py/x.py",
    ):
        expected = isinstance(resolve_checkout_file(cwd, relative), RegularFile)
        assert harness._checkout_file(os.path.join(cwd, relative)) is expected, relative


def test_without_held_traversal_every_path_authority_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A platform without dir_fd support (Windows) never falls back to paths.
    (tmp_path / "mod.py").write_text("x = 1\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(os, "supports_dir_fd", set())

    assert tree_manifest(tmp_path) == {".": UNREADABLE}
    with pytest.raises(CheckPackageError):
        publish_exact(tmp_path / "store" / "record.json", b"{}")
    assert not (tmp_path / "store").exists()
    with pytest.raises(harness._Unprovable):
        harness._require_inside_checkout("mod.target", _function_from(tmp_path / "mod.py"), None)
