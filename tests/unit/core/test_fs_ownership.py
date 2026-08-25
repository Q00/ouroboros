"""Tests for the claim-then-verify filesystem primitives."""

from __future__ import annotations

from pathlib import Path

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
    real_remove = fs_ownership._PinnedParent.remove_entry
    failures = iter([True])

    def flaky_remove(
        self: object, name: str, expected_identity: tuple[int, int, int] | None = None
    ) -> None:
        if next(failures, False):
            raise OSError("simulated transient removal failure")
        real_remove(self, name, expected_identity)

    monkeypatch.setattr(fs_ownership._PinnedParent, "remove_entry", flaky_remove)

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
