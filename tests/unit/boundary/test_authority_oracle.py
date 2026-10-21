"""Check package on: tiers decide, the legacy verifier annotates, repairs follow the package."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import re
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.boundary import tree
from ouroboros.boundary.acceptance import PackageCriterionStatus
from ouroboros.boundary.authority import (
    CheckPackageAuthority,
    CheckPackageGate,
    existing_outcomes_from_results,
    legacy_verdict_in_tree,
)
from ouroboros.boundary.binding import CheckTier
from ouroboros.boundary.constructor import ConstructionOutcome
from ouroboros.boundary.oracle_build import ReplyError, ReplyFailure, package_from_reply
from ouroboros.boundary.package import seed_criterion_keys
from ouroboros.boundary.run_wiring import (
    CheckPackageSettings,
    prepare_check_package,
)
from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata
from ouroboros.orchestrator.evidence_schema import EvidenceRecord
from ouroboros.orchestrator.parallel_executor import ParallelACExecutor
from ouroboros.orchestrator.parallel_executor_models import (
    ACExecutionOutcome,
    ACExecutionResult,
    ParallelExecutionResult,
    legacy_owned,
    package_failure_class,
    package_repair,
    package_settlement_view,
    settle_package_results,
)
from ouroboros.orchestrator.verifier import VerifierVerdict
from ouroboros.persistence.event_store import EventStore

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
            "target_named_in_criterion": False,
            "cases": [
                {
                    "case_id": "stated",
                    "held_out": False,
                    "args": {"value": 15, "low": 0, "high": 10},
                    "expect": {"kind": "returns", "value": 10},
                },
                {
                    # The buggy base returns 20 here: a pass is a genuine fix.
                    "case_id": "held",
                    "held_out": True,
                    "args": {"value": 20, "low": -5, "high": 7},
                    "expect": {"kind": "returns", "value": 7},
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
            "target_named_in_criterion": False,
            "cases": [
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
            ],
        },
    ],
    # The reason is descriptive text; it routes nothing (criterion 3 has no
    # admitted check, so the legacy verifier decides it).
    "uncovered": [{"criterion": 3, "reason": "non_behavioral"}],
}


class _Constructor:
    def __init__(self, seed: Seed, base: Path) -> None:
        package = package_from_reply(REPLY, seed, input_digest="1" * 64, generator="fake")
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
        # The executor keeps the rejection it made advisory (gate installed).
        legacy_rejection="legacy verifier: evidence form mismatch",
        typed_evidence=EvidenceRecord(data={"entry_points": [entry]} if entry else {}),
    )


def _settled(result: ACExecutionResult) -> ACExecutionResult:
    """``result`` after a final settlement that found every other gate holding."""
    return settle_package_results([result], [package_settlement_view(result)])[0]


def _transcript_unavailable(index: int) -> ACExecutionResult:
    """What the leaf returns when the transcript could not be collected: no rejection."""
    return ACExecutionResult(
        ac_index=index,
        ac_content=f"criterion {index}",
        success=True,
        outcome=ACExecutionOutcome.SUCCEEDED,
        atomic_verifier_verdict=VerifierVerdict(
            passed=False,
            reasons=("transcript_missing_infrastructure: runtime support messages were empty",),
            failure_class="TRANSCRIPT_MISSING_INFRASTRUCTURE",
        ),
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
    # The gate runs visible cases only: never a verified pass, no repair signal.
    assert authority.gate.log == [{"ac_index": 0, "status": "unverified", "tier": "A"}]


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
    assert "held" not in repair.lower() and "2.5" not in repair
    assert "EVIDENCE_FORM_MISMATCH" not in repair  # the legacy class drives nothing
    assert [entry["status"] for entry in authority.gate.log] == ["fail", "unverified"]
    assert authority.gate.log[0]["tier"] == "A_prime"


def _legacy_accepted(index: int, *, entry: dict | None = None) -> ACExecutionResult:
    """What the leaf returns when the legacy verifier accepted on evidence."""
    return ACExecutionResult(
        ac_index=index,
        ac_content=f"criterion {index}",
        success=True,
        outcome=ACExecutionOutcome.SUCCEEDED,
        atomic_verifier_verdict=VerifierVerdict(passed=True, reasons=(), failure_class=None),
        typed_evidence=EvidenceRecord(data={"entry_points": [entry]} if entry else {}),
    )


async def _authority_matrix_and_exit_semantics_scenario(
    store: EventStore, repo: Path, tmp_path: Path
) -> CheckPackageAuthority:
    """A decides; A' only corroborates (H2); the README one is decided by the legacy verifier."""
    seed, authority = await _authority(store, repo, tmp_path)
    (repo / "mathutils.py").write_text(FIXED + GOOD_MIX)
    authority.install(_executor(repo))
    results = (
        _legacy_rejected(0),  # A, passes; the legacy rejection is advisory
        _legacy_rejected(1, entry=MIX_ENTRY),  # A', passes, but cannot overrule the rejection
        _legacy_rejected(2),  # no admitted check: the legacy rejection decides it
    )
    parallel = ParallelExecutionResult(results=results, success_count=3, failure_count=0)
    decided = await authority(seed=seed, execution_id="exec_oracle", parallel_result=parallel)
    keys = seed_criterion_keys(seed)
    verdicts = authority.outcome.verdict.verdicts
    assert [(verdicts[k].status, verdicts[k].tier, verdicts[k].reason) for k in keys] == [
        (PackageCriterionStatus.PASS, CheckTier.A, "passed"),
        (PackageCriterionStatus.PASS, CheckTier.A_PRIME, "passed"),
        (PackageCriterionStatus.UNCOVERED, CheckTier.U, "uncovered:declared_not_executable"),
    ]
    reconciliation = authority.outcome.reconciliation
    # A is accepted over the legacy rejection; the A' pass only corroborates,
    # so the legacy rejection stands; the legacy-decided criterion fails, so
    # the run fails (exit 1, durable status failed).
    assert [(d.accepted, d.governed_by.value, d.reason) for d in reconciliation.decisions] == [
        (True, "check_package", "passed"),
        (False, "existing_verifier", "a_prime_corroborates_only"),
        (False, "existing_verifier", "uncovered:declared_not_executable"),
    ]
    assert not reconciliation.run_accepted and not decided.all_succeeded
    assert [r.outcome for r in decided.results] == [
        ACExecutionOutcome.SUCCEEDED,
        ACExecutionOutcome.FAILED,
        ACExecutionOutcome.FAILED,
    ]
    assert decided.results[1].error == (
        "legacy-decided (a_prime_corroborates_only): legacy verifier: evidence form mismatch"
    )
    assert decided.results[2].error == (
        "legacy-decided (uncovered:declared_not_executable): legacy verifier: evidence form mismatch"
    )
    assert [d.existing_failure_class for d in reconciliation.decisions] == [
        "EVIDENCE_FORM_MISMATCH"
    ] * 3
    return authority


