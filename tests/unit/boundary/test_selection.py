"""Incumbent retention: replacement only on passing the unchanged package."""

from __future__ import annotations

from pathlib import Path

import pytest

from ouroboros.boundary import (
    ArtifactRef,
    PackageVerdict,
    SelectionError,
    SelectionReason,
    admit_check_package,
    select_incumbent,
    tree_digest,
    verify_candidate,
)

from .conftest import REPRO_SCRIPT, build_package


def _ref(name: str, root: Path, package) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=name, tree_digest=tree_digest(root), seed_digest=package.seed_digest
    )


@pytest.fixture
async def receipts(tmp_path: Path, base_checkout, fixed_checkout, package):
    admission = await admit_check_package(package, base_checkout, work_dir=tmp_path / "adm")
    assert admission.verdict is PackageVerdict.ADMITTED
    good = await verify_candidate(package, fixed_checkout, work_dir=tmp_path / "good")
    bad = await verify_candidate(package, base_checkout, work_dir=tmp_path / "bad")
    return admission, good, bad


async def test_candidate_passing_frozen_package_replaces_incumbent(
    receipts, base_checkout, fixed_checkout, package
) -> None:
    admission, good, _ = receipts
    incumbent = _ref("incumbent", base_checkout, package)
    candidate = _ref("candidate", fixed_checkout, package)
    decision = select_incumbent(
        incumbent=incumbent,
        candidate=candidate,
        package=package,
        admission=admission,
        verification=good,
        candidate_checkout=fixed_checkout,
    )
    assert decision.replaced
    assert decision.selected == candidate
    assert decision.reason is SelectionReason.CANDIDATE_PASSED
    assert decision.package_sha256 == package.sha256
    assert decision.verified_tree_digest == candidate.tree_digest


async def test_incumbent_kept_on_failed_candidate(receipts, base_checkout, package) -> None:
    admission, _, bad = receipts
    incumbent = ArtifactRef(
        artifact_id="inc", tree_digest="a" * 64, seed_digest=package.seed_digest
    )
    candidate = _ref("candidate", base_checkout, package)
    decision = select_incumbent(
        incumbent=incumbent,
        candidate=candidate,
        package=package,
        admission=admission,
        verification=bad,
    )
    assert not decision.replaced
    assert decision.selected == incumbent
    assert decision.reason is SelectionReason.CANDIDATE_FAILED


async def test_incumbent_kept_when_identity_revalidation_fails(
    receipts, base_checkout, fixed_checkout, package
) -> None:
    admission, good, _ = receipts
    incumbent = _ref("incumbent", base_checkout, package)
    # Declared digest differs from the tree that was verified.
    impostor = ArtifactRef(
        artifact_id="candidate", tree_digest="b" * 64, seed_digest=package.seed_digest
    )
    decision = select_incumbent(
        incumbent=incumbent,
        candidate=impostor,
        package=package,
        admission=admission,
        verification=good,
    )
    assert decision.reason is SelectionReason.CANDIDATE_IDENTITY_MISMATCH
    assert decision.selected == incumbent

    # Candidate tree changed after verification: rehash at selection time.
    candidate = _ref("candidate", fixed_checkout, package)
    (fixed_checkout / "calc.py").write_text("def add(a, b):\n    return 0\n")
    decision = select_incumbent(
        incumbent=incumbent,
        candidate=candidate,
        package=package,
        admission=admission,
        verification=good,
        candidate_checkout=fixed_checkout,
    )
    assert decision.reason is SelectionReason.CANDIDATE_IDENTITY_MISMATCH
    assert not decision.replaced


async def test_incumbent_kept_when_package_changed(
    receipts, seed, base_checkout, fixed_checkout, package
) -> None:
    admission, good, _ = receipts
    other_package = build_package(seed, repro_script=REPRO_SCRIPT + "# regenerated\n")
    decision = select_incumbent(
        incumbent=_ref("incumbent", base_checkout, package),
        candidate=_ref("candidate", fixed_checkout, package),
        package=other_package,
        admission=admission,
        verification=good,
    )
    assert decision.reason is SelectionReason.ADMISSION_PACKAGE_MISMATCH
    assert not decision.replaced


async def test_incumbent_kept_without_admitted_package_or_verification(
    receipts, base_checkout, fixed_checkout, package
) -> None:
    admission, good, _ = receipts
    incumbent = _ref("incumbent", base_checkout, package)
    candidate = _ref("candidate", fixed_checkout, package)
    rejected = admission.model_copy(update={"verdict": PackageVerdict.REJECTED})
    assert (
        select_incumbent(
            incumbent=incumbent,
            candidate=candidate,
            package=package,
            admission=rejected,
            verification=good,
        ).reason
        is SelectionReason.PACKAGE_NOT_ADMITTED
    )
    assert (
        select_incumbent(
            incumbent=incumbent,
            candidate=candidate,
            package=package,
            admission=admission,
            verification=None,
        ).reason
        is SelectionReason.VERIFICATION_MISSING
    )
    assert (
        select_incumbent(
            incumbent=incumbent,
            candidate=None,
            package=package,
            admission=admission,
            verification=None,
        ).reason
        is SelectionReason.NO_CANDIDATE
    )


async def test_incumbent_from_another_seed_is_refused(receipts, package) -> None:
    admission, good, _ = receipts
    stranger = ArtifactRef(artifact_id="x", tree_digest="a" * 64, seed_digest="f" * 64)
    with pytest.raises(SelectionError):
        select_incumbent(
            incumbent=stranger,
            candidate=None,
            package=package,
            admission=admission,
            verification=good,
        )
