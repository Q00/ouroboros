"""Tests for `ouroboros.plugin.digest.canonical_tree_hash`.

The canonical tree hash is the trust subject's `artifact_digest`. Per
the locked RFC (`docs/rfc/userlevel-plugins.md`, "Trust identity"), it
MUST cover every executable path the plugin can run, including
symlinks (both file symlinks AND directory symlinks). A digest that
ignored any of those would let a plugin retarget hidden bytes without
producing a `trust_subject_changed` failure on the next invocation.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ouroboros.plugin.digest import (
    UnsupportedFileTypeError,
    canonical_tree_hash,
    normalize_repo_url,
)


def test_canonical_tree_hash_stable_for_identical_subtree(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "manifest.json").write_text('{"k": 1}')
    (b / "manifest.json").write_text('{"k": 1}')
    assert canonical_tree_hash(a) == canonical_tree_hash(b)


def test_canonical_tree_hash_changes_when_file_content_changes(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "f.txt").write_text("v1")
    pre = canonical_tree_hash(root)
    (root / "f.txt").write_text("v2")
    assert canonical_tree_hash(root) != pre


def test_canonical_tree_hash_changes_when_directory_symlink_target_changes(
    tmp_path: Path,
) -> None:
    """Regression for the bot's BLOCKING finding on digest.py:95.

    A plugin can hide executable content behind a directory symlink and
    later retarget that symlink without touching any other byte. Before
    this fix the digest only walked ``filenames`` from ``os.walk``, so
    directory symlinks were silently ignored — defeating the trust
    model's "bytes drift = re-grant required" invariant.

    With the fix, retargeting the symlink changes the symlink record's
    `<sha256-of-link-target>` and the digest changes accordingly.
    """
    root = tmp_path / "root"
    root.mkdir()
    target_a = tmp_path / "target_a"
    target_a.mkdir()
    (target_a / "code.py").write_text("print('A')")
    target_b = tmp_path / "target_b"
    target_b.mkdir()
    (target_b / "code.py").write_text("print('B')")

    # Initial: root/extras -> target_a
    (root / "extras").symlink_to(target_a)
    digest_pre = canonical_tree_hash(root)

    # Retarget: root/extras -> target_b. Bytes inside `root` itself are
    # unchanged at the regular-file layer; only the symlink target moved.
    (root / "extras").unlink()
    (root / "extras").symlink_to(target_b)
    digest_post = canonical_tree_hash(root)
    assert digest_pre != digest_post, (
        "directory-symlink retarget must change the canonical tree hash; "
        "if it doesn't, a plugin can hide executable bytes behind a "
        "directory symlink and bypass `trust_subject_changed`"
    )


def test_canonical_tree_hash_directory_symlink_does_not_recurse_into_target(
    tmp_path: Path,
) -> None:
    """The walk runs with `followlinks=False` and the new code path
    explicitly drops symlinked directories from the descent list. Adding
    a file deep inside the link's target therefore does NOT change the
    digest of the root subtree — only retargeting the link itself does.
    Without this guarantee, an attacker could perturb a file that the
    plugin doesn't even ship with to fake a digest change on every
    invocation, and conversely an unrelated file's modification would
    spuriously block invocation.
    """
    root = tmp_path / "root"
    root.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    (target / "main.py").write_text("v1")
    (root / "ext").symlink_to(target)
    pre = canonical_tree_hash(root)

    # Mutate INSIDE the symlink target (not via the symlink path).
    (target / "extra-noise.txt").write_text("noise")
    post = canonical_tree_hash(root)
    assert pre == post, (
        "follow-the-link recursion would let unrelated changes inside the "
        "linked target perturb the digest; the canonical hash must only "
        "depend on the symlink target string, not the target's contents"
    )


def test_canonical_tree_hash_rejects_unsupported_file_type(tmp_path: Path) -> None:
    """FIFOs / devices / sockets are rejected at install time per the
    RFC. The hash function refuses to canonicalize them so unknown file
    types cannot sneak into the trust subject.
    """
    root = tmp_path / "root"
    root.mkdir()
    fifo = root / "weird"
    try:
        os.mkfifo(fifo)
    except (AttributeError, OSError):
        pytest.skip("platform does not support FIFO creation")
    with pytest.raises(UnsupportedFileTypeError):
        canonical_tree_hash(root)


def test_normalize_repo_url_strips_userinfo_and_dot_git(tmp_path: Path) -> None:
    assert (
        normalize_repo_url("https://user:secret@github.com/Q00/repo.git#frag")
        == "https://github.com/Q00/repo"
    )


def test_normalize_repo_url_preserves_scheme(tmp_path: Path) -> None:
    # http and https are deliberately distinct trust subjects per the RFC.
    assert normalize_repo_url("http://github.com/x/y") != normalize_repo_url(
        "https://github.com/x/y"
    )


def test_normalize_repo_url_unwraps_git_plus_https() -> None:
    """Regression: ``git+https://repo`` and ``https://repo`` clone the
    same upstream, so they MUST canonicalize to a single
    ``source_identity``. Recording them as different sources splits
    trust on cosmetic spelling and breaks ``ooo plugin install <name>``
    when the catalog ends up with both forms.
    """
    canonical = normalize_repo_url("https://github.com/Q00/plug")
    assert normalize_repo_url("git+https://github.com/Q00/plug") == canonical
    assert normalize_repo_url("git+https://github.com/Q00/plug.git") == canonical


def test_normalize_repo_url_unwraps_git_plus_ssh_to_ssh() -> None:
    """``git+ssh://`` is just ``ssh://`` with a Git-flavored wrapper —
    same transport, same host, same trust subject. It must not be
    recorded under a separate identity than plain ``ssh://``."""
    canonical = normalize_repo_url("ssh://git@github.com/Q00/plug")
    assert normalize_repo_url("git+ssh://git@github.com/Q00/plug") == canonical
    assert normalize_repo_url("git+ssh://git@github.com/Q00/plug.git") == canonical


def test_normalize_repo_url_canonicalizes_plain_ssh() -> None:
    """Plain ``ssh://`` URLs must go through the same userinfo-strip /
    host-lowercase path as https — otherwise a uppercase host or an
    embedded user creates a fake "different source" record.
    """
    assert normalize_repo_url("ssh://git@GitHub.com/Q00/plug.git") == "ssh://github.com/Q00/plug"