async def test_authority_matrix_and_exit_semantics(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    await _authority_matrix_and_exit_semantics_scenario(store, repo, tmp_path)


async def _a_legacy_accepted_unverified_criterion_exits_zero_scenario(
    store: EventStore, repo: Path, tmp_path: Path
) -> CheckPackageAuthority:
    seed, authority = await _authority(store, repo, tmp_path)
    (repo / "mathutils.py").write_text(FIXED + GOOD_MIX)
    authority.install(_executor(repo))
    results = (
        _legacy_rejected(0),
        _legacy_accepted(1, entry=MIX_ENTRY),  # A' corroborates a legacy acceptance
        _legacy_accepted(2),  # the legacy verifier accepts it on evidence
    )
    parallel = ParallelExecutionResult(results=results, success_count=3, failure_count=0)
    decided = await authority(seed=seed, execution_id="exec_oracle", parallel_result=parallel)
    reconciliation = authority.outcome.reconciliation
    assert reconciliation.run_accepted and decided.all_succeeded
    assert reconciliation.decisions[2].legacy_decided and reconciliation.decisions[2].accepted
    assert reconciliation.accepted_unverified == ()
    return authority


async def test_a_legacy_accepted_unverified_criterion_exits_zero(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    await _a_legacy_accepted_unverified_criterion_exits_zero_scenario(store, repo, tmp_path)


async def _a_package_failure_is_not_masked_by_a_legacy_acceptance_scenario(
    store: EventStore, repo: Path, tmp_path: Path
) -> CheckPackageAuthority:
    """A criteria stay decided by the package: legacy acceptance changes nothing."""
    seed, authority = await _authority(store, repo, tmp_path)
    (repo / "mathutils.py").write_text(BUGGY + GOOD_MIX)
    authority.install(_executor(repo))
    results = (
        _legacy_accepted(0),
        _legacy_rejected(1, entry=MIX_ENTRY),
        _legacy_accepted(2),
    )
    parallel = ParallelExecutionResult(results=results, success_count=3, failure_count=0)
    decided = await authority(seed=seed, execution_id="exec_oracle", parallel_result=parallel)
    first = authority.outcome.reconciliation.decisions[0]
    assert first.governed_by.value == "check_package" and not first.accepted
    assert first.package_status is PackageCriterionStatus.FAIL
    assert not decided.all_succeeded
    # The rejection comes from the package (tier A), not from a legacy-decided criterion.
    return authority


async def test_a_package_failure_is_not_masked_by_a_legacy_acceptance(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    await _a_package_failure_is_not_masked_by_a_legacy_acceptance_scenario(store, repo, tmp_path)


async def _both_verifiers_without_evidence_leave_the_criterion_unverified_scenario(
    store: EventStore, repo: Path, tmp_path: Path
) -> CheckPackageAuthority:
    """Transcript unavailable on the legacy side, no check on the package side: exit 0, flagged."""
    seed, authority = await _authority(store, repo, tmp_path)
    (repo / "mathutils.py").write_text(FIXED + GOOD_MIX)
    authority.install(_executor(repo))
    results = (
        _legacy_rejected(0),
        _legacy_accepted(1, entry=MIX_ENTRY),
        _transcript_unavailable(2),
    )
    parallel = ParallelExecutionResult(results=results, success_count=3, failure_count=0)
    decided = await authority(seed=seed, execution_id="exec_oracle", parallel_result=parallel)
    reconciliation = authority.outcome.reconciliation
    assert reconciliation.run_accepted and decided.all_succeeded
    (unverified,) = reconciliation.accepted_unverified
    assert unverified.root_ac_index == 2 and not unverified.legacy_decided
    return authority


async def test_both_verifiers_without_evidence_leave_the_criterion_unverified(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    await _both_verifiers_without_evidence_leave_the_criterion_unverified_scenario(
        store, repo, tmp_path
    )


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
            # The package gate alone failed it; the final settlement found every other gate holding.
            _settled(CheckPackageGate._repair(_legacy_accepted(2), "counterexample")),
            # The package gate failed it, but no final settlement judged its other gates.
            CheckPackageGate._repair(_legacy_accepted(3), "counterexample"),
        ),
        success_count=1,
        failure_count=3,
    )
    gated = existing_outcomes_from_results(parallel, gated=True)
    assert [gated[i].attempted for i in range(4)] == [True, False, True, False]
    assert gated[0].failure_class == "EVIDENCE_FORM_MISMATCH" and not gated[0].passed


@pytest.mark.parametrize("gated", [True, False])
def test_an_unavailable_transcript_is_no_legacy_rejection(gated: bool) -> None:
    # the executor keeps such a result successful and sets no
    # rejection (the switch off accepts it); a failing verdict alone is no
    # information, on the root and on a sub-AC.
    decomposed = ACExecutionResult(
        ac_index=1,
        ac_content="c1",
        success=True,
        outcome=ACExecutionOutcome.SUCCEEDED,
        is_decomposed=True,
        sub_results=(replace(_transcript_unavailable(1), ac_content="sub"),),
    )
    parallel = ParallelExecutionResult(
        results=(_transcript_unavailable(0), decomposed), success_count=2, failure_count=0
    )
    assert legacy_verdict_in_tree(parallel.results[0]) == (False, None, None)
    assert legacy_verdict_in_tree(decomposed) == (False, None, None)
    outcomes = existing_outcomes_from_results(parallel, gated=gated)
    assert [outcomes[i].passed for i in range(2)] == [True, True]


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
    assert package_repair(again) == package_repair(first)


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


# Passes the stated case by returning its expected value; wrong on any other input above high.
STATED_ONLY = "def clamp(value, low, high):\n    return 10 if value > high else max(low, value)\n"


async def test_held_out_cases_run_only_in_the_final_verification(
    store: EventStore, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H1 and the review's reveal probe: nothing held out reaches the worker's code early.

    The implementation passes the stated case and fails the held-out one. The
    per-attempt gate runs the visible case only: no held-out input reaches a
    target process, and there is no repair message that could reveal or
    count a held-out case. The final verification runs every case and fails
    the criterion on the held-out one.
    """
    from ouroboros.boundary import oracle_run

    seen: list[tuple[str, str]] = []
    real = oracle_run._python_case

    async def recording(command: Any, nonce: str, call: dict[str, Any], budget: float) -> Any:
        seen.append((phase, call["case_id"]))
        return await real(command, nonce, call, budget)

    monkeypatch.setattr(oracle_run, "_python_case", recording)
    phase = "admission"  # before any worker: every case runs on the base
    seed, authority = await _authority(store, repo, tmp_path)
    authority.install(_executor(repo))
    (repo / "mathutils.py").write_text(STATED_ONLY + GOOD_MIX)
    phase = "gate"
    attempt = _legacy_accepted(0)
    gated = await authority.gate(seed=seed, ac_index=0, result=attempt)
    assert gated is attempt and package_repair(gated) is None
    # Case ids are the product's: c1 is the stated case, c2 the held-out one.
    assert ("gate", "c2") not in seen and ("gate", "c1") in seen
    phase = "final"
    parallel = ParallelExecutionResult(
        results=(attempt, _legacy_accepted(1, entry=MIX_ENTRY), _legacy_accepted(2)),
        success_count=3,
        failure_count=0,
    )
    decided = await authority(seed=seed, execution_id="exec_oracle", parallel_result=parallel)
    assert ("final", "c2") in seen
    final = authority.outcome.verdict.verdicts[seed_criterion_keys(seed)[0]]
    assert final.status is PackageCriterionStatus.FAIL and final.failed_heldout_only is True
    assert not decided.all_succeeded


async def test_a_repair_message_never_mentions_held_out_cases(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    seed, authority = await _authority(store, repo, tmp_path)
    (repo / "mathutils.py").write_text(FIXED + BAD_MIX)  # fails the stated and held-out cases
    attempt = _legacy_rejected(1, entry=MIX_ENTRY)
    gated = await authority.gate(seed=seed, ac_index=1, result=attempt)
    repair = package_repair(gated)
    assert "mix(start=0, end=10, weight=0.5): expected 5, observed 9.5" in repair
    assert "held" not in repair.lower() and "2.5" not in repair and "start=2" not in repair


async def _an_authority_error_leaves_covered_criteria_undecided_never_accepted_scenario(
    store: EventStore, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> CheckPackageAuthority:
    seed, authority = await _authority(store, repo, tmp_path)
    authority.install(_executor(repo))

    async def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("disk full")

    monkeypatch.setattr("ouroboros.boundary.authority.verify_check_package", broken)
    gate_only = _settled(CheckPackageGate._repair(_legacy_accepted(0), "counterexample"))
    parallel = ParallelExecutionResult(
        results=(gate_only, _legacy_accepted(1), _legacy_accepted(2)),
        success_count=2,
        failure_count=1,
    )
    decided = await authority(seed=seed, execution_id="exec_oracle", parallel_result=parallel)
    assert not decided.all_succeeded
    assert [r.outcome for r in decided.results] == [
        ACExecutionOutcome.FAILED,  # the package failure stands
        ACExecutionOutcome.FAILED,  # covered: undecided, not accepted
        ACExecutionOutcome.SUCCEEDED,  # uncovered: the legacy acceptance decides
    ]
    decisions = authority.outcome.reconciliation.decisions
    assert [(d.package_status.value, d.reason, d.governed_by.value) for d in decisions] == [
        ("indeterminate", "authority_error:OSError", "check_package"),
        ("indeterminate", "authority_error:OSError", "check_package"),
        ("uncovered", "uncovered", "existing_verifier"),
    ]
    assert authority.outcome.error == "OSError"
    return authority


async def test_an_authority_error_leaves_covered_criteria_undecided_never_accepted(
    store: EventStore, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # B1: the gate failed criterion 0 on the package; the authority then
    # raises. Nothing the package covers may be accepted because something
    # else failed: covered criteria are indeterminate, and only the uncovered
    # criterion is left to the legacy verifier (resume's no-package rule).
    await _an_authority_error_leaves_covered_criteria_undecided_never_accepted_scenario(
        store, repo, tmp_path, monkeypatch
    )


async def test_a_verification_that_raises_is_recorded_as_undecided_never_verified(
    store: EventStore, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #2465: the final bindings (a runnable check) are recorded, then the
    # candidate verification raises. The journal holds no candidate
    # verification, and the decision it records says the package could not
    # decide (every covered criterion indeterminate), which replay accepts.
    from ouroboros.boundary.events import (
        ACCEPTANCE_RECONCILED,
        BINDING_RECORDED,
        BOUNDARY_AGGREGATE_TYPE,
        CANDIDATE_VERIFIED,
    )
    from ouroboros.boundary.ledger import verify_boundary_order

    seed, authority = await _authority(store, repo, tmp_path)
    authority.install(_executor(repo))

    async def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("disk full")

    monkeypatch.setattr("ouroboros.boundary.run_wiring.verify_with_bindings", broken)
    parallel = ParallelExecutionResult(
        results=tuple(_legacy_accepted(i) for i in range(3)), success_count=3, failure_count=0
    )
    decided = await authority(seed=seed, execution_id="exec_oracle", parallel_result=parallel)
    assert not decided.all_succeeded
    events = await store.replay(BOUNDARY_AGGREGATE_TYPE, authority.state.boundary_id)
    types = [event.type for event in events]
    assert BINDING_RECORDED in types and CANDIDATE_VERIFIED not in types
    reconciled = [event for event in events if event.type == ACCEPTANCE_RECONCILED]
    assert len(reconciled) == 1
    assert reconciled[0].data["undecided_reason"] == "authority_error:OSError"
    assert {c["package_status"] for c in reconciled[0].data["criteria"]} <= {
        "indeterminate",
        "uncovered",
    }
    assert verify_boundary_order(events) == ()


async def test_an_error_reading_the_legacy_verdicts_is_inside_the_fail_closed_scope(
    store: EventStore, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed, authority = await _authority(store, repo, tmp_path)
    authority.install(_executor(repo))

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise KeyError("legacy")

    monkeypatch.setattr("ouroboros.boundary.authority.existing_outcomes_from_results", broken)
    parallel = ParallelExecutionResult(
        results=tuple(_legacy_accepted(i) for i in range(3)), success_count=3, failure_count=0
    )
    decided = await authority(seed=seed, execution_id="exec_oracle", parallel_result=parallel)
    assert authority.outcome.error == "KeyError"
    assert not decided.all_succeeded
    assert (decided.success_count, decided.failure_count) == (0, 3)


async def test_an_unreadable_candidate_file_cannot_turn_a_package_fail_into_accept(
    store: EventStore, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # B1 review probe: the worker leaves a file the controller cannot read.
    # The gate saw the bug; the final verification must not fall back to the
    # legacy verdicts and accept it.
    seed, authority = await _authority(store, repo, tmp_path)
    (repo / "mathutils.py").write_text(BUGGY + GOOD_MIX)  # clamp stays wrong
    authority.install(_executor(repo))
    ok = ACExecutionResult(
        ac_index=0, ac_content="c0", success=True, outcome=ACExecutionOutcome.SUCCEEDED
    )
    gated = await authority.gate(seed=seed, ac_index=0, result=ok)
    assert gated.outcome is ACExecutionOutcome.FAILED  # the package saw the bug
    trap = repo / "notes.bin"
    trap.write_text("x")
    # The read fails as it would for a file mode 000 (injected, so the test
    # also holds when it runs as root, where mode 000 does not stop a read).
    hash_file = tree._file_sha256

    def unreadable(directory: Any, name: str) -> str:
        if name == trap.name:
            raise PermissionError(13, "Permission denied", name)
        return hash_file(directory, name)

    monkeypatch.setattr(tree, "_file_sha256", unreadable)
    parallel = ParallelExecutionResult(
        results=(
            gated,
            ACExecutionResult(
                ac_index=1,
                ac_content="c1",
                success=True,
                typed_evidence=EvidenceRecord(data={"entry_points": [MIX_ENTRY]}),
            ),
            ACExecutionResult(ac_index=2, ac_content="c2", success=True),
        ),
        success_count=2,
        failure_count=1,
    )
    decided = await authority(seed=seed, execution_id="exec_oracle", parallel_result=parallel)
    assert not decided.all_succeeded, "wrong code accepted after a worker-triggered fault"
    first = authority.outcome.reconciliation.decisions[0]
    assert not first.accepted and decided.results[0].outcome is ACExecutionOutcome.FAILED
    assert (first.package_status, first.reason) == (
        PackageCriterionStatus.INDETERMINATE,
        "candidate_unreadable",
    )


async def _unattempted_criteria_leave_the_package_undeciding_scenario(
    store: EventStore, repo: Path, tmp_path: Path
) -> CheckPackageAuthority:
    seed, authority = await _authority(store, repo, tmp_path)
    authority.install(_executor(repo))
    crashed = tuple(
        ACExecutionResult(ac_index=index, ac_content="c", success=False, error="runtime crashed")
        for index in range(3)
    )
    parallel = ParallelExecutionResult(results=crashed, success_count=0, failure_count=3)
    await authority(seed=seed, execution_id="exec_oracle", parallel_result=parallel)
    assert authority.outcome.reconciliation is not None and not authority.outcome.package_decided
    return authority


async def test_unattempted_criteria_leave_the_package_undeciding(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    # an admitted package whose criteria the worker never attempted decides
    # nothing: the legacy verifier decides the run (fallback_to_legacy).
    await _unattempted_criteria_leave_the_package_undeciding_scenario(store, repo, tmp_path)


async def test_a_declared_entry_point_the_workspace_lacks_fails_through_it_with_a_repair(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    # Whether a declared entry point exists is shown by running the oracle
    # through it, never by a static look at the files: the base run through a
    # missing symbol is the reproduction's expected failure, and the
    # candidate run through it fails with a counterexample that names it.
    seed, authority = await _authority(store, repo, tmp_path)
    authority.install(_executor(repo))
    (repo / "mathutils.py").write_text(FIXED + GOOD_MIX)
    wrong = {"symbol": "mathutils.blend", "arg_map": MIX_ENTRY["arg_map"]}
    gated = await authority.gate(seed=seed, ac_index=1, result=_legacy_rejected(1, entry=wrong))
    assert gated.success is False
    assert str(package_failure_class(gated)).startswith("CHECK_PACKAGE_FAIL:")
    repair = package_repair(gated)
    assert "It called your declared entry point: function mathutils.blend" in repair
    assert "blend not found" in repair
    assert "2.5" not in repair  # no oracle value
    # The worker fixes its declaration within the retry budget.
    fixed = await authority.gate(
        seed=seed, ac_index=1, result=replace(_legacy_rejected(1, entry=MIX_ENTRY), retry_attempt=1)
    )
    assert fixed.success is True


def _decomposed_root(index: int, *, via: str) -> ACExecutionResult:
    """A decomposed root whose second sub-AC the legacy verifier rejected (gate on)."""
    sub_ok = ACExecutionResult(
        ac_index=index, ac_content="sub a", success=True, outcome=ACExecutionOutcome.SUCCEEDED
    )
    if via == "verdict":
        sub_rejected = replace(_legacy_rejected(index), ac_content="sub b")
    else:
        sub_rejected = ACExecutionResult(
            ac_index=index,
            ac_content="sub b",
            success=True,
            outcome=ACExecutionOutcome.SUCCEEDED,
            legacy_rejection="evidence form mismatch",
        )
    return ACExecutionResult(
        ac_index=index,
        ac_content=f"criterion {index}",
        success=True,
        outcome=ACExecutionOutcome.SUCCEEDED,
        is_decomposed=True,
        sub_results=(sub_ok, sub_rejected),
    )


async def _after_an_authority_error_the_legacy_verdicts_of_sub_acs_decide_uncovered_roots_scenario(
    store: EventStore, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, via: str
) -> CheckPackageAuthority:
    seed, authority = await _authority(store, repo, tmp_path)
    authority.install(_executor(repo))

    async def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("disk full")

    monkeypatch.setattr("ouroboros.boundary.authority.verify_check_package", broken)
    results = (
        ACExecutionResult(ac_index=0, ac_content="c0", success=True),
        ACExecutionResult(ac_index=1, ac_content="c1", success=True),
        _decomposed_root(2, via=via),
    )
    parallel = ParallelExecutionResult(results=results, success_count=3, failure_count=0)
    assert not existing_outcomes_from_results(parallel, gated=True)[2].passed
    decided = await authority(seed=seed, execution_id="exec_oracle", parallel_result=parallel)
    assert authority.outcome.error == "OSError"
    assert not decided.all_succeeded
    assert decided.results[2].outcome is ACExecutionOutcome.FAILED
    assert decided.results[2].error.startswith("legacy-decided (uncovered): ")
    return authority


@pytest.mark.parametrize("via", ["verdict", "annotation"])
async def test_after_an_authority_error_the_legacy_verdicts_of_sub_acs_decide_uncovered_roots(
    store: EventStore, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, via: str
) -> None:
    # a decomposed root carries no legacy rejection itself; its sub-AC
    # does. When the authority fails, the uncovered root is decided by the
    # legacy verdict tree, so it fails and the run does not exit 0.
    await _after_an_authority_error_the_legacy_verdicts_of_sub_acs_decide_uncovered_roots_scenario(
        store, repo, tmp_path, monkeypatch, via
    )


async def test_when_no_decision_can_be_built_every_root_fails(
    store: EventStore, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the run never reports a decision it did not apply, and never the
    # unchanged executor result.
    seed, authority = await _authority(store, repo, tmp_path)
    authority.install(_executor(repo))

    async def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("disk full")

    def broken_apply(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("apply")

    monkeypatch.setattr("ouroboros.boundary.authority.verify_check_package", broken)
    monkeypatch.setattr("ouroboros.boundary.authority.apply_reconciliation", broken_apply)
    parallel = ParallelExecutionResult(
        results=tuple(
            ACExecutionResult(ac_index=i, ac_content=f"c{i}", success=True) for i in range(3)
        ),
        success_count=3,
        failure_count=0,
    )
    decided = await authority(seed=seed, execution_id="exec_oracle", parallel_result=parallel)
    assert authority.outcome.error == "OSError" and authority.outcome.reconciliation is None
    assert not decided.all_succeeded
    assert (decided.success_count, decided.failure_count) == (0, 3)


DEEP_FRAME = (
    "import os, stat, sys\n"
    "_fd = next(fd for fd in range(3, 64) if os.path.exists(f'/dev/fd/{fd}')"
    " and stat.S_ISFIFO(os.fstat(fd).st_mode))\n"
    "os.write(_fd, ('\\n' + sys.argv[2] + ' ' + '[' * 200000 + ']' * 200000 + '\\n').encode())\n"
)


async def test_a_hostile_frame_is_a_package_fail_not_an_authority_error(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    # End to end: the candidate writes a deeply nested frame while it is
    # imported. Every case of its checks fails with a counterexample; the gate
    # sends a repair, and the authority decides (no error path): not
    # accepted, so the run exits 1.
    seed, authority = await _authority(store, repo, tmp_path)
    (repo / "mathutils.py").write_text(DEEP_FRAME + BUGGY + GOOD_MIX)
    authority.install(_executor(repo))
    ok = ACExecutionResult(
        ac_index=0, ac_content="c0", success=True, outcome=ACExecutionOutcome.SUCCEEDED
    )
    gated = await authority.gate(seed=seed, ac_index=0, result=ok)
    assert package_repair(gated)
    assert "observed malformed or oversized output" in package_repair(gated)
    parallel = ParallelExecutionResult(
        results=(
            ok,
            ACExecutionResult(
                ac_index=1,
                ac_content="c1",
                success=True,
                typed_evidence=EvidenceRecord(data={"entry_points": [MIX_ENTRY]}),
            ),
            ACExecutionResult(ac_index=2, ac_content="c2", success=True),
        ),
        success_count=3,
        failure_count=0,
    )
    decided = await authority(seed=seed, execution_id="exec_oracle", parallel_result=parallel)
    assert authority.outcome.error is None
    assert authority.outcome.verdict is not None
    assert authority.outcome.verdict.verdict == "fail"
    assert not decided.all_succeeded


async def test_held_out_values_never_reach_the_boundary_store(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    # after a run (admission, a gate repair, the final verification), no
    # file under the boundary store and no boundary event holds a held-out
    # input or expected value. The values are distinctive so a hit is real.
    import copy

    from ouroboros.boundary.events import BOUNDARY_AGGREGATE_TYPE

    reply: dict[str, Any] = copy.deepcopy(REPLY)
    reply["oracles"][0]["cases"][1] = {
        "case_id": "held",
        "held_out": True,
        "args": {"value": 6173, "low": 1, "high": 4409},
        "expect": {"kind": "returns", "value": 4409},
    }
    reply["oracles"][1]["cases"][1] = {
        "case_id": "held",
        "held_out": True,
        "args": {"a": 3079, "b": 3083, "t": 0.5},
        "expect": {"kind": "returns", "value": 3081, "approx": 1e-9},
    }
    secrets = ("6173", "4409", "3079", "3083", "3081")

    def found(token: str, data: bytes) -> bool:
        # A standalone number: not part of a hex digest, a longer number, or
        # a timestamp's fraction.
        return (
            re.search(rb"(?<![0-9A-Za-z.])" + token.encode() + rb"(?![0-9A-Za-z])", data)
            is not None
        )

    seed = _seed()
    constructor = _Constructor(seed, repo)
    constructor.outcome = ConstructionOutcome(
        package_from_reply(reply, seed, input_digest="1" * 64, generator="fake"),
        None,
        "1" * 64,
        "fake",
    )
    settings = CheckPackageSettings(enabled=True)
    state = await prepare_check_package(
        seed,
        event_store=store,
        constructor=constructor,
        execution_id="exec_oracle",
        base_checkout=repo,
        worker_workspace=repo,
        runtime_label="codex",
        settings=settings,
        store_dir=tmp_path / "store",
    )
    assert state.package is not None
    assert [case.held_out for spec in state.package.oracles for case in spec.cases] == [
        False,
        True,
        False,
        True,
    ]
    authority = CheckPackageAuthority(state, settings, event_store=store, candidate_checkout=repo)
    authority.install(_executor(repo))
    (repo / "mathutils.py").write_text(BUGGY + BAD_MIX)
    ok = ACExecutionResult(
        ac_index=0, ac_content="c0", success=True, outcome=ACExecutionOutcome.SUCCEEDED
    )
    gated = await authority.gate(seed=seed, ac_index=0, result=ok)
    assert package_repair(gated)
    parallel = ParallelExecutionResult(
        results=(
            ok,
            ACExecutionResult(
                ac_index=1,
                ac_content="c1",
                success=True,
                typed_evidence=EvidenceRecord(data={"entry_points": [MIX_ENTRY]}),
            ),
            ACExecutionResult(ac_index=2, ac_content="c2", success=True),
        ),
        success_count=3,
        failure_count=0,
    )
    decided = await authority(seed=seed, execution_id="exec_oracle", parallel_result=parallel)
    assert not decided.all_succeeded

    stored = [path for path in (tmp_path / "store").rglob("*") if path.is_file()]
    assert any(path.parent.name == "packages" for path in stored)
    assert any(path.parent.name == "receipts" for path in stored)
    hits = [
        (str(path), token)
        for path in stored
        for token in secrets
        if found(token, path.read_bytes())
    ]
    assert hits == []
    events = await store.replay(BOUNDARY_AGGREGATE_TYPE, state.boundary_id)
    journal = json.dumps([event.data for event in events]).encode()
    assert [token for token in secrets if found(token, journal)] == []
    # The matcher does find a value where it would be leaked.
    assert found("4409", b'{"value": 4409}') and not found("4409", b"ab4409cd")
    # The record names the package by its id and keeps held-out cases as counts only.
    (record_path,) = (tmp_path / "store" / "packages").iterdir()
    record = json.loads(record_path.read_text())
    assert record["package_id"] == state.package.package_id
    assert "package_sha256" not in record
    oracles = record["package"]["oracles"]
    assert all("cases" not in spec for spec in oracles)
    held = [
        (spec["check_id"], spec["held_out_count"]) for spec in oracles if spec["held_out_count"]
    ]
    assert held == [("oracle_1", 1), ("oracle_2", 1)]


async def _the_outcome_summary_never_reads_an_unavailable_transcript_as_a_rejection_scenario(
    store: EventStore, repo: Path, tmp_path: Path
) -> CheckPackageAuthority:
    seed, authority = await _authority(store, repo, tmp_path)
    (repo / "mathutils.py").write_text(FIXED + GOOD_MIX)
    authority.install(_executor(repo))
    results = (
        _transcript_unavailable(0),
        replace(
            _transcript_unavailable(1),
            typed_evidence=EvidenceRecord(data={"entry_points": [MIX_ENTRY]}),
        ),
        _transcript_unavailable(2),
    )
    parallel = ParallelExecutionResult(results=results, success_count=3, failure_count=0)
    decided = await authority(seed=seed, execution_id="exec_oracle", parallel_result=parallel)
    assert decided.all_succeeded and authority.outcome.legacy_run_accepted is True
    return authority


async def test_the_outcome_summary_never_reads_an_unavailable_transcript_as_a_rejection(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    # the package passes and the legacy verifier had no transcript:
    # that is agreement with what the switch off decides (accept), never
    # package_accepted_over_legacy_reject.
    await _the_outcome_summary_never_reads_an_unavailable_transcript_as_a_rejection_scenario(
        store, repo, tmp_path
    )


async def test_a_legacy_rejection_of_a_legacy_decided_criterion_drives_the_retry(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    """The README criterion has no admitted check: its legacy rejection fails the attempt."""
    seed, authority = await _authority(store, repo, tmp_path)
    executor = _executor(repo)
    authority.install(executor)
    # No admitted oracle for this root: its prompt carries no entry_points request.
    assert 2 not in executor.check_package_interfaces
    prompts: list[dict[int, str]] = []

    async def fake_batch(**kwargs: Any) -> list[ACExecutionResult]:
        prompts.append(dict(kwargs.get("retry_prompts") or {}))
        first = len(prompts) == 1
        result = _legacy_rejected(2) if first else _legacy_accepted(2)
        return [replace(result, retry_attempt=len(prompts) - 1)]

    executor._execute_ac_batch = fake_batch  # type: ignore[method-assign]
    results = await _batch(executor, seed, [2])
    assert len(prompts) == 2 and results[0].success is True
    assert authority.gate.legacy_failures == 1
    retry = prompts[1][2]
    assert "### Check package counterexample" not in retry
    # The legacy verdict's own class, exactly as with the check package off.
    assert "### Prior failure classification\nEVIDENCE_FORM_MISMATCH\n" in retry
    assert "LEGACY_DECIDED" not in retry
    assert authority.gate.log == []  # no package verification ran for it
    # A settlement path handing the same attempt to the gate again is not recounted.
    again = await authority.gate(seed=seed, ac_index=2, result=_legacy_rejected(2))
    assert again.success is False and authority.gate.legacy_failures == 1


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX venv layout")
async def test_a_replaced_interpreter_leaves_every_check_undecided_and_never_runs(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    """M6: the interpreter is pinned before the worker; a swapped binary runs nothing."""
    python = repo / ".venv" / "bin" / "python3"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    seed, authority = await _authority(store, repo, tmp_path)
    assert authority.state.interpreter.source == "project_venv"
    authority.install(_executor(repo))
    (repo / "mathutils.py").write_text(FIXED + GOOD_MIX)
    marker = tmp_path / "replacement-ran"
    python.unlink()
    python.write_text(f'#!/bin/sh\ntouch "{marker}"\nexec "{sys.executable}" "$@"\n')
    python.chmod(0o755)
    attempt = _legacy_accepted(0)
    gated = await authority.gate(seed=seed, ac_index=0, result=attempt)
    assert gated is attempt  # undecided: no repair signal, no verdict
    parallel = ParallelExecutionResult(
        results=(_legacy_accepted(0), _legacy_accepted(1), _legacy_accepted(2)),
        success_count=3,
        failure_count=0,
    )
    decided = await authority(seed=seed, execution_id="exec_oracle", parallel_result=parallel)
    keys = seed_criterion_keys(seed)
    verdicts = authority.outcome.verdict.verdicts
    assert (verdicts[keys[0]].status, verdicts[keys[0]].reason) == (
        PackageCriterionStatus.INDETERMINATE,
        "interpreter_changed",
    )
    assert not decided.results[0].success
    assert not marker.exists()


HARDCODED = (
    "def clamp(value, low, high):\n    if value == 15:\n        return 10\n    return value\n"
)


async def test_a_visible_only_oracle_pass_never_overrides_a_legacy_rejection(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    """B2 review probe: an oracle holding only the Seed's example cannot verify a pass.

    The worker hard-codes the stated example. An oracle made of that example
    alone could only confirm what the specification already says, so the
    reply parser refuses it (every oracle carries a held-out case); the
    criterion is left without an admitted check and the legacy verifier's
    rejection decides it.
    """
    import copy

    reply = copy.deepcopy(REPLY)
    reply["oracles"][0]["cases"] = reply["oracles"][0]["cases"][:1]  # the stated case only
    seed = _seed()
    with pytest.raises(ReplyError) as refused:
        package_from_reply(reply, seed, input_digest="1" * 64, generator="fake")
    assert refused.value.code is ReplyFailure.ORACLE_WITHOUT_HELD_OUT_CASE
    # What the product keeps: criterion 1 unchecked, the others as constructed.
    kept = copy.deepcopy(REPLY)
    kept["oracles"] = kept["oracles"][1:]
    kept.setdefault("uncovered", []).append({"criterion": 1})

    class _StatedOnlyRefused:
        async def construct(self, seed: Seed, base: Path, *, feedback=()) -> ConstructionOutcome:
            package = package_from_reply(kept, seed, input_digest="1" * 64, generator="fake")
            return ConstructionOutcome(package, None, "1" * 64, "fake")

    settings = CheckPackageSettings(enabled=True)
    state = await prepare_check_package(
        seed,
        event_store=store,
        constructor=_StatedOnlyRefused(),
        execution_id="exec_probe",
        base_checkout=repo,
        worker_workspace=repo,
        runtime_label="codex",
        settings=settings,
        store_dir=tmp_path / "store",
    )
    assert state.admitted
    authority = CheckPackageAuthority(state, settings, event_store=store, candidate_checkout=repo)
    authority.install(_executor(repo))
    (repo / "mathutils.py").write_text(HARDCODED + GOOD_MIX)
    parallel = ParallelExecutionResult(
        results=(_legacy_rejected(0), _legacy_accepted(1), _legacy_accepted(2)),
        success_count=3,
        failure_count=0,
    )
    decided = await authority(seed=seed, execution_id="exec_probe", parallel_result=parallel)
    first = authority.outcome.reconciliation.decisions[0]
    assert first.package_status is PackageCriterionStatus.UNCOVERED
    assert first.governed_by.value == "existing_verifier" and not first.accepted
    assert decided.results[0].outcome is ACExecutionOutcome.FAILED
    assert not decided.all_succeeded


def _another_run(seed: Seed, foreign: str) -> tuple[Seed, str]:
    """A call for another run: another execution id, or another Seed with the same criteria."""
    if foreign == "execution_id":
        return seed, "exec_foreign"
    other = seed.model_copy(update={"goal": f"{seed.goal} (another run)"})
    assert seed_criterion_keys(other) == seed_criterion_keys(seed)
    return other, "exec_oracle"


@pytest.mark.parametrize("foreign", ["execution_id", "seed_digest"])
async def test_a_terminal_call_for_another_run_never_uses_this_runs_package(
    store: EventStore, repo: Path, tmp_path: Path, foreign: str
) -> None:
    from ouroboros.boundary.events import ACCEPTANCE_RECONCILED, BOUNDARY_AGGREGATE_TYPE

    seed, authority = await _authority(store, repo, tmp_path)
    (repo / "mathutils.py").write_text(FIXED + GOOD_MIX)
    authority.install(_executor(repo))
    parallel = ParallelExecutionResult(
        results=(_legacy_rejected(0), _legacy_rejected(1, entry=MIX_ENTRY), _legacy_rejected(2)),
        success_count=3,
        failure_count=0,
    )
    other_seed, other_execution = _another_run(seed, foreign)

    refused = await authority(
        seed=other_seed, execution_id=other_execution, parallel_result=parallel
    )

    # The package would accept criterion 0 over its legacy rejection for this
    # run; for another run it decides nothing: covered criteria are undecided,
    # the uncovered one is the legacy verifier's (rejected).
    assert [r.outcome for r in refused.results] == [ACExecutionOutcome.FAILED] * 3
    assert f"run_mismatch:{foreign}" in (refused.results[0].error or "")
    # Nothing is recorded in this run's journal and its one decision is unused.
    assert authority.outcome is None
    events = await store.replay(BOUNDARY_AGGREGATE_TYPE, authority.state.boundary_id)
    assert not [event for event in events if event.type == ACCEPTANCE_RECONCILED]

    decided = await authority(seed=seed, execution_id="exec_oracle", parallel_result=parallel)
    assert authority.outcome is not None
    assert decided.results[0].outcome is ACExecutionOutcome.SUCCEEDED


@pytest.mark.parametrize("foreign", ["execution_id", "seed_digest"])
async def test_the_gate_never_judges_another_runs_attempt(
    store: EventStore, repo: Path, tmp_path: Path, foreign: str
) -> None:
    seed, authority = await _authority(store, repo, tmp_path)
    (repo / "mathutils.py").write_text(FIXED + GOOD_MIX)
    authority.install(_executor(repo))
    other_seed, other_execution = _another_run(seed, foreign)
    attempt = _legacy_rejected(0)

    foreign_attempt = await authority.gate(
        seed=other_seed, ac_index=0, result=attempt, execution_id=other_execution
    )

    # The legacy rejection the executor made advisory decides again; the
    # package ran nothing for the other run's attempt.
    assert foreign_attempt.success is False
    assert legacy_owned(foreign_attempt) and package_failure_class(foreign_attempt) is None
    assert authority.gate.log == []

    own_attempt = await authority.gate(
        seed=seed, ac_index=0, result=attempt, execution_id="exec_oracle"
    )
    assert own_attempt.success is True and len(authority.gate.log) == 1


# The #2463 review probe: code the candidate runs at import time writes the
# target's report itself. It reads the nonce from the process arguments, finds
# the frame pipe among its descriptors, reports "resolved", reads the case the
# controller sends, and reports the stated example's value for every case,
# while the bound function itself returns 999.
FORGER = """\
import json, os, stat, sys

def _pipes():
    for fd in range(3, 64):
        try:
            if stat.S_ISFIFO(os.fstat(fd).st_mode):
                yield fd
        except OSError:
            continue

def _say(payload):
    line = ("\\n" + sys.argv[2] + " " + json.dumps(payload) + "\\n").encode()
    for fd in _pipes():
        try:
            os.write(fd, line)
        except OSError:
            pass

if len(sys.argv) > 2 and sys.argv[1] == "target":
    _say({"phase": "resolved", "resolve": "ok", "detail": ""})
    data = b""
    while True:
        chunk = os.read(0, 65536)
        if not chunk:
            break
        data += chunk
    call = json.loads(data)
    _say({"phase": "result", "entry": {"case_id": call["case_id"], "outcome": "returned",
          "repr": "10", "value": 10, "encodable": True}})
    os._exit(0)

def clamp(value, low, high):
    return 999
"""


async def test_a_forged_report_never_makes_a_verified_pass(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    """#2463 review probe: a report the candidate forges is evidence, never authority.

    The forger passes the stated case (its expected value is in the
    specification) without calling ``clamp``. The held-out case's inputs
    reach it only in the terminal verification, and a forger that does not
    compute the rule reports the wrong value there: the criterion fails, so a
    forged report cannot become a verified pass, even over a legacy acceptance.
    """
    seed, authority = await _authority(store, repo, tmp_path)
    authority.install(_executor(repo))
    (repo / "mathutils.py").write_text(FORGER + GOOD_MIX)
    parallel = ParallelExecutionResult(
        results=(_legacy_accepted(0), _legacy_accepted(1), _legacy_accepted(2)),
        success_count=3,
        failure_count=0,
    )
    decided = await authority(seed=seed, execution_id="exec_oracle", parallel_result=parallel)
    verdict = authority.outcome.verdict.verdicts[seed_criterion_keys(seed)[0]]
    cases = {
        case.case_id: case.passed
        for case in authority.outcome.verdict.oracle_results["oracle_1"].cases
    }
    # The forged report matches the stated case and nothing else.
    assert cases == {"c1": True, "c2": False}
    assert verdict.status is PackageCriterionStatus.FAIL
    first = authority.outcome.reconciliation.decisions[0]
    assert not first.accepted and first.governed_by.value == "check_package"
    assert decided.results[0].outcome is ACExecutionOutcome.FAILED
    assert not decided.all_succeeded


@pytest.mark.parametrize("trap", ["file000", "dir000", "gitdir000", "fifo"])
async def test_a_workspace_the_controller_cannot_read_never_lifts_a_package_fail(
    store: EventStore, repo: Path, tmp_path: Path, trap: str
) -> None:
    """Independent review probe B1: errors the worker can trigger grant nothing."""
    import os

    if trap != "fifo" and hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("mode 000 does not stop root (the injected read error covers it)")
    seed, authority = await _authority(store, repo, tmp_path)
    (repo / "mathutils.py").write_text(BUGGY + GOOD_MIX)
    authority.install(_executor(repo))
    ok = ACExecutionResult(
        ac_index=0, ac_content="c0", success=True, outcome=ACExecutionOutcome.SUCCEEDED
    )
    gated = await authority.gate(seed=seed, ac_index=0, result=ok)
    assert gated.outcome is ACExecutionOutcome.FAILED  # the package saw the bug
    target = {
        "file000": repo / "notes.bin",
        "dir000": repo / "d",
        "gitdir000": repo / ".git" / "x",
        "fifo": repo / "p",
    }[trap]
    if trap == "file000":
        target.write_text("x")
    elif trap == "fifo":
        os.mkfifo(target)
    else:
        target.mkdir(parents=True)
    if trap != "fifo":
        os.chmod(target, 0)
    try:
        parallel = ParallelExecutionResult(
            results=(gated, _legacy_accepted(1, entry=MIX_ENTRY), _legacy_accepted(2)),
            success_count=2,
            failure_count=1,
        )
        decided = await authority(seed=seed, execution_id="exec_oracle", parallel_result=parallel)
    finally:
        if trap != "fifo":
            os.chmod(target, 0o700)
    first = authority.outcome.reconciliation.decisions[0]
    assert not first.accepted and decided.results[0].outcome is ACExecutionOutcome.FAILED
    assert first.package_status is not PackageCriterionStatus.PASS
    assert not decided.all_succeeded


def _leaky(leak: Path) -> str:
    """``clamp`` that records every input it gets; it stalls on any input but the stated one."""
    return (
        "import json, time\n"
        "def clamp(value, low, high):\n"
        f"    with open({str(leak)!r}, 'a') as f:\n"
        "        f.write(json.dumps([value, low, high]) + '\\n')\n"
        "    if (value, low, high) != (15, 0, 10):\n"
        "        time.sleep(10)\n"
        "    return max(low, min(high, value))\n"
    )


async def test_an_interrupted_terminal_verification_never_reuses_its_held_out_cases(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    """Independent review probe H1: a cancelled final verification forgets the held-out cases."""
    import asyncio

    from ouroboros.boundary.run_wiring import live_state

    seed, authority = await _authority(store, repo, tmp_path)
    leak = tmp_path / "leak.jsonl"
    (repo / "mathutils.py").write_text(_leaky(leak) + GOOD_MIX)
    authority.install(_executor(repo))
    parallel = ParallelExecutionResult(
        results=(_legacy_accepted(0), _legacy_accepted(1, entry=MIX_ENTRY), _legacy_accepted(2)),
        success_count=3,
        failure_count=0,
    )
    assert live_state("exec_oracle") is authority.state
    task = asyncio.create_task(
        authority(seed=seed, execution_id="exec_oracle", parallel_result=parallel)
    )
    for _ in range(400):
        await asyncio.sleep(0.05)
        if leak.exists() and len(leak.read_text().splitlines()) > 1:
            break
    # A held-out input reached the candidate: the final verification is running.
    assert len(leak.read_text().splitlines()) > 1
    task.cancel()  # for example the job is cancelled during the final verification
    with pytest.raises(asyncio.CancelledError):
        await task
    assert authority.outcome is None
    # Nothing in this process can decide with the held-out cases again: the
    # live registry (what a same-process resume reads) forgot them ...
    assert live_state("exec_oracle") is None
    seen = leak.read_text()
    # ... and a second terminal call runs no check: covered criteria stay undecided.
    decided = await authority(seed=seed, execution_id="exec_oracle", parallel_result=parallel)
    assert leak.read_text() == seen
    assert authority.outcome is not None
    assert authority.outcome.error == "terminal_interrupted"
    decisions = authority.outcome.reconciliation.decisions
    assert [(d.package_status.value, d.accepted) for d in decisions][:2] == [
        ("indeterminate", False),
        ("indeterminate", False),
    ]
    assert not decided.all_succeeded


async def test_a_cross_harness_alternate_attempt_is_judged_afresh(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    """Independent review probe (gate memo): an alternate attempt shares only its retry number.

    The cross-harness alternate of an attempt carries the same criterion and
    retry number; the decision of the attempt it replaces must not be
    replayed onto it, on a legacy-decided criterion or a covered one.
    """
    seed, authority = await _authority(store, repo, tmp_path)
    (repo / "mathutils.py").write_text(FIXED + GOOD_MIX)
    authority.install(_executor(repo))
    # Legacy-decided criterion: the legacy verifier rejects, then accepts the alternate.
    first = await authority.gate(
        seed=seed, ac_index=2, result=replace(_legacy_rejected(2), retry_attempt=2)
    )
    alternate = await authority.gate(
        seed=seed, ac_index=2, result=replace(_legacy_accepted(2), retry_attempt=2)
    )
    assert first.outcome is ACExecutionOutcome.FAILED
    assert alternate.outcome is ACExecutionOutcome.SUCCEEDED
    assert authority.gate.legacy_failures == 1
    # Covered criterion: a wrong declaration fails, the alternate's right one passes.
    wrong = {"symbol": "mathutils.blend", "arg_map": MIX_ENTRY["arg_map"]}
    failed = await authority.gate(
        seed=seed, ac_index=1, result=replace(_legacy_rejected(1, entry=wrong), retry_attempt=1)
    )
    fixed = await authority.gate(
        seed=seed,
        ac_index=1,
        result=replace(_legacy_rejected(1, entry=MIX_ENTRY), retry_attempt=1),
    )
    assert failed.success is False and package_repair(failed)
    assert fixed.success is True and package_repair(fixed) is None


async def test_a_seed_that_fails_to_read_after_the_run_check_cannot_escape_the_decision(
    store: EventStore, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Independent review note: the terminal decision reads the Seed's keys only once."""
    from ouroboros.boundary import authority as authority_module

    seed, authority = await _authority(store, repo, tmp_path)
    (repo / "mathutils.py").write_text(FIXED + GOOD_MIX)
    authority.install(_executor(repo))
    reads: list[int] = []
    real = authority_module.seed_criterion_keys

    def flaky(value: Any) -> tuple[str, ...]:
        reads.append(1)
        if len(reads) > 1:
            raise RuntimeError("seed became unreadable")
        return real(value)

    monkeypatch.setattr(authority_module, "seed_criterion_keys", flaky)
    parallel = ParallelExecutionResult(
        results=(_legacy_accepted(0), _legacy_accepted(1, entry=MIX_ENTRY), _legacy_rejected(2)),
        success_count=3,
        failure_count=0,
    )
    decided = await authority(seed=seed, execution_id="exec_oracle", parallel_result=parallel)
    assert authority.outcome is not None and authority.outcome.reconciliation is not None
    decisions = authority.outcome.reconciliation.decisions
    assert decisions[2].legacy_decided and not decisions[2].accepted
    assert authority.outcome.error is None and not decided.all_succeeded


async def test_a_legacy_blocked_verdict_on_an_uncovered_criterion_keeps_its_class(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    """Review probe (#2466): the handed-back legacy class is the legacy verdict's own."""
    from ouroboros.orchestrator.failure_taxonomy import FailureClass
    from ouroboros.orchestrator.retry_hints import failure_class_for_result

    seed, authority = await _authority(store, repo, tmp_path)
    authority.install(_executor(repo))
    blocked = replace(
        _legacy_rejected(2),
        atomic_verifier_verdict=VerifierVerdict(
            passed=False, reasons=("no access",), failure_class="BLOCKED"
        ),
    )
    gated = await authority.gate(seed=seed, ac_index=2, result=blocked)
    assert gated.success is False and legacy_owned(gated)
    assert FailureClass(failure_class_for_result(gated)) is FailureClass.BLOCKED


async def test_an_oracle_whose_only_held_out_case_passes_on_the_base_decides_nothing(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    """A held-out case the buggy base already passes is no evidence of a fix.

    The earlier shape of this fixture: oracle_1's only held-out case,
    ``clamp(-3, -2, 4) == -2``, passes on the buggy base. Admission excludes
    that oracle (``held_out_not_discriminating``), so its criterion has no
    admitted check: the legacy verifier decides it, even when the finished
    workspace is correct, and the package can never pass it.
    """
    import copy

    reply: dict[str, Any] = copy.deepcopy(REPLY)
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
    settings = CheckPackageSettings(enabled=True)
    state = await prepare_check_package(
        seed,
        event_store=store,
        constructor=constructor,
        execution_id="exec_oracle",
        base_checkout=repo,
        worker_workspace=repo,
        runtime_label="codex",
        settings=settings,
        store_dir=tmp_path / "store",
    )
    assert state.admitted and state.admission is not None
    assert state.admission.excluded_checks == {"oracle_1": "held_out_not_discriminating"}
    authority = CheckPackageAuthority(state, settings, event_store=store, candidate_checkout=repo)
    executor = _executor(repo)
    authority.install(executor)
    keys = seed_criterion_keys(seed)
    assert keys[0] in authority.legacy_decided_keys()
    assert 0 not in executor.check_package_interfaces  # no entry_points request for it
    (repo / "mathutils.py").write_text(FIXED + GOOD_MIX)
    # The gate runs no package check for it: the legacy rejection decides the attempt.
    gated = await authority.gate(seed=seed, ac_index=0, result=_legacy_rejected(0))
    assert gated.success is False and legacy_owned(gated) and package_repair(gated) is None
    assert authority.gate.log == []
    parallel = ParallelExecutionResult(
        results=(_legacy_rejected(0), _legacy_accepted(1, entry=MIX_ENTRY), _legacy_accepted(2)),
        success_count=3,
        failure_count=0,
    )
    decided = await authority(seed=seed, execution_id="exec_oracle", parallel_result=parallel)
    verdicts = authority.outcome.verdict.verdicts
    assert verdicts[keys[0]].status is not PackageCriterionStatus.PASS
    first = authority.outcome.reconciliation.decisions[0]
    assert first.legacy_decided and not first.accepted
    assert decided.results[0].outcome is ACExecutionOutcome.FAILED
    assert not decided.all_succeeded
