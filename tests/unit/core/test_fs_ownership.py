"""Tests for the claim-then-verify filesystem primitives."""

from __future__ import annotations

import errno
import os
from pathlib import Path
import stat

import pytest

from ouroboros.core import fs_ownership
from ouroboros.core.fs_ownership import (
    UnownedArtifactError,
    claim_and_remove_owned,
    publish_owned_file,
    publish_owned_tree,
)


def _claim_siblings(path: Path, suffix: str) -> list[Path]:
    return sorted(path.parent.glob(f".{path.name}.*.{suffix}"))


@pytest.fixture(autouse=True)
def _isolated_transaction_ledger(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Keep the writer-owned transaction ledger out of the real home directory
    (and out of the shared-parent ``tmp_path`` the tests inspect)."""
    ledger = tmp_path_factory.mktemp("txn-ledger")
    monkeypatch.setenv(fs_ownership._TRANSACTION_LEDGER_ENV, str(ledger))
    return ledger


def test_remove_deletes_only_an_owned_claimed_generation(tmp_path: Path) -> None:
    target = tmp_path / "artifact.txt"
    target.write_text("owned\n", encoding="utf-8")

    assert claim_and_remove_owned(target, is_owned=lambda _p: True)
    assert not target.exists()
    assert not claim_and_remove_owned(target, is_owned=lambda _p: True)


def test_remove_restores_an_unowned_claimed_generation(tmp_path: Path) -> None:
    target = tmp_path / "artifact.txt"
    target.write_text("operator\n", encoding="utf-8")

    assert not claim_and_remove_owned(target, is_owned=lambda _p: False)
    assert target.read_text(encoding="utf-8") == "operator\n"


def test_remove_preserves_a_generation_recreated_while_the_claim_was_held(
    tmp_path: Path,
) -> None:
    """Restoration must not clobber a canonical path recreated after the claim."""
    target = tmp_path / "artifact.txt"
    target.write_text("old generation\n", encoding="utf-8")

    def recreate_then_reject(claimed: Path) -> bool:
        target.write_text("new concurrent generation\n", encoding="utf-8")
        return False

    assert not claim_and_remove_owned(target, is_owned=recreate_then_reject)

    assert target.read_text(encoding="utf-8") == "new concurrent generation\n"
    preserved = _claim_siblings(target, "removing")
    assert len(preserved) == 1
    assert preserved[0].read_text(encoding="utf-8") == "old generation\n"


def test_publish_never_writes_through_a_symlink(tmp_path: Path) -> None:
    external = tmp_path / "external.txt"
    external.write_text("external target\n", encoding="utf-8")
    target = tmp_path / "artifact.txt"
    try:
        target.symlink_to(external)
    except OSError:
        pytest.skip("symlinks are not supported on this platform")

    with pytest.raises(UnownedArtifactError):
        publish_owned_file(target, "managed\n", is_owned=lambda _p: True)

    assert external.read_text(encoding="utf-8") == "external target\n"
    assert target.is_symlink()


def test_publish_refuses_an_unowned_existing_generation(tmp_path: Path) -> None:
    target = tmp_path / "artifact.txt"
    target.write_text("operator\n", encoding="utf-8")

    with pytest.raises(UnownedArtifactError):
        publish_owned_file(target, "managed\n", is_owned=lambda _p: False)

    assert target.read_text(encoding="utf-8") == "operator\n"


def test_publish_preserves_a_generation_recreated_while_the_claim_was_held(
    tmp_path: Path,
) -> None:
    target = tmp_path / "artifact.txt"
    target.write_text("old generation\n", encoding="utf-8")

    def recreate_then_reject(claimed: Path) -> bool:
        target.write_text("new concurrent generation\n", encoding="utf-8")
        return False

    with pytest.raises(UnownedArtifactError):
        publish_owned_file(target, "managed\n", is_owned=recreate_then_reject)

    assert target.read_text(encoding="utf-8") == "new concurrent generation\n"
    preserved = _claim_siblings(target, "replacing")
    assert len(preserved) == 1
    assert preserved[0].read_text(encoding="utf-8") == "old generation\n"


def test_publish_replaces_an_owned_generation(tmp_path: Path) -> None:
    target = tmp_path / "artifact.txt"
    target.write_text("previous managed\n", encoding="utf-8")

    publish_owned_file(target, "next managed\n", is_owned=lambda _p: True)

    assert target.read_text(encoding="utf-8") == "next managed\n"
    assert _claim_siblings(target, "replacing") == []


def test_publish_fails_when_canonical_recreated_after_validation(tmp_path: Path) -> None:
    """The final rename is no-replace: a generation recreated after ownership
    validation is preserved and the publication fails."""
    target = tmp_path / "artifact.txt"
    target.write_text("owned generation\n", encoding="utf-8")

    def approve_then_recreate(claimed: Path) -> bool:
        target.write_text("recreated generation\n", encoding="utf-8")
        return True

    with pytest.raises(UnownedArtifactError, match="recreated during publication"):
        publish_owned_file(target, "managed\n", is_owned=approve_then_recreate)

    assert target.read_text(encoding="utf-8") == "recreated generation\n"
    preserved = _claim_siblings(target, "replacing")
    assert len(preserved) == 1
    assert preserved[0].read_text(encoding="utf-8") == "owned generation\n"


def test_tree_publish_fails_when_canonical_recreated_after_validation(
    tmp_path: Path,
) -> None:
    target = tmp_path / "artifact-dir"
    target.mkdir()
    (target / "content.txt").write_text("owned generation\n", encoding="utf-8")

    def approve_then_recreate(claimed: Path) -> bool:
        target.mkdir()
        return True

    def build(staging: Path) -> None:
        (staging / "content.txt").write_text("next generation\n", encoding="utf-8")

    with pytest.raises(UnownedArtifactError, match="recreated during publication"):
        publish_owned_tree(target, build, is_owned=approve_then_recreate)

    assert target.is_dir()
    assert list(target.iterdir()) == []
    preserved = _claim_siblings(target, "replacing")
    assert len(preserved) == 1
    assert (preserved[0] / "content.txt").read_text(encoding="utf-8") == "owned generation\n"


def test_publish_refuses_symlinked_parent_component(tmp_path: Path) -> None:
    """A symlinked component below the trusted ancestor cannot redirect writes."""
    profile = tmp_path / "profile"
    profile.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    try:
        (profile / "ouroboros").symlink_to(external)
    except OSError:
        pytest.skip("symlinks are not supported on this platform")

    with pytest.raises(OSError, match="symlinked artifact parent component"):
        publish_owned_file(
            profile / "ouroboros" / "artifact.yaml",
            "managed\n",
            is_owned=lambda _p: True,
            trusted_ancestor=profile,
        )

    assert list(external.iterdir()) == []


def test_remove_refuses_symlinked_parent_component(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "artifact.yaml").write_text("external content\n", encoding="utf-8")
    try:
        (profile / "ouroboros").symlink_to(external)
    except OSError:
        pytest.skip("symlinks are not supported on this platform")

    with pytest.raises(OSError, match="symlinked artifact parent component"):
        claim_and_remove_owned(
            profile / "ouroboros" / "artifact.yaml",
            is_owned=lambda _p: True,
            trusted_ancestor=profile,
        )

    assert (external / "artifact.yaml").read_text(encoding="utf-8") == "external content\n"


def test_publish_restores_claim_when_file_staging_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A staging failure after the claim must restore the canonical generation."""
    target = tmp_path / "artifact.txt"
    target.write_text("owned generation\n", encoding="utf-8")

    def broken_temp(self: object, prefix: str) -> tuple[int, str]:
        raise OSError("simulated staging failure")

    monkeypatch.setattr(fs_ownership._PinnedParent, "create_temp_file", broken_temp)

    with pytest.raises(OSError, match="simulated staging failure"):
        publish_owned_file(target, "managed\n", is_owned=lambda _p: True)

    assert target.read_text(encoding="utf-8") == "owned generation\n"
    assert _claim_siblings(target, "replacing") == []


def test_tree_publish_restores_claim_when_build_fails(tmp_path: Path) -> None:
    target = tmp_path / "artifact-dir"
    target.mkdir()
    (target / "content.txt").write_text("owned generation\n", encoding="utf-8")

    def failing_build(staging: Path) -> None:
        raise RuntimeError("simulated build failure")

    with pytest.raises(RuntimeError, match="simulated build failure"):
        publish_owned_tree(target, failing_build, is_owned=lambda _p: True)

    assert (target / "content.txt").read_text(encoding="utf-8") == "owned generation\n"
    assert _claim_siblings(target, "replacing") == []


def test_remove_restores_claim_on_transient_failure_and_supports_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed removal keeps the generation discoverable at its canonical
    path, and a retry after the transient fault succeeds."""
    target = tmp_path / "artifact.txt"
    target.write_text("owned generation\n", encoding="utf-8")
    real_remove = fs_ownership._PinnedParent.quarantined_remove
    failures = iter([True])

    def flaky_remove(
        self: object,
        name: str,
        expected_identity: tuple[int, int, int],
        **kwargs: object,
    ) -> None:
        if next(failures, False):
            raise OSError("simulated transient removal failure")
        real_remove(self, name, expected_identity, **kwargs)

    monkeypatch.setattr(fs_ownership._PinnedParent, "quarantined_remove", flaky_remove)

    with pytest.raises(OSError, match="simulated transient removal failure"):
        claim_and_remove_owned(target, is_owned=lambda _p: True)

    assert target.read_text(encoding="utf-8") == "owned generation\n"
    assert _claim_siblings(target, "removing") == []

    assert claim_and_remove_owned(target, is_owned=lambda _p: True)
    assert not target.exists()


def test_remove_aborts_when_parent_swapped_after_claim(tmp_path: Path) -> None:
    """A parent renamed away after the claim fails identity revalidation; the
    claimed generation is restored through the held descriptor."""
    parent = tmp_path / "profile"
    parent.mkdir()
    target = parent / "artifact.txt"
    target.write_text("owned generation\n", encoding="utf-8")
    moved = tmp_path / "profile-moved"

    def swap_parent_then_approve(claimed: Path) -> bool:
        parent.rename(moved)
        return True

    with pytest.raises(OSError, match="parent directory changed"):
        claim_and_remove_owned(target, is_owned=swap_parent_then_approve)

    assert (moved / "artifact.txt").read_text(encoding="utf-8") == "owned generation\n"


def test_remove_never_deletes_through_a_symlinked_replacement_parent(
    tmp_path: Path,
) -> None:
    """The review probe: rename the parent, plant a symlink at its canonical
    path pointing at an external directory seeded with the claim name. The
    external file must survive and the managed generation must be restored."""
    parent = tmp_path / "profile"
    parent.mkdir()
    target = parent / "artifact.txt"
    target.write_text("owned generation\n", encoding="utf-8")
    moved = tmp_path / "profile-moved"
    external = tmp_path / "external"
    external.mkdir()

    def swap_in_symlinked_parent(claimed: Path) -> bool:
        parent.rename(moved)
        try:
            parent.symlink_to(external)
        except OSError:
            pytest.skip("symlinks are not supported on this platform")
        (external / claimed.name).write_text("external operator file\n", encoding="utf-8")
        return True

    with pytest.raises(OSError, match="parent directory changed"):
        claim_and_remove_owned(target, is_owned=swap_in_symlinked_parent)

    external_files = sorted(item.name for item in external.iterdir())
    assert len(external_files) == 1
    assert (external / external_files[0]).read_text(encoding="utf-8") == (
        "external operator file\n"
    )
    assert (moved / "artifact.txt").read_text(encoding="utf-8") == "owned generation\n"


def test_tree_publish_aborts_when_parent_swapped_during_build(tmp_path: Path) -> None:
    parent = tmp_path / "profile"
    parent.mkdir()
    target = parent / "artifact-dir"
    target.mkdir()
    (target / "content.txt").write_text("owned generation\n", encoding="utf-8")
    moved = tmp_path / "profile-moved"
    external = tmp_path / "external"
    external.mkdir()

    def swap_parent_during_build(staging: Path) -> None:
        parent.rename(moved)
        try:
            parent.symlink_to(external)
        except OSError:
            pytest.skip("symlinks are not supported on this platform")

    with pytest.raises(OSError, match="parent directory changed"):
        publish_owned_tree(target, swap_parent_during_build, is_owned=lambda _p: True)

    assert list(external.iterdir()) == []
    assert (moved / "artifact-dir" / "content.txt").read_text(encoding="utf-8") == (
        "owned generation\n"
    )


def _make_symlinked_root(tmp_path: Path) -> tuple[Path, Path]:
    external = tmp_path / "external"
    external.mkdir()
    root = tmp_path / "agent"
    try:
        root.symlink_to(external)
    except OSError:
        pytest.skip("symlinks are not supported on this platform")
    return root, external


def test_publish_rejects_a_symlinked_trusted_root(tmp_path: Path) -> None:
    """A symlinked configured profile root must not redirect publication into
    its target — the reviewer's `agent -> external` reproduction."""
    root, external = _make_symlinked_root(tmp_path)

    with pytest.raises(OSError, match="symlinked trusted root"):
        publish_owned_file(
            root / "rules" / "guide.md",
            "managed\n",
            is_owned=lambda _p: True,
            trusted_ancestor=root,
        )

    assert list(external.rglob("*")) == []


def test_tree_publish_rejects_a_symlinked_trusted_root(tmp_path: Path) -> None:
    root, external = _make_symlinked_root(tmp_path)

    def build(staging: Path) -> None:  # pragma: no cover - must not run
        staging.mkdir()

    with pytest.raises(OSError, match="symlinked trusted root"):
        publish_owned_tree(
            root / "skills" / "ooo-run", build, is_owned=lambda _p: True, trusted_ancestor=root
        )

    assert list(external.rglob("*")) == []


def test_remove_rejects_a_symlinked_trusted_root(tmp_path: Path) -> None:
    root, external = _make_symlinked_root(tmp_path)
    (external / "rules").mkdir()
    victim = external / "rules" / "guide.md"
    victim.write_text("managed\n", encoding="utf-8")

    with pytest.raises(OSError, match="symlinked trusted root"):
        claim_and_remove_owned(
            root / "rules" / "guide.md", is_owned=lambda _p: True, trusted_ancestor=root
        )

    assert victim.read_text(encoding="utf-8") == "managed\n"


def test_recover_owned_claims_restores_an_interrupted_claim(tmp_path: Path) -> None:
    """A crash between claim and completion leaves the generation only under a
    hidden claim name; recovery restores the canonical route."""
    target = tmp_path / "artifact.txt"
    claim = target.with_name(fs_ownership._claim_name(target.name, "replacing"))
    claim.write_text("interrupted generation\n", encoding="utf-8")

    assert fs_ownership.recover_owned_claims(target, is_owned=lambda _p: True)

    assert target.read_text(encoding="utf-8") == "interrupted generation\n"
    assert _claim_siblings(target, "replacing") == []


def test_recovery_deletes_only_an_owned_leftover_claim(tmp_path: Path) -> None:
    target = tmp_path / "artifact.txt"
    target.write_text("current generation\n", encoding="utf-8")
    owned_claim = target.with_name(fs_ownership._claim_name(target.name, "replacing"))
    owned_claim.write_text("owned leftover\n", encoding="utf-8")
    operator_claim = target.with_name(fs_ownership._claim_name(target.name, "removing"))
    operator_claim.write_text("operator content\n", encoding="utf-8")

    def is_owned(path: Path) -> bool:
        return path.read_text(encoding="utf-8") == "owned leftover\n"

    assert fs_ownership.recover_owned_claims(target, is_owned=is_owned)

    assert target.read_text(encoding="utf-8") == "current generation\n"
    assert not owned_claim.exists()
    assert operator_claim.read_text(encoding="utf-8") == "operator content\n"


def test_every_transaction_reconciles_prior_interrupted_claims(tmp_path: Path) -> None:
    """Publish and removal are self-healing: they first reconcile orphaned
    claim state a crashed run left behind for the same path."""
    target = tmp_path / "artifact.txt"
    claim = target.with_name(fs_ownership._claim_name(target.name, "removing"))
    claim.write_text("interrupted generation\n", encoding="utf-8")

    assert claim_and_remove_owned(target, is_owned=lambda _p: True)
    assert not target.exists()
    assert _claim_siblings(target, "removing") == []


def test_recovery_never_promotes_a_forged_claim(tmp_path: Path) -> None:
    """Claim-name syntax is not ownership evidence: a claim-shaped sibling that
    fails authentication is neither restored into the canonical path nor
    deleted — it stays in place as a collision."""
    target = tmp_path / "artifact.txt"
    forged = target.with_name(fs_ownership._claim_name(target.name, "replacing"))
    forged.write_text("forged payload\n", encoding="utf-8")

    assert not fs_ownership.recover_owned_claims(target, is_owned=lambda _p: False)
    assert not target.exists()
    assert forged.read_text(encoding="utf-8") == "forged payload\n"

    publish_owned_file(target, "managed\n", is_owned=lambda _p: False)

    assert target.read_text(encoding="utf-8") == "managed\n"
    assert forged.read_text(encoding="utf-8") == "forged payload\n"


def test_remove_refuses_a_claim_whose_identity_was_swapped(tmp_path: Path) -> None:
    """The deletion is bound to the inode the predicate validated: a claimed
    sibling swapped for different content after the check is not deleted."""
    target = tmp_path / "artifact.txt"
    target.write_text("owned generation\n", encoding="utf-8")

    def swap_claimed_then_approve(claimed: Path) -> bool:
        claimed.unlink()
        claimed.write_text("swapped-in generation\n", encoding="utf-8")
        return True

    assert not claim_and_remove_owned(target, is_owned=swap_claimed_then_approve)

    assert target.read_text(encoding="utf-8") == "swapped-in generation\n"


def test_publish_owned_entry_replaces_only_the_expected_generation(tmp_path: Path) -> None:
    target = tmp_path / "artifact.txt"
    target.write_text("expected generation\n", encoding="utf-8")

    def build(staging: Path) -> None:
        staging.write_text("restored snapshot\n", encoding="utf-8")

    fs_ownership.publish_owned_entry(
        target,
        build,
        is_owned=lambda p: p.read_text(encoding="utf-8") == "expected generation\n",
    )
    assert target.read_text(encoding="utf-8") == "restored snapshot\n"

    target.write_text("operator generation\n", encoding="utf-8")
    with pytest.raises(UnownedArtifactError):
        fs_ownership.publish_owned_entry(
            target,
            build,
            is_owned=lambda p: p.read_text(encoding="utf-8") == "expected generation\n",
        )
    assert target.read_text(encoding="utf-8") == "operator generation\n"


def test_publish_owned_entry_supports_symlink_topology(tmp_path: Path) -> None:
    link_target = tmp_path / "elsewhere.txt"
    link_target.write_text("payload\n", encoding="utf-8")
    target = tmp_path / "artifact.txt"

    def build(staging: Path) -> None:
        try:
            staging.symlink_to(link_target)
        except OSError:
            pytest.skip("symlinks are not supported on this platform")

    fs_ownership.publish_owned_entry(target, build, is_owned=lambda _p: False)
    assert target.is_symlink()


def test_find_orphaned_claims_names_the_canonical_entries(tmp_path: Path) -> None:
    (tmp_path / fs_ownership._claim_name("guide.md", "replacing")).write_text("x", encoding="utf-8")
    (tmp_path / fs_ownership._claim_name("index.ts", "removing")).write_text("y", encoding="utf-8")
    (tmp_path / "unrelated.txt").write_text("z", encoding="utf-8")

    assert fs_ownership.find_orphaned_claims(tmp_path) == ("guide.md", "index.ts")


def test_special_file_at_canonical_path_is_rejected_without_blocking(tmp_path: Path) -> None:
    """A FIFO planted at a managed path must neither hang the transaction nor
    pass ownership — the reviewer's `os.mkfifo` probe."""
    target = tmp_path / "artifact.txt"
    try:
        os.mkfifo(target)
    except (AttributeError, OSError):
        pytest.skip("FIFOs are not supported on this platform")

    def must_not_run(_claimed: Path) -> bool:  # pragma: no cover - must not run
        raise AssertionError("ownership predicate must not read a special file")

    with pytest.raises(UnownedArtifactError):
        publish_owned_file(target, "managed\n", is_owned=must_not_run)
    assert stat.S_ISFIFO(target.lstat().st_mode)

    assert not claim_and_remove_owned(target, is_owned=must_not_run)
    assert stat.S_ISFIFO(target.lstat().st_mode)


def test_has_recoverable_claim_requires_authentication(tmp_path: Path) -> None:
    target = tmp_path / "artifact.txt"
    owned_claim = target.with_name(fs_ownership._claim_name(target.name, "replacing"))
    owned_claim.write_text("owned generation\n", encoding="utf-8")
    forged_claim = target.with_name(fs_ownership._claim_name(target.name, "removing"))
    forged_claim.write_text("forged payload\n", encoding="utf-8")

    def is_owned(path: Path) -> bool:
        return path.read_text(encoding="utf-8") == "owned generation\n"

    assert fs_ownership.has_recoverable_claim(target, is_owned=is_owned)
    assert not fs_ownership.has_recoverable_claim(target, is_owned=lambda _p: False)
    # Discovery is read-only: nothing was promoted or deleted.
    assert not target.exists()
    assert owned_claim.exists()
    assert forged_claim.exists()


def test_tree_builder_writes_cannot_escape_through_a_swapped_parent(tmp_path: Path) -> None:
    """The reviewer's builder-redirection probe: a parent renamed away and
    replaced by a symlink *during* the build must not receive a single builder
    write — builders operate in a private workspace, never through the shared
    parent's pathname."""
    profile = tmp_path / "profile"
    profile.mkdir()
    target = profile / "artifact-dir"
    target.mkdir()
    (target / "content.txt").write_text("owned generation\n", encoding="utf-8")
    external = tmp_path / "external"
    external.mkdir()
    moved = tmp_path / "profile-moved"

    def swap_parent_then_write(staging: Path) -> None:
        (staging / "content.txt").write_text("next generation\n", encoding="utf-8")
        profile.rename(moved)
        try:
            profile.symlink_to(external)
        except OSError:
            pytest.skip("symlinks are not supported on this platform")
        (staging / "escaped.txt").write_text("escaped payload\n", encoding="utf-8")

    with pytest.raises(OSError):
        publish_owned_tree(target, swap_parent_then_write, is_owned=lambda _p: True)

    assert list(external.rglob("*")) == []
    assert (moved / "artifact-dir" / "content.txt").read_text(encoding="utf-8") == (
        "owned generation\n"
    )


def test_entry_builder_writes_cannot_escape_through_a_swapped_parent(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    target = profile / "guide.md"
    external = tmp_path / "external"
    external.mkdir()
    moved = tmp_path / "profile-moved"

    def swap_parent_then_write(staging: Path) -> None:
        profile.rename(moved)
        try:
            profile.symlink_to(external)
        except OSError:
            pytest.skip("symlinks are not supported on this platform")
        staging.write_text("restored snapshot\n", encoding="utf-8")

    with pytest.raises(OSError):
        fs_ownership.publish_owned_entry(target, swap_parent_then_write, is_owned=lambda _p: True)

    assert list(external.rglob("*")) == []


def test_remove_never_deletes_a_replacement_swapped_at_the_last_instant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewer's final-syscall race: a replacement swapped onto the claim
    name after validation is captured by the quarantine rename, fails
    re-authentication, and survives untouched."""
    target = tmp_path / "artifact.txt"
    target.write_text("owned generation\n", encoding="utf-8")
    hidden = tmp_path / "hidden"
    state: dict[str, Path | None] = {"claimed": None}

    def capture_then_approve(claimed: Path) -> bool:
        state["claimed"] = claimed
        return True

    real_make = fs_ownership._PinnedParent.make_staging_dir

    def hostile_make(self: fs_ownership._PinnedParent, prefix: str) -> str:
        quarantine = real_make(self, prefix)
        claimed = state["claimed"]
        if claimed is not None and claimed.exists():
            claimed.rename(hidden)
            claimed.write_text("replacement generation\n", encoding="utf-8")
            state["claimed"] = None
        return quarantine

    monkeypatch.setattr(fs_ownership._PinnedParent, "make_staging_dir", hostile_make)

    assert not claim_and_remove_owned(target, is_owned=capture_then_approve)

    assert hidden.read_text(encoding="utf-8") == "owned generation\n"
    assert target.read_text(encoding="utf-8") == "replacement generation\n"


def test_tree_remove_never_deletes_a_replacement_swapped_at_the_last_instant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "artifact-dir"
    target.mkdir()
    (target / "content.txt").write_text("owned generation\n", encoding="utf-8")
    hidden = tmp_path / "hidden"
    state: dict[str, Path | None] = {"claimed": None}

    def capture_then_approve(claimed: Path) -> bool:
        state["claimed"] = claimed
        return True

    real_make = fs_ownership._PinnedParent.make_staging_dir

    def hostile_make(self: fs_ownership._PinnedParent, prefix: str) -> str:
        quarantine = real_make(self, prefix)
        claimed = state["claimed"]
        if claimed is not None and claimed.exists():
            claimed.rename(hidden)
            claimed.mkdir()
            (claimed / "content.txt").write_text("replacement generation\n", encoding="utf-8")
            state["claimed"] = None
        return quarantine

    monkeypatch.setattr(fs_ownership._PinnedParent, "make_staging_dir", hostile_make)

    assert not claim_and_remove_owned(target, is_owned=capture_then_approve)

    assert (hidden / "content.txt").read_text(encoding="utf-8") == "owned generation\n"
    assert (target / "content.txt").read_text(encoding="utf-8") == "replacement generation\n"


def test_rejects_a_symlinked_ancestor_of_the_trusted_root(tmp_path: Path) -> None:
    """The reviewer's nested-parent-symlink probe: a configured root reached
    through a symlinked ancestor (`/profile-link/agent`) must be rejected, not
    silently redirected into the link target."""
    external = tmp_path / "external"
    (external / "agent").mkdir(parents=True)
    link = tmp_path / "profile-link"
    try:
        link.symlink_to(external)
    except OSError:
        pytest.skip("symlinks are not supported on this platform")
    root = link / "agent"

    with pytest.raises(OSError, match="symlinked trusted root"):
        publish_owned_file(
            root / "rules" / "guide.md",
            "managed\n",
            is_owned=lambda _p: True,
            trusted_ancestor=root,
        )

    assert list((external / "agent").rglob("*")) == []

    with pytest.raises(OSError, match="symlinked trusted root"):
        claim_and_remove_owned(
            root / "rules" / "guide.md", is_owned=lambda _p: True, trusted_ancestor=root
        )


def test_tree_remove_never_restores_a_half_destroyed_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewer's partial-destruction probe: a recursive deletion that
    fails after removing descendants must never rename damaged remains back
    to the canonical path — removal commits with the atomic isolation, and
    residue stays out of the canonical namespace."""
    target = tmp_path / "artifact-dir"
    target.mkdir()
    (target / "keep.txt").write_text("owned generation\n", encoding="utf-8")
    (target / "lost.txt").write_text("owned generation\n", encoding="utf-8")
    real_rename = fs_ownership.os.rename
    real_rmtree = fs_ownership.shutil.rmtree

    def deny_private_move(
        src: object,
        dst: object,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        if src_dir_fd is not None and dst_dir_fd is None:
            raise OSError(errno.EXDEV, "simulated cross-device link")
        real_rename(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    def half_destroy(path: object, *args: object, **kwargs: object) -> None:
        dir_fd = kwargs.get("dir_fd")
        if path == "entry" and dir_fd is not None:
            # In-place destruction: delete one descendant, then fail.
            os.unlink("entry/lost.txt", dir_fd=dir_fd)
            raise OSError("simulated destruction failure")
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(fs_ownership.os, "rename", deny_private_move)
    monkeypatch.setattr(fs_ownership.shutil, "rmtree", half_destroy)

    with pytest.raises(OSError, match="residue retained"):
        claim_and_remove_owned(target, is_owned=lambda _p: True)

    # The canonical namespace never sees the damaged tree again: the removal
    # is reported as failed, and the residue stays quarantined under an
    # intent-marked, discoverable tombstone — never as the artifact or claim.
    assert not target.exists()
    assert _claim_siblings(target, "removing") == []
    tombstones = [p for p in tmp_path.iterdir() if p.name.endswith(".discarding")]
    assert len(tombstones) == 1
    assert (tombstones[0] / ".ouroboros-intent").read_text(encoding="utf-8") == "artifact-dir"

    # A later transaction on the same path reconciles the tombstone.
    monkeypatch.setattr(fs_ownership.os, "rename", real_rename)
    monkeypatch.setattr(fs_ownership.shutil, "rmtree", real_rmtree)
    assert fs_ownership.recover_owned_claims(target, is_owned=lambda _p: True)
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".discarding")] == []


def test_failed_cross_filesystem_import_leaves_no_staging_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewer's EXDEV probe: a partial descriptor-relative import that
    fails midway must clean its own staging state under the shared parent."""
    target = tmp_path / "artifact-dir"
    real_rename = fs_ownership.os.rename

    def deny_workspace_rename(
        src: object,
        dst: object,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        if src_dir_fd is None and dst_dir_fd is not None:
            raise OSError(errno.EXDEV, "simulated cross-device link")
        real_rename(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(fs_ownership.os, "rename", deny_workspace_rename)

    def build_with_unsupported_entry(staging: Path) -> None:
        (staging / "content.txt").write_text("next generation\n", encoding="utf-8")
        try:
            os.mkfifo(staging / "unsupported")
        except (AttributeError, OSError):
            pytest.skip("FIFOs are not supported on this platform")

    with pytest.raises(OSError, match="unsupported staged entry type"):
        publish_owned_tree(target, build_with_unsupported_entry, is_owned=lambda _p: True)

    assert not target.exists()
    assert [p.name for p in tmp_path.iterdir()] == []

    def build(staging: Path) -> None:
        (staging / "content.txt").write_text("next generation\n", encoding="utf-8")

    publish_owned_tree(target, build, is_owned=lambda _p: True)
    assert (target / "content.txt").read_text(encoding="utf-8") == "next generation\n"
    assert [p.name for p in tmp_path.iterdir()] == ["artifact-dir"]


def test_tree_remove_preserves_a_tree_modified_after_the_ownership_read(tmp_path: Path) -> None:
    """The reviewer's descendant-modification probe: a tree edited after the
    ownership read — same top-level inode — must survive, caught by the
    re-authentication inside the descriptor-pinned quarantine."""
    target = tmp_path / "artifact-dir"
    target.mkdir()
    (target / "content.txt").write_text("owned generation\n", encoding="utf-8")
    tampered = {"done": False}

    def stale_approve(candidate: Path) -> bool:
        owned = (candidate / "content.txt").read_text(encoding="utf-8") == "owned generation\n"
        if owned and not tampered["done"]:
            # The operator edits a descendant after the read; the stale
            # judgment is still reported as owned.
            (candidate / "content.txt").write_text("operator edit\n", encoding="utf-8")
            tampered["done"] = True
            return True
        return owned

    assert not claim_and_remove_owned(target, is_owned=stale_approve)

    assert (target / "content.txt").read_text(encoding="utf-8") == "operator edit\n"


def test_publish_never_publishes_a_staged_generation_swapped_before_the_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewer's staging-swap probe: content swapped onto the staging
    name after the build must never become the canonical artifact — the
    staged generation is pinned and authenticated through the publication."""
    target = tmp_path / "artifact.txt"
    real_rnr = fs_ownership._PinnedParent.rename_no_replace

    def swap_then_rename(
        self: fs_ownership._PinnedParent, source_name: str, target_name: str
    ) -> None:
        if target_name == target.name:
            staged = tmp_path / source_name
            staged.unlink()
            staged.write_text("attacker generation\n", encoding="utf-8")
        real_rnr(self, source_name, target_name)

    monkeypatch.setattr(fs_ownership._PinnedParent, "rename_no_replace", swap_then_rename)

    with pytest.raises(UnownedArtifactError, match="raced during publication"):
        publish_owned_file(target, "managed\n", is_owned=lambda _p: True)

    assert not target.exists() or target.read_text(encoding="utf-8") != "attacker generation\n"
    survivors = {p.read_text(encoding="utf-8") for p in tmp_path.iterdir() if p.is_file()}
    assert "attacker generation\n" in survivors  # preserved aside, never canonical


def test_tree_publish_never_publishes_a_staged_generation_swapped_before_the_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "artifact-dir"
    target.mkdir()
    (target / "content.txt").write_text("owned generation\n", encoding="utf-8")
    real_rnr = fs_ownership._PinnedParent.rename_no_replace

    swapped = {"done": False}

    def swap_then_rename(
        self: fs_ownership._PinnedParent, source_name: str, target_name: str
    ) -> None:
        if target_name == target.name and not swapped["done"]:
            swapped["done"] = True
            staged = tmp_path / source_name
            fs_ownership.remove_path(staged)
            staged.mkdir()
            (staged / "content.txt").write_text("attacker generation\n", encoding="utf-8")
        real_rnr(self, source_name, target_name)

    monkeypatch.setattr(fs_ownership._PinnedParent, "rename_no_replace", swap_then_rename)

    def build(staging: Path) -> None:
        (staging / "content.txt").write_text("next generation\n", encoding="utf-8")

    with pytest.raises(UnownedArtifactError, match="raced during publication"):
        publish_owned_tree(target, build, is_owned=lambda _p: True)

    # The previous owned generation is restored; the attacker tree never
    # becomes canonical.
    assert (target / "content.txt").read_text(encoding="utf-8") == "owned generation\n"


def test_publish_never_publishes_staged_content_mutated_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewer's same-inode probe: staged file content modified in place
    (same inode) after the build must never become canonical — publication is
    bound to the authored content generation, not just the inode."""
    target = tmp_path / "artifact.txt"
    real_rnr = fs_ownership._PinnedParent.rename_no_replace
    mutated = {"done": False}

    def mutate_then_rename(
        self: fs_ownership._PinnedParent, source_name: str, target_name: str
    ) -> None:
        if target_name == target.name and not mutated["done"]:
            mutated["done"] = True
            staged = tmp_path / source_name
            with staged.open("r+", encoding="utf-8") as handle:  # same inode
                handle.seek(0)
                handle.write("attacker")
        real_rnr(self, source_name, target_name)

    monkeypatch.setattr(fs_ownership._PinnedParent, "rename_no_replace", mutate_then_rename)

    # Caught either before the commit (staged digest) or immediately after
    # it (canonical digest); the mutated content is never left canonical.
    with pytest.raises(UnownedArtifactError, match="publication"):
        publish_owned_file(target, "managed generation\n", is_owned=lambda _p: True)

    # The unauthored generation was pulled aside; nothing canonical remains.
    assert not target.exists()


def test_tree_publish_never_publishes_descendants_mutated_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "artifact-dir"
    target.mkdir()
    (target / "content.txt").write_text("owned generation\n", encoding="utf-8")
    real_rnr = fs_ownership._PinnedParent.rename_no_replace
    mutated = {"done": False}

    def mutate_then_rename(
        self: fs_ownership._PinnedParent, source_name: str, target_name: str
    ) -> None:
        if target_name == target.name and not mutated["done"]:
            mutated["done"] = True
            # Same top-level inode: only a descendant's bytes change.
            (tmp_path / source_name / "content.txt").write_text(
                "attacker generation\n", encoding="utf-8"
            )
        real_rnr(self, source_name, target_name)

    monkeypatch.setattr(fs_ownership._PinnedParent, "rename_no_replace", mutate_then_rename)

    def build(staging: Path) -> None:
        (staging / "content.txt").write_text("next generation\n", encoding="utf-8")

    with pytest.raises(UnownedArtifactError, match="publication"):
        publish_owned_tree(target, build, is_owned=lambda _p: True)

    # The prior owned generation is restored; the mutated tree never stays
    # canonical.
    assert (target / "content.txt").read_text(encoding="utf-8") == "owned generation\n"


def test_interrupted_import_container_is_reconciled_on_the_next_transaction(
    tmp_path: Path,
) -> None:
    """The reviewer's crash probe: a process exit during a cross-filesystem
    import leaves a container whose transaction was recorded in the
    writer-owned ledger before the container appeared; the next transaction
    on the same path replays the discard and retires the record. A container
    the ledger does not vouch for is operator state and stays untouched."""
    target = tmp_path / "artifact-dir"
    staged_shape = f".{target.name}.{os.urandom(8).hex()}.tmp"
    nonce = os.urandom(8).hex()
    crashed = tmp_path / f".{staged_shape}.{nonce}.importing"
    fs_ownership._ledger_record(
        nonce, canonical=target, container=crashed.name, operation="importing"
    )
    crashed.mkdir(mode=0o700)
    (crashed / ".ouroboros-intent").write_text(target.name, encoding="utf-8")
    (crashed / "entry").mkdir()
    (crashed / "entry" / "partial.txt").write_text("partial import\n", encoding="utf-8")

    forged = tmp_path / f".forged.{os.urandom(8).hex()}.importing"
    forged.mkdir(mode=0o700)
    (forged / "entry").mkdir()
    (forged / "entry" / "operator.txt").write_text("operator data\n", encoding="utf-8")

    def build(staging: Path) -> None:
        (staging / "content.txt").write_text("next generation\n", encoding="utf-8")

    publish_owned_tree(target, build, is_owned=lambda _p: True)

    assert (target / "content.txt").read_text(encoding="utf-8") == "next generation\n"
    assert not crashed.exists()  # reconciled: the ledger vouched for it
    assert fs_ownership._ledger_read(nonce) is None  # record retired with the replay
    assert (forged / "entry" / "operator.txt").exists()  # forgery left untouched


def test_live_record_survives_concurrent_reconcile_and_creator_crash_recovery(
    tmp_path: Path,
) -> None:
    """The reviewer's interleaving probe: a publisher writes its ledger
    record before its container exists, and a concurrent transaction
    reconciles the same canonical path inside that window. The per-path
    ledger lock serializes the two — the reconciler blocks instead of
    retiring the live record, and when the creator then publishes the
    container and dies, the blocked reconciliation replays it and retires
    the record instead of stranding unauthenticated residue."""
    import threading

    target = tmp_path / "artifact-dir"
    canonical = fs_ownership._ledger_canonical(target)
    nonce = os.urandom(8).hex()
    container = f".{target.name}.{nonce}.importing"
    outcome: dict[str, bool] = {}

    def concurrent_reconcile() -> None:
        outcome["changed"] = fs_ownership.recover_owned_claims(target, is_owned=lambda _p: False)

    racer = threading.Thread(target=concurrent_reconcile)
    with fs_ownership._ledger_lock(canonical):
        # Creator: inside the record-before-container window.
        fs_ownership._ledger_record(
            nonce, canonical=target, container=container, operation="importing"
        )
        racer.start()
        racer.join(timeout=1.0)
        # The reconciler is blocked on the lock; the live record survives.
        assert racer.is_alive()
        assert fs_ownership._ledger_read(nonce) is not None
        # Creator publishes the container, then dies (the crash releases the
        # lock with the import unfinished).
        (tmp_path / container).mkdir(mode=0o700)
    racer.join(timeout=30.0)
    assert not racer.is_alive()

    # The reconciliation ran only after the container existed: it replayed
    # the interrupted import and retired its record.
    assert outcome["changed"] is True
    assert not (tmp_path / container).exists()
    assert fs_ownership._ledger_read(nonce) is None


def test_reconcile_replays_a_container_that_crashed_before_its_marker_write(
    tmp_path: Path,
) -> None:
    """A crash can die between the container mkdir and the intent-marker
    write. The ledger record — written before the container ever existed —
    is the recovery authority, so the marker-less residue is still replayed
    and the record retired."""
    target = tmp_path / "artifact-dir"
    nonce = os.urandom(8).hex()
    crashed = tmp_path / f".{target.name}.{nonce}.importing"
    fs_ownership._ledger_record(
        nonce, canonical=target, container=crashed.name, operation="importing"
    )
    crashed.mkdir(mode=0o700)  # crash: no marker, no entry yet

    assert fs_ownership.recover_owned_claims(target, is_owned=lambda _p: False)
    assert not crashed.exists()
    assert fs_ownership._ledger_read(nonce) is None


def test_recovery_never_destroys_a_forged_container_with_a_matching_marker(
    tmp_path: Path,
) -> None:
    """The reviewer's forged-marker probe: a same-user writer creates a
    container matching the ``.{name}.{nonce}.importing`` shape, writes the
    target basename into ``.ouroboros-intent``, and stores operator data
    under ``entry``. Name shape and marker are forgeable discovery metadata;
    without a writer-owned ledger record no recovery path — direct recovery,
    publication, or removal — may destroy the container."""
    target = tmp_path / "artifact-dir"
    forged = tmp_path / f".{target.name}.0123456789abcdef.importing"
    forged.mkdir(mode=0o700)
    (forged / ".ouroboros-intent").write_text(target.name, encoding="utf-8")
    (forged / "entry").mkdir()
    (forged / "entry" / "operator.txt").write_text("operator data\n", encoding="utf-8")

    assert not fs_ownership.recover_owned_claims(target, is_owned=lambda _p: False)
    assert (forged / "entry" / "operator.txt").read_text(encoding="utf-8") == "operator data\n"

    def build(staging: Path) -> None:
        (staging / "content.txt").write_text("next generation\n", encoding="utf-8")

    publish_owned_tree(target, build, is_owned=lambda _p: True)
    assert (forged / "entry" / "operator.txt").read_text(encoding="utf-8") == "operator data\n"

    claim_and_remove_owned(target, is_owned=lambda _p: True)
    assert (forged / "entry" / "operator.txt").read_text(encoding="utf-8") == "operator data\n"

    forged_discarding = tmp_path / f".{target.name}.fedcba9876543210.discarding"
    forged.rename(forged_discarding)
    assert not fs_ownership.recover_owned_claims(target, is_owned=lambda _p: False)
    assert (forged_discarding / "entry" / "operator.txt").exists()
