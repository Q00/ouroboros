"""Anonymous telemetry of criteria accepted without evidence (boundary/no_evidence.py).

The count is folded into the run's ``workflow_outcome`` through the outcome
tally bound to the run (``telemetry.outcome_scope``); no separate event is sent.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from ouroboros import telemetry
from ouroboros.boundary import no_evidence
from ouroboros.boundary.acceptance import (
    NO_HELD_OUT_CASE,
    CriterionVerdict,
    ExistingOutcome,
    LegacyNoEvidenceReason,
    PackageCriterionStatus,
    reconcile_acceptance,
)
from ouroboros.boundary.authority import (
    AuthorityOutcome,
    CheckPackageAuthority,
    existing_outcomes_from_results,
)
from ouroboros.boundary.binding import CheckTier
from ouroboros.boundary.events import RunContract
from ouroboros.boundary.package import seed_criterion_keys, seed_digest
from ouroboros.boundary.run_control import CheckPackageRun
from ouroboros.boundary.run_wiring import CheckPackageSettings
from ouroboros.orchestrator.failure_taxonomy import FailureClass
from ouroboros.orchestrator.parallel_executor_models import (
    ACExecutionOutcome,
    ACExecutionResult,
    ParallelExecutionResult,
)
from ouroboros.orchestrator.verifier import VerifierVerdict

from .calc_fixtures import _seed


@pytest.fixture
def tally(monkeypatch: pytest.MonkeyPatch) -> Iterator[telemetry.OutcomeTally]:
    """The run's outcome tally; tests bind it around ``finish`` and drain it."""
    sent: list[str] = []
    # No event may reach the network from this boundary any more.
    monkeypatch.setattr(telemetry, "capture", lambda event, _properties=None: sent.append(event))
    yield telemetry.new_outcome_tally()
    assert sent == []


def _legacy_result(**fields: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "ac_index": 0,
        "outcome": SimpleNamespace(value="succeeded"),
        "success": True,
        "verify_gate_outcome": None,
        "atomic_verifier_verdict": None,
        "sub_results": (),
        "legacy_rejection": None,
    }
    base.update(fields)
    return SimpleNamespace(**base)


def _reason(result: SimpleNamespace) -> LegacyNoEvidenceReason | None:
    parallel = SimpleNamespace(results=(result,))
    return existing_outcomes_from_results(parallel)[0].no_evidence_reason


def test_the_replay_reason_is_read_from_the_results_typed_fields() -> None:
    unverifiable = SimpleNamespace(passed=True, environment_unverifiable=True)
    transcript = SimpleNamespace(
        passed=False, failure_class=FailureClass.TRANSCRIPT_MISSING_INFRASTRUCTURE.value
    )
    failed_verdict = SimpleNamespace(passed=False, failure_class=None)
    script_absent = SimpleNamespace(
        passed=False, failure_class=FailureClass.SCRIPT_ABSENT_FROM_ARTIFACT.value
    )
    assert _reason(_legacy_result(verify_gate_outcome=unverifiable)) is (
        LegacyNoEvidenceReason.ENVIRONMENT_UNVERIFIABLE
    )
    assert _reason(_legacy_result(atomic_verifier_verdict=script_absent)) is (
        LegacyNoEvidenceReason.SCRIPT_ABSENT_FROM_ARTIFACT
    )
    no_call_evidence = SimpleNamespace(
        passed=False, failure_class=FailureClass.NO_CALL_EVIDENCE.value
    )
    assert _reason(_legacy_result(atomic_verifier_verdict=no_call_evidence)) is (
        LegacyNoEvidenceReason.NO_CALL_EVIDENCE
    )
    assert _reason(_legacy_result(atomic_verifier_verdict=transcript)) is (
        LegacyNoEvidenceReason.TRANSCRIPT_UNAVAILABLE
    )
    assert _reason(_legacy_result()) is LegacyNoEvidenceReason.NO_VERIFIER_VERDICT
    assert _reason(_legacy_result(atomic_verifier_verdict=failed_verdict)) is (
        LegacyNoEvidenceReason.VERIFIER_VERDICT_NOT_PASSED
    )
    # A decomposed root takes the reason of its first sub-result without evidence.
    passing = SimpleNamespace(passed=True)
    subs = (
        _legacy_result(atomic_verifier_verdict=passing),
        _legacy_result(atomic_verifier_verdict=transcript),
    )
    assert _reason(_legacy_result(sub_results=subs)) is (
        LegacyNoEvidenceReason.TRANSCRIPT_UNAVAILABLE
    )


