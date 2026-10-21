"""Oracle hash before dispatch, binding after the worker stops, tiers, and verdicts."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
from typing import Any

import pytest

from ouroboros.boundary.acceptance import (
    ArtifactVerdict,
    ExistingOutcome,
    PackageCriterionStatus,
    reconcile_acceptance,
)
from ouroboros.boundary.admission import verify_candidate
from ouroboros.boundary.binding import CheckTier
from ouroboros.boundary.constructor import ConstructionOutcome
from ouroboros.boundary.events import (
    ACTOR_STARTED,
    ADMISSION_COMPLETED,
    BINDING_RECORDED,
    BOUNDARY_AGGREGATE_TYPE,
    CANDIDATE_VERIFIED,
    PACKAGE_FROZEN,
    BindingsPayload,
    RunContract,
)
from ouroboros.boundary.ledger import BoundaryLedger, BoundaryOrderError, verify_boundary_order
from ouroboros.boundary.oracle_build import assemble_package, build_oracle_spec, package_from_reply
from ouroboros.boundary.package import (
    CheckRole,
    PackageFile,
    seed_criterion_keys,
)
from ouroboros.boundary.receipts import CandidateVerdict, CheckStatus
from ouroboros.boundary.run_wiring import (
    CheckPackageSettings,
    prepare_check_package,
    render_verdict,
    repair_text,
    verify_check_package,
)
from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata
from ouroboros.persistence.event_store import EventStore

from .journal_fixtures import settled_verification
from .test_acceptance import admission_on_base
from .test_oracle import CASES, _package, _repo
from .test_oracle import _seed as _oracle_seed

BUGGY = "def clamp(value, low, high):\n    if value > high:\n        return value\n    return max(low, value)\n"
FIXED = "def clamp(value, low, high):\n    return max(low, min(high, value))\n"
CONTRACT = RunContract(check_timeout_seconds=120)
LERP_CASES = [
    {
        "case_id": "stated",
        "held_out": False,
        "args": {"a": 0, "b": 10, "t": 0.5},
        "expect": {"kind": "returns", "value": 5, "approx": 1e-9},
    },
    {
        "case_id": "held",
        "held_out": True,
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
                "target_named_in_criterion": False,
                "cases": [
                    {
                        "case_id": "stated",
                        "held_out": False,
                        "args": {"value": 15, "low": 0, "high": 10},
                        "expect": {"kind": "returns", "value": 10},
                    },
                    {
                        "case_id": "held",
                        "held_out": True,
                        # The base fails it (``value > high``): a held-out case
                        # the base passes shows no fix and verifies nothing.
                        "args": {"value": 7, "low": -2, "high": 4},
                        "expect": {"kind": "returns", "value": 4},
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
                "target_named_in_criterion": False,
                "cases": LERP_CASES,
            },
        ],
        "uncovered": [{"criterion": 3, "reason": "not executable"}],
    }


class _Constructor:
    def __init__(self, seed: Seed, base: Path) -> None:
        self.outcome = ConstructionOutcome(
            package_from_reply(_reply(), seed, input_digest="1" * 64, generator="fake"),
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
        settings=CheckPackageSettings(True, max_construction_attempts=1),
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
        declared_entry_points=declared,
    )
    assert await _types(store, state.boundary_id) == [
        PACKAGE_FROZEN,
        ADMISSION_COMPLETED,
        ACTOR_STARTED,
        BINDING_RECORDED,
        CANDIDATE_VERIFIED,
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
    assert decision.to_dict()["tier_summary"] == {"A": 1, "A_prime": 1, "U": 1, "S": 0, "C": 0}


async def test_bindings_cannot_be_recorded_before_the_worker_starts(store: EventStore) -> None:
    ledger = BoundaryLedger(store)
    with pytest.raises(BoundaryOrderError):
        await ledger.record_bindings(
            "b1", package_id="0" * 64, payload=BindingsPayload(phase="final", checks=())
        )


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
        declared_entry_points=declared,
    )
    item = verdict.verdicts[keys[1]]
    assert (item.status, item.tier) == (PackageCriterionStatus.FAIL, CheckTier.A_PRIME)
    assert verdict.artifact_verdict is ArtifactVerdict.FAIL
    message = repair_text(verdict, keys[1])
    assert message is not None
    assert "declared entry point: function mathutils.mix" in message
    assert '"a": "start"' in message
    assert "mix(start=0, end=10, weight=0.5)" not in message  # the stated case passes
    # Only held-out cases failed: none of them is shown.
    assert "start=2" not in message and "2.5" not in message


async def test_no_declared_binding_is_unverified_and_an_invalid_one_indeterminate(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    seed, state = await _prepare(store, repo, tmp_path)
    _write(repo, {"mathutils.py": FIXED + "\ndef lerp(a, b, t):\n    return a + (b - a) * t\n"})
    keys = seed_criterion_keys(seed)
    verdict = await verify_check_package(state, event_store=store, candidate_checkout=repo)
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
            # What the product records for an oracle run that timed out
            # before the target answered (``oracle_run``): no exit code, no
            # signature, an undecided result in which no case passed.
            checks = tuple(
                check.model_copy(
                    update={
                        "status": CheckStatus.INDETERMINATE,
                        "reason": "timeout",
                        "timed_out": True,
                        "return_code": None,
                        "signature_seen": False,
                        "oracle_result": check.oracle_result.model_copy(
                            update={
                                "resolve": "setup_timeout",
                                "cases": tuple(
                                    case.model_copy(update={"passed": False, "detail": ""})
                                    for case in check.oracle_result.cases
                                ),
                            }
                        ),
                    }
                )
                for check in result.checks
            )
            # The receipt-level fields (verdict, reasons, mutation flag) as
            # ``verify_candidate`` derives them from those checks.
            settled = settled_verification(result.model_copy(update={"checks": checks}))
            assert settled.verdict is CandidateVerdict.INDETERMINATE
            return settled
        return result

    monkeypatch.setattr(binding_flow, "verify_candidate", flaky)
    verdict = await verify_check_package(state, event_store=store, candidate_checkout=repo)
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
            base=state.base_snapshot,
            contract=state.contract,
            declared={keys[1]: [{"symbol": "mathutils.lerp"}]},
            expected_base_digest=state.admission.base_tree_digest,
            base_run_cache=cache,
        )
    assert runs == ["oracle_2"]
    # A different declaration is a different late binding: its own single run.
    await binding_flow.assign_tiers(
        state.package,
        base=state.base_snapshot,
        contract=state.contract,
        declared={keys[1]: [{"symbol": "mathutils.lerp", "arg_map": {"a": 0, "b": 1, "t": 2}}]},
        base_run_cache=cache,
    )
    assert runs == ["oracle_2", "oracle_2"]


def test_a_repair_shows_visible_cases_and_never_a_held_out_one() -> None:
    from ouroboros.boundary.acceptance import CriterionVerdict
    from ouroboros.boundary.oracle import OracleResult
    from ouroboros.boundary.run_wiring import BoundaryVerdict

    result = OracleResult.model_validate(
        {
            "check_id": "oracle_1",
            "criterion_key": "k",
            "binding_source": "declared",
            "symbol": "m.f",
            "call_kind": "function",
            "resolve": "ok",
            "cases": [
                {"case_id": "c1", "held_out": False, "passed": False, "detail": "f(0): expected 1"},
                {"case_id": "c2", "held_out": True, "passed": False, "detail": "f(1): expected 2"},
                {"case_id": "c3", "held_out": True, "passed": False, "detail": "f(3): expected 4"},
            ],
        }
    )
    verdict = BoundaryVerdict(
        verdict="fail",
        reasons=(),
        boundary_id="b",
        package_id="0" * 64,
        verdicts={
            "k": CriterionVerdict(
                "k",
                PackageCriterionStatus.FAIL,
                CheckTier.A_PRIME,
                "reproduction_still_failing",
                ("oracle_1",),
                False,
                {"symbol": "m.f", "call_kind": "function", "arg_map": {}},
                "declared",
                declared_binding_pass=False,
            )
        },
        oracle_results={"oracle_1": result},
    )
    message = repair_text(verdict, "k")
    assert message is not None
    assert "It called your declared entry point: function m.f." in message
    assert "- f(0): expected 1" in message
    assert "f(1)" not in message and "f(3)" not in message


async def test_store_is_owner_only_and_receipts_hide_held_out_values(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    # the store holding the package is 0700; a stored receipt keeps a
    # held-out case's id and pass/fail only.
    import stat

    seed, state = await _prepare(store, repo, tmp_path)
    assert stat.S_IMODE((tmp_path / "store").stat().st_mode) == 0o700
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
        declared_entry_points=declared,
    )
    assert verdict.verdicts[keys[1]].failed_heldout_only
    receipts = [path.read_text() for path in (tmp_path / "store" / "receipts").iterdir()]
    assert receipts
    for text in receipts:
        assert "weight=0.25" not in text and "expected 2.5" not in text
    assert all("weight=0.25" not in line for line in render_verdict(verdict))


async def test_all_unverified_criteria_give_the_all_unverified_reason(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    # every criterion unverified is the artifact verdict "unverified",
    # reason "all_unverified".
    seed = _seed()
    reply = _reply()
    reply["oracles"] = reply["oracles"][1:]
    reply["uncovered"].append({"criterion": 1, "reason": "not executable"})
    constructor = _Constructor(seed, repo)
    constructor.outcome = ConstructionOutcome(
        package_from_reply(reply, seed, input_digest="1" * 64, generator="fake"),
        None,
        "1" * 64,
        "fake",
    )
    state = await prepare_check_package(
        seed,
        event_store=store,
        constructor=constructor,
        execution_id="exec_all_u",
        base_checkout=repo,
        worker_workspace=repo,
        runtime_label="test",
        settings=CheckPackageSettings(True, max_construction_attempts=1),
        store_dir=tmp_path / "store",
    )
    assert state.admitted
    verdict = await verify_check_package(state, event_store=store, candidate_checkout=repo)
    assert verdict.artifact_verdict is ArtifactVerdict.UNVERIFIED
    assert verdict.reasons == ("all_unverified",)


async def test_no_held_out_value_reaches_the_store(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    # after preparation and a failing verification, no file in the store
    # (package record, receipts, base snapshot) holds a held-out input or
    # expected value; the record keeps the held-out case as an id.

    reply = _reply()
    reply["oracles"][0]["cases"][1] = {
        "case_id": "held",
        "held_out": True,
        "args": {"value": 6173, "low": 1, "high": 4409},
        "expect": {"kind": "returns", "value": 4409},
    }
    seed = _seed()
    constructor = _Constructor(seed, repo)
    constructor.outcome = ConstructionOutcome(
        package_from_reply(reply, seed, input_digest="1" * 64, generator="fake"),
        None,
        "1" * 64,
        "fake",
    )
    state = await prepare_check_package(
        seed,
        event_store=store,
        constructor=constructor,
        execution_id="exec_flow",
        base_checkout=repo,
        worker_workspace=repo,
        runtime_label="test",
        settings=CheckPackageSettings(True, max_construction_attempts=1),
        store_dir=tmp_path / "store",
    )
    assert state.package is not None and state.package.oracles[0].cases[1].held_out
    verdict = await verify_check_package(state, event_store=store, candidate_checkout=repo)
    assert verdict.verdict == "fail"
    stored = [path for path in (tmp_path / "store").rglob("*") if path.is_file()]
    assert {path.parent.name for path in stored} >= {"packages", "receipts"}
    # A standalone number only: not part of a hex digest, a longer number, or
    # a timestamp's fraction.
    hits = [
        (str(path), token)
        for path in stored
        for token in (b"6173", b"4409")
        if re.search(rb"(?<![0-9A-Za-z.])" + token + rb"(?![0-9A-Za-z])", path.read_bytes())
    ]
    assert hits == []
    (record,) = (tmp_path / "store" / "packages").iterdir()
    # The record (schema v4) keeps case counts only, never a case or its id.
    oracle = json.loads(record.read_text())["package"]["oracles"][0]
    assert "cases" not in oracle
    assert (oracle["case_count"], oracle["held_out_count"]) == (2, 1)


@pytest.fixture
def base(tmp_path: Path) -> Path:
    return _repo(tmp_path / "base", {"mathutils.py": BUGGY})


async def test_a_bare_name_in_the_criterion_is_not_tier_a_and_a_declaration_binds(
    tmp_path: Path,
) -> None:
    # the criterion says "interpolate(a, b, t)" without a module, so the
    # constructor guessed mathutils.interpolate and did not declare it named
    # by the criterion. That guess is not pre-bound;
    # the worker's valid declaration of interp.interpolate is used (A').
    from ouroboros.boundary.binding_flow import assign_tiers, verify_with_bindings

    base = _repo(tmp_path / "base", {"mathutils.py": BUGGY})
    seed = _oracle_seed("add interpolate(a, b, t): interpolate(0, 10, 0.5) returns 5")
    package = _package(
        seed,
        base,
        symbol="mathutils.interpolate",
        params=("a", "b", "t"),
        cases=[
            {
                "case_id": "stated",
                "held_out": False,
                "args": {"a": 0, "b": 10, "t": 0.5},
                "expect": {"kind": "returns", "value": 5},
            },
            {
                "case_id": "held",
                "held_out": True,
                "args": {"a": 2, "b": 4, "t": 0.25},
                "expect": {"kind": "returns", "value": 2.5},
            },
        ],
    )
    assert package.oracles[0].base_run_tier("missing").value == "U"  # a guess is not tier A
    candidate = _repo(
        tmp_path / "cand",
        {
            "mathutils.py": BUGGY,
            "interp.py": "def interpolate(a, b, t):\n    return a + (b - a) * t\n",
        },
    )
    key = package.oracles[0].criterion_key
    assignments, _ = await assign_tiers(
        package,
        base=base,
        contract=CONTRACT,
        declared={key: [{"symbol": "interp.interpolate"}]},
    )
    assert assignments["oracle_1"].tier.value == "A_prime"
    bound = await verify_with_bindings(package, candidate, assignments, contract=CONTRACT)
    assert bound.effective is not None and bound.effective.verdict is CandidateVerdict.PASS


async def test_a_criterion_takes_its_tier_from_the_checks_that_decided_it(base: Path) -> None:
    # a criterion failed through a tier A check that also links an
    # unverified check is "fail, tier A", not "fail, tier U".
    from ouroboros.boundary.acceptance import PackageCriterionStatus, criterion_verdicts
    from ouroboros.boundary.binding import BindingSource, CheckTier, TierAssignment

    seed = _oracle_seed()
    specs = [
        build_oracle_spec(
            seed,
            criterion_index=0,
            check_id=check_id,
            call_kind="function",
            params=("value", "low", "high"),
            default_binding={"symbol": symbol},
            cases=CASES,
        )
        for check_id, symbol in (("oracle_1", "mathutils.clamp"), ("oracle_1_2", "mathutils.bound"))
    ]
    package = assemble_package(
        seed,
        input_digest="1" * 64,
        generator="test",
        oracles=[(spec, CheckRole.REPRODUCTION) for spec in specs],
    )
    key = specs[0].criterion_key
    assignments = {
        "oracle_1": TierAssignment(
            key,
            "oracle_1",
            CheckTier.A,
            specs[0].default_binding,
            BindingSource.DEFAULT,
            "run",
            "x",
        ),
        "oracle_1_2": TierAssignment(
            key, "oracle_1_2", CheckTier.U, None, None, "unverified", "no_binding"
        ),
    }
    verification = await verify_candidate(package, base, only_checks=["oracle_1"])
    verdict = criterion_verdicts(
        package, verification, admission=admission_on_base(package), assignments=assignments
    )[key]
    assert (verdict.status, verdict.tier) == (PackageCriterionStatus.FAIL, CheckTier.A)


async def test_a_script_check_pass_under_a_planted_sitecustomize_is_only_advisory(
    base: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A model-written script check imports the workspace in the process whose
    # exit code is its verdict, so a planted sitecustomize (or the code under
    # test) can make it exit 0. Its pass is therefore advisory: only oracle
    # checks, compared in the controller's interpreter, are verified passes.
    # A script check's failure still fails the criterion.
    import venv

    from ouroboros.boundary.acceptance import (
        SCRIPT_CHECK_ADVISORY,
        ArtifactVerdict,
        PackageCriterionStatus,
        artifact_verdict,
        criterion_verdicts,
    )
    from ouroboros.boundary.package import AssertionLink, CheckSpec, seed_criterion_keys

    seed = _oracle_seed()
    (key,) = seed_criterion_keys(seed)
    script = (
        "import sys\n"
        "sys.path.insert(0, '.')\n"
        "from mathutils import clamp\n"
        "if clamp(15, 0, 10) != 10:\n"
        "    print('OUROBOROS_CHECK_FAILED:script_1')\n"
        "    sys.exit(1)\n"
    )
    package = assemble_package(
        seed,
        input_digest="1" * 64,
        generator="test",
        script_checks=[
            CheckSpec(
                check_id="script_1",
                role=CheckRole.REPRODUCTION,
                argv=("python3", ".ouroboros_checks/check_clamp.py"),
                assertions=(AssertionLink(assertion_id="script_1.a1", criterion_key=key),),
                failure_signature="OUROBOROS_CHECK_FAILED:script_1",
            )
        ],
        script_files=[PackageFile.from_content(".ouroboros_checks/check_clamp.py", script)],
    )
    candidate = _repo(tmp_path / "cand", {"mathutils.py": BUGGY})
    honest = await verify_candidate(package, candidate)
    admission = admission_on_base(package)
    assert (
        criterion_verdicts(package, honest, admission=admission)[key].status
        is PackageCriterionStatus.FAIL
    )

    venv.create(candidate / ".venv", with_pip=False, symlinks=True)
    (site,) = (candidate / ".venv/lib").glob("python*/site-packages")
    (site / "sitecustomize.py").write_text("import os\nos._exit(0)\n")
    monkeypatch.setenv("PATH", f"{candidate / '.venv/bin'}{os.pathsep}{os.environ.get('PATH', '')}")
    forged = await verify_candidate(package, candidate)
    assert forged.verdict is CandidateVerdict.PASS  # the forged exit code
    verdict = criterion_verdicts(package, forged, admission=admission)[key]
    assert (verdict.status, verdict.reason) == (
        PackageCriterionStatus.UNVERIFIED,
        SCRIPT_CHECK_ADVISORY,
    )
    assert artifact_verdict([verdict.status]) is ArtifactVerdict.UNVERIFIED


async def test_a_failed_base_snapshot_records_no_worker_start(
    store: EventStore, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The #2465 probe: the late binding needs the base snapshot; when taking it
    # fails, preparation fails and the journal must not claim a worker start.
    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("disk full")

    monkeypatch.setattr("ouroboros.boundary.run_wiring.snapshot_base", broken)
    with pytest.raises(OSError, match="disk full"):
        await _prepare(store, repo, tmp_path)
    version = "exec_flow/check_package/v1"
    assert await _types(store, version) == [PACKAGE_FROZEN, ADMISSION_COMPLETED]
    assert verify_boundary_order(await store.replay(BOUNDARY_AGGREGATE_TYPE, version)) == ()


async def test_a_verification_that_raises_leaves_no_verified_claim(
    store: EventStore, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The #2465 probe: final bindings with a runnable check are recorded, then
    # the verification raises. Nothing records a candidate verification, and
    # the ledger refuses a decision that claims one.
    seed, state = await _prepare(store, repo, tmp_path)

    async def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("disk full")

    monkeypatch.setattr("ouroboros.boundary.run_wiring.verify_with_bindings", broken)
    with pytest.raises(OSError, match="disk full"):
        await verify_check_package(state, event_store=store, candidate_checkout=repo)
    types = await _types(store, state.boundary_id)
    assert BINDING_RECORDED in types and CANDIDATE_VERIFIED not in types
    from ouroboros.boundary.acceptance import CriterionVerdict
    from ouroboros.boundary.events import ReconciliationPayload

    verified = reconcile_acceptance(
        seed_criterion_keys(seed),
        {
            key: CriterionVerdict(
                key, PackageCriterionStatus.PASS, CheckTier.A, "passed", declared_binding_pass=False
            )
            for key in seed_criterion_keys(seed)
        },
        {},
        existing_run_accepted=True,
        legacy_decides_unverified=True,  # the only rule the journal admits
    )
    with pytest.raises(BoundaryOrderError, match="acceptance must cite a verification"):
        await BoundaryLedger(store).record_acceptance_reconciled(
            state.boundary_id,
            package_id=state.package.package_id,
            reconciliation=verified.to_payload(),
        )
    undecided = reconcile_acceptance(
        seed_criterion_keys(seed),
        {
            key: CriterionVerdict(
                key,
                PackageCriterionStatus.INDETERMINATE,
                CheckTier.A,
                "authority_error:OSError",
                declared_binding_pass=False,
            )
            for key in seed_criterion_keys(seed)
        },
        {},
        existing_run_accepted=True,
        legacy_decides_unverified=True,  # the only rule the journal admits
    )
    await BoundaryLedger(store).record_acceptance_reconciled(
        state.boundary_id,
        package_id=state.package.package_id,
        reconciliation=ReconciliationPayload.model_validate(
            {**undecided.to_dict(), "undecided_reason": "authority_error:OSError"}
        ),
    )
    assert (
        verify_boundary_order(await store.replay(BOUNDARY_AGGREGATE_TYPE, state.boundary_id)) == ()
    )


# The review probe's candidate: the base bug is kept, and only the one
# visible (stated) input is special-cased.
HARDCODED = (
    "def clamp(value, low, high):\n"
    "    if (value, low, high) == (15, 0, 10):\n"
    "        return 10\n"
    "    if value > high:\n"
    "        return value\n"
    "    return max(low, value)\n"
)


async def test_a_held_out_case_the_base_passes_never_makes_a_hardcoded_candidate_pass(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    # B2 review probe, end to end: oracle_1's only held-out case already
    # passes on the buggy base, so it cannot tell a fix from none. Admission
    # excludes it, the criterion is left to the legacy verifier, and a
    # candidate that hardcodes the visible case is never a verified pass.
    reply = _reply()
    reply["oracles"][0]["cases"][1] = {
        "case_id": "held",
        "held_out": True,
        "args": {"value": -3, "low": -2, "high": 4},
        "expect": {"kind": "returns", "value": -2},
    }
    seed = _seed()
    constructor = _Constructor(seed, repo)
    constructor.outcome = ConstructionOutcome(
        package_from_reply(reply, seed, input_digest="1" * 64, generator="fake"),
        None,
        "1" * 64,
        "fake",
    )
    state = await prepare_check_package(
        seed,
        event_store=store,
        constructor=constructor,
        execution_id="exec_b2",
        base_checkout=repo,
        worker_workspace=repo,
        runtime_label="test",
        settings=CheckPackageSettings(True, max_construction_attempts=1),
        store_dir=tmp_path / "store",
    )
    assert state.admission is not None
    assert state.admission.excluded_checks == {"oracle_1": "held_out_not_discriminating"}
    _write(repo, {"mathutils.py": HARDCODED})
    keys = seed_criterion_keys(seed)
    verdict = await verify_check_package(state, event_store=store, candidate_checkout=repo)
    item = verdict.verdicts[keys[0]]
    assert item.status is PackageCriterionStatus.UNCOVERED
    assert item.reason == "uncovered:held_out_not_discriminating"
    decision = reconcile_acceptance(
        keys,
        verdict.verdicts,
        {
            0: ExistingOutcome(0, "failed", "failed", "failed"),
            1: ExistingOutcome(1, "blocked", "blocked", "blocked"),
            2: ExistingOutcome(2, "blocked", "blocked", "blocked"),
        },
        existing_run_accepted=False,
        legacy_decides_unverified=True,
    )
    first = decision.decisions[0]
    assert not first.accepted and first.legacy_decided
    await BoundaryLedger(store).record_acceptance_reconciled(
        state.boundary_id,
        package_id=state.package.package_id,
        reconciliation=decision.to_payload(),
    )
