"""Acceptance authority: the admitted package decides the criteria it covers."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from ouroboros.boundary.acceptance import (
    ExistingOutcome,
    Governor,
    PackageCriterionStatus,
    existing_outcomes_from_events,
    package_criterion_statuses,
    reconcile_acceptance,
    render_reconciliation,
)
from ouroboros.boundary.admission import (
    CandidateVerdict,
    CandidateVerification,
    CheckExecution,
    CheckStatus,
)
from ouroboros.boundary.constructor import CHECK_DIR, package_from_reply
from ouroboros.boundary.package import CheckRole
from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata

PASS = PackageCriterionStatus.PASS
FAIL = PackageCriterionStatus.FAIL
INDETERMINATE = PackageCriterionStatus.INDETERMINATE
UNCOVERED = PackageCriterionStatus.UNCOVERED


def _seed() -> Seed:
    return Seed(
        goal="calc works",
        acceptance_criteria=("add(2, 3) returns 5", "sub(5, 3) returns 2", "docs are clear"),
        ontology_schema=OntologySchema(name="calc", description="calculator"),
        metadata=SeedMetadata(seed_id="seed_acceptance", ambiguity_score=0.1),
    )


def _check(check_id: str, criterion: int) -> dict:
    return {
        "check_id": check_id,
        "role": "reproduction",
        "argv": ["python3", f"{CHECK_DIR}/{check_id}.py"],
        "cwd": ".",
        "failure_signature": f"SIG:{check_id}",
        "assertions": [{"criterion": criterion, "locator": "main"}],
    }


def _package(seed: Seed):
    reply = {
        "checks": [_check("repro_add", 1), _check("repro_sub", 2)],
        "files": [
            {"path": f"{CHECK_DIR}/repro_add.py", "content": "print('add')\n"},
            {"path": f"{CHECK_DIR}/repro_sub.py", "content": "print('sub')\n"},
        ],
        "uncovered": [{"criterion": 3, "reason": "not mechanical"}],
    }
    return package_from_reply(reply, seed, input_digest="2" * 64, generator="fake")


def _execution(check_id: str, status: CheckStatus) -> CheckExecution:
    return CheckExecution(
        check_id=check_id,
        role=CheckRole.REPRODUCTION,
        argv=("python3", f"{CHECK_DIR}/{check_id}.py"),
        cwd=".",
        status=status,
        reason="passed" if status is CheckStatus.EXPECTED else "reproduction_still_failing",
        return_code=0 if status is CheckStatus.EXPECTED else 1,
        timed_out=False,
        duration_seconds=0.1,
        signature_seen=status is CheckStatus.VIOLATED,
        stdout_sha256="0" * 64,
        stderr_sha256="0" * 64,
        output_tail="",
        protected_digest_before="1" * 64,
        protected_digest_after="1" * 64,
        mutated_paths=(),
        scratch_outputs=(),
        undeclared_outputs=(),
    )


def _verification(package, *statuses: CheckStatus, mutated: bool = False) -> CandidateVerification:
    now = datetime.now(UTC)
    return CandidateVerification(
        package_sha256=package.sha256,
        artifact_tree_digest="3" * 64,
        artifact_tree_digest_after="3" * 64,
        verdict=CandidateVerdict.PASS,
        reasons=(),
        protected_bytes_mutated=mutated,
        timeout_seconds=120,
        checks=tuple(
            _execution(check_id, status)
            for check_id, status in zip(("repro_add", "repro_sub"), statuses, strict=False)
        ),
        started_at=now,
        completed_at=now,
    )


def test_statuses_follow_the_linked_checks() -> None:
    seed = _seed()
    package = _package(seed)
    add_key, sub_key, docs_key = package.criterion_keys
    statuses = package_criterion_statuses(
        package, _verification(package, CheckStatus.EXPECTED, CheckStatus.VIOLATED)
    )
    assert statuses == {add_key: PASS, sub_key: FAIL, docs_key: UNCOVERED}

    partial = package_criterion_statuses(
        package, _verification(package, CheckStatus.EXPECTED, CheckStatus.INDETERMINATE)
    )
    assert partial[sub_key] is INDETERMINATE


@pytest.mark.parametrize(
    "verification_kwargs, identity_ok",
    [({"mutated": True}, True), ({}, False)],
)
def test_untrusted_verification_makes_covered_criteria_indeterminate(
    verification_kwargs: dict, identity_ok: bool
) -> None:
    seed = _seed()
    package = _package(seed)
    verification = _verification(
        package, CheckStatus.EXPECTED, CheckStatus.EXPECTED, **verification_kwargs
    )
    statuses = package_criterion_statuses(package, verification, candidate_identity_ok=identity_ok)
    assert list(statuses.values()) == [INDETERMINATE, INDETERMINATE, UNCOVERED]


def _outcome(index: int, outcome: str, terminal: str = "failed") -> ExistingOutcome:
    disposition = "accepted" if terminal == "completed" else outcome
    return ExistingOutcome(index, outcome, disposition, terminal)


def test_package_pass_accepts_a_criterion_the_existing_verifier_rejected() -> None:
    keys = ("k1",)
    result = reconcile_acceptance(
        keys, {"k1": PASS}, {0: _outcome(0, "failed")}, existing_run_accepted=False
    )
    (decision,) = result.decisions
    assert decision.accepted and decision.governed_by is Governor.CHECK_PACKAGE
    assert decision.existing_accepted is False and decision.existing_outcome == "failed"
    assert result.run_accepted and not result.existing_run_accepted
    assert result.overridden == (decision,)
    assert "existing verifier (advisory): failed" in render_reconciliation(result)[0]


def test_package_fail_rejects_a_criterion_the_existing_verifier_accepted() -> None:
    result = reconcile_acceptance(
        ("k1",),
        {"k1": FAIL},
        {0: _outcome(0, "succeeded", "completed")},
        existing_run_accepted=True,
    )
    (decision,) = result.decisions
    assert not decision.accepted and decision.governed_by is Governor.CHECK_PACKAGE
    assert not result.run_accepted


@pytest.mark.parametrize("status", [INDETERMINATE, UNCOVERED])
@pytest.mark.parametrize("outcome, terminal", [("failed", "failed"), ("succeeded", "completed")])
def test_indeterminate_or_uncovered_keeps_the_existing_verdict(
    status: PackageCriterionStatus, outcome: str, terminal: str
) -> None:
    result = reconcile_acceptance(
        ("k1",),
        {"k1": status},
        {0: _outcome(0, outcome, terminal)},
        existing_run_accepted=terminal == "completed",
    )
    (decision,) = result.decisions
    assert decision.governed_by is Governor.EXISTING_VERIFIER
    assert decision.accepted is (outcome == "succeeded")


@pytest.mark.parametrize(
    "prior",
    [
        _outcome(0, "blocked"),
        _outcome(0, "invalid"),
        ExistingOutcome(0, "failed", "cancelled", "cancelled"),
        None,
    ],
)
def test_package_pass_cannot_accept_a_criterion_nobody_attempted(
    prior: ExistingOutcome | None,
) -> None:
    existing = {} if prior is None else {0: prior}
    result = reconcile_acceptance(("k1",), {"k1": PASS}, existing, existing_run_accepted=False)
    (decision,) = result.decisions
    assert not decision.accepted and decision.governed_by is Governor.EXISTING_VERIFIER
    assert not result.run_accepted


def test_uncovered_criterion_failing_keeps_the_run_failed() -> None:
    result = reconcile_acceptance(
        ("k1", "k2"),
        {"k1": PASS, "k2": UNCOVERED},
        {0: _outcome(0, "failed"), 1: _outcome(1, "failed")},
        existing_run_accepted=False,
    )
    assert [d.accepted for d in result.decisions] == [True, False]
    assert not result.run_accepted


def test_completed_run_without_per_criterion_records_counts_as_accepted() -> None:
    result = reconcile_acceptance(("k1",), {"k1": PASS}, {}, existing_run_accepted=True)
    assert result.run_accepted


def test_existing_outcomes_read_the_latest_final_decision() -> None:
    events = [
        SimpleNamespace(type="execution.ac.attempt_judged", data={"root_ac_index": 0}),
        SimpleNamespace(
            type="execution.ac.acceptance_finalized",
            data={
                "root_ac_index": 0,
                "outcome": "failed",
                "disposition": "failed",
                "terminal_status": "failed",
            },
        ),
        SimpleNamespace(type="execution.ac.acceptance_finalized", data={"root_ac_index": True}),
    ]
    assert existing_outcomes_from_events(events) == {
        0: ExistingOutcome(0, "failed", "failed", "failed")
    }
