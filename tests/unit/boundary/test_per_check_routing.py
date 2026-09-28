"""Routing by admitted check after per-check admission: tiers, verdicts and reconciliation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ouroboros.boundary.acceptance import (
    RECONCILIATION_SCHEMA,
    ExistingOutcome,
    PackageCriterionStatus,
    VerificationCoverage,
    criterion_verdicts,
    reconcile_acceptance,
    verification_coverage,
)
from ouroboros.boundary.admission import admit_check_package
from ouroboros.boundary.binding import CheckTier
from ouroboros.boundary.binding_flow import assign_tiers, verify_with_bindings
from ouroboros.boundary.events import RunContract
from ouroboros.boundary.oracle_build import package_from_reply
from ouroboros.boundary.per_check import (
    REPRO_PASSES_ON_BASE,
)
from ouroboros.boundary.receipts import PackageVerdict

from .test_per_check import (
    BAD_REPRO_2,
    BUGGY,
    FIXED,
    GOOD_PRESERVE_3,
    GOOD_REPRO_1,
    _dev_seed,
    _seed,
)

CONTRACT = RunContract(check_timeout_seconds=120)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "mathutils.py").write_text(BUGGY)
    return root


async def test_an_excluded_check_is_tier_c_and_never_runs(repo: Path) -> None:
    """The per-check rule on the base, then the usual tiers and verdicts."""
    seed = _seed()
    package = package_from_reply(
        {"oracles": [GOOD_REPRO_1, BAD_REPRO_2, GOOD_PRESERVE_3]},
        seed,
        input_digest="1" * 64,
        generator="fake",
    )
    applied = await admit_check_package(package, repo)
    assert applied.verdict is PackageVerdict.ADMITTED
    assert applied.excluded_checks == {"oracle_2": REPRO_PASSES_ON_BASE}
    (repo / "mathutils.py").write_text(FIXED)
    assignments, _ = await assign_tiers(
        package, base=None, contract=CONTRACT, admitted_tiers=applied.check_tiers
    )
    assert assignments["oracle_2"].tier is CheckTier.C
    bound = await verify_with_bindings(package, repo, assignments, contract=CONTRACT)
    assert {check.check_id for check in bound.effective.checks} == {"oracle_1", "oracle_3"}
    verdicts = criterion_verdicts(package, bound.effective, assignments=assignments)
    # Criterion 3 has only a preservation check: it passes, but a check the
    # base already passed verifies nothing new (no_reproduction_check).
    assert [(v.status, v.reason) for v in verdicts.values()] == [
        (PackageCriterionStatus.PASS, "passed"),
        (PackageCriterionStatus.UNCOVERED, "uncovered:repro_passes_on_base"),
        (PackageCriterionStatus.UNVERIFIED, "no_reproduction_check"),
    ]


def test_an_uncovered_reason_never_routes() -> None:
    """Routing rests on one fact: whether an admitted check covers the criterion.

    The constructor's uncovered reason is descriptive text. A criterion left
    uncovered with any reason, ``non_behavioral`` included, is uncovered, is
    counted as not decided by the package, and the legacy verifier decides it.
    """
    seed = _dev_seed()
    package = package_from_reply(
        {
            "uncovered": [
                {"criterion": 1, "reason": "non_behavioral"},
                {"criterion": 2, "reason": "not executable"},
            ]
        },
        seed,
        input_digest="1" * 64,
        generator="fake",
    )
    verdicts = criterion_verdicts(package, None)
    assert [v.status for v in verdicts.values()] == [PackageCriterionStatus.UNCOVERED] * 2
    rejected = {i: ExistingOutcome(i, "failed", "failed", "failed") for i in range(2)}
    decided = reconcile_acceptance(
        package.criterion_keys,
        verdicts,
        rejected,
        existing_run_accepted=False,
        legacy_decides_unverified=True,
    )
    assert [d.legacy_decided and not d.accepted for d in decided.decisions] == [True, True]
    assert len(decided.not_package_decided) == 2
    assert decided.coverage is VerificationCoverage.LOW
    assert "non_behavioral_count" not in decided.to_dict()


async def test_a_linked_check_decides_whatever_else_the_reply_says(repo: Path) -> None:
    """A reply may still carry a ``labels`` entry (an older prompt): it is ignored.

    The criterion keeps its check, the check is admitted, and the package
    decides the criterion.
    """
    seed = _seed("clamp(15, 0, 10) returns 10")
    reply = {
        "oracles": [GOOD_REPRO_1],
        "labels": [{"criterion": 1, "kind": "context", "evidence_span": "returns 10"}],
    }
    package = package_from_reply(reply, seed, input_digest="1" * 64, generator="fake")
    assert [check.check_id for check in package.checks] == ["oracle_1"]
    assert package.uncovered == ()
    admission = await admit_check_package(package, repo)
    assert admission.verdict is PackageVerdict.ADMITTED and admission.excluded_checks is None
    (repo / "mathutils.py").write_text(FIXED)
    assignments, _ = await assign_tiers(
        package, base=None, contract=CONTRACT, admitted_tiers=admission.check_tiers
    )
    bound = await verify_with_bindings(package, repo, assignments, contract=CONTRACT)
    verdicts = criterion_verdicts(package, bound.effective, assignments=assignments)
    assert [v.status for v in verdicts.values()] == [PackageCriterionStatus.PASS]


@pytest.mark.parametrize(
    ("total", "not_decided", "unverified", "level"),
    [
        (3, 0, 0, VerificationCoverage.FULL),
        (3, 1, 0, VerificationCoverage.PARTIAL),
        (4, 2, 0, VerificationCoverage.LOW),
        (2, 1, 0, VerificationCoverage.LOW),
        (5, 1, 1, VerificationCoverage.LOW),
    ],
)
def test_coverage_levels(total: int, not_decided: int, unverified: int, level: Any) -> None:
    assert verification_coverage(total, not_decided, unverified) is level


def test_the_default_reconciliation_is_unchanged() -> None:
    """Without the legacy rule U is accepted and the schema stays v2."""
    keys = ("k0",)
    rejected = {0: ExistingOutcome(0, "failed", "failed", "failed")}
    before = reconcile_acceptance(
        keys, {"k0": PackageCriterionStatus.UNCOVERED}, rejected, existing_run_accepted=False
    )
    assert before.run_accepted and before.to_dict()["schema_version"] == RECONCILIATION_SCHEMA
    assert set(before.to_dict()) == {
        "schema_version",
        "run_accepted",
        "existing_run_accepted",
        "artifact_verdict",
        "verified_pass_count",
        "unverified_count",
        "criterion_count",
        "tier_summary",
        "criteria",
    }
    after = reconcile_acceptance(
        keys,
        {"k0": PackageCriterionStatus.UNCOVERED},
        rejected,
        existing_run_accepted=False,
        legacy_decides_unverified=True,
    )
    assert not after.run_accepted and after.decisions[0].legacy_decided
