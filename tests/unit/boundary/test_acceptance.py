"""Acceptance authority: the admitted package decides the criteria it covers."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ouroboros.boundary.acceptance import (
    ExistingOutcome,
    Governor,
    PackageCriterionStatus,
    criterion_verdicts,
    reconcile_acceptance,
    render_reconciliation,
)
from ouroboros.boundary.binding import CHECK_DIR
from ouroboros.boundary.oracle import OracleResult
from ouroboros.boundary.oracle_build import package_from_reply
from ouroboros.boundary.package import CheckRole
from ouroboros.boundary.receipts import (
    CandidateVerdict,
    CandidateVerification,
    CheckExecution,
    CheckStatus,
)
from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata

PASS = PackageCriterionStatus.PASS
FAIL = PackageCriterionStatus.FAIL
INDETERMINATE = PackageCriterionStatus.INDETERMINATE
UNCOVERED = PackageCriterionStatus.UNCOVERED
UNVERIFIED = PackageCriterionStatus.UNVERIFIED


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
        "target_named_in_criterion": False,
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
        package_id=None,
        seed_digest=package.seed_digest,
        artifact_tree_digest="3" * 64,
        artifact_tree_digest_after="3" * 64,
        verdict=CandidateVerdict.PASS,
        reasons=(),
        protected_bytes_mutated=mutated,
        timeout_seconds=120,
        checks=tuple(
            _execution(check_id, status)
            for check_id, status in zip(
                (check.check_id for check in package.checks), statuses, strict=False
            )
        ),
        started_at=now,
        completed_at=now,
    )


def _statuses(package, verification, **kwargs) -> dict:
    return {
        key: item.status
        for key, item in criterion_verdicts(package, verification, **kwargs).items()
    }


def test_statuses_follow_the_linked_checks() -> None:
    seed = _seed()
    package = _package(seed)
    add_key, sub_key, docs_key = package.criterion_keys
    statuses = _statuses(
        package, _verification(package, CheckStatus.EXPECTED, CheckStatus.VIOLATED)
    )
    # A passing script check is advisory (only an oracle check is a verified
    # pass), so the criterion is unverified; a failing one still fails.
    assert statuses == {add_key: UNVERIFIED, sub_key: FAIL, docs_key: UNCOVERED}

    partial = _statuses(
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
    statuses = _statuses(package, verification, candidate_identity_ok=identity_ok)
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


@pytest.mark.parametrize("outcome, terminal", [("failed", "failed"), ("succeeded", "completed")])
def test_indeterminate_rejects_whatever_the_existing_verdict(outcome: str, terminal: str) -> None:
    result = reconcile_acceptance(
        ("k1",),
        {"k1": INDETERMINATE},
        {0: _outcome(0, outcome, terminal)},
        existing_run_accepted=terminal == "completed",
    )
    (decision,) = result.decisions
    assert decision.governed_by is Governor.CHECK_PACKAGE
    assert not decision.accepted and not result.run_accepted
    assert result.verdict.value == "indeterminate"


@pytest.mark.parametrize("status", [UNVERIFIED, UNCOVERED])
@pytest.mark.parametrize("outcome, terminal", [("failed", "failed"), ("succeeded", "completed")])
def test_unverified_accepts_whatever_the_existing_verdict_and_is_never_a_pass(
    status: PackageCriterionStatus, outcome: str, terminal: str
) -> None:
    result = reconcile_acceptance(
        ("k1",),
        {"k1": status},
        {0: _outcome(0, outcome, terminal)},
        existing_run_accepted=terminal == "completed",
    )
    (decision,) = result.decisions
    assert decision.governed_by is Governor.CHECK_PACKAGE
    assert decision.accepted and decision.unverified and result.run_accepted
    # Unverified is never a pass: no verified pass, artifact verdict unverified.
    assert result.verified_pass_count == 0
    assert result.verdict.value == "unverified"
    assert result.to_dict()["unverified_count"] == 1


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
    assert not decision.accepted and decision.governed_by is Governor.EXECUTION
    assert not result.run_accepted


def test_one_verified_pass_and_the_rest_unverified_is_an_accepted_pass() -> None:
    result = reconcile_acceptance(
        ("k1", "k2"),
        {"k1": PASS, "k2": UNCOVERED},
        {0: _outcome(0, "failed"), 1: _outcome(1, "failed")},
        existing_run_accepted=False,
    )
    assert [d.accepted for d in result.decisions] == [True, True]
    assert result.run_accepted and result.verdict.value == "pass"
    assert result.verified_pass_count == 1 and len(result.unverified) == 1
    lines = render_reconciliation(result)
    assert "Verified: 1 of 2 passed; unverified: 1" in lines[-2]
    assert lines[-1].startswith("- unverified AC 2:")


def test_completed_run_without_per_criterion_records_counts_as_accepted() -> None:
    result = reconcile_acceptance(("k1",), {"k1": PASS}, {}, existing_run_accepted=True)
    assert result.run_accepted


# ----------------------------------------------------------------------
# What a verified pass is (B2, H2)


def _oracle_reply(role: str) -> dict:
    return {
        "oracles": [
            {
                "criterion": 1,
                "check_id": "oracle_add",
                "role": role,
                "call_kind": "function",
                "params": ["a", "b"],
                "default_binding": {"symbol": "calc.add"},
                "target_named_in_criterion": False,
                "cases": [
                    {
                        "case_id": "stated",
                        "args": {"a": 2, "b": 3},
                        "expect": {"kind": "returns", "value": 5},
                        "held_out": False,
                    },
                    {
                        "case_id": "held",
                        "args": {"a": 40, "b": 2},
                        "expect": {"kind": "returns", "value": 42},
                        "held_out": True,
                    },
                ],
            }
        ],
        "uncovered": [
            {"criterion": 2, "reason": "not mechanical"},
            {"criterion": 3, "reason": "not mechanical"},
        ],
    }


def _oracle_verification(package, *, cases: tuple[tuple[str, bool, bool], ...]):
    """One passing oracle execution whose result lists ``(case_id, held_out, passed)``."""
    now = datetime.now(UTC)
    (check,) = package.checks
    execution = _execution(check.check_id, CheckStatus.EXPECTED).model_copy(
        update={
            "role": check.role,
            "oracle_result": OracleResult.model_validate(
                {
                    "check_id": check.check_id,
                    "criterion_key": package.criterion_keys[0],
                    "binding_source": "default",
                    "symbol": "calc.add",
                    "call_kind": "function",
                    "resolve": "ok",
                    "cases": [
                        {"case_id": case_id, "held_out": held_out, "passed": passed}
                        for case_id, held_out, passed in cases
                    ],
                }
            ),
        }
    )
    return CandidateVerification(
        package_sha256=package.sha256,
        package_id=None,
        seed_digest=package.seed_digest,
        artifact_tree_digest="3" * 64,
        artifact_tree_digest_after="3" * 64,
        verdict=CandidateVerdict.PASS,
        reasons=(),
        protected_bytes_mutated=False,
        timeout_seconds=120,
        checks=(execution,),
        started_at=now,
        completed_at=now,
    )


def _first_verdict(package, verification):
    return criterion_verdicts(package, verification)[package.criterion_keys[0]]


def test_a_reproduction_oracle_passing_a_held_out_case_is_a_verified_pass() -> None:
    package = package_from_reply(
        _oracle_reply("reproduction"), _seed(), input_digest="2" * 64, generator="fake"
    )
    verification = _oracle_verification(package, cases=(("c1", False, True), ("c2", True, True)))
    item = _first_verdict(package, verification)
    assert (item.status, item.reason) == (PASS, "passed")


def test_passing_only_the_visible_cases_is_unverified() -> None:
    # The per-attempt gate runs visible cases only, and a package whose
    # oracle states only the Seed's examples proves no more: a hard-coded
    # answer to the stated example must never be a verified pass.
    package = package_from_reply(
        _oracle_reply("reproduction"), _seed(), input_digest="2" * 64, generator="fake"
    )
    verification = _oracle_verification(package, cases=(("c1", False, True),))
    item = _first_verdict(package, verification)
    assert (item.status, item.reason) == (UNVERIFIED, "no_held_out_case")


def test_a_preservation_only_pass_is_unverified() -> None:
    package = package_from_reply(
        _oracle_reply("preservation"), _seed(), input_digest="2" * 64, generator="fake"
    )
    verification = _oracle_verification(package, cases=(("c1", False, True), ("c2", True, True)))
    item = _first_verdict(package, verification)
    assert (item.status, item.reason) == (UNVERIFIED, "no_reproduction_check")


def _a_prime(status: PackageCriterionStatus = PASS):
    from ouroboros.boundary.acceptance import CriterionVerdict
    from ouroboros.boundary.binding import CheckTier

    return CriterionVerdict("k1", status, CheckTier.A_PRIME, "passed")


def test_a_worker_declared_binding_pass_never_overrules_a_legacy_rejection() -> None:
    result = reconcile_acceptance(
        ("k1",),
        {"k1": _a_prime()},
        {0: _outcome(0, "failed")},
        existing_run_accepted=False,
        legacy_decides_unverified=True,
    )
    (decision,) = result.decisions
    assert not decision.accepted and decision.governed_by is Governor.EXISTING_VERIFIER
    assert decision.reason == "a_prime_corroborates_only" and not result.run_accepted


# The runner's canonical shape of a successful attempt that the final
# verification rejected (execution_authority's terminal acceptance plan).
_FINALLY_REJECTED = ExistingOutcome(0, "succeeded", "rejected", "failed")


def test_a_finally_rejected_success_is_a_rejected_attempt_not_a_pass() -> None:
    assert not _FINALLY_REJECTED.passed
    assert _FINALLY_REJECTED.rejected_attempt and _FINALLY_REJECTED.attempted


@pytest.mark.parametrize("package_status", [_a_prime(), UNVERIFIED, UNCOVERED])
def test_a_weak_package_signal_never_accepts_a_finally_rejected_success(
    package_status: object,
) -> None:
    result = reconcile_acceptance(
        ("k1",),
        {"k1": package_status},  # type: ignore[dict-item]
        {0: _FINALLY_REJECTED},
        existing_run_accepted=False,
        legacy_decides_unverified=True,
    )
    (decision,) = result.decisions
    assert not decision.accepted and decision.governed_by is Governor.EXISTING_VERIFIER
    assert decision.existing_accepted is False and not result.run_accepted


@pytest.mark.parametrize("existing", [{0: _outcome(0, "succeeded", "completed")}, {}])
def test_a_worker_declared_binding_pass_corroborates_an_acceptance(existing: dict) -> None:
    result = reconcile_acceptance(
        ("k1",),
        {"k1": _a_prime()},
        existing,
        existing_run_accepted=True,
        legacy_decides_unverified=True,
    )
    (decision,) = result.decisions
    assert decision.accepted and decision.governed_by is Governor.CHECK_PACKAGE


def test_an_unattempted_uncovered_criterion_is_not_counted_as_package_decided() -> None:
    # #2465 follow-up: a run whose package decided nothing never reports full
    # coverage, whether or not the worker attempted the criterion.
    result = reconcile_acceptance(
        ("k1",),
        {"k1": UNCOVERED},
        {0: _outcome(0, "blocked")},
        existing_run_accepted=False,
        legacy_decides_unverified=True,
    )
    (decision,) = result.decisions
    assert decision.governed_by is Governor.EXECUTION and not decision.accepted
    assert result.not_package_decided == (decision,)
    assert result.coverage.value == "low"


def test_the_verdict_names_the_binding_of_the_check_that_decided_it() -> None:
    # #2465 follow-up: a criterion checked through a default binding (tier A)
    # and a worker-declared one (tier A'); the tier A check fails, so the
    # verdict names its binding, not the last one iterated.
    from ouroboros.boundary.binding import (
        Binding,
        BindingSource,
        CallKind,
        CheckTier,
        TierAssignment,
    )

    seed = _seed()
    reply = {
        "checks": [_check("repro_add", 1), _check("repro_sub", 1)],
        "files": [
            {"path": f"{CHECK_DIR}/repro_add.py", "content": "print('add')\n"},
            {"path": f"{CHECK_DIR}/repro_sub.py", "content": "print('sub')\n"},
        ],
        "uncovered": [{"criterion": 2}, {"criterion": 3}],
    }
    package = package_from_reply(reply, seed, input_digest="2" * 64, generator="fake")
    key = package.criterion_keys[0]

    def assigned(check_id: str, tier: CheckTier, symbol: str) -> TierAssignment:
        return TierAssignment(
            criterion_key=key,
            check_id=check_id,
            tier=tier,
            binding=Binding(criterion_key=key, symbol=symbol, call_kind=CallKind.FUNCTION),
            binding_source=(
                BindingSource.DEFAULT if tier is CheckTier.A else BindingSource.DECLARED
            ),
            status_hint="",
            reason="bound",
        )

    assignments = {
        "script_1_1": assigned("script_1_1", CheckTier.A, "calc.add"),
        "script_1_2": assigned("script_1_2", CheckTier.A_PRIME, "calc.plus"),
    }
    verification = _verification(package, CheckStatus.VIOLATED, CheckStatus.EXPECTED)
    item = criterion_verdicts(package, verification, assignments=assignments)[key]
    assert item.status is FAIL and item.tier is CheckTier.A
    assert item.binding is not None and item.binding["symbol"] == "calc.add"
    assert item.binding_source == "default"
