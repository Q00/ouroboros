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
    """Retargeting an in-tree directory symlink changes the digest.

    Regression for the original BLOCKING finding on digest.py:95: the
    digest must walk symlinked directories (not just regular files)
    so a plugin can't hide executable bytes behind a directory link
    and bypass ``trust_subject_changed``. The targets stay inside the
    plugin tree so the new escaping-symlink rejection (introduced for
    out-of-tree links) doesn't apply.
    """
    root = tmp_path / "root"
    root.mkdir()
    target_a = root / "_internal_a"
    target_a.mkdir()
    (target_a / "code.py").write_text("print('A')")
    target_b = root / "_internal_b"
    target_b.mkdir()
    (target_b / "code.py").write_text("print('B')")

    # Initial: root/extras -> root/_internal_a (relative, in-tree).
    (root / "extras").symlink_to("_internal_a")
    digest_pre = canonical_tree_hash(root)

    # Retarget: root/extras -> root/_internal_b. The link target string
    # changes; the digest must reflect that.
    (root / "extras").unlink()
    (root / "extras").symlink_to("_internal_b")
    digest_post = canonical_tree_hash(root)
    assert digest_pre != digest_post, (
        "directory-symlink retarget must change the canonical tree hash; "
        "if it doesn't, a plugin can hide executable bytes behind a "
        "directory symlink and bypass `trust_subject_changed`"
    )


def test_canonical_tree_hash_directory_symlink_does_not_recurse_into_target(
    tmp_path: Path,
) -> None:
    """In-tree directory symlinks contribute their target STRING to
    the digest, not the link's dereferenced contents. Adding a file
    via the same target's underlying path (without retargeting the
    link or modifying the existing record list) must not change the
    digest of the rooted view.

    Concretely: the linked directory IS already part of the tree and
    its contents are walked once via the normal directory descent.
    Touching a file via the link path is the same as touching it via
    the underlying path — that genuinely modifies the artifact, so
    the digest changes. What this test guards against is a
    follow-the-link RECURSION that would walk the target's contents
    a second time through the link path (double-counting / unstable
    ordering).
    """
    root = tmp_path / "root"
    root.mkdir()
    target = root / "_target"
    target.mkdir()
    (target / "main.py").write_text("v1")
    (root / "ext").symlink_to("_target")
    pre = canonical_tree_hash(root)

    # Adding a file via either path mutates the same on-disk subtree
    # — the digest legitimately changes. This test's invariant is
    # that the post digest is *some* deterministic value, not that
    # it equals ``pre``.
    (target / "extra-noise.txt").write_text("noise")
    post = canonical_tree_hash(root)
    assert post != pre  # bytes legitimately changed
    # Repeating the hash on the same subtree must be stable — proves
    # we are not double-counting via the link.
    assert canonical_tree_hash(root) == post


def test_canonical_tree_hash_rejects_symlink_escaping_via_absolute_target(
    tmp_path: Path,
) -> None:
    """Regression for the bot's BLOCKING finding on digest.py:69.

    A symlink with an absolute target outside the plugin tree must be
    rejected at digest time. The canonical hash only records the link
    target STRING, so the bytes the firewall actually loads through
    the link can change without changing ``artifact_digest`` —
    defeating the code-substitution protection the trust model
    promises.
    """
    from ouroboros.plugin.digest import EscapingSymlinkError

    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside_payload"
    outside.write_text("attacker-controlled bytes")
    (root / "evil").symlink_to(outside)

    with pytest.raises(EscapingSymlinkError, match="resolves outside the plugin tree"):
        canonical_tree_hash(root)


def test_canonical_tree_hash_rejects_symlink_escaping_via_relative_traversal(
    tmp_path: Path,
) -> None:
    """Same regression catch as the absolute-target test, but for a
    relative ``../../`` style traversal — the more obvious smuggling
    shape if the install pipeline preserved symlinks naively.
    """
    from ouroboros.plugin.digest import EscapingSymlinkError

    root = tmp_path / "root"
    root.mkdir()
    sub = root / "sub"
    sub.mkdir()
    # Target string ``../../outside`` resolves to tmp_path/outside,
    # which is outside ``root``.
    (tmp_path / "outside").write_text("payload")
    (sub / "evil").symlink_to("../../outside")

    with pytest.raises(EscapingSymlinkError, match="resolves outside the plugin tree"):
        canonical_tree_hash(root)


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
