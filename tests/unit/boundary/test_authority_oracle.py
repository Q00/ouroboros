"""Check package on: tiers decide, the legacy verifier annotates, repairs follow the package."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.boundary.acceptance import PackageCriterionStatus
from ouroboros.boundary.authority import CheckPackageAuthority, existing_outcomes_from_results
from ouroboros.boundary.binding import CheckTier
from ouroboros.boundary.constructor import ConstructionOutcome, package_from_reply
from ouroboros.boundary.package import seed_criterion_keys
from ouroboros.boundary.rollout import Arm, AssignmentSource, CheckPackageAssignment
from ouroboros.boundary.run_control import CheckPackageRun, tier_summary_value
from ouroboros.boundary.run_wiring import CheckPackageSettings, prepare_check_package
from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata
from ouroboros.orchestrator.evidence_schema import EvidenceRecord
from ouroboros.orchestrator.parallel_executor import ParallelACExecutor
from ouroboros.orchestrator.parallel_executor_models import (
    ACExecutionOutcome,
    ACExecutionResult,
    ParallelExecutionResult,
)
from ouroboros.orchestrator.verifier import VerifierVerdict
from ouroboros.persistence.event_store import EventStore
from ouroboros.telemetry import _check_package_properties

BUGGY = "def clamp(value, low, high):\n    if value > high:\n        return value\n    return max(low, value)\n"
FIXED = "def clamp(value, low, high):\n    return max(low, min(high, value))\n"
GOOD_MIX = "\ndef mix(start, end, weight):\n    return start + (end - start) * weight\n"
BAD_MIX = "\ndef mix(start, end, weight):\n    return start + end - weight\n"
MIX_ENTRY = {"symbol": "mathutils.mix", "arg_map": {"a": "start", "b": "end", "t": "weight"}}


def _seed() -> Seed:
    return Seed(
        goal="math helpers",
        acceptance_criteria=(
            "clamp(15, 0, 10) returns 10",
            "linear interpolation between a and b by t: interpolating 0 and 10 at 0.5 gives 5",
            "the helpers are documented in the README",
        ),
        ontology_schema=OntologySchema(name="mathutils", description="math helpers"),
        metadata=SeedMetadata(seed_id="seed_authority_oracle", ambiguity_score=0.1),
    )


REPLY = {
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
            "default_binding": {"symbol": "mathutils.interpolate"},
            "cases": [
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
            ],
        },
    ],
    "uncovered": [{"criterion": 3, "reason": "not executable"}],
}


class _Constructor:
    def __init__(self, seed: Seed, base: Path) -> None:
        package = package_from_reply(
            REPLY, seed, input_digest="1" * 64, generator="fake", base_checkout=base
        )
        self.outcome = ConstructionOutcome(package, None, "1" * 64, "fake")

    async def construct(self, seed: Seed, base: Path, *, feedback=()) -> ConstructionOutcome:
        return self.outcome


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
    (root / "mathutils.py").write_text(BUGGY)
    return root


async def _authority(
    store: EventStore, repo: Path, tmp_path: Path
) -> tuple[Seed, CheckPackageAuthority]:
    seed = _seed()
    settings = CheckPackageSettings(enabled=True)
    state = await prepare_check_package(
        seed,
        event_store=store,
        constructor=_Constructor(seed, repo),
        execution_id="exec_oracle",
        base_checkout=repo,
        worker_workspace=repo,
        runtime_label="codex",
        settings=settings,
        store_dir=tmp_path / "store",
    )
    return seed, CheckPackageAuthority(state, settings, event_store=store, candidate_checkout=repo)


def _legacy_rejected(index: int, *, entry: dict | None = None) -> ACExecutionResult:
    """What the leaf returns with the gate installed: success, legacy rejection kept as annotation."""
    return ACExecutionResult(
        ac_index=index,
        ac_content=f"criterion {index}",
        success=True,
        outcome=ACExecutionOutcome.SUCCEEDED,
        atomic_verifier_verdict=VerifierVerdict(
            passed=False, reasons=("form",), failure_class="EVIDENCE_FORM_MISMATCH"
        ),
        typed_evidence=EvidenceRecord(data={"entry_points": [entry]} if entry else {}),
    )


def _executor(repo: Path, retries: int = 2) -> ParallelACExecutor:
    adapter = MagicMock()
    adapter.working_directory = str(repo)
    adapter.runtime_backend = "claude"
    return ParallelACExecutor(
        adapter=adapter,
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
        run_verify_commands=False,
        ac_retry_attempts=retries,
    )


async def _batch(executor: ParallelACExecutor, seed: Seed, indices: list[int]) -> list[Any]:
    return await executor._run_batch_with_verify_and_retry(
        seed=seed,
        batch_executable=indices,
        session_id="s",
        execution_id="exec_oracle",
        tools=[],
        tool_catalog=None,
        system_prompt="sys",
        level_contexts=[],
        ac_retry_attempts=dict.fromkeys(indices, 0),
        execution_counters=None,
    )


async def test_no_legacy_triggered_retries_when_the_flag_is_on(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    seed, authority = await _authority(store, repo, tmp_path)
    (repo / "mathutils.py").write_text(FIXED + GOOD_MIX)
    executor = _executor(repo)
    authority.install(executor)
    calls: list[list[int]] = []

    async def fake_batch(**kwargs: Any) -> list[ACExecutionResult]:
        calls.append(list(kwargs["batch_indices"]))
        return [_legacy_rejected(0)]

    executor._execute_ac_batch = fake_batch  # type: ignore[method-assign]
    results = await _batch(executor, seed, [0])
    # The legacy verifier rejected the attempt, the package passed it: one dispatch.
    assert calls == [[0]] and results[0].success is True
    assert authority.gate.log == [{"ac_index": 0, "status": "pass", "tier": "A"}]


async def test_flag_off_legacy_rejection_still_retries(repo: Path) -> None:
    executor = _executor(repo)
    calls: list[list[int]] = []

    async def fake_batch(**kwargs: Any) -> list[ACExecutionResult]:
        calls.append(list(kwargs["batch_indices"]))
        cls = ["EVIDENCE_MISSING", "STALL", "SCOPE_CREEP"][len(calls) - 1]
        return [
            ACExecutionResult(
                ac_index=0,
                ac_content="c",
                success=False,
                error="legacy",
                atomic_verifier_verdict=VerifierVerdict(
                    passed=False, reasons=("legacy",), failure_class=cls
                ),
            )
        ]

    executor._execute_ac_batch = fake_batch  # type: ignore[method-assign]
    await _batch(executor, _seed(), [0])
    assert calls == [[0], [0], [0]]
    assert not hasattr(executor, "check_package_gate")


async def test_package_fail_drives_repair_and_names_the_declared_binding(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    seed, authority = await _authority(store, repo, tmp_path)
    (repo / "mathutils.py").write_text(FIXED + BAD_MIX)
    executor = _executor(repo)
    authority.install(executor)
    prompts: list[dict[int, str]] = []

    async def fake_batch(**kwargs: Any) -> list[ACExecutionResult]:
        prompts.append(dict(kwargs.get("retry_prompts") or {}))
        if len(prompts) == 2:
            (repo / "mathutils.py").write_text(FIXED + GOOD_MIX)  # the repair
        result = replace(_legacy_rejected(1, entry=MIX_ENTRY), retry_attempt=len(prompts) - 1)
        return [result]

    executor._execute_ac_batch = fake_batch  # type: ignore[method-assign]
    results = await _batch(executor, seed, [1])
    assert len(prompts) == 2 and results[0].success is True
    repair = prompts[1][1]
    assert "### Check package counterexample" in repair
    assert "declared entry point: function mathutils.mix" in repair
    assert "mix(start=0, end=10, weight=0.5): expected 5, observed 9.5" in repair
    assert "held-out case(s) also failed" in repair and "2.5" not in repair
    assert "EVIDENCE_FORM_MISMATCH" not in repair  # the legacy class drives nothing
    assert [entry["status"] for entry in authority.gate.log] == ["fail", "pass"]
    assert authority.gate.log[0]["tier"] == "A_prime"


async def test_authority_matrix_and_exit_semantics(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    seed, authority = await _authority(store, repo, tmp_path)
    (repo / "mathutils.py").write_text(FIXED + GOOD_MIX)
    authority.install(_executor(repo))
    results = (
        _legacy_rejected(0),  # A, passes; the legacy rejection is advisory
        _legacy_rejected(1, entry=MIX_ENTRY),  # A', passes
        _legacy_rejected(2),  # uncovered: unverified
    )
    parallel = ParallelExecutionResult(results=results, success_count=3, failure_count=0)
    decided = await authority(seed=seed, execution_id="exec_oracle", parallel_result=parallel)
    keys = seed_criterion_keys(seed)
    verdicts = authority.outcome.verdict.verdicts
    assert [(verdicts[k].status, verdicts[k].tier) for k in keys] == [
        (PackageCriterionStatus.PASS, CheckTier.A),
        (PackageCriterionStatus.PASS, CheckTier.A_PRIME),
        (PackageCriterionStatus.UNCOVERED, CheckTier.U),
    ]
    reconciliation = authority.outcome.reconciliation
    # 2 of 3 verified, 0 failed, 1 unverified: accepted, durable status completed.
    assert reconciliation.run_accepted and decided.all_succeeded
    assert authority.outcome.legacy_run_accepted is False  # annotation only
    assert [d.existing_failure_class for d in reconciliation.decisions] == [
        "EVIDENCE_FORM_MISMATCH"
    ] * 3
    run = CheckPackageRun(
        CheckPackageSettings(
            enabled=True, assignment=CheckPackageAssignment(Arm.ON, AssignmentSource.USER_FORCED_ON)
        ),
        state=authority.state,
        authority=authority,
        attempted=True,
    )
    lines = run.render_outcome()
    assert any(line.startswith("Verified: 2 of 3 passed; unverified: 1") for line in lines)
    assert any(line.startswith("- unverified AC 3: uncovered:not executable") for line in lines)

    class _Store:
        async def query_events(self, **_kwargs: Any) -> list[Any]:
            return []

    meta = await run.outcome_meta(
        _Store(), execution_id="exec_oracle", session_id="s", terminal_status="completed"
    )  # type: ignore[arg-type]
    assert meta["package_verdict"] == "pass"
    assert meta["unverified_count"] == "1"
    assert meta["check_tier_summary"] == "A:1,A_prime:1,U:1"
    assert meta["legacy_verdict"] == "reject"
    assert meta["reconciliation"] == "package_accepted_over_legacy_reject"
    assert meta["legacy_failure_class"] == "evidence_form_mismatch"
    assert meta["legacy_failure_class_count"] == "3+"
    sent = _check_package_properties(meta)
    assert sent["unverified_count"] == "1" and sent["check_tier_summary"] == "A:1,A_prime:1,U:1"


async def test_failures_and_unattempted_criteria_are_not_accepted(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    seed, authority = await _authority(store, repo, tmp_path)
    (repo / "mathutils.py").write_text(BUGGY + BAD_MIX)
    authority.install(_executor(repo))
    crashed = ACExecutionResult(ac_index=2, ac_content="c", success=False, error="runtime crashed")
    parallel = ParallelExecutionResult(
        results=(_legacy_rejected(0), _legacy_rejected(1, entry=MIX_ENTRY), crashed),
        success_count=2,
        failure_count=1,
    )
    decided = await authority(seed=seed, execution_id="exec_oracle", parallel_result=parallel)
    decisions = authority.outcome.reconciliation.decisions
    assert [(d.package_status.value, d.accepted, d.governed_by.value) for d in decisions] == [
        ("fail", False, "check_package"),
        ("fail", False, "check_package"),
        ("uncovered", False, "execution"),
    ]
    assert authority.outcome.verdict.verdict == "fail"
    assert not decided.all_succeeded
    assert [r.outcome for r in decided.results] == [ACExecutionOutcome.FAILED] * 3


def test_existing_outcomes_with_the_gate_treat_runtime_failures_as_unattempted() -> None:
    parallel = ParallelExecutionResult(
        results=(
            _legacy_rejected(0),
            ACExecutionResult(ac_index=1, ac_content="c", success=False, error="crash"),
            ACExecutionResult(
                ac_index=2,
                ac_content="c",
                success=False,
                error="pkg",
                check_package_failure_class="CHECK_PACKAGE_FAIL:abc",
            ),
        ),
        success_count=1,
        failure_count=2,
    )
    gated = existing_outcomes_from_results(parallel, gated=True)
    assert [gated[i].attempted for i in range(3)] == [True, False, True]
    assert gated[0].failure_class == "EVIDENCE_FORM_MISMATCH" and not gated[0].passed


def test_tier_summary_value_is_a_closed_bucketed_enum() -> None:
    assert tier_summary_value({"A": 5, "A_prime": 2, "U": 0, "C": 4}) == "A:3+,A_prime:2,U:0"
    dropped = _check_package_properties(
        {"check_tier_summary": "A:9,A_prime:0,U:0", "unverified_count": "7"}
    )
    assert dropped == {}


async def test_the_gate_decides_each_attempt_once(
    store: EventStore, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ouroboros.boundary import authority as authority_module

    seed, authority = await _authority(store, repo, tmp_path)
    (repo / "mathutils.py").write_text(FIXED + BAD_MIX)
    runs: list[int] = []
    real = authority_module.verify_with_bindings

    async def counted(*args: Any, **kwargs: Any) -> Any:
        runs.append(1)
        return await real(*args, **kwargs)

    monkeypatch.setattr(authority_module, "verify_with_bindings", counted)
    attempt = _legacy_rejected(1, entry=MIX_ENTRY)
    first = await authority.gate(seed=seed, ac_index=1, result=attempt)
    again = await authority.gate(seed=seed, ac_index=1, result=attempt)  # a settlement path
    assert runs == [1]
    assert first.success is False and again.success is False
    assert again.check_package_repair == first.check_package_repair


async def test_omitting_entry_points_after_a_counterexample_does_not_withdraw_the_binding(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    # Found by smoke s3c: after a counterexample the worker kept its wrong
    # implementation and simply stopped declaring entry_points, which turned
    # the failing criterion into an unverified one (exit 0).
    seed, authority = await _authority(store, repo, tmp_path)
    (repo / "mathutils.py").write_text(FIXED + BAD_MIX)
    executor = _executor(repo)
    authority.install(executor)
    calls: list[int] = []

    async def fake_batch(**kwargs: Any) -> list[ACExecutionResult]:
        calls.append(1)
        entry = MIX_ENTRY if len(calls) == 1 else None  # later attempts declare nothing
        return [replace(_legacy_rejected(1, entry=entry), retry_attempt=len(calls) - 1)]

    executor._execute_ac_batch = fake_batch  # type: ignore[method-assign]
    results = await _batch(executor, seed, [1])
    assert [entry["status"] for entry in authority.gate.log] == ["fail"] * len(calls)
    assert results[0].success is False
    parallel = ParallelExecutionResult(
        results=(_legacy_rejected(0), results[0]),
        success_count=1,
        failure_count=1,
    )
    decided = await authority(seed=seed, execution_id="exec_oracle", parallel_result=parallel)
    keys = seed_criterion_keys(seed)
    item = authority.outcome.verdict.verdicts[keys[1]]
    assert (item.status, item.tier) == (PackageCriterionStatus.FAIL, CheckTier.A_PRIME)
    assert not decided.all_succeeded


class _FailingConstructor:
    def __init__(self, **_kwargs: Any) -> None:
        pass

    async def construct(self, seed: Seed, base: Path, *, feedback=()) -> ConstructionOutcome:
        return ConstructionOutcome(None, "constructor_timeout", "1" * 64, "fake")


class _EmptyStore:
    async def query_events(self, **_kwargs: Any) -> list[Any]:
        return []


@pytest.mark.parametrize(("terminal", "legacy"), [("completed", "accept"), ("failed", "reject")])
async def test_outage_falls_back_to_the_legacy_path(
    store: EventStore,
    repo: Path,
    tmp_path: Path,
    terminal: str,
    legacy: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Constructor outage: zero checks admitted. Nothing is installed on the
    # runner, so the executor and the terminal status are the legacy ones;
    # the run exits 0 only when the legacy verifier passed it.
    from types import SimpleNamespace

    monkeypatch.setattr(
        "ouroboros.boundary.run_wiring.default_store_dir",
        lambda execution_id: tmp_path / "store" / execution_id,
    )
    runner = SimpleNamespace(acceptance_authority=None)
    run = CheckPackageRun(
        CheckPackageSettings(
            enabled=True,
            max_construction_attempts=2,
            assignment=CheckPackageAssignment(Arm.ON, AssignmentSource.RANDOMIZED),
        )
    )
    lines = await run.prepare(
        runner,
        _seed(),
        event_store=store,
        execution_id="exec_outage",
        worker_dir=repo,
        runtime_backend="codex",
        model=None,
        resume=False,
        constructor_factory=_FailingConstructor,
    )
    assert runner.acceptance_authority is None and run.authority is None
    assert any("No admitted package (constructor_timeout)" in line for line in lines)
    assert run.render_outcome() == [
        "Check package unavailable (constructor_timeout); legacy verification decided this run."
    ]
    meta = await run.outcome_meta(
        _EmptyStore(),
        execution_id="exec_outage",
        session_id="s",
        terminal_status=terminal,  # type: ignore[arg-type]
    )
    assert meta["check_package_status"] == "construction_failed"
    assert meta["reconciliation"] == "fallback_to_legacy"
    assert meta["package_verdict"] == "none"
    assert meta["legacy_verdict"] == legacy
    assert "unverified_count" not in meta and "check_tier_summary" not in meta
    # The executor gets no gate: its prompt and retry loop are the legacy ones.
    executor = _executor(repo)
    assert not hasattr(executor, "check_package_gate")


async def test_held_out_only_failure_reveals_one_case_and_retires_it(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    from ouroboros.boundary.events import BOUNDARY_AGGREGATE_TYPE, CASE_REVEALED

    seed, authority = await _authority(store, repo, tmp_path)
    # mix passes the stated case (0, 10, 0.5 -> 5) and fails the held-out one.
    (repo / "mathutils.py").write_text(
        FIXED + "\ndef mix(start, end, weight):\n    return start + end * weight\n"
    )
    attempt = _legacy_rejected(1, entry=MIX_ENTRY)
    gated = await authority.gate(seed=seed, ac_index=1, result=attempt)
    assert gated.success is False
    repair = gated.check_package_repair
    assert "declared entry point: function mathutils.mix" in repair
    assert (
        "- revealed held-out case: mix(start=2, end=4, weight=0.25): expected 2.5, observed 3.0"
        in repair
    )
    assert authority.revealed == {"oracle_2": {"held"}}
    events = await store.replay(BOUNDARY_AGGREGATE_TYPE, authority.state.boundary_id)
    reveal = [e for e in events if e.type == CASE_REVEALED]
    assert [(e.data["check_id"], e.data["case_id"], e.data["root_ac_index"]) for e in reveal] == [
        ("oracle_2", "held", 1)
    ]
    # A second attempt that still fails reveals nothing new (the case is visible now).
    again = await authority.gate(seed=seed, ac_index=1, result=replace(attempt, retry_attempt=1))
    assert "revealed held-out case: mix(start=2, end=4, weight=0.25)" in again.check_package_repair
    events = await store.replay(BOUNDARY_AGGREGATE_TYPE, authority.state.boundary_id)
    assert len([e for e in events if e.type == CASE_REVEALED]) == 1
    # Final verdict: the retired case no longer counts as held out.
    parallel = ParallelExecutionResult(
        results=(_legacy_rejected(0), replace(attempt, retry_attempt=1)),
        success_count=2,
        failure_count=0,
    )
    await authority(seed=seed, execution_id="exec_oracle", parallel_result=parallel)
    keys = seed_criterion_keys(seed)
    final = authority.outcome.verdict.verdicts[keys[1]]
    assert final.status is PackageCriterionStatus.FAIL and final.failed_heldout_only is False
