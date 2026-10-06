"""The check package as acceptance authority inside the runner (boundary/authority.py)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from ouroboros.boundary.acceptance import (
    AcceptanceReconciliation,
    CriterionDecision,
    Governor,
    PackageCriterionStatus,
)
from ouroboros.boundary.authority import (
    PACKAGE_REJECTION_ERROR,
    CheckPackageAuthority,
    CheckPackageGate,
    apply_reconciliation,
    existing_outcomes_from_results,
)
from ouroboros.boundary.constructor import ALL_CRITERIA_UNCOVERED, ConstructionOutcome
from ouroboros.boundary.events import (
    ACCEPTANCE_RECONCILED,
    BOUNDARY_AGGREGATE_TYPE,
    RunContract,
)
from ouroboros.boundary.package import seed_criterion_keys, seed_digest
from ouroboros.boundary.run_wiring import CheckPackageSettings, prepare_check_package
from ouroboros.orchestrator.parallel_executor_models import (
    ACExecutionOutcome,
    ACExecutionResult,
    ParallelExecutionResult,
    package_settlement_view,
    settle_package_results,
)
from ouroboros.persistence.event_store import EventStore

from .calc_fixtures import BUGFIX_SCRIPT, FIXED, _package, _seed
from .fake_constructors import FakeConstructor, _ok


def _result(index: int, outcome: ACExecutionOutcome) -> ACExecutionResult:
    return ACExecutionResult(
        ac_index=index,
        ac_content=f"criterion {index}",
        success=outcome in (ACExecutionOutcome.SUCCEEDED, ACExecutionOutcome.SATISFIED_EXTERNALLY),
        outcome=outcome,
        error=None if outcome is ACExecutionOutcome.SUCCEEDED else "legacy reason",
    )


def _parallel(*outcomes: ACExecutionOutcome) -> ParallelExecutionResult:
    results = tuple(_result(index, outcome) for index, outcome in enumerate(outcomes))
    return ParallelExecutionResult(
        results=results,
        success_count=sum(o is ACExecutionOutcome.SUCCEEDED for o in outcomes),
        failure_count=sum(o is ACExecutionOutcome.FAILED for o in outcomes),
        externally_satisfied_count=sum(
            o is ACExecutionOutcome.SATISFIED_EXTERNALLY for o in outcomes
        ),
        blocked_count=sum(o is ACExecutionOutcome.BLOCKED for o in outcomes),
    )


def _decision(index: int, *, accepted: bool, existing_accepted: bool) -> CriterionDecision:
    return CriterionDecision(
        root_ac_index=index,
        criterion_key=f"k{index}",
        package_status=PackageCriterionStatus.PASS if accepted else PackageCriterionStatus.FAIL,
        existing_outcome=None,
        existing_accepted=existing_accepted,
        accepted=accepted,
        governed_by=Governor.CHECK_PACKAGE,
        declared_binding_pass=False,
    )


def test_existing_outcomes_mark_only_failed_results_as_rejected_attempts() -> None:
    outcomes = existing_outcomes_from_results(
        _parallel(
            ACExecutionOutcome.FAILED,
            ACExecutionOutcome.SUCCEEDED,
            ACExecutionOutcome.BLOCKED,
            ACExecutionOutcome.SATISFIED_EXTERNALLY,
        )
    )
    assert [outcomes[i].rejected_attempt for i in range(4)] == [True, False, False, False]
    assert [outcomes[i].passed for i in range(4)] == [False, True, False, True]


def _gate_failed(result: ACExecutionResult, **changes: Any) -> ACExecutionResult:
    """``result`` as the package gate leaves an attempt it failed."""
    return CheckPackageGate._repair(replace(result, **changes), "counterexample")


def _settled(result: ACExecutionResult) -> ACExecutionResult:
    """``result`` after a final settlement that found every other gate holding."""
    return settle_package_results([result], [package_settlement_view(result)])[0]


def test_apply_reconciliation_flips_results_and_recomputes_counts() -> None:
    legacy = _parallel(
        ACExecutionOutcome.FAILED,
        ACExecutionOutcome.SUCCEEDED,
        ACExecutionOutcome.SATISFIED_EXTERNALLY,
    )
    legacy = replace(
        legacy, results=(_settled(_gate_failed(legacy.results[0])), *legacy.results[1:])
    )
    reconciliation = AcceptanceReconciliation(
        decisions=(
            _decision(0, accepted=True, existing_accepted=False),
            _decision(1, accepted=False, existing_accepted=True),
            _decision(2, accepted=False, existing_accepted=True),
        ),
        run_accepted=False,
        existing_run_accepted=False,
    )
    decided = apply_reconciliation(legacy, reconciliation)
    assert [r.outcome for r in decided.results] == [
        ACExecutionOutcome.SUCCEEDED,
        ACExecutionOutcome.FAILED,
        ACExecutionOutcome.FAILED,
    ]
    assert decided.results[0].success is True and decided.results[0].error is None
    assert decided.results[1].error == PACKAGE_REJECTION_ERROR
    assert (decided.success_count, decided.failure_count, decided.externally_satisfied_count) == (
        1,
        2,
        0,
    )
    assert decided.all_succeeded is False


@pytest.mark.parametrize(
    "failed",
    [
        _result(0, ACExecutionOutcome.FAILED),  # another gate (runtime, legacy) failed it
        _settled(
            _gate_failed(
                _result(0, ACExecutionOutcome.FAILED),
                verify_gate_outcome=SimpleNamespace(passed=False),
            )
        ),
        # Only the attempt-time (cached) state: no final settlement judged it.
        _gate_failed(
            _result(0, ACExecutionOutcome.FAILED),
            verify_gate_outcome=SimpleNamespace(passed=True),
        ),
        # The final settlement left a verify pass that still needs its replay.
        _settled(
            _gate_failed(
                _result(0, ACExecutionOutcome.FAILED),
                verify_gate_outcome=SimpleNamespace(passed=True, replay_required=True),
            )
        ),
    ],
)
def test_a_result_another_gate_failed_is_never_turned_into_a_success(
    failed: ACExecutionResult,
) -> None:
    # M2: an accepted decision flips a failed result only when the package
    # gate alone failed it, after every other gate (verify_command) passed.
    legacy = ParallelExecutionResult(results=(failed,), success_count=0, failure_count=1)
    reconciliation = AcceptanceReconciliation(
        decisions=(_decision(0, accepted=True, existing_accepted=False),),
        run_accepted=True,
        existing_run_accepted=False,
    )
    decided = apply_reconciliation(legacy, reconciliation)
    assert decided.results[0].outcome is ACExecutionOutcome.FAILED
    assert not decided.all_succeeded
    assert not existing_outcomes_from_results(legacy, gated=True)[0].attempted


def test_apply_reconciliation_without_overrides_returns_the_same_object() -> None:
    legacy = _parallel(ACExecutionOutcome.SUCCEEDED)
    reconciliation = AcceptanceReconciliation(
        decisions=(_decision(0, accepted=True, existing_accepted=True),),
        run_accepted=True,
        existing_run_accepted=True,
    )
    assert apply_reconciliation(legacy, reconciliation) is legacy


@pytest.fixture
async def store():
    event_store = EventStore("sqlite+aiosqlite:///:memory:")
    await event_store.initialize()
    yield event_store
    await event_store.close()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    return root


async def _authority(store: EventStore, repo: Path, tmp_path: Path) -> tuple[Any, Any]:
    seed = _seed("add(2, 3) returns 5")
    settings = CheckPackageSettings(enabled=True)
    state = await prepare_check_package(
        seed,
        event_store=store,
        constructor=FakeConstructor(_ok(_package(seed, "repro_add", BUGFIX_SCRIPT))),
        execution_id="exec_auth",
        base_checkout=repo,
        worker_workspace=repo,
        runtime_label="codex",
        settings=settings,
        store_dir=tmp_path / "store",
    )
    authority = CheckPackageAuthority(state, settings, event_store=store, candidate_checkout=repo)
    return seed, authority


async def test_a_script_only_criterion_is_decided_by_the_legacy_verifier(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    """A script check's pass is advisory (U); the legacy rejection decides (2026-09-27)."""
    seed, authority = await _authority(store, repo, tmp_path)
    (repo / "calc.py").write_text(FIXED)
    decided = await authority(
        seed=seed, execution_id="exec_auth", parallel_result=_parallel(ACExecutionOutcome.FAILED)
    )
    assert decided.all_succeeded is False
    assert authority.outcome.legacy_run_accepted is False
    assert authority.outcome.package_decided is False
    (decision,) = authority.outcome.reconciliation.decisions
    assert decision.legacy_decided and decision.reason == "script_check_advisory"
    events = await store.replay(BOUNDARY_AGGREGATE_TYPE, "exec_auth/check_package/v1")
    assert events[-1].type == ACCEPTANCE_RECONCILED
    # A second call keeps the first decision and records nothing more.
    again = await authority(
        seed=seed, execution_id="exec_auth", parallel_result=_parallel(ACExecutionOutcome.FAILED)
    )
    assert again.all_succeeded is False
    assert len(await store.replay(BOUNDARY_AGGREGATE_TYPE, "exec_auth/check_package/v1")) == len(
        events
    )