def test_no_reason_when_the_legacy_verifier_had_evidence_or_rejected() -> None:
    passing = SimpleNamespace(passed=True)
    evidenced = existing_outcomes_from_results(
        SimpleNamespace(results=(_legacy_result(atomic_verifier_verdict=passing),))
    )[0]
    assert evidenced.no_evidence is False and evidenced.no_evidence_reason is None
    rejected = existing_outcomes_from_results(
        SimpleNamespace(
            results=(_legacy_result(outcome=SimpleNamespace(value="failed"), success=False),)
        )
    )[0]
    assert rejected.no_evidence is False and rejected.no_evidence_reason is None


def test_a_failed_cited_call_is_a_legacy_rejection_never_no_evidence() -> None:
    verdict = VerifierVerdict(
        passed=False,
        reasons=("cited_call_failed: call 1: replay_failed",),
        failure_class=FailureClass.CITED_CALL_FAILED.value,
    )
    # With the gate the executor keeps the rejection advisory on a successful result.
    gated = existing_outcomes_from_results(
        SimpleNamespace(
            results=(
                _legacy_result(
                    atomic_verifier_verdict=verdict,
                    legacy_rejection="Fat-harness verifier failed (cited_call_failed).",
                ),
            )
        ),
        gated=True,
    )[0]
    assert gated.outcome == "failed" and gated.no_evidence is False
    assert gated.failure_class == FailureClass.CITED_CALL_FAILED.value
    reconciliation = reconcile_acceptance(
        ("c0",), {}, {0: gated}, existing_run_accepted=True, legacy_decides_unverified=True
    )
    assert reconciliation.run_accepted is False
    # Without the gate the executor fails the result itself.
    ungated = existing_outcomes_from_results(
        SimpleNamespace(
            results=(
                _legacy_result(
                    outcome=SimpleNamespace(value="failed"),
                    success=False,
                    atomic_verifier_verdict=verdict,
                ),
            )
        )
    )[0]
    assert ungated.outcome == "failed" and ungated.no_evidence is False


def _outcome(
    verdicts: dict[str, Any], legacy: dict[int, ExistingOutcome], *, package_id: str | None
) -> AuthorityOutcome:
    keys = list(verdicts)
    reconciliation = reconcile_acceptance(
        keys, verdicts, legacy, existing_run_accepted=True, legacy_decides_unverified=True
    )
    verdict = SimpleNamespace(package_id=package_id)
    return AuthorityOutcome(
        True,
        verdict=verdict,  # type: ignore[arg-type]
        reconciliation=reconciliation,
        legacy=legacy,
    )


def _accepted(index: int, reason: LegacyNoEvidenceReason | None = None) -> ExistingOutcome:
    return ExistingOutcome(
        index,
        "succeeded",
        "accepted",
        "completed",
        no_evidence=reason is not None,
        no_evidence_reason=reason,
    )


def _run(outcome: AuthorityOutcome) -> CheckPackageRun:
    run = CheckPackageRun(CheckPackageSettings(enabled=True))
    run.resumed = SimpleNamespace(  # type: ignore[assignment]
        outcome=outcome, boundary=SimpleNamespace(reason=None)
    )
    run.runtime_backend = "codex"
    return run


