"""Tests for the claim-then-verify filesystem primitives."""

from __future__ import annotations

from pathlib import Path

import pytest

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
