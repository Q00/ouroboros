"""Oracle hash before dispatch, binding after the worker stops, tiers, and verdicts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ouroboros.boundary.acceptance import (
    ArtifactVerdict,
    ExistingOutcome,
    PackageCriterionStatus,
    reconcile_acceptance,
)
from ouroboros.boundary.admission import CandidateVerdict, CheckStatus
from ouroboros.boundary.binding import CheckTier
from ouroboros.boundary.constructor import ConstructionOutcome, package_from_reply
from ouroboros.boundary.events import (
    ACTOR_STARTED,
    ADMISSION_COMPLETED,
    BINDING_RECORDED,
    BOUNDARY_AGGREGATE_TYPE,
    CANDIDATE_VERIFIED,
    PACKAGE_FROZEN,
    SELECTION_DECIDED,
)
from ouroboros.boundary.ledger import BoundaryLedger, BoundaryOrderError, verify_boundary_order
from ouroboros.boundary.package import seed_criterion_keys
from ouroboros.boundary.run_wiring import (
    CheckPackageSettings,
    RegenerationPolicy,
    prepare_check_package,
    repair_message,
    verify_check_package,
)
from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata
from ouroboros.persistence.event_store import EventStore

BUGGY = "def clamp(value, low, high):\n    if value > high:\n        return value\n    return max(low, value)\n"
FIXED = "def clamp(value, low, high):\n    return max(low, min(high, value))\n"
LERP_CASES = [
    {
        "case_id": "stated",
        "args": {"a": 0, "b": 10, "t": 0.5},
        "expect": {"kind": "returns", "value": 5, "approx": 1e-9},
    },
    {
        "case_id": "held",
        "args": {"a": 2, "b": 4, "t": 0.25},
        "expect": {"kind": "returns", "value": 2.5, "approx": 1e-9},
    },
]


def _seed() -> Seed:
    return Seed(
        goal="math helpers",
        acceptance_criteria=(
            "clamp(15, 0, 10) returns 10",
            "linear interpolation between a and b by t: interpolating 0 and 10 at 0.5 gives 5",
            "the helpers are documented in the README",
        ),
        ontology_schema=OntologySchema(name="mathutils", description="math helpers"),
        metadata=SeedMetadata(seed_id="seed_flow", ambiguity_score=0.1),
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
                "cases": [
                    {
                        "case_id": "stated",
                        "args": {"value": 15, "low": 0, "high": 10},
                        "expect": {"kind": "returns", "value": 10},
                    },
                    {
                        "case_id": "held",
                        "args": {"value": -3, "low": -2, "high": 4},
                        "expect": {"kind": "returns", "value": -2},
                    },
                ],
            },
            {
                "criterion": 2,
                "check_id": "oracle_2",
                "role": "reproduction",
                "call_kind": "function",
                "params": ["a", "b", "t"],
                # The criterion leaves the name open: the default cannot resolve.
                "default_binding": {"symbol": "mathutils.interpolate"},
                "cases": LERP_CASES,
            },
        ],
        "uncovered": [{"criterion": 3, "reason": "not executable"}],
    }


class _Constructor:
    def __init__(self, seed: Seed, base: Path) -> None:
        self.outcome = ConstructionOutcome(
            package_from_reply(
                _reply(), seed, input_digest="1" * 64, generator="fake", base_checkout=base
            ),
            None,
            "1" * 64,
            "fake",
        )

    async def construct(self, seed: Seed, base: Path, *, feedback=()) -> ConstructionOutcome:
        return self.outcome


@pytest.fixture
async def store():
    event_store = EventStore("sqlite+aiosqlite:///:memory:")
    await event_store.initialize()
    yield event_store
    await event_store.close()


def _write(root: Path, files: dict[str, str]) -> None:
    for path, text in files.items():
        (root / path).write_text(text)


async def _prepare(store: EventStore, repo: Path, tmp_path: Path):
    seed = _seed()
    state = await prepare_check_package(
        seed,
        event_store=store,
        constructor=_Constructor(seed, repo),
        execution_id="exec_flow",
        base_checkout=repo,
        worker_workspace=repo,
        runtime_label="test",
        settings=CheckPackageSettings(True, policy=RegenerationPolicy.STUDY),
        store_dir=tmp_path / "store",
    )
    return seed, state


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "mathutils.py").write_text(BUGGY)
    return root


async def _types(store: EventStore, boundary_id: str) -> list[str]:
    return [event.type for event in await store.replay(BOUNDARY_AGGREGATE_TYPE, boundary_id)]


async def test_oracle_hash_before_dispatch_and_binding_after_stop(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    seed, state = await _prepare(store, repo, tmp_path)
    assert state.admitted and state.admission.check_tiers == {"oracle_1": "A", "oracle_2": "U"}
    assert await _types(store, state.boundary_id) == [
        PACKAGE_FROZEN,
        ADMISSION_COMPLETED,
        ACTOR_STARTED,
    ]
    # The base is kept outside the checkout for the late binding (oracle_2).
    assert state.base_snapshot is not None and state.base_snapshot.is_dir()

    # Worker: fixes clamp, adds interpolation under its own name, declares it.
    _write(repo, {"mathutils.py": FIXED + "\ndef lerp(a, b, t):\n    return a + (b - a) * t\n"})
    keys = seed_criterion_keys(seed)
    declared = {keys[1]: [{"symbol": "mathutils.lerp", "call_kind": "function"}]}
    verdict = await verify_check_package(
        state,
        event_store=store,
        candidate_checkout=repo,
        settings=CheckPackageSettings(True),
        declared_entry_points=declared,
    )
    assert await _types(store, state.boundary_id) == [
        PACKAGE_FROZEN,
        ADMISSION_COMPLETED,
        ACTOR_STARTED,
        BINDING_RECORDED,
        CANDIDATE_VERIFIED,
        SELECTION_DECIDED,
    ]
    events = await store.replay(BOUNDARY_AGGREGATE_TYPE, state.boundary_id)
    assert verify_boundary_order(events) == ()
    recorded = next(e for e in events if e.type == BINDING_RECORDED).data
    tiers = {item["check_id"]: item["tier"] for item in recorded["checks"]}
    assert tiers == {"oracle_1": "A", "oracle_2": "A_prime"}
    # Bindings are data: no cases, inputs, or expected values in the journal.
    assert "'args'" not in str(recorded) and "'value': 10" not in str(recorded)

    statuses = {key: (item.status, item.tier) for key, item in verdict.verdicts.items()}
    assert statuses == {
        keys[0]: (PackageCriterionStatus.PASS, CheckTier.A),
        keys[1]: (PackageCriterionStatus.PASS, CheckTier.A_PRIME),
        keys[2]: (PackageCriterionStatus.UNCOVERED, CheckTier.U),
    }
    assert verdict.artifact_verdict is ArtifactVerdict.PASS
    decision = reconcile_acceptance(keys, verdict.verdicts, {}, existing_run_accepted=True)
    # 2 of 3 verified, 0 failed, 1 unverified: accepted (exit 0), listed.
    assert decision.run_accepted and decision.verified_pass_count == 2
    assert [d.criterion_key for d in decision.unverified] == [keys[2]]
    assert decision.to_dict()["tier_summary"] == {"A": 1, "A_prime": 1, "U": 1, "C": 0}


async def test_bindings_cannot_be_recorded_before_the_worker_starts(store: EventStore) -> None:
    ledger = BoundaryLedger(store)
    with pytest.raises(BoundaryOrderError):
        await ledger.record_bindings("b1", package_sha256="0" * 64, payload={"phase": "final"})


async def test_wrong_declared_implementation_fails_and_the_repair_names_the_binding(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    seed, state = await _prepare(store, repo, tmp_path)
    _write(
        repo,
        {
            "mathutils.py": FIXED
            + "\ndef mix(start, end, weight):\n    return start + end * weight\n"
        },
    )
    keys = seed_criterion_keys(seed)
    declared = {
        keys[1]: [{"symbol": "mathutils.mix", "arg_map": {"a": "start", "b": "end", "t": "weight"}}]
    }
    verdict = await verify_check_package(
        state,
        event_store=store,
        candidate_checkout=repo,
        settings=CheckPackageSettings(True),
        declared_entry_points=declared,
    )
    item = verdict.verdicts[keys[1]]
    assert (item.status, item.tier) == (PackageCriterionStatus.FAIL, CheckTier.A_PRIME)
    assert verdict.artifact_verdict is ArtifactVerdict.FAIL
    message = repair_message(verdict, keys[1])
    assert message is not None
    assert "declared entry point: function mathutils.mix" in message
    assert '"a": "start"' in message
    assert "mix(start=0, end=10, weight=0.5)" not in message  # the stated case passes
    # Only held-out cases failed: exactly one is revealed, with input,
    # expected and observed output.
    assert (
        "- revealed held-out case: mix(start=2, end=4, weight=0.25): expected 2.5, observed 3.0"
        in message
    )
    assert "held-out case(s) also failed" not in message


async def test_no_declared_binding_is_unverified_and_an_invalid_one_indeterminate(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    seed, state = await _prepare(store, repo, tmp_path)
    _write(repo, {"mathutils.py": FIXED + "\ndef lerp(a, b, t):\n    return a + (b - a) * t\n"})
    keys = seed_criterion_keys(seed)
    verdict = await verify_check_package(
        state, event_store=store, candidate_checkout=repo, settings=CheckPackageSettings(True)
    )
    item = verdict.verdicts[keys[1]]
    assert (item.status, item.reason) == (PackageCriterionStatus.UNVERIFIED, "no_binding")
    # A verified pass plus unverified criteria: exit 0 with the list.
    assert verdict.artifact_verdict is ArtifactVerdict.PASS
    decision = reconcile_acceptance(
        keys,
        verdict.verdicts,
        {0: ExistingOutcome(0, "failed", "failed", "failed")},
        existing_run_accepted=True,
    )
    assert decision.run_accepted and len(decision.unverified) == 2
    assert decision.decisions[0].existing_outcome == "failed"  # advisory only


async def test_declared_binding_to_a_test_helper_under_the_check_dir_is_invalid(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    seed, state = await _prepare(store, repo, tmp_path)
    _write(repo, {"mathutils.py": FIXED})
    keys = seed_criterion_keys(seed)
    declared = {keys[1]: [{"symbol": "mathutils.lerp", "arg_map": {"a": "1 + 1", "b": 1, "t": 2}}]}
    verdict = await verify_check_package(
        state,
        event_store=store,
        candidate_checkout=repo,
        settings=CheckPackageSettings(True),
        declared_entry_points=declared,
    )
    item = verdict.verdicts[keys[1]]
    assert item.status is PackageCriterionStatus.INDETERMINATE
    assert item.reason.startswith("binding_invalid:")
    assert verdict.artifact_verdict is ArtifactVerdict.INDETERMINATE
    decision = reconcile_acceptance(keys, verdict.verdicts, {}, existing_run_accepted=True)
    assert not decision.run_accepted  # indeterminate left: non-zero exit


async def test_transient_indeterminate_checks_are_rerun_once(
    store: EventStore, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ouroboros.boundary import binding_flow

    seed, state = await _prepare(store, repo, tmp_path)
    _write(repo, {"mathutils.py": FIXED})
    calls: list[Any] = []
    real = binding_flow.verify_candidate

    async def flaky(*args: Any, **kwargs: Any):
        result = await real(*args, **kwargs)
        calls.append(kwargs.get("only_checks"))
        if len(calls) == 1:
            checks = tuple(
                check.model_copy(update={"status": CheckStatus.INDETERMINATE, "reason": "timeout"})
                for check in result.checks
            )
            return result.model_copy(
                update={"checks": checks, "verdict": CandidateVerdict.INDETERMINATE}
            )
        return result

    monkeypatch.setattr(binding_flow, "verify_candidate", flaky)
    verdict = await verify_check_package(
        state, event_store=store, candidate_checkout=repo, settings=CheckPackageSettings(True)
    )
    assert calls == [["oracle_1"], ["oracle_1"]]
    keys = seed_criterion_keys(seed)
    assert verdict.verdicts[keys[0]].status is PackageCriterionStatus.PASS
    types = await _types(store, state.boundary_id)
    assert types.count(CANDIDATE_VERIFIED) == 2  # both receipts kept


async def test_each_late_binding_has_exactly_one_base_run(
    store: EventStore, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ouroboros.boundary import binding_flow

    seed, state = await _prepare(store, repo, tmp_path)
    _write(repo, {"mathutils.py": FIXED + "\ndef lerp(a, b, t):\n    return a + (b - a) * t\n"})
    runs: list[str] = []
    real = binding_flow.admit_binding

    async def counted(*args: Any, **kwargs: Any):
        runs.append(args[1])
        return await real(*args, **kwargs)

    monkeypatch.setattr(binding_flow, "admit_binding", counted)
    keys = seed_criterion_keys(seed)
    cache: dict[str, Any] = {}
    for _attempt in range(3):  # for example two repair attempts, then the final verification
        await binding_flow.assign_tiers(
            state.package,
            artifact=repo,
            base=state.base_snapshot,
            declared={keys[1]: [{"symbol": "mathutils.lerp"}]},
            expected_base_digest=state.admission.base_tree_digest,
            base_run_cache=cache,
        )
    assert runs == ["oracle_2"]
    # A different declaration is a different late binding: its own single run.
    await binding_flow.assign_tiers(
        state.package,
        artifact=repo,
        base=state.base_snapshot,
        declared={keys[1]: [{"symbol": "mathutils.lerp", "arg_map": {"a": 0, "b": 1, "t": 2}}]},
        base_run_cache=cache,
    )
    assert runs == ["oracle_2", "oracle_2"]


async def test_reveal_one_held_out_case_and_retire_it(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    from ouroboros.boundary.acceptance import criterion_verdicts
    from ouroboros.boundary.binding_flow import retire_revealed
    from ouroboros.boundary.events import CASE_REVEALED
    from ouroboros.boundary.ledger import BoundaryLedger, BoundaryOrderError
    from ouroboros.boundary.run_wiring import plan_repair

    seed, state = await _prepare(store, repo, tmp_path)
    # clamp is fixed for the stated case only: both held-out cases fail?
    # The stated case (15, 0, 10) passes; the held-out (-3, -2, 4) fails.
    _write(repo, {"mathutils.py": "def clamp(value, low, high):\n    return min(high, value)\n"})
    keys = seed_criterion_keys(seed)
    # A repair-time verification (the gate's path): no journal writes.
    from ouroboros.boundary.binding_flow import assign_tiers, verify_with_bindings
    from ouroboros.boundary.run_wiring import BoundaryVerdict

    assignments, _results = await assign_tiers(
        state.package, artifact=repo, base=state.base_snapshot
    )
    subset = {"oracle_1": assignments["oracle_1"]}
    bound = await verify_with_bindings(state.package, repo, subset)
    verdicts = criterion_verdicts(state.package, bound.effective, assignments=subset)
    verdict = BoundaryVerdict(
        verdict="fail",
        reasons=(),
        boundary_id=state.boundary_id,
        package_sha256=state.package.sha256,
        verdicts=verdicts,
        oracle_results={
            c.check_id: c.oracle_result for c in bound.effective.checks if c.oracle_result
        },
    )
    item = verdict.verdicts[keys[0]]
    assert item.status is PackageCriterionStatus.FAIL and item.failed_heldout_only
    plan = plan_repair(verdict, keys[0])
    assert plan is not None and (plan.revealed_check_id, plan.revealed_case_id) == (
        "oracle_1",
        "held",
    )
    assert (
        "revealed held-out case: clamp(value=-3, low=-2, high=4): expected -2, observed -3"
        in plan.message
    )

    ledger = BoundaryLedger(store)
    await ledger.record_case_revealed(
        state.boundary_id,
        package_sha256=state.package.sha256,
        check_id="oracle_1",
        criterion_key=keys[0],
        case_id="held",
        root_ac_index=0,
        retry_attempt=0,
    )
    with pytest.raises(BoundaryOrderError):  # once per case
        await ledger.record_case_revealed(
            state.boundary_id,
            package_sha256=state.package.sha256,
            check_id="oracle_1",
            criterion_key=keys[0],
            case_id="held",
        )
    events = await store.replay(BOUNDARY_AGGREGATE_TYPE, state.boundary_id)
    revealed_event = next(e for e in events if e.type == CASE_REVEALED)
    assert revealed_event.data["case_id"] == "held" and "args" not in revealed_event.data

    # Retired: the case no longer counts as held out in later verdicts and events.
    again = await verify_check_package(
        state,
        event_store=store,
        candidate_checkout=repo,
        settings=CheckPackageSettings(True),
        revealed={"oracle_1": {"held"}},
    )
    retired = again.verdicts[keys[0]]
    assert retired.status is PackageCriterionStatus.FAIL and not retired.failed_heldout_only
    case = next(c for c in again.oracle_results["oracle_1"]["cases"] if c["case_id"] == "held")
    assert case["held_out"] is False and case["revealed"] is True
    latest = [
        e
        for e in await store.replay(BOUNDARY_AGGREGATE_TYPE, state.boundary_id)
        if e.type == CANDIDATE_VERIFIED
    ][-1]
    journal_case = next(
        c for c in latest.data["checks"][0]["oracle_result"]["cases"] if c["case_id"] == "held"
    )
    assert journal_case == {"case_id": "held", "held_out": False, "passed": False, "revealed": True}
    # A second repair shows the retired case as visible and reveals nothing new.
    second = plan_repair(again, keys[0])
    assert second is not None and second.revealed_case_id is None
    assert retire_revealed(None, {"oracle_1": {"held"}}) is None
    assert criterion_verdicts  # imported API stays available


def test_only_one_of_several_failing_held_out_cases_is_revealed() -> None:
    from ouroboros.boundary.acceptance import CriterionVerdict
    from ouroboros.boundary.oracle import apply_reveals, failed_heldout_only
    from ouroboros.boundary.run_wiring import BoundaryVerdict, plan_repair

    result = {
        "cases": [
            {"case_id": "stated", "held_out": False, "passed": True, "detail": ""},
            {
                "case_id": "h1",
                "held_out": True,
                "passed": False,
                "detail": "f(1): expected 2, observed 9",
            },
            {
                "case_id": "h2",
                "held_out": True,
                "passed": False,
                "detail": "f(3): expected 4, observed 9",
            },
            {
                "case_id": "h3",
                "held_out": True,
                "passed": False,
                "detail": "f(5): expected 6, observed 9",
            },
        ]
    }
    verdict = BoundaryVerdict(
        verdict="fail",
        reasons=(),
        boundary_id="b",
        package_sha256="0" * 64,
        verdicts={
            "k": CriterionVerdict(
                "k",
                PackageCriterionStatus.FAIL,
                CheckTier.A_PRIME,
                "reproduction_still_failing",
                ("o1",),
                True,
                {"symbol": "m.f", "call_kind": "function", "arg_map": {}},
                "declared",
            )
        },
        oracle_results={"o1": result},
    )
    plan = plan_repair(verdict, "k")
    assert plan is not None and plan.revealed_case_id == "h1"
    assert "It called your declared entry point: function m.f." in plan.message
    assert "- revealed held-out case: f(1): expected 2, observed 9" in plan.message
    assert "f(3)" not in plan.message and "f(5)" not in plan.message
    assert "- 2 held-out case(s) also failed" in plan.message
    # Retiring h1 leaves two failing held-out cases and one failing visible case.
    retired = apply_reveals(result, ["h1"])
    assert not failed_heldout_only(retired)
    assert sum(1 for c in retired["cases"] if c["held_out"]) == 2
