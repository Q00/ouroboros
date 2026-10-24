"""Held no-follow directory capabilities: checkout file proofs and inode-attributed cleanup."""

from __future__ import annotations

import os
from pathlib import Path
import sys

import pytest

from ouroboros.core import filesystem_capability
from ouroboros.core.filesystem_capability import (
    CheckoutFileRefusal,
    HeldPathChanged,
    RegularFile,
    open_directory_anchor,
    open_nofollow_directory_chain,
    resolve_checkout_file,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX links and dirfds")


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "mod.py").write_text("x = 1\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "mod.py").write_text("x = 2\n")
    (root / "linked.py").symlink_to(outside / "mod.py")
    (root / "linked_dir").symlink_to(outside, target_is_directory=True)
    (root / "plain.txt").write_text("text\n")
    os.mkfifo(root / "pipe")
    return root


def test_a_regular_checkout_file_is_read_with_its_identity(checkout: Path) -> None:
    resolved = resolve_checkout_file(checkout, "pkg/mod.py")
    assert isinstance(resolved, RegularFile)
    status = os.stat(checkout / "pkg" / "mod.py")
    assert (resolved.device, resolved.inode, resolved.size, resolved.data) == (
        status.st_dev,
        status.st_ino,
        6,
        b"x = 1\n",
    )


@pytest.mark.parametrize(
    ("relative", "refusal"),
    [
        ("pkg/missing.py", CheckoutFileRefusal.MISSING),
        ("linked.py", CheckoutFileRefusal.LINK),
        ("linked_dir/mod.py", CheckoutFileRefusal.LINK),
        ("plain.txt/mod.py", CheckoutFileRefusal.NOT_DIRECTORY),
        ("pkg", CheckoutFileRefusal.NOT_REGULAR),
        ("pipe", CheckoutFileRefusal.NOT_REGULAR),
        ("../outside/mod.py", CheckoutFileRefusal.NOT_CHECKOUT_RELATIVE),
        ("/etc/hosts", CheckoutFileRefusal.NOT_CHECKOUT_RELATIVE),
        ("pkg//mod.py", CheckoutFileRefusal.NOT_CHECKOUT_RELATIVE),
        ("", CheckoutFileRefusal.NOT_CHECKOUT_RELATIVE),
    ],
)
def test_anything_but_a_regular_file_reached_without_a_link_is_refused(
    checkout: Path, relative: str, refusal: CheckoutFileRefusal
) -> None:
    assert resolve_checkout_file(checkout, relative) is refusal


def test_a_missing_root_is_refused(tmp_path: Path) -> None:
    assert resolve_checkout_file(tmp_path / "absent", "x.py") is CheckoutFileRefusal.ROOT_UNREADABLE


def test_without_held_traversal_everything_fails_closed(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "supports_dir_fd", set())
    assert resolve_checkout_file(checkout, "pkg/mod.py") is CheckoutFileRefusal.UNAVAILABLE
    with pytest.raises(OSError):
        open_directory_anchor(checkout)
    with pytest.raises(OSError):
        open_nofollow_directory_chain(checkout, create_missing=True)


def test_a_directory_moved_while_its_file_is_read_is_refused(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_read = os.read
    moved: list[bool] = []

    def moving(descriptor: int, size: int) -> bytes:
        if not moved:
            moved.append(True)
            (checkout / "pkg").rename(checkout / "elsewhere")
        return real_read(descriptor, size)

    monkeypatch.setattr(os, "read", moving)
    assert resolve_checkout_file(checkout, "pkg/mod.py") is CheckoutFileRefusal.MOVED
    assert moved


def test_create_exclusive_publishes_once_and_never_through_a_link(tmp_path: Path) -> None:
    (tmp_path / "store").mkdir()
    (tmp_path / "store" / "link.json").symlink_to(tmp_path / "missing.json")
    chain = open_nofollow_directory_chain(tmp_path / "store")
    try:
        assert chain.create_exclusive("record.json", b"abc") is True
        assert chain.create_exclusive("record.json", b"abd") is False
        assert chain.create_exclusive("link.json", b"abc") is False
    finally:
        chain.close()
    assert (tmp_path / "store" / "record.json").read_bytes() == b"abc"
    assert not (tmp_path / "missing.json").exists()


def test_a_failed_publication_removes_only_the_file_it_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "store"
    store.mkdir()
    real_write = os.write

    def replaced_then_failing(descriptor: int, data: object) -> int:
        # Another writer takes the name before this write fails.
        (store / "record.json").rename(store / "ours.json")
        (store / "record.json").write_bytes(b"theirs")
        raise OSError("disk full")

    chain = open_nofollow_directory_chain(store)
    try:
        monkeypatch.setattr(os, "write", replaced_then_failing)
        with pytest.raises(OSError, match="disk full"):
            chain.create_exclusive("record.json", b"abc")
        monkeypatch.setattr(os, "write", real_write)
        assert (store / "record.json").read_bytes() == b"theirs"

        def failing(descriptor: int, data: object) -> int:
            raise OSError("disk full")

        monkeypatch.setattr(os, "write", failing)
        with pytest.raises(OSError, match="disk full"):
            chain.create_exclusive("fresh.json", b"abc")
    finally:
        monkeypatch.setattr(os, "write", real_write)
        chain.close()
    assert not (store / "fresh.json").exists()


def test_missing_directories_are_created_through_held_parents(tmp_path: Path) -> None:
    chain = open_nofollow_directory_chain(tmp_path / "a" / "b", create_missing=True)
    try:
        assert chain.postvalidate()
    finally:
        chain.close()
    assert (tmp_path / "a" / "b").is_dir()
    (tmp_path / "c").symlink_to(tmp_path / "a", target_is_directory=True)
    with pytest.raises(OSError):
        open_nofollow_directory_chain(tmp_path / "c" / "d", create_missing=True)
    assert not (tmp_path / "a" / "d").exists()


def test_a_descended_chain_closes_only_its_own_descriptor(checkout: Path) -> None:
    anchor = open_directory_anchor(checkout)
    try:
        child = anchor.descend("pkg")
        assert child.descriptor_count == 2
        assert child.postvalidate()
        child.close()
        assert anchor.names()  # the anchor's descriptor is still held
        with pytest.raises(OSError):
            anchor.descend("linked_dir")
    finally:
        anchor.close()
    assert filesystem_capability.nofollow_directory_capabilities_available()


def test_a_leaf_replaced_while_it_is_written_is_never_reported_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "store"
    store.mkdir()
    real_write = os.write
    swapped: list[bool] = []

    def swapping(descriptor: int, data: object) -> int:
        if not swapped:
            swapped.append(True)
            (store / "record.json").rename(store / "ours.json")
            (store / "record.json").write_bytes(b"planted")
        return real_write(descriptor, data)  # type: ignore[arg-type]

    chain = open_nofollow_directory_chain(store)
    try:
        monkeypatch.setattr(os, "write", swapping)
        with pytest.raises(HeldPathChanged):
            chain.create_exclusive("record.json", b"expected")
    finally:
        monkeypatch.setattr(os, "write", real_write)
        chain.close()
    assert swapped
    # The planted file is not ours to remove; nothing reported it as published.
    assert (store / "record.json").read_bytes() == b"planted"


def test_publish_exact_refuses_a_target_replaced_while_it_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ouroboros.boundary.package import CheckPackageError, publish_exact

    target = tmp_path / "store" / "record.json"
    target.parent.mkdir()
    real_write = os.write

    def swapping(descriptor: int, data: object) -> int:
        if target.exists() and target.read_bytes() == b"":
            target.rename(tmp_path / "store" / "ours.json")
            target.write_bytes(b"planted")
        return real_write(descriptor, data)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "write", swapping)
    with pytest.raises(CheckPackageError):
        publish_exact(target, b"expected")
    monkeypatch.setattr(os, "write", real_write)
    assert target.read_bytes() == b"planted"
