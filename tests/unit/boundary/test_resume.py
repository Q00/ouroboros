"""A controller that dies after the worker stops never hands the decision to legacy.

The crash is simulated after the worker stopped and before the package
decided: the run's in-memory state is dropped (another process) or kept (the
same process), nothing of the authority ran, and the resumed authority is built
from the journal as ``run_control`` builds it. What a resume may rely on is the
ledger's recovery projection (``ledger.recovery_projection``): off and a failed
construction resume as legacy; any journal the product could not have written
is undecidable (every criterion indeterminate, nothing runs); a bound package
decides in full only from the same process's memory, and otherwise nothing
runs and covered criteria are undecided.
"""

from __future__ import annotations

import asyncio
import copy as copy_module
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from ouroboros.boundary.acceptance import PackageCriterionStatus
from ouroboros.boundary.authority import CheckPackageAuthority
from ouroboros.boundary.check_env import INTERPRETER_CHANGED
from ouroboros.boundary.constructor import ConstructionOutcome
from ouroboros.boundary.events import (
    ACCEPTANCE_RESUMED,
    ACTOR_STARTED,
    ADMISSION_COMPLETED,
    BOUNDARY_AGGREGATE_TYPE,
    CHECK_PACKAGE_ENABLED,
    CONSTRUCTION_FAILED,
    PACKAGE_FROZEN,
    RunContract,
    actor_started_event,
    boundary_version_id,
    construction_failed_event,
    package_frozen_event,
    superseded_event,
)
from ouroboros.boundary.ledger import BoundaryLedger, BoundaryOrderError, verify_boundary_order
from ouroboros.boundary.oracle_build import package_from_reply
from ouroboros.boundary.package import seal_package, seed_criterion_keys
import ouroboros.boundary.resume as resume_module
from ouroboros.boundary.resume import (
    BOUNDARY_RECORD_MISSING,
    HELD_OUT_UNAVAILABLE,
    ResumedCheckPackageAuthority,
    load_resumed_boundary,
)
from ouroboros.boundary.run_wiring import (
    BoundaryRunState,
    CheckPackageSettings,
    forget_live_state,
    live_state,
    prepare_check_package,
)
from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata
from ouroboros.events.base import BaseEvent
from ouroboros.orchestrator.parallel_executor_models import (
    ACExecutionOutcome,
    ACExecutionResult,
    CheckPackageOwner,
    CheckPackageProvenance,
    FinalGateSettlement,
    ParallelExecutionResult,
)
from ouroboros.persistence.event_store import EventStore

CLAMP_BUGGY = "def clamp(value, low, high):\n    if value > high:\n        return value\n    return max(low, value)\n"
CLAMP_FIXED = "def clamp(value, low, high):\n    return max(low, min(high, value))\n"
DOUBLE = "def double(x):\n    return 2 * x\n"
EXECUTION = "exec_crash"
LEGACY_TEXT = "legacy verifier: evidence form mismatch"
V1 = boundary_version_id(EXECUTION, 1)


def _seed() -> Seed:
    return Seed(
        goal="math helpers",
        acceptance_criteria=(
            "clamp(15, 0, 10) returns 10",
            "double(3) returns 6 and double(4) returns 8",
            "the helpers are documented in the README",
        ),
        ontology_schema=OntologySchema(name="mathutils", description="math helpers"),
        metadata=SeedMetadata(seed_id="seed_resume", ambiguity_score=0.1),
    )


def _reply() -> dict[str, Any]:
    return {
        "oracles": [
            {
                "criterion": 1,
                "check_id": "oracle_1",
                "role": "reproduction",
                "call_kind": "function",
                "params": ["value", "low", "high"],
                "default_binding": {"symbol": "mathutils.clamp"},
                "target_named_in_criterion": False,
                "cases": [
                    {
                        "case_id": "c1",
                        "held_out": False,
                        "args": {"value": 15, "low": 0, "high": 10},
                        "expect": {"kind": "returns", "value": 10},
                    },
                    {
                        "case_id": "c2",
                        "held_out": True,
                        "args": {"value": 99, "low": 1, "high": 7},
                        "expect": {"kind": "returns", "value": 7},
                    },
                ],
            },
            {
                "criterion": 2,
                "check_id": "oracle_2",
                "role": "preservation",
                "call_kind": "function",
                "params": ["x"],
                "default_binding": {"symbol": "mathutils.double"},
                "target_named_in_criterion": False,
                "cases": [
                    {
                        "case_id": "three",
                        "held_out": False,
                        "args": {"x": 3},
                        "expect": {"kind": "returns", "value": 6},
                    },
                    {
                        "case_id": "four",
                        "held_out": True,
                        "args": {"x": 4},
                        "expect": {"kind": "returns", "value": 8},
                    },
                ],
            },
        ],
        "uncovered": [{"criterion": 3, "reason": "not executable"}],
    }


class _Constructor:
    def __init__(self, seed: Seed, base: Path, reply: dict[str, Any] | None = None) -> None:
        self.outcome = ConstructionOutcome(
            package_from_reply(reply or _reply(), seed, input_digest="1" * 64, generator="fake"),
            None,
            "1" * 64,
            "fake",
        )

    async def construct(self, seed: Seed, base: Path, *, feedback=()) -> ConstructionOutcome:
        return self.outcome


class _Failing:
    async def construct(self, seed: Seed, base: Path, *, feedback=()) -> ConstructionOutcome:
        return ConstructionOutcome(None, "constructor_timeout", "1" * 64, "fake")


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
    (root / "mathutils.py").write_text(CLAMP_BUGGY + DOUBLE)
    return root


