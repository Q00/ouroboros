"""Anonymous telemetry of criteria accepted without evidence (boundary/no_evidence.py)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from ouroboros import telemetry
from ouroboros.boundary import no_evidence
from ouroboros.boundary.acceptance import (
    NO_HELD_OUT_CASE,
    AcceptedBy,
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

from .calc_fixtures import _seed


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """Every ``acceptance_no_evidence`` event the telemetry boundary would queue."""
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        telemetry,
        "capture",
        lambda event, properties=None: (
            events.append((event, properties)) if event == "acceptance_no_evidence" else None
        ),
    )
    return events


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
    withheld = SimpleNamespace(
        passed=False, failure_class=FailureClass.CITED_EVIDENCE_WITHHELD.value
    )
    assert _reason(_legacy_result(atomic_verifier_verdict=withheld)) is (
        LegacyNoEvidenceReason.CITED_EVIDENCE_WITHHELD
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
    run.resumed = SimpleNamespace(outcome=outcome)  # type: ignore[assignment]
    run.runtime_backend = "codex"
    return run


def test_a_reconciliation_with_no_evidence_criteria_emits_exactly_one_event(
    captured: list[tuple[str, dict[str, Any]]],
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
    run.finish("completed", surface="cli_run")
    run.finish("completed", surface="cli_run")  # once per run
    assert len(captured) == 1
    event, props = captured[0]
    assert event == "acceptance_no_evidence"
    assert props["pair_no_held_out_case__transcript_unavailable"] == 1
    assert props["pair_uncovered__no_verifier_verdict"] == 2
    assert props["no_evidence_count"] == 3
    assert props["criterion_count"] == 5
    assert props["verification_coverage"] == "low"
    assert props["surface"] == "cli_run"
    assert props["check_package"] == "on"
    assert props["check_package_status"] == "admitted"
    assert props["runtime_backend"] == "codex"
    assert set(props) <= telemetry._ACCEPTANCE_NO_EVIDENCE_KEYS
    # Acceptance is unchanged: every criterion is still accepted.
    assert outcome.reconciliation is not None and outcome.reconciliation.run_accepted


def test_no_event_without_a_no_evidence_acceptance_or_a_terminal_status(
    captured: list[tuple[str, dict[str, Any]]],
) -> None:
    evidenced = _outcome(
        {"a": PackageCriterionStatus.PASS, "b": PackageCriterionStatus.UNCOVERED},
        {0: _accepted(0), 1: _accepted(1)},
        package_id="pkg_1",
    )
    _run(evidenced).finish("completed", surface="mcp_execute")
    paused = _outcome(
        {"a": PackageCriterionStatus.UNCOVERED},
        {0: _accepted(0, LegacyNoEvidenceReason.NO_VERIFIER_VERDICT)},
        package_id="pkg_1",
    )
    run = _run(paused)
    run.finish("paused", surface="mcp_execute")
    assert captured == []
    run.finish("completed", surface="mcp_execute")
    assert len(captured) == 1


def test_a_run_without_a_decision_emits_nothing(
    captured: list[tuple[str, dict[str, Any]]],
) -> None:
    CheckPackageRun(CheckPackageSettings(enabled=False)).finish("completed", surface="cli_run")
    assert captured == []


def test_the_package_vocabulary_matches_telemetry() -> None:
    reasons = {
        no_evidence.UNCOVERED,
        no_evidence.NO_ADMITTED_PACKAGE,
        *no_evidence._UNVERIFIED_REASONS,
    }
    assert reasons == telemetry._NO_EVIDENCE_PACKAGE_REASONS
    replay = {reason.value for reason in LegacyNoEvidenceReason} | {no_evidence.NO_LEGACY_RECORD}
    assert replay == telemetry._NO_EVIDENCE_REPLAY_REASONS


async def test_an_unadmitted_run_names_no_admitted_package(
    tmp_path: Path, captured: list[tuple[str, dict[str, Any]]]
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
    run.finish("completed", surface="evolve")
    assert len(captured) == 1
    props = captured[0][1]
    assert props["pair_no_admitted_package__no_verifier_verdict"] == 1
    assert props["surface"] == "evolve"
    assert props["check_package_status"] == "construction_failed"


def test_the_evidence_path_is_read_from_the_verdict() -> None:
    def path(**fields: Any) -> Any:
        return existing_outcomes_from_results(SimpleNamespace(results=(_legacy_result(**fields),)))[
            0
        ].evidence_path

    cited = SimpleNamespace(passed=True, decided_by="call_citations")
    strings = SimpleNamespace(passed=True, decided_by="")
    gate = SimpleNamespace(passed=True, environment_unverifiable=False)
    assert path(atomic_verifier_verdict=cited) is AcceptedBy.TRANSCRIPT_EVIDENCE
    assert path(atomic_verifier_verdict=strings) is AcceptedBy.COMMAND_STRINGS
    assert path(verify_gate_outcome=gate) is AcceptedBy.VERIFY_COMMAND
    assert path() is None


def test_accepted_by_separates_transcript_evidence_from_the_package() -> None:
    def outcome(evidence_path: AcceptedBy | None, *, no_evidence: bool = False) -> Any:
        return ExistingOutcome(
            root_ac_index=0,
            outcome="succeeded",
            disposition="accepted",
            terminal_status="completed",
            no_evidence=no_evidence,
            evidence_path=evidence_path,
        )

    def accepted_by(status: PackageCriterionStatus, prior: ExistingOutcome) -> Any:
        reconciliation = reconcile_acceptance(
            ["ac_0"], {"ac_0": status}, {0: prior}, existing_run_accepted=True
        )
        return reconciliation.decisions[0].accepted_by

    cited = outcome(AcceptedBy.TRANSCRIPT_EVIDENCE)
    assert accepted_by(PackageCriterionStatus.PASS, cited) is AcceptedBy.CHECK_PACKAGE
    assert accepted_by(PackageCriterionStatus.UNCOVERED, cited) is AcceptedBy.TRANSCRIPT_EVIDENCE
    assert (
        accepted_by(PackageCriterionStatus.UNVERIFIED, outcome(None, no_evidence=True))
        is AcceptedBy.NO_EVIDENCE
    )
    assert accepted_by(PackageCriterionStatus.FAIL, cited) is None