def test_a_reconciliation_with_no_evidence_criteria_is_counted_once(
    tally: telemetry.OutcomeTally,
) -> None:
    unverified = CriterionVerdict(
        "b",
        PackageCriterionStatus.UNVERIFIED,
        CheckTier.A,
        NO_HELD_OUT_CASE,
        declared_binding_pass=False,
    )
    outcome = _outcome(
        {
            "a": PackageCriterionStatus.PASS,
            "b": unverified,
            "c": PackageCriterionStatus.UNCOVERED,
            "d": PackageCriterionStatus.UNCOVERED,
            "e": PackageCriterionStatus.UNCOVERED,
        },
        {
            0: _accepted(0),
            1: _accepted(1, LegacyNoEvidenceReason.TRANSCRIPT_UNAVAILABLE),
            2: _accepted(2, LegacyNoEvidenceReason.NO_VERIFIER_VERDICT),
            3: _accepted(3, LegacyNoEvidenceReason.NO_VERIFIER_VERDICT),
            # Legacy evidence: the legacy verifier decides it, so it has evidence.
            4: _accepted(4),
        },
        package_id="pkg_1",
    )
    run = _run(outcome)
    with telemetry.outcome_scope(tally):
        run.finish("completed", surface="cli_run")
        run.finish("completed", surface="cli_run")  # once per run
    # Two criteria lack a package binding (uncovered), one a held-out case.
    assert tally.drain() == {
        "no_evidence_criteria": 3,
        "no_evidence_top_reason": "uncovered",
        "verification_coverage": "low",
    }
    # Acceptance is unchanged: every criterion is still accepted.
    assert outcome.reconciliation is not None and outcome.reconciliation.run_accepted


def test_nothing_counted_without_a_no_evidence_acceptance_or_a_terminal_status(
    tally: telemetry.OutcomeTally,
) -> None:
    evidenced = _outcome(
        {"a": PackageCriterionStatus.PASS, "b": PackageCriterionStatus.UNCOVERED},
        {0: _accepted(0), 1: _accepted(1)},
        package_id="pkg_1",
    )
    with telemetry.outcome_scope(tally):
        _run(evidenced).finish("completed", surface="mcp_execute")
        paused = _outcome(
            {"a": PackageCriterionStatus.UNCOVERED},
            {0: _accepted(0, LegacyNoEvidenceReason.NO_VERIFIER_VERDICT)},
            package_id="pkg_1",
        )
        run = _run(paused)
        run.finish("paused", surface="mcp_execute")
        assert tally.drain() == {}
        run.finish("completed", surface="mcp_execute")
    assert tally.drain()["no_evidence_criteria"] == 1


def test_a_run_without_a_decision_counts_nothing(tally: telemetry.OutcomeTally) -> None:
    with telemetry.outcome_scope(tally):
        CheckPackageRun(CheckPackageSettings(enabled=False)).finish("completed", surface="cli_run")
    assert tally.drain() == {}


def test_the_package_vocabulary_matches_telemetry() -> None:
    reasons = {
        no_evidence.UNCOVERED,
        no_evidence.NO_ADMITTED_PACKAGE,
        *no_evidence._UNVERIFIED_REASONS,
    }
    assert reasons == telemetry._NO_EVIDENCE_PACKAGE_REASONS


async def test_an_unadmitted_run_names_no_admitted_package(
    tmp_path: Path, tally: telemetry.OutcomeTally
) -> None:
    # The real authority with no admitted package: the legacy verifier owns the
    # decision, and a success without evidence is still accepted.
    seed = _seed("the docs describe add")
    state = SimpleNamespace(
        admitted=False,
        package=None,
        admission=None,
        failure_reason="constructor_failed",
        criterion_keys=seed_criterion_keys(seed),
        seed_digest=seed_digest(seed),
        boundary_id="exec_none/check_package/v1",
        execution_id="exec_none",
        contract=RunContract(
            check_timeout_seconds=CheckPackageSettings(True).check_timeout_seconds
        ),
    )
    authority = CheckPackageAuthority(
        state,  # type: ignore[arg-type]
        CheckPackageSettings(enabled=True),
        event_store=MagicMock(),
        candidate_checkout=tmp_path,
    )
    parallel = ParallelExecutionResult(
        results=(
            ACExecutionResult(
                ac_index=0,
                ac_content="the docs describe add",
                success=True,
                outcome=ACExecutionOutcome.SUCCEEDED,
            ),
        ),
        success_count=1,
        failure_count=0,
    )
    decided = await authority(seed=seed, execution_id="exec_none", parallel_result=parallel)
    assert decided.all_succeeded
    run = CheckPackageRun(
        CheckPackageSettings(enabled=True),
        state=state,  # type: ignore[arg-type]
        authority=authority,
        attempted=True,
    )
    with telemetry.outcome_scope(tally):
        run.finish("completed", surface="evolve")
    props = tally.drain()
    assert props["no_evidence_criteria"] == 1
    assert props["no_evidence_top_reason"] == "no_admitted_package"