async def test_authority_rejects_a_wrong_candidate_the_legacy_verifier_accepted(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    seed, authority = await _authority(store, repo, tmp_path)
    decided = await authority(
        seed=seed,
        execution_id="exec_auth",
        parallel_result=_parallel(ACExecutionOutcome.SUCCEEDED),
    )
    assert decided.all_succeeded is False
    assert decided.results[0].error == PACKAGE_REJECTION_ERROR
    assert authority.outcome.verdict.verdict == "fail"


async def test_authority_error_keeps_the_legacy_result(
    store: EventStore, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed, authority = await _authority(store, repo, tmp_path)

    async def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("disk full")

    monkeypatch.setattr("ouroboros.boundary.authority.verify_check_package", broken)
    legacy = _parallel(ACExecutionOutcome.FAILED)
    assert await authority(seed=seed, execution_id="exec_auth", parallel_result=legacy) is legacy
    assert authority.outcome.error == "RuntimeError"


def _event(index: Any, failure_class: Any, session_id: str = "s") -> SimpleNamespace:
    return SimpleNamespace(
        data={"root_ac_index": index, "last_failure_class": failure_class, "session_id": session_id}
    )


DOC_CRITERION = (
    "README.md documents clamp(value, low, high): it returns high when value > high, "
    "low when value < low, and value otherwise, with the example clamp(15, 0, 10) == 10."
)


@pytest.fixture
def docs_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "mathutils.py").write_text(
        "def clamp(value, low, high):\n    return max(low, min(value, high))\n"
    )
    (root / "README.md").write_text("# mathutils\n")
    return root


async def test_with_no_admitted_package_the_legacy_verifier_decides_every_criterion(
    docs_repo: Path, tmp_path: Path
) -> None:
    seed = _seed(DOC_CRITERION)
    # The constructor lists the only criterion as not executable: nothing to admit.
    constructor = FakeConstructor(
        ConstructionOutcome(None, ALL_CRITERIA_UNCOVERED, "3" * 64, "fake")
    )
    store = EventStore("sqlite+aiosqlite:///:memory:")
    await store.initialize()
    try:
        # The artifact checks off: their repair turn would install the gate.
        settings = CheckPackageSettings(
            enabled=True,
            max_construction_attempts=3,
            base_regression="off",
            worker_test_gate="off",
        )
        state = await prepare_check_package(
            seed,
            event_store=store,
            constructor=constructor,
            execution_id="exec_s4",
            base_checkout=docs_repo,
            worker_workspace=docs_repo,
            runtime_label="codex",
            settings=settings,
            store_dir=tmp_path / "store",
        )
        # Every criterion declared not executable: regenerating cannot help.
        assert len(constructor.calls) == 1
        assert state.admitted is False and state.failure_reason == ALL_CRITERIA_UNCOVERED

        # No package was admitted: the run falls back to the legacy verifier
        # exactly as with the check package off.
        authority = CheckPackageAuthority(
            state, settings, event_store=store, candidate_checkout=docs_repo
        )
        legacy = ParallelExecutionResult(
            results=(
                ACExecutionResult(
                    ac_index=0,
                    ac_content=DOC_CRITERION,
                    success=True,
                    outcome=ACExecutionOutcome.SUCCEEDED,
                ),
            ),
            success_count=1,
            failure_count=0,
        )
        assert await authority(seed=seed, execution_id="exec_s4", parallel_result=legacy) is legacy
        assert authority.outcome is not None and authority.outcome.reconciliation is not None
        decisions = authority.outcome.reconciliation.decisions
        assert all(d.package_status.value == "uncovered" for d in decisions)
        assert authority.outcome.verdict.verdict == "unavailable"
        # An accepted attempt with no verifier evidence is the legacy verifier's
        # decision, never the package's: no package was admitted.
        assert [d.governed_by for d in decisions] == [Governor.EXISTING_VERIFIER]
        assert all(d.accepted for d in decisions)
        assert authority.outcome.package_decided is False

        # Installed without an admitted package: the executor stays legacy, and
        # the terminal decision keeps the legacy rejection of an attempt.
        installed = CheckPackageAuthority(
            state, settings, event_store=store, candidate_checkout=docs_repo
        )
        executor = SimpleNamespace()
        installed.install(executor)
        assert vars(executor) == {} and installed.installed is False
        rejected = ParallelExecutionResult(
            results=(
                ACExecutionResult(
                    ac_index=0,
                    ac_content=DOC_CRITERION,
                    success=False,
                    outcome=ACExecutionOutcome.FAILED,
                    error="legacy verifier rejected it",
                ),
            ),
            success_count=0,
            failure_count=1,
        )
        decided = await installed(seed=seed, execution_id="exec_s4", parallel_result=rejected)
        assert decided.results[0].outcome is ACExecutionOutcome.FAILED
        assert not decided.all_succeeded
        assert installed.outcome is not None and installed.outcome.reconciliation is not None
        (decision,) = installed.outcome.reconciliation.decisions
        assert decision.package_status.value == "uncovered" and decision.accepted is False
    finally:
        await store.close()


async def test_an_unadmitted_no_evidence_success_is_governed_by_the_existing_verifier(
    tmp_path: Path,
) -> None:
    # Construction failed: no package was admitted. A success without any
    # verifier evidence is accepted exactly as with the check package off, and
    # the legacy verifier, not the package, owns that decision.
    seed = _seed(DOC_CRITERION)
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
                ac_content=DOC_CRITERION,
                success=True,
                outcome=ACExecutionOutcome.SUCCEEDED,
            ),
        ),
        success_count=1,
        failure_count=0,
    )
    decided = await authority(seed=seed, execution_id="exec_none", parallel_result=parallel)
    assert decided is parallel
    assert authority.outcome is not None and authority.outcome.reconciliation is not None
    [decision] = authority.outcome.reconciliation.decisions
    assert decision.accepted and decision.governed_by is Governor.EXISTING_VERIFIER
    assert authority.outcome.package_decided is False


async def test_an_authority_refuses_settings_other_than_the_run_contract(
    store, repo: Path, tmp_path: Path
) -> None:
    seed = _seed("add(2, 3) returns 5")
    state = await prepare_check_package(
        seed,
        event_store=store,
        constructor=FakeConstructor(_ok(_package(seed, "repro_add", BUGFIX_SCRIPT))),
        execution_id="exec_contract_mismatch",
        base_checkout=repo,
        worker_workspace=repo,
        runtime_label="codex",
        settings=CheckPackageSettings(enabled=True, check_timeout_seconds=37),
        store_dir=tmp_path / "store",
    )
    with pytest.raises(ValueError, match="run contract"):
        CheckPackageAuthority(
            state,
            CheckPackageSettings(enabled=True, check_timeout_seconds=1),
            event_store=store,
            candidate_checkout=repo,
        )
