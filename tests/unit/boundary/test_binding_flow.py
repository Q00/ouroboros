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
    assert "mix(start=0, end=10, weight=0.5): expected 5, observed 5.0" not in message  # passes
    assert "held-out case(s) also failed" in message  # held-out inputs withheld
    assert "2.5" not in message


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