@pytest.fixture
def no_check_runs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Records any attempt of the resumed authority to bind or run a check."""
    calls: list[str] = []

    async def refuse(*_args: Any, **_kwargs: Any) -> Any:
        calls.append("ran")
        raise AssertionError("a resumed run without its package must run no check")

    monkeypatch.setattr(resume_module, "assign_tiers", refuse)
    monkeypatch.setattr(resume_module, "verify_with_bindings", refuse)
    return calls


async def _run_until_the_worker_stops(
    store: EventStore,
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    constructor: Any = None,
) -> tuple[Seed, BoundaryRunState]:
    """Prepare (switch on), dispatch the worker, and let it stop; the package never decides."""
    seed = _seed()
    state = await prepare_check_package(
        seed,
        event_store=store,
        constructor=constructor or _Constructor(seed, repo),
        execution_id=EXECUTION,
        base_checkout=repo,
        worker_workspace=repo,
        runtime_label="codex",
        settings=CheckPackageSettings(True, max_construction_attempts=1),
        store_dir=tmp_path / "store",
    )
    if constructor is None:
        assert state.admitted and state.package is not None
        held = [case.held_out for spec in state.package.oracles for case in spec.cases]
        assert held == [False, True, False, True]
        assert live_state(EXECUTION) is state
    return seed, state


def _restored(*, legacy_rejected: tuple[int, ...] = ()) -> ParallelExecutionResult:
    """What the resumed executor restores: every root succeeded before the crash."""
    results = tuple(
        ACExecutionResult(
            ac_index=index,
            ac_content=f"criterion {index}",
            success=True,
            outcome=ACExecutionOutcome.SUCCEEDED,
            legacy_rejection=LEGACY_TEXT if index in legacy_rejected else None,
        )
        for index in range(3)
    )
    return ParallelExecutionResult(results=results, success_count=3, failure_count=0)


async def _resume(store: EventStore, repo: Path) -> ResumedCheckPackageAuthority:
    """The resumed authority, as run_control installs it (``test_run_control`` covers that step)."""
    boundary = await load_resumed_boundary(store, EXECUTION)
    assert boundary is not None
    return ResumedCheckPackageAuthority(boundary, event_store=store, candidate_checkout=repo)


def _failed(index: int, error: str, **fields: Any) -> ACExecutionResult:
    return ACExecutionResult(
        ac_index=index,
        ac_content=f"criterion {index}",
        success=False,
        outcome=ACExecutionOutcome.FAILED,
        error=error,
        **fields,
    )


# --------------------------------------------------------------------------
# A bound package: the same process decides in full; another process runs nothing.


async def test_in_the_same_process_the_held_out_cases_decide(
    store: EventStore, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed, _state = await _run_until_the_worker_stops(store, repo, tmp_path, monkeypatch)
    (repo / "mathutils.py").write_text(CLAMP_FIXED + DOUBLE)
    # The run's task died but the process did not: the state is still live.
    authority = await _resume(store, repo)
    assert authority.boundary.source == "memory"
    decided = await authority(seed=seed, execution_id=EXECUTION, parallel_result=_restored())
    verdicts = authority.outcome.verdict.verdicts
    assert verdicts[seed_criterion_keys(seed)[0]].status is PackageCriterionStatus.PASS
    assert decided.all_succeeded
    # The final verdict exists: the in-process state is dropped.
    assert live_state(EXECUTION) is None


async def test_in_the_same_process_a_failure_fails_a_criterion_the_legacy_verifier_accepted(
    store: EventStore, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed, _state = await _run_until_the_worker_stops(store, repo, tmp_path, monkeypatch)
    # The worker left clamp broken; the legacy verifier accepted everything.
    authority = await _resume(store, repo)
    decided = await authority(seed=seed, execution_id=EXECUTION, parallel_result=_restored())
    verdicts = authority.outcome.verdict.verdicts
    assert verdicts[seed_criterion_keys(seed)[0]].status is PackageCriterionStatus.FAIL
    assert decided.results[0].outcome is ACExecutionOutcome.FAILED
    assert "fails the frozen check package" in (decided.results[0].error or "")
    assert not decided.all_succeeded


async def test_in_another_process_no_check_runs_and_covered_criteria_are_undecided(
    store: EventStore,
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_check_runs: list[str],
) -> None:
    seed, state = await _run_until_the_worker_stops(store, repo, tmp_path, monkeypatch)
    (repo / "mathutils.py").write_text(CLAMP_FIXED + DOUBLE)  # the worker's final workspace
    forget_live_state(state)  # the controller process died: its memory is gone
    authority = await _resume(store, repo)
    assert authority.boundary.source == "journal"
    assert authority.boundary.held_out_checks == frozenset({"oracle_1", "oracle_2"})
    decided = await authority(
        seed=seed, execution_id=EXECUTION, parallel_result=_restored(legacy_rejected=(2,))
    )
    assert no_check_runs == []
    keys = seed_criterion_keys(seed)
    verdicts = authority.outcome.verdict.verdicts
    assert [(verdicts[key].status, verdicts[key].reason) for key in keys[:2]] == [
        (PackageCriterionStatus.INDETERMINATE, HELD_OUT_UNAVAILABLE)
    ] * 2
    assert verdicts[keys[2]].status is PackageCriterionStatus.UNCOVERED
    # Covered: not accepted, whatever the legacy verifier said. Uncovered: the
    # legacy verifier decides (it rejected criterion 3 here).
    assert [result.outcome for result in decided.results] == [ACExecutionOutcome.FAILED] * 3
    assert decided.results[2].error and LEGACY_TEXT in decided.results[2].error
    events = await store.replay(BOUNDARY_AGGREGATE_TYPE, state.boundary_id)
    (resumed,) = [event for event in events if event.type == ACCEPTANCE_RESUMED]
    assert state.package is not None
    assert resumed.data["package_id"] == state.package.package_id
    assert resumed.data["held_out_checks"] == ["oracle_1", "oracle_2"]
    assert verify_boundary_order(events) == ()
    assert any("not re-run" in line for line in authority.render())
    # A second call keeps the first decision.
    again = await authority(seed=seed, execution_id=EXECUTION, parallel_result=_restored())
    assert again.all_succeeded


async def test_in_another_process_an_uncovered_criterion_the_legacy_verifier_accepted_passes(
    store: EventStore,
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_check_runs: list[str],
) -> None:
    seed, state = await _run_until_the_worker_stops(store, repo, tmp_path, monkeypatch)
    forget_live_state(state)
    authority = await _resume(store, repo)
    decided = await authority(seed=seed, execution_id=EXECUTION, parallel_result=_restored())
    assert [result.outcome for result in decided.results] == [
        ACExecutionOutcome.FAILED,
        ACExecutionOutcome.FAILED,
        ACExecutionOutcome.SUCCEEDED,  # uncovered: unverified, as in the live run
    ]
    assert not decided.all_succeeded and no_check_runs == []


def _other_package(state: BoundaryRunState) -> BoundaryRunState:
    assert state.package is not None
    return replace(state, package=seal_package(state.package))


def _other_contract(state: BoundaryRunState) -> BoundaryRunState:
    return replace(
        state,
        contract=RunContract(check_timeout_seconds=state.contract.check_timeout_seconds + 1),
    )


def _other_interpreter_digest(state: BoundaryRunState) -> BoundaryRunState:
    return replace(state, interpreter=replace(state.interpreter, sha256="e" * 64))


def _other_interpreter_binary(state: BoundaryRunState) -> BoundaryRunState:
    # The pin names another real path: the binary it points at is not the pin.
    return replace(state, interpreter=replace(state.interpreter, realpath="/nonexistent/python3"))


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (_other_package, HELD_OUT_UNAVAILABLE),
        (_other_contract, HELD_OUT_UNAVAILABLE),
        (_other_interpreter_digest, INTERPRETER_CHANGED),
        (_other_interpreter_binary, INTERPRETER_CHANGED),
    ],
    ids=["package", "contract", "interpreter_digest", "interpreter_binary"],
)
async def test_a_live_state_that_is_not_the_projection_runs_nothing(
    store: EventStore,
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_check_runs: list[str],
    change: Any,
    reason: str,
) -> None:
    seed, state = await _run_until_the_worker_stops(store, repo, tmp_path, monkeypatch)
    (repo / "mathutils.py").write_text(CLAMP_FIXED + DOUBLE)
    changed = change(state)
    monkeypatch.setattr(resume_module, "live_state", lambda _execution: changed)
    authority = await _resume(store, repo)
    assert authority.boundary.source == "journal"
    decided = await authority(seed=seed, execution_id=EXECUTION, parallel_result=_restored())
    keys = seed_criterion_keys(seed)
    verdicts = authority.outcome.verdict.verdicts
    assert [verdicts[key].reason for key in keys[:2]] == [reason] * 2
    assert [result.outcome for result in decided.results[:2]] == [ACExecutionOutcome.FAILED] * 2
    assert no_check_runs == []
    forget_live_state(state)


async def test_the_same_process_uses_the_check_timeout_the_run_started_with(
    store: EventStore, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed, _state = await _run_until_the_worker_stops(store, repo, tmp_path, monkeypatch)
    recorded = await BoundaryLedger(store).run_contract(EXECUTION)
    assert recorded is not None
    seen: list[float] = []
    real = resume_module.verify_with_bindings

    async def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs["contract"])
        return await real(*args, **kwargs)

    monkeypatch.setattr(resume_module, "verify_with_bindings", spy)
    authority = await _resume(store, repo)
    assert authority.boundary.contract == recorded
    await authority(seed=seed, execution_id=EXECUTION, parallel_result=_restored())
    assert seen == [recorded]


# A resumed run counts attempts exactly as the live run with the package on does.


async def test_after_a_crash_a_root_that_already_failed_is_never_accepted(
    store: EventStore, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed session and a failed verify command stay failed, even in the same process."""
    seed, _state = await _run_until_the_worker_stops(store, repo, tmp_path, monkeypatch)
    (repo / "mathutils.py").write_text(CLAMP_FIXED + DOUBLE)
    authority = await _resume(store, repo)
    restored = ParallelExecutionResult(
        results=(
            _restored().results[0],
            _failed(1, "Implementation session failed"),
            _failed(2, "Verify gate failed: exit 1"),
        ),
        success_count=1,
        failure_count=2,
    )
    decided = await authority(seed=seed, execution_id=EXECUTION, parallel_result=restored)
    assert [result.outcome for result in decided.results] == [
        ACExecutionOutcome.SUCCEEDED,  # a verified pass, held-out case included
        ACExecutionOutcome.FAILED,
        ACExecutionOutcome.FAILED,
    ]
    assert decided.results[1].error == "Implementation session failed"
    assert decided.results[2].error == "Verify gate failed: exit 1"
    decisions = authority.outcome.reconciliation.decisions
    assert [decision.accepted for decision in decisions] == [True, False, False]


