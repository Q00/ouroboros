"""The check package as acceptance authority inside the runner (boundary/authority.py)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

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
    apply_reconciliation,
    existing_outcomes_from_results,
)
from ouroboros.boundary.events import ACCEPTANCE_RECONCILED, BOUNDARY_AGGREGATE_TYPE
from ouroboros.boundary.rollout import Arm, AssignmentSource, CheckPackageAssignment
from ouroboros.boundary.run_control import (
    CheckPackageRun,
    legacy_failure_class_from_events,
    legacy_failure_dimensions,
)
from ouroboros.boundary.run_wiring import CheckPackageSettings, prepare_check_package
from ouroboros.orchestrator.parallel_executor_models import (
    ACExecutionOutcome,
    ACExecutionResult,
    ParallelExecutionResult,
)
from ouroboros.persistence.event_store import EventStore

from .test_run_wiring import (
    BUGFIX_SCRIPT,
    FIXED,
    FakeConstructor,
    _ok,
    _package,
    _seed,
)


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


def test_apply_reconciliation_flips_results_and_recomputes_counts() -> None:
    legacy = _parallel(
        ACExecutionOutcome.FAILED,
        ACExecutionOutcome.SUCCEEDED,
        ACExecutionOutcome.SATISFIED_EXTERNALLY,
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


async def test_authority_accepts_a_correct_fix_the_legacy_verifier_rejected(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    seed, authority = await _authority(store, repo, tmp_path)
    (repo / "calc.py").write_text(FIXED)
    decided = await authority(
        seed=seed, execution_id="exec_auth", parallel_result=_parallel(ACExecutionOutcome.FAILED)
    )
    assert decided.all_succeeded is True
    assert authority.outcome.legacy_run_accepted is False
    assert authority.outcome.package_decided is True
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


def test_legacy_failure_class_takes_the_first_rejected_criterion() -> None:
    events = [
        _event(2, "STALL"),
        _event(1, "EVIDENCE_FORM_MISMATCH"),
        _event(3, "unknown"),
        _event(0, "BLOCKED", session_id="other-session"),
        _event(True, "BLOCKED"),
    ]
    assert legacy_failure_class_from_events(events, session_id="s") == (
        "evidence_form_mismatch",
        "3+",
    )
    assert legacy_failure_class_from_events([_event(0, "unknown")], session_id="s") == (
        "other",
        "1",
    )
    assert legacy_failure_class_from_events([], session_id="s") == ("other", "0")


async def test_legacy_failure_dimensions_for_accepted_and_undecided_runs() -> None:
    class _Store:
        async def query_events(self, **_kwargs: Any) -> list[Any]:
            return [_event(0, "FABRICATION_SUSPECTED"), _event(1, "SCOPE_CREEP")]

    store: Any = _Store()
    assert await legacy_failure_dimensions(
        store, execution_id="e", session_id="s", legacy_verdict="accept"
    ) == {"legacy_failure_class": "accepted", "legacy_failure_class_count": "0"}
    assert await legacy_failure_dimensions(
        store, execution_id="e", session_id="s", legacy_verdict="none"
    ) == {"legacy_failure_class": "none", "legacy_failure_class_count": "0"}
    assert await legacy_failure_dimensions(
        store, execution_id="e", session_id="s", legacy_verdict="reject"
    ) == {"legacy_failure_class": "fabrication_suspected", "legacy_failure_class_count": "2"}


def _run(enabled: bool, source: AssignmentSource) -> CheckPackageRun:
    arm = Arm.ON if enabled else Arm.OFF
    return CheckPackageRun(
        CheckPackageSettings(enabled=enabled, assignment=CheckPackageAssignment(arm, source))
    )


async def _meta(run: CheckPackageRun, terminal_status: str, **kwargs: Any) -> dict[str, str]:
    class _Store:
        async def query_events(self, **_kwargs: Any) -> list[Any]:
            return []

    store: Any = _Store()
    return await run.outcome_meta(
        store, execution_id="e", session_id="s", terminal_status=terminal_status, **kwargs
    )


async def test_outcome_meta_for_an_arm_that_never_ran() -> None:
    off = await _meta(_run(False, AssignmentSource.RANDOMIZED), "failed")
    assert off == {
        "check_package_arm": "off",
        "check_package_assignment": "randomized",
        "check_package_status": "not_run",
        "package_verdict": "none",
        "legacy_verdict": "reject",
        "reconciliation": "none",
        "legacy_failure_class": "other",
        "legacy_failure_class_count": "0",
    }
    errored = await _meta(_run(False, AssignmentSource.FALLBACK), "failed", verdict_available=False)
    assert errored["legacy_verdict"] == "none"
    assert errored["legacy_failure_class"] == "none"
    cancelled = await _meta(_run(False, AssignmentSource.FALLBACK), "cancelled")
    assert cancelled["legacy_verdict"] == "none"


async def test_outcome_meta_when_preparation_failed_or_the_hook_never_ran(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    run = _run(True, AssignmentSource.RANDOMIZED)
    run.preparation_error = "OSError"
    meta = await _meta(run, "completed")
    assert meta["check_package_status"] == "construction_failed"
    assert meta["reconciliation"] == "fallback_to_legacy"
    assert meta["package_verdict"] == "none"

    _seed_value, authority = await _authority(store, repo, tmp_path)
    run = _run(True, AssignmentSource.USER_FORCED_ON)
    run.state = authority._state  # noqa: SLF001 - test wiring
    run.authority = authority  # installed, but the runner never called it
    meta = await _meta(run, "completed")
    assert meta["check_package_status"] == "admitted"
    assert meta["package_verdict"] == "none"
    assert meta["reconciliation"] == "fallback_to_legacy"
    assert run.render_outcome() == [
        "Check package was not consulted: this execution path does not support it; "
        "the existing verifier decided the run."
    ]


async def test_prepare_with_the_arm_off_does_not_touch_the_runner(tmp_path: Path) -> None:
    runner = SimpleNamespace(acceptance_authority=None)
    run = _run(False, AssignmentSource.USER_FORCED_OFF)
    lines = await run.prepare(
        runner,
        _seed("add(2, 3) returns 5"),
        event_store=None,  # type: ignore[arg-type]
        execution_id="exec_off",
        worker_dir=tmp_path,
        runtime_backend="codex",
        model=None,
        resume=False,
        constructor_factory=lambda **_kwargs: pytest.fail("constructor must not be built"),
    )
    assert lines == [] and runner.acceptance_authority is None
