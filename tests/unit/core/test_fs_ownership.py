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

    def flaky_remove(self: object, name: str) -> None:
        if next(failures, False):
            raise OSError("simulated transient removal failure")
        real_remove(self, name)

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