def _gate_failed(settlement: FinalGateSettlement | None) -> ParallelExecutionResult:
    """Criterion 1 failed by the package gate alone; ``settlement`` is this run's final one."""
    gate_failed = _failed(
        0,
        "check_package: the finished workspace fails the frozen check package",
        check_package=CheckPackageProvenance(
            CheckPackageOwner.CHECK_PACKAGE, repair="counterexample"
        ),
        final_gate_settlement=settlement,
    )
    base = _restored().results
    return ParallelExecutionResult(
        results=(gate_failed, base[1], base[2]), success_count=2, failure_count=1
    )


async def test_after_a_crash_a_root_only_the_gate_failed_is_decided_by_the_package(
    store: EventStore, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As in the live run, a root the package gate failed is an attempt the package decides."""
    seed, _state = await _run_until_the_worker_stops(store, repo, tmp_path, monkeypatch)
    (repo / "mathutils.py").write_text(CLAMP_FIXED + DOUBLE)
    authority = await _resume(store, repo)
    # The resumed executor's final settlement found every other gate holding.
    restored = _gate_failed(FinalGateSettlement.HOLDS)
    decided = await authority(seed=seed, execution_id=EXECUTION, parallel_result=restored)
    # The finished workspace passes criterion 1, held-out case included.
    assert decided.results[0].outcome is ACExecutionOutcome.SUCCEEDED
    assert decided.all_succeeded


async def test_after_a_crash_an_unsettled_gate_failure_is_never_accepted(
    store: EventStore, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The settlement is never persisted: until the resumed executor settles it, it stays failed."""
    seed, _state = await _run_until_the_worker_stops(store, repo, tmp_path, monkeypatch)
    (repo / "mathutils.py").write_text(CLAMP_FIXED + DOUBLE)
    authority = await _resume(store, repo)
    decided = await authority(seed=seed, execution_id=EXECUTION, parallel_result=_gate_failed(None))
    assert decided.results[0].outcome is ACExecutionOutcome.FAILED
    assert not authority.outcome.reconciliation.decisions[0].accepted
    assert not decided.all_succeeded


# The live authority's fail-closed rule, on resume (B1 parity).


async def test_a_resume_error_leaves_covered_criteria_undecided_and_legacy_decides_the_rest(
    store: EventStore, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed, _state = await _run_until_the_worker_stops(store, repo, tmp_path, monkeypatch)
    (repo / "mathutils.py").write_text(CLAMP_FIXED + DOUBLE)
    authority = await _resume(store, repo)

    async def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("disk full")

    monkeypatch.setattr(resume_module, "decide_resumed", broken)
    decided = await authority(seed=seed, execution_id=EXECUTION, parallel_result=_restored())
    assert authority.outcome.error == "OSError"
    decisions = authority.outcome.reconciliation.decisions
    assert [d.reason for d in decisions[:2]] == ["authority_error:OSError"] * 2
    assert [d.accepted for d in decisions] == [False, False, True]
    assert [result.outcome for result in decided.results] == [
        ACExecutionOutcome.FAILED,
        ACExecutionOutcome.FAILED,
        ACExecutionOutcome.SUCCEEDED,  # uncovered, the legacy verifier accepted it
    ]
    assert any("covered criteria are undecided" in line for line in authority.render())


async def test_a_resume_error_reading_the_legacy_verdicts_fails_every_root(
    store: EventStore, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed, state = await _run_until_the_worker_stops(store, repo, tmp_path, monkeypatch)
    forget_live_state(state)
    authority = await _resume(store, repo)

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise KeyError("legacy")

    monkeypatch.setattr(resume_module, "existing_outcomes_from_results", broken)
    monkeypatch.setattr("ouroboros.boundary.authority.existing_outcomes_from_results", broken)
    decided = await authority(seed=seed, execution_id=EXECUTION, parallel_result=_restored())
    assert authority.outcome.error == "KeyError"
    assert (decided.success_count, decided.failure_count) == (0, 3)
    assert not decided.all_succeeded


# --------------------------------------------------------------------------
# The recovery projection: any journal the product could not have written is
# undecidable, before any legacy decision and before any check runs.


async def _copy_journal(store: EventStore, edit: Any = None) -> EventStore:
    """A copy of the run's journal (the run aggregate and every version), edited by ``edit``.

    ``edit`` takes and returns the list of events; it may drop, change,
    reorder (by timestamp) or add events.
    """
    events: list[BaseEvent] = []
    for aggregate in (EXECUTION, V1, boundary_version_id(EXECUTION, 2)):
        events.extend(await store.replay(BOUNDARY_AGGREGATE_TYPE, aggregate))
    if edit is not None:
        events = edit([event.model_copy(deep=True) for event in events])
    copy = EventStore("sqlite+aiosqlite:///:memory:")
    await copy.initialize()
    for event in events:
        await copy.append(event)
    return copy


def _drop(*types: str) -> Any:
    return lambda events: [event for event in events if event.type not in types]


def _edit(event_type: str, change: Any) -> Any:
    def apply(events: list[BaseEvent]) -> list[BaseEvent]:
        out = []
        for event in events:
            if event.type == event_type:
                data = copy_module.deepcopy(dict(event.data))
                change(data)
                event = event.model_copy(update={"data": data})
            out.append(event)
        return out

    return apply


def _duplicate(event_type: str) -> Any:
    def apply(events: list[BaseEvent]) -> list[BaseEvent]:
        (original,) = [event for event in events if event.type == event_type]
        later = max(event.timestamp for event in events) + timedelta(seconds=1)
        return [*events, original.model_copy(update={"id": str(uuid4()), "timestamp": later})]

    return apply


def _actor_before_seal(events: list[BaseEvent]) -> list[BaseEvent]:
    (frozen,) = [event for event in events if event.type == PACKAGE_FROZEN]
    return [
        event.model_copy(update={"timestamp": frozen.timestamp - timedelta(milliseconds=1)})
        if event.type == ACTOR_STARTED
        else event
        for event in events
    ]


def _run_events_only(events: list[BaseEvent]) -> list[BaseEvent]:
    return [event for event in events if event.aggregate_id == EXECUTION]


def _plus_version(number: int) -> Any:
    def apply(events: list[BaseEvent]) -> list[BaseEvent]:
        (frozen,) = [event for event in events if event.type == PACKAGE_FROZEN]
        later = max(event.timestamp for event in events) + timedelta(seconds=1)
        return [
            *events,
            frozen.model_copy(
                update={
                    "id": str(uuid4()),
                    "aggregate_id": boundary_version_id(EXECUTION, number),
                    "timestamp": later,
                }
            ),
        ]

    return apply


def _every_tier_c(data: dict[str, Any]) -> None:
    data["check_tiers"] = dict.fromkeys(data["check_tiers"], "C")


MALFORMED = {
    "actor_before_seal": _actor_before_seal,
    "duplicate_seal": _duplicate(PACKAGE_FROZEN),
    "duplicate_admission": _duplicate(ADMISSION_COMPLETED),
    "duplicate_actor_start": _duplicate(ACTOR_STARTED),
    "duplicate_enabled_record": _duplicate(CHECK_PACKAGE_ENABLED),
    "admission_before_seal": _drop(PACKAGE_FROZEN),
    "no_admission": _drop(ADMISSION_COMPLETED),
    "no_worker_start": _drop(ACTOR_STARTED),
    "versions_deleted": _run_events_only,
    "enabled_record_deleted": _drop(CHECK_PACKAGE_ENABLED),
    "missing_record_sha256": _edit(PACKAGE_FROZEN, lambda d: d.pop("record_sha256")),
    "missing_manifest": _edit(PACKAGE_FROZEN, lambda d: d.pop("manifest")),
    "shortened_manifest": _edit(
        PACKAGE_FROZEN,
        lambda d: d["manifest"].update(checks=d["manifest"]["checks"][1:]),
    ),
    "missing_interpreter_pin": _edit(ADMISSION_COMPLETED, lambda d: d.pop("interpreter_sha256")),
    "missing_interpreter_path_pin": _edit(
        ADMISSION_COMPLETED, lambda d: d.pop("interpreter_realpath_sha256")
    ),
    "missing_contract": _edit(CHECK_PACKAGE_ENABLED, lambda d: d.pop("contract")),
    "malformed_contract": _edit(
        CHECK_PACKAGE_ENABLED, lambda d: d.update(contract={"check_timeout_seconds": 0})
    ),
    "conflicting_admission_package": _edit(
        ADMISSION_COMPLETED, lambda d: d.update(package_id="f" * 64)
    ),
    "conflicting_actor_package": _edit(ACTOR_STARTED, lambda d: d.update(package_id="f" * 64)),
    "admission_for_another_seed": _edit(
        ADMISSION_COMPLETED, lambda d: d.update(seed_digest="f" * 64)
    ),
    "every_check_excluded": _edit(ADMISSION_COMPLETED, _every_tier_c),
    "admission_without_tiers": _edit(ADMISSION_COMPLETED, lambda d: d.pop("check_tiers")),
    "admission_not_admitted": _edit(ADMISSION_COMPLETED, lambda d: d.update(verdict="rejected")),
    "admitted_check_violated": _edit(
        ADMISSION_COMPLETED,
        lambda d: d["checks"][0].update(status="violated"),
    ),
    "version_gap": _plus_version(3),
    "second_bound_version": _plus_version(2),
}


@pytest.mark.parametrize("edit", list(MALFORMED.values()), ids=list(MALFORMED))
async def test_a_journal_the_product_could_not_write_is_undecidable_and_runs_nothing(
    store: EventStore,
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_check_runs: list[str],
    edit: Any,
) -> None:
    # The live state is kept on purpose: an undecidable journal never lets the
    # in-memory package run, and never hands a criterion to the legacy verifier.
    seed, state = await _run_until_the_worker_stops(store, repo, tmp_path, monkeypatch)
    (repo / "mathutils.py").write_text(CLAMP_FIXED + DOUBLE)
    journal = await _copy_journal(store, edit)
    authority = await _resume(journal, repo)
    assert authority.boundary.reason == BOUNDARY_RECORD_MISSING
    assert authority.boundary.covered is None and authority.boundary.live is None
    decided = await authority(seed=seed, execution_id=EXECUTION, parallel_result=_restored())
    verdicts = authority.outcome.verdict.verdicts
    assert {item.status for item in verdicts.values()} == {PackageCriterionStatus.INDETERMINATE}
    assert {item.reason for item in verdicts.values()} == {BOUNDARY_RECORD_MISSING}
    assert [result.outcome for result in decided.results] == [ACExecutionOutcome.FAILED] * 3
    assert no_check_runs == []
    (resumed,) = [
        event
        for event in await journal.replay(BOUNDARY_AGGREGATE_TYPE, EXECUTION)
        if event.type == ACCEPTANCE_RESUMED
    ]
    assert resumed.data["package_id"] is None
    assert resumed.data["reason"] == BOUNDARY_RECORD_MISSING
    forget_live_state(state)
    await journal.close()


async def test_an_untouched_journal_copy_is_bound(
    store: EventStore, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The control of the matrix above: the copy itself is a journal the product wrote.
    await _run_until_the_worker_stops(store, repo, tmp_path, monkeypatch)
    journal = await _copy_journal(store)
    boundary = await load_resumed_boundary(journal, EXECUTION)
    assert boundary is not None and boundary.reason is None and boundary.source == "memory"
    await journal.close()


async def test_a_journal_with_no_record_of_the_run_resumes_as_the_legacy_run(
    store: EventStore, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Documented residual: the journal is writable by the same user; removing
    # every record of the run, the enabled record included, reads as "off".
    await _run_until_the_worker_stops(store, repo, tmp_path, monkeypatch)
    journal = await _copy_journal(store, lambda _events: [])
    assert await load_resumed_boundary(journal, EXECUTION) is None
    await journal.close()


async def test_a_worker_bound_to_a_failed_construction_resumes_as_the_legacy_run(
    store: EventStore, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_, state = await _run_until_the_worker_stops(
        store, repo, tmp_path, monkeypatch, constructor=_Failing()
    )
    assert not state.admitted
    assert await load_resumed_boundary(store, EXECUTION) is None


@pytest.mark.parametrize(
    "edit",
    [_duplicate(CONSTRUCTION_FAILED), _drop(CONSTRUCTION_FAILED), _drop(CHECK_PACKAGE_ENABLED)],
    ids=["duplicate_construction_failed", "no_seal", "enabled_record_deleted"],
)
async def test_a_malformed_failed_construction_is_undecidable_never_legacy(
    store: EventStore, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, edit: Any
) -> None:
    await _run_until_the_worker_stops(store, repo, tmp_path, monkeypatch, constructor=_Failing())
    journal = await _copy_journal(store, edit)
    boundary = await load_resumed_boundary(journal, EXECUTION)
    assert boundary is not None and boundary.covered is None
    assert boundary.reason == BOUNDARY_RECORD_MISSING
    await journal.close()


async def test_a_resume_without_an_execution_id_is_an_error(store: EventStore) -> None:
    with pytest.raises(BoundaryOrderError):
        await load_resumed_boundary(store, "")


async def test_a_later_version_without_the_first_is_never_a_legacy_run(
    store: EventStore, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With the enabled record and v1 gone, a v2 still proves the package was
    # on: undecidable, never the legacy verifier.
    _seed_, state = await _run_until_the_worker_stops(store, repo, tmp_path, monkeypatch)
    assert state.package is not None
    journal = await _copy_journal(store, lambda _events: [])
    await journal.append(package_frozen_event(boundary_version_id(EXECUTION, 2), state.package))
    boundary = await load_resumed_boundary(journal, EXECUTION)
    assert boundary is not None and boundary.covered is None
    assert boundary.reason == BOUNDARY_RECORD_MISSING
    await journal.close()


# --------------------------------------------------------------------------
# The resumed authority belongs to the run the journal projects: its execution
# id, Seed digest and ordered criterion keys. A call for any other run runs no
# check, records nothing, and leaves every covered criterion undecided.


@pytest.fixture
def check_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Records every bind or run of a check by the resumed authority (they still run)."""
    calls: list[str] = []
    for name in ("assign_tiers", "verify_with_bindings"):
        real = getattr(resume_module, name)

        def spy(*args: Any, _real: Any = real, _name: str = name, **kwargs: Any) -> Any:
            calls.append(_name)
            return _real(*args, **kwargs)

        monkeypatch.setattr(resume_module, name, spy)
    return calls


def _with(seed: Seed, **fields: Any) -> Seed:
    return Seed(
        goal=fields.get("goal", seed.goal),
        acceptance_criteria=fields.get("acceptance_criteria", seed.acceptance_criteria),
        ontology_schema=seed.ontology_schema,
        metadata=seed.metadata,
    )


async def _resumed_records(store: EventStore) -> list[BaseEvent]:
    return [
        event
        for aggregate in (EXECUTION, V1)
        for event in await store.replay(BOUNDARY_AGGREGATE_TYPE, aggregate)
        if event.type == ACCEPTANCE_RESUMED
    ]


def _other_goal(seed: Seed) -> tuple[Seed, str]:
    other = _with(seed, goal="math helpers, faster")
    assert seed_criterion_keys(other) == seed_criterion_keys(seed)
    return other, EXECUTION


def _other_execution(seed: Seed) -> tuple[Seed, str]:
    return seed, "exec_other"


def _reordered_criteria(seed: Seed) -> tuple[Seed, str]:
    first, second, third = seed.acceptance_criteria
    other = _with(seed, acceptance_criteria=(second, first, third))
    assert set(seed_criterion_keys(other)) == set(seed_criterion_keys(seed))
    assert seed_criterion_keys(other) != seed_criterion_keys(seed)
    return other, EXECUTION


@pytest.mark.parametrize(
    ("foreign", "field", "covered"),
    [
        (_other_goal, "seed_digest", (0, 1)),
        (_other_execution, "execution_id", (0, 1)),
        # Not the run's criteria: coverage is unknown, every criterion counts as covered.
        (_reordered_criteria, "seed_digest", (0, 1, 2)),
    ],
    ids=["other_goal_same_keys", "other_execution_id", "same_keys_other_order"],
)
async def test_a_resume_for_another_run_runs_nothing_and_records_nothing(
    store: EventStore,
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    check_calls: list[str],
    foreign: Any,
    field: str,
    covered: tuple[int, ...],
) -> None:
    seed, state = await _run_until_the_worker_stops(store, repo, tmp_path, monkeypatch)
    (repo / "mathutils.py").write_text(CLAMP_FIXED + DOUBLE)  # the package would pass it
    authority = await _resume(store, repo)
    assert authority.boundary.source == "memory"
    other_seed, execution_id = foreign(seed)
    # Criterion 1: only the package gate failed it and every other gate holds;
    # criterion 3 (uncovered): the legacy verifier rejected it.
    restored = _gate_failed(FinalGateSettlement.HOLDS)
    restored = replace(
        restored,
        results=(
            *restored.results[:2],
            replace(restored.results[2], legacy_rejection=LEGACY_TEXT),
        ),
    )
    decided = await authority(seed=other_seed, execution_id=execution_id, parallel_result=restored)
    assert check_calls == []
    assert await _resumed_records(store) == []
    # The run's one decision is not used and its held-out cases are kept.
    assert authority.outcome is None
    assert live_state(EXECUTION) is state
    mismatch = f"run_mismatch:{field}"
    assert [result.outcome for result in decided.results] == [ACExecutionOutcome.FAILED] * 3
    # The package gate failure is not lifted; the covered success is undecided.
    assert decided.results[0].error == restored.results[0].error
    assert mismatch in (decided.results[1].error or "")
    # The uncovered criterion: undecided when the criteria are not the run's,
    # else the legacy verifier's rejection.
    expected = mismatch if 2 in covered else LEGACY_TEXT
    assert expected in (decided.results[2].error or "")
    # The run's own call still decides in full from memory.
    decided = await authority(seed=seed, execution_id=EXECUTION, parallel_result=restored)
    assert check_calls
    assert authority.boundary.source == "memory"
    assert decided.results[0].outcome is ACExecutionOutcome.SUCCEEDED
    assert len(await _resumed_records(store)) == 1


async def test_an_undecidable_resume_for_another_execution_records_nothing(
    store: EventStore,
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_check_runs: list[str],
) -> None:
    seed, state = await _run_until_the_worker_stops(store, repo, tmp_path, monkeypatch)
    journal = await _copy_journal(store, _duplicate(PACKAGE_FROZEN))
    authority = await _resume(journal, repo)
    assert authority.boundary.reason == BOUNDARY_RECORD_MISSING
    decided = await authority(seed=seed, execution_id="exec_other", parallel_result=_restored())
    assert [result.outcome for result in decided.results] == [ACExecutionOutcome.FAILED] * 3
    assert "run_mismatch:execution_id" in (decided.results[0].error or "")
    assert await _resumed_records(journal) == []
    assert authority.outcome is None and no_check_runs == []
    forget_live_state(state)
    await journal.close()


# A run-level resumed record the product could not have written makes recovery
# undecidable: it is built from the record the product wrote, then changed.


async def _product_run_level_record(store: EventStore) -> BaseEvent:
    """The run-level record an undecidable resume writes (on a copy of ``store``)."""
    journal = await _copy_journal(store, _duplicate(PACKAGE_FROZEN))
    authority = await _resume(journal, Path("."))
    await authority(seed=_seed(), execution_id=EXECUTION, parallel_result=_restored())
    (record,) = await _resumed_records(journal)
    assert record.aggregate_id == EXECUTION and record.data["package_id"] is None
    await journal.close()
    return record


def _set(**fields: Any) -> Any:
    return lambda data: data.update(fields)


def _first_criterion(**fields: Any) -> Any:
    return lambda data: data["criteria"][0].update(fields)


RUN_LEVEL_RESUMED = {
    "cites_a_package": _set(package_id="f" * 64),
    "no_reason": _set(reason=None),
    "empty_reason": _set(reason=""),
    "cites_held_out_checks": _set(held_out_checks=["oracle_1"]),
    "claims_a_pass": _first_criterion(package_status="pass"),
    "claims_a_fail": _first_criterion(package_status="fail"),
    "extra_field": _set(verdict="pass"),
}


@pytest.mark.parametrize("change", list(RUN_LEVEL_RESUMED.values()), ids=list(RUN_LEVEL_RESUMED))
async def test_an_impossible_run_level_resumed_record_is_undecidable(
    store: EventStore, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: Any
) -> None:
    _seed_, state = await _run_until_the_worker_stops(store, repo, tmp_path, monkeypatch)
    record = await _product_run_level_record(store)

    def plant(events: list[BaseEvent]) -> list[BaseEvent]:
        data = copy_module.deepcopy(dict(record.data))
        change(data)
        later = max(event.timestamp for event in events) + timedelta(seconds=1)
        return [*events, record.model_copy(update={"data": data, "timestamp": later})]

    journal = await _copy_journal(store, plant)
    boundary = await load_resumed_boundary(journal, EXECUTION)
    assert boundary is not None
    assert boundary.reason == BOUNDARY_RECORD_MISSING
    assert boundary.live is None and boundary.source == "journal"
    forget_live_state(state)
    await journal.close()


async def test_a_run_level_resumed_record_before_the_enabled_record_is_undecidable(
    store: EventStore, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_, state = await _run_until_the_worker_stops(store, repo, tmp_path, monkeypatch)
    record = await _product_run_level_record(store)

    def plant(events: list[BaseEvent]) -> list[BaseEvent]:
        earlier = min(event.timestamp for event in events) - timedelta(seconds=1)
        return [record.model_copy(update={"timestamp": earlier}), *events]

    journal = await _copy_journal(store, plant)
    boundary = await load_resumed_boundary(journal, EXECUTION)
    assert boundary is not None and boundary.reason == BOUNDARY_RECORD_MISSING
    assert boundary.source == "journal"
    forget_live_state(state)
    await journal.close()


# Held-out cases that reached a target during an interrupted terminal
# verification never decide again in this process (independent review H1).


def _leaky(leak: Path) -> str:
    """``clamp`` that records every input; it stalls on any input but the stated one."""
    return (
        "import json, time\n"
        "def clamp(value, low, high):\n"
        f"    with open({str(leak)!r}, 'a') as f:\n"
        "        f.write(json.dumps([value, low, high]) + '\\n')\n"
        "    if (value, low, high) != (15, 0, 10):\n"
        "        time.sleep(10)\n"
        "    return max(low, min(high, value))\n"
    )


async def _cancel_once_a_held_out_input_arrives(call: Any, leak: Path) -> None:
    task = asyncio.create_task(call())
    for _ in range(400):
        await asyncio.sleep(0.05)
        if leak.exists() and "[99, 1, 7]" in leak.read_text():
            break
    # The held-out input (99, 1, 7) reached the candidate: the terminal verification runs.
    assert "[99, 1, 7]" in leak.read_text()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_after_an_interrupted_live_verification_a_resume_runs_no_held_out_case(
    store: EventStore, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed, state = await _run_until_the_worker_stops(store, repo, tmp_path, monkeypatch)
    leak = tmp_path / "leak.jsonl"
    (repo / "mathutils.py").write_text(_leaky(leak) + DOUBLE)
    live = CheckPackageAuthority(
        state,
        CheckPackageSettings(True, max_construction_attempts=1),
        event_store=store,
        candidate_checkout=repo,
    )
    await _cancel_once_a_held_out_input_arrives(
        lambda: live(seed=seed, execution_id=EXECUTION, parallel_result=_restored()), leak
    )
    seen = leak.read_text()
    authority = await _resume(store, repo)
    assert authority.boundary.source == "journal"
    assert authority.boundary.reason == HELD_OUT_UNAVAILABLE
    decided = await authority(seed=seed, execution_id=EXECUTION, parallel_result=_restored())
    assert leak.read_text() == seen
    verdicts = authority.outcome.verdict.verdicts
    keys = seed_criterion_keys(seed)
    assert [verdicts[key].reason for key in keys[:2]] == [HELD_OUT_UNAVAILABLE] * 2
    assert [result.outcome for result in decided.results[:2]] == [ACExecutionOutcome.FAILED] * 2


async def test_an_interrupted_resumed_verification_never_reuses_its_held_out_cases(
    store: EventStore, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed, _state = await _run_until_the_worker_stops(store, repo, tmp_path, monkeypatch)
    leak = tmp_path / "leak.jsonl"
    (repo / "mathutils.py").write_text(_leaky(leak) + DOUBLE)
    authority = await _resume(store, repo)
    assert authority.boundary.source == "memory"
    await _cancel_once_a_held_out_input_arrives(
        lambda: authority(seed=seed, execution_id=EXECUTION, parallel_result=_restored()), leak
    )
    assert authority.outcome is None
    seen = leak.read_text()
    # Another resume in this process finds no held-out cases in memory ...
    assert live_state(EXECUTION) is None
    again = await _resume(store, repo)
    assert again.boundary.source == "journal"
    # ... and a second call of the interrupted authority runs no check.
    decided = await authority(seed=seed, execution_id=EXECUTION, parallel_result=_restored())
    assert leak.read_text() == seen
    assert authority.outcome is not None
    assert authority.outcome.error == "terminal_interrupted"
    decisions = authority.outcome.reconciliation.decisions
    assert [(d.package_status, d.accepted) for d in decisions[:2]] == [
        (PackageCriterionStatus.INDETERMINATE, False)
    ] * 2
    assert not decided.all_succeeded


# Appending (never deleting) a failed construction to a later version never
# turns a run bound to an admitted package into a legacy resume (independent
# review M3).

V2 = boundary_version_id(EXECUTION, 2)


def _bare(_state: Any) -> list[BaseEvent]:
    return [
        BaseEvent(type=kind, aggregate_type=BOUNDARY_AGGREGATE_TYPE, aggregate_id=V2, data={})
        for kind in (CONSTRUCTION_FAILED, ACTOR_STARTED)
    ]


def _product_built(state: Any) -> list[BaseEvent]:
    return [
        construction_failed_event(
            V2, seed_digest=state.seed_digest, input_digest="1" * 64, reason="constructor_timeout"
        ),
        actor_started_event(V2, actor_id="worker", package_id=None, runtime="codex"),
    ]


def _v1_superseded(state: Any) -> list[BaseEvent]:
    assert state.package is not None
    return [
        superseded_event(
            V1,
            superseded_by=V2,
            package_id=state.package.package_id,
            successor_package_id=None,
            reason="replaced",
        ),
        *_product_built(state),
    ]


@pytest.mark.parametrize(
    "appended",
    [_bare, _product_built, _v1_superseded],
    ids=["bare", "product_built", "v1_superseded"],
)
async def test_appended_failed_construction_never_resumes_as_legacy(
    store: EventStore, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, appended: Any
) -> None:
    _seed, state = await _run_until_the_worker_stops(store, repo, tmp_path, monkeypatch)
    forget_live_state(state)  # another process
    later = max(event.timestamp for event in await store.replay(BOUNDARY_AGGREGATE_TYPE, V1))
    for offset, event in enumerate(appended(state), start=1):
        await store.append(
            event.model_copy(update={"timestamp": later + timedelta(seconds=offset)})
        )
    boundary = await load_resumed_boundary(store, EXECUTION)
    assert boundary is not None, "appended rows turned a package-on run into a legacy resume"
    assert boundary.reason == BOUNDARY_RECORD_MISSING


class _UnreadableSeed:
    """A Seed whose criteria cannot be read (for example a corrupted resume payload)."""

    @property
    def acceptance_criteria(self) -> Any:
        raise ValueError("unreadable criteria")

    def to_dict(self) -> Any:
        raise ValueError("unreadable seed")


@pytest.mark.parametrize(
    "journal_edit", [None, _duplicate(PACKAGE_FROZEN)], ids=["bound", "undecidable"]
)
async def test_a_seed_that_cannot_be_read_fails_every_root_closed(
    store: EventStore,
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_check_runs: list[str],
    journal_edit: Any,
) -> None:
    _seed_, state = await _run_until_the_worker_stops(store, repo, tmp_path, monkeypatch)
    journal = await _copy_journal(store, journal_edit)
    authority = await _resume(journal, repo)
    decided = await authority(
        seed=_UnreadableSeed(), execution_id=EXECUTION, parallel_result=_restored()
    )
    assert [result.outcome for result in decided.results] == [ACExecutionOutcome.FAILED] * 3
    assert no_check_runs == [] and await _resumed_records(journal) == []
    forget_live_state(state)
    await journal.close()
