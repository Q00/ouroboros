"""Tests for PR-V verify-by-default: V1 gate, retry, lateral, trust leaks."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.core.seed import (
    AcceptanceCriterionSpec,
    OntologySchema,
    Seed,
    SeedMetadata,
)
from ouroboros.orchestrator.parallel_executor import (
    ACExecutionOutcome,
    ACExecutionResult,
    ParallelACExecutor,
    _complete_sibling_acs_from_evidence,
)
from ouroboros.orchestrator.verifier import VerifierVerdict


class _StubAdapter:
    """Minimal adapter satisfying the executor constructor + verify gate cwd."""

    def __init__(self, working_directory: str) -> None:
        self.runtime_backend = "claude"
        self.self_governs_rate_limit = True
        self.working_directory = working_directory
        self.permission_mode = "acceptEdits"


def _make_executor(
    *,
    working_directory: str = "/workspace",
    run_verify_commands: bool = True,
    ac_retry_attempts: int = 0,
    verify_command_timeout_seconds: int = 30,
) -> ParallelACExecutor:
    return ParallelACExecutor(
        adapter=_StubAdapter(working_directory),
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
        run_verify_commands=run_verify_commands,
        ac_retry_attempts=ac_retry_attempts,
        verify_command_timeout_seconds=verify_command_timeout_seconds,
    )


def _seed_with_specs(*specs: AcceptanceCriterionSpec | str) -> Seed:
    return Seed(
        goal="verify-by-default",
        acceptance_criteria=specs,
        ontology_schema=OntologySchema(name="n", description="d"),
        metadata=SeedMetadata(ambiguity_score=0.05),
    )


# ---------------------------------------------------------------------------
# V1 gate — _run_ac_verify_gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_gate_passes_on_exit_zero(tmp_path: Any) -> None:
    executor = _make_executor(working_directory=str(tmp_path))
    spec = AcceptanceCriterionSpec(description="ok", verify_command="exit 0")

    outcome = await executor._run_ac_verify_gate(spec=spec, cwd=str(tmp_path))

    assert outcome.passed is True
    assert outcome.reason is None


@pytest.mark.asyncio
async def test_verify_gate_fails_on_nonzero_exit(tmp_path: Any) -> None:
    executor = _make_executor(working_directory=str(tmp_path))
    spec = AcceptanceCriterionSpec(description="bad", verify_command="exit 3")

    outcome = await executor._run_ac_verify_gate(spec=spec, cwd=str(tmp_path))

    assert outcome.passed is False
    assert "status 3" in (outcome.reason or "")


@pytest.mark.asyncio
async def test_verify_gate_output_assertion_match_and_mismatch(tmp_path: Any) -> None:
    executor = _make_executor(working_directory=str(tmp_path))
    match_spec = AcceptanceCriterionSpec(
        description="doc",
        verify_command="printf 'BUILD SUCCESS'",
        output_assertion="SUCCESS",
    )
    mismatch_spec = AcceptanceCriterionSpec(
        description="doc",
        verify_command="printf 'BUILD SUCCESS'",
        output_assertion="FAILURE",
    )

    assert (await executor._run_ac_verify_gate(spec=match_spec, cwd=str(tmp_path))).passed is True
    mismatch = await executor._run_ac_verify_gate(spec=mismatch_spec, cwd=str(tmp_path))
    assert mismatch.passed is False
    assert "output_assertion" in (mismatch.reason or "")


# ---------------------------------------------------------------------------
# V1 gate integration — _apply_verify_gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_verify_gate_flips_success_to_failed(tmp_path: Any) -> None:
    executor = _make_executor(working_directory=str(tmp_path))
    seed = _seed_with_specs(AcceptanceCriterionSpec(description="ac", verify_command="exit 1"))
    result = ACExecutionResult(ac_index=0, ac_content="ac", success=True)

    gated = await executor._apply_verify_gate(
        seed=seed, ac_index=0, result=result, session_id="s", execution_id="e"
    )

    assert gated.success is False
    assert gated.outcome == ACExecutionOutcome.FAILED
    assert "Verify gate failed" in (gated.error or "")
    assert gated.atomic_verifier_verdict is not None
    assert gated.atomic_verifier_verdict.failure_class == "EVIDENCE_MISSING"


@pytest.mark.asyncio
async def test_early_stop_alt_success_still_verify_gated(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An alternate 'success' taken on the retry early-stop path must be verify-gated.

    Regression: the early-stop cross-harness hook replaces the stored result with
    the alternate's, and the alternate runs via ``_execute_single_ac``, which has
    no seed-level success contract. A failing ``verify_command`` must still flip an
    alternate ``success=True`` to FAILED, exactly like the same-runtime path — the
    alternate must not bypass the verify-by-default contract.
    """
    from ouroboros.orchestrator import cross_harness_redispatch as chr

    executor = ParallelACExecutor(
        adapter=_StubAdapter(str(tmp_path)),
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
        run_verify_commands=True,
        ac_retry_attempts=2,
        cross_harness_redispatch=True,
    )
    seed = _seed_with_specs(AcceptanceCriterionSpec(description="ac", verify_command="exit 1"))
    monkeypatch.setattr(chr, "pick_alternative_runtime", lambda *_a, **_k: "codex")

    fab = VerifierVerdict(
        passed=False,
        reasons=("fabricated a file",),
        failure_class="FABRICATION_SUSPECTED",
    )

    async def fake_batch(**kwargs: Any) -> list[ACExecutionResult]:
        # Same eligible failure class on the initial attempt and retry 1 so the
        # loop early-stops before the counter cap and reaches the alt-harness hook.
        return [
            ACExecutionResult(
                ac_index=idx,
                ac_content="ac",
                success=False,
                error="fabricated",
                outcome=ACExecutionOutcome.FAILED,
                atomic_verifier_verdict=fab,
            )
            for idx in kwargs["batch_indices"]
        ]

    executor._execute_ac_batch = fake_batch  # type: ignore[method-assign]

    async def alt_reports_success(backend: str, **kwargs: Any) -> ACExecutionResult:
        # The alternate backend claims success without honoring the contract.
        return ACExecutionResult(ac_index=0, ac_content="ac", success=True, session_id="alt-sess")

    executor._run_single_ac_on_backend = alt_reports_success  # type: ignore[method-assign]

    results = await executor._run_batch_with_verify_and_retry(
        seed=seed,
        batch_executable=[0],
        session_id="s",
        execution_id="e",
        tools=["Read"],
        tool_catalog=None,
        system_prompt="system",
        level_contexts=[],
        ac_retry_attempts={0: 0},
        execution_counters=None,
    )

    # The alternate reported success, but 'verify_command: exit 1' must gate it
    # to FAILED just like a same-runtime success — no contract bypass.
    assert isinstance(results[0], ACExecutionResult)
    assert results[0].success is False
    assert results[0].outcome == ACExecutionOutcome.FAILED
    assert "Verify gate failed" in (results[0].error or "")


@pytest.mark.asyncio
async def test_apply_verify_gate_contract_less_is_noop(tmp_path: Any) -> None:
    """A description-only AC (no verify_command) is byte-identical to today."""
    executor = _make_executor(working_directory=str(tmp_path))
    seed = _seed_with_specs("plain string AC")
    result = ACExecutionResult(ac_index=0, ac_content="plain string AC", success=True)

    gated = await executor._apply_verify_gate(
        seed=seed, ac_index=0, result=result, session_id="s", execution_id="e"
    )

    assert gated is result


@pytest.mark.asyncio
async def test_apply_verify_gate_disabled_is_noop(tmp_path: Any) -> None:
    executor = _make_executor(working_directory=str(tmp_path), run_verify_commands=False)
    seed = _seed_with_specs(AcceptanceCriterionSpec(description="ac", verify_command="exit 1"))
    result = ACExecutionResult(ac_index=0, ac_content="ac", success=True)

    gated = await executor._apply_verify_gate(
        seed=seed, ac_index=0, result=result, session_id="s", execution_id="e"
    )

    assert gated is result


@pytest.mark.asyncio
async def test_apply_verify_gate_skips_already_failed(tmp_path: Any) -> None:
    """No double-fail: an already-failed AC is not re-gated (one root cause)."""
    executor = _make_executor(working_directory=str(tmp_path))
    seed = _seed_with_specs(AcceptanceCriterionSpec(description="ac", verify_command="exit 1"))
    result = ACExecutionResult(ac_index=0, ac_content="ac", success=False, error="already failed")

    gated = await executor._apply_verify_gate(
        seed=seed, ac_index=0, result=result, session_id="s", execution_id="e"
    )

    assert gated is result


# ---------------------------------------------------------------------------
# V3 retry — _run_batch_with_verify_and_retry
# ---------------------------------------------------------------------------


def _fail(ac_index: int, failure_class: str) -> ACExecutionResult:
    return ACExecutionResult(
        ac_index=ac_index,
        ac_content="ac",
        success=False,
        error="boom",
        outcome=ACExecutionOutcome.FAILED,
        atomic_verifier_verdict=VerifierVerdict(
            passed=False, reasons=("boom",), failure_class=failure_class
        ),
    )


def _ok(ac_index: int) -> ACExecutionResult:
    return ACExecutionResult(ac_index=ac_index, ac_content="ac", success=True)


async def _run_retry(executor: ParallelACExecutor, seed: Seed) -> list[Any]:
    return await executor._run_batch_with_verify_and_retry(
        seed=seed,
        batch_executable=[0],
        session_id="s",
        execution_id="e",
        tools=[],
        tool_catalog=None,
        system_prompt="sys",
        level_contexts=[],
        ac_retry_attempts={0: 0},
        execution_counters=None,
    )


@pytest.mark.asyncio
async def test_retry_redispatches_and_exhausts(tmp_path: Any) -> None:
    executor = _make_executor(
        working_directory=str(tmp_path), run_verify_commands=False, ac_retry_attempts=2
    )
    seed = _seed_with_specs("ac")
    ac_retry_attempts = {0: 0}
    calls: list[list[int]] = []

    async def fake_batch(**kwargs: Any) -> list[ACExecutionResult]:
        calls.append(list(kwargs["batch_indices"]))
        # Distinct classes each attempt so early-stop does not trigger.
        cls = ["EVIDENCE_MISSING", "STALL", "SCOPE_CREEP"][len(calls) - 1]
        return [_fail(0, cls)]

    executor._execute_ac_batch = fake_batch  # type: ignore[method-assign]

    results = await executor._run_batch_with_verify_and_retry(
        seed=seed,
        batch_executable=[0],
        session_id="s",
        execution_id="e",
        tools=[],
        tool_catalog=None,
        system_prompt="sys",
        level_contexts=[],
        ac_retry_attempts=ac_retry_attempts,
        execution_counters=None,
    )

    # initial + 2 retries = 3 dispatches; counter incremented to the limit.
    assert calls == [[0], [0], [0]]
    assert ac_retry_attempts[0] == 2
    assert results[0].success is False


@pytest.mark.asyncio
async def test_retry_early_stop_on_identical_failure_class(tmp_path: Any) -> None:
    executor = _make_executor(
        working_directory=str(tmp_path), run_verify_commands=False, ac_retry_attempts=2
    )
    seed = _seed_with_specs("ac")
    ac_retry_attempts = {0: 0}
    calls: list[list[int]] = []

    async def fake_batch(**kwargs: Any) -> list[ACExecutionResult]:
        calls.append(list(kwargs["batch_indices"]))
        return [_fail(0, "EVIDENCE_MISSING")]  # identical class every time

    executor._execute_ac_batch = fake_batch  # type: ignore[method-assign]

    await executor._run_batch_with_verify_and_retry(
        seed=seed,
        batch_executable=[0],
        session_id="s",
        execution_id="e",
        tools=[],
        tool_catalog=None,
        system_prompt="sys",
        level_contexts=[],
        ac_retry_attempts=ac_retry_attempts,
        execution_counters=None,
    )

    # Initial dispatch + a single retry that returns the identical class stops
    # early rather than burning the last attempt (2 dispatches, not 3).
    assert calls == [[0], [0]]
    assert ac_retry_attempts[0] == 1


@pytest.mark.asyncio
async def test_retry_succeeds_before_dependents(tmp_path: Any) -> None:
    executor = _make_executor(
        working_directory=str(tmp_path), run_verify_commands=False, ac_retry_attempts=2
    )
    seed = _seed_with_specs("ac")
    ac_retry_attempts = {0: 0}
    calls: list[list[int]] = []

    async def fake_batch(**kwargs: Any) -> list[ACExecutionResult]:
        calls.append(list(kwargs["batch_indices"]))
        return [_fail(0, "EVIDENCE_MISSING")] if len(calls) == 1 else [_ok(0)]

    executor._execute_ac_batch = fake_batch  # type: ignore[method-assign]

    results = await executor._run_batch_with_verify_and_retry(
        seed=seed,
        batch_executable=[0],
        session_id="s",
        execution_id="e",
        tools=[],
        tool_catalog=None,
        system_prompt="sys",
        level_contexts=[],
        ac_retry_attempts=ac_retry_attempts,
        execution_counters=None,
    )

    assert calls == [[0], [0]]
    assert results[0].success is True


@pytest.mark.asyncio
async def test_no_retry_when_attempts_zero(tmp_path: Any) -> None:
    executor = _make_executor(
        working_directory=str(tmp_path), run_verify_commands=False, ac_retry_attempts=0
    )
    seed = _seed_with_specs("ac")
    calls: list[list[int]] = []

    async def fake_batch(**kwargs: Any) -> list[ACExecutionResult]:
        calls.append(list(kwargs["batch_indices"]))
        return [_fail(0, "EVIDENCE_MISSING")]

    executor._execute_ac_batch = fake_batch  # type: ignore[method-assign]

    await _run_retry(executor, seed)

    assert calls == [[0]]


# ---------------------------------------------------------------------------
# V4 lateral directive — _build_ac_retry_prompt
# ---------------------------------------------------------------------------


def test_retry_prompt_final_attempt_carries_lateral_directive() -> None:
    executor = _make_executor()
    result = _fail(0, "EVIDENCE_MISSING")

    final = executor._build_ac_retry_prompt(
        result=result, ac_content="build the thing", is_final_attempt=True
    )
    interim = executor._build_ac_retry_prompt(
        result=result, ac_content="build the thing", is_final_attempt=False
    )

    assert "Change of Approach" in final
    assert "EVIDENCE_MISSING" in final
    assert "Change of Approach" not in interim


# ---------------------------------------------------------------------------
# V4 trust leaks — sibling flip gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_sibling_flip_gated_out_blocks_failing_contract(tmp_path: Any) -> None:
    executor = _make_executor(working_directory=str(tmp_path))
    seed = _seed_with_specs(
        "sibling did work",
        AcceptanceCriterionSpec(description="contract", verify_command="exit 1"),
        AcceptanceCriterionSpec(description="passing", verify_command="exit 0"),
        "plain",
    )
    level_results = [
        ACExecutionResult(ac_index=0, ac_content="sibling did work", success=True),
        ACExecutionResult(
            ac_index=1, ac_content="contract", success=False, outcome=ACExecutionOutcome.FAILED
        ),
        ACExecutionResult(
            ac_index=2, ac_content="passing", success=False, outcome=ACExecutionOutcome.FAILED
        ),
        ACExecutionResult(
            ac_index=3, ac_content="plain", success=False, outcome=ACExecutionOutcome.FAILED
        ),
    ]

    gated = await executor._compute_sibling_flip_gated_out(
        seed=seed, level_results=level_results, session_id="s", execution_id="e"
    )

    # AC 1's verify fails → gated out; AC 2 passes → allowed; AC 3 has no
    # contract → never gated.
    assert gated == frozenset({1})


def test_sibling_flip_respects_gated_out(tmp_path: Any) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_hello_auto.py").write_text("def test_hello(): pass\n")
    from ouroboros.orchestrator.adapter import AgentMessage, RuntimeHandle
    from ouroboros.orchestrator.evidence_schema import EvidenceRecord

    success = ACExecutionResult(
        ac_index=0,
        ac_content="`hello_auto.py` defines `hello_auto()`.",
        success=True,
        messages=(
            AgentMessage(
                type="tool_use",
                content="write test",
                tool_name="Write",
                data={"tool_input": {"file_path": "tests/test_hello_auto.py"}},
            ),
        ),
        typed_evidence=EvidenceRecord(data={"files_touched": ["tests/test_hello_auto.py"]}),
        runtime_handle=RuntimeHandle(backend="codex_cli", cwd=str(tmp_path)),
    )
    failed = ACExecutionResult(
        ac_index=1,
        ac_content="`tests/test_hello_auto.py` exists.",
        success=False,
        error="not done separately",
        outcome=ACExecutionOutcome.FAILED,
    )

    # Without gating, the failed AC is flipped to satisfied by sibling evidence.
    _, _, _, open_results = _complete_sibling_acs_from_evidence(
        level_results=[success, failed],
        ac_statuses={0: "completed", 1: "failed"},
        failed_indices={1},
        completed_count=1,
        level_success=1,
        level_failed=1,
    )
    assert open_results[1].outcome == ACExecutionOutcome.SATISFIED_EXTERNALLY

    # With AC 1 gated out (its own verify_command did not pass), it stays FAILED.
    _, _, _, gated_results = _complete_sibling_acs_from_evidence(
        level_results=[success, failed],
        ac_statuses={0: "completed", 1: "failed"},
        failed_indices={1},
        completed_count=1,
        level_success=1,
        level_failed=1,
        flip_gated_out=frozenset({1}),
    )
    assert gated_results[1].outcome == ACExecutionOutcome.FAILED


# ---------------------------------------------------------------------------
# V4 trust leaks — --skip-completed gate + verification_status stamp
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skip_completed_stamps_assumed_for_contract_less(tmp_path: Any) -> None:
    from ouroboros.orchestrator.dependency_analyzer import ACNode, DependencyGraph

    seed = _seed_with_specs("plain AC")
    executor = _make_executor(working_directory=str(tmp_path))
    executor._execute_ac_batch = AsyncMock(return_value=[])  # type: ignore[method-assign]
    graph = DependencyGraph(
        nodes=(ACNode(index=0, content="plain AC", depends_on=()),),
        execution_levels=((0,),),
    )

    result = await executor.execute_parallel(
        seed=seed,
        execution_plan=graph.to_execution_plan(),
        session_id="s",
        execution_id="e",
        tools=["Read"],
        tool_catalog=None,
        system_prompt="sys",
        externally_satisfied_acs={0: {"reason": "done manually"}},
    )

    assert result.externally_satisfied_count == 1
    assert "verification_status=assumed" in result.results[0].final_message


@pytest.mark.asyncio
async def test_skip_completed_executes_when_verify_gate_fails(tmp_path: Any) -> None:
    from ouroboros.orchestrator.dependency_analyzer import ACNode, DependencyGraph

    seed = _seed_with_specs(
        AcceptanceCriterionSpec(description="contract AC", verify_command="exit 1")
    )
    executor = _make_executor(working_directory=str(tmp_path))
    dispatched: list[list[int]] = []

    async def fake_batch(**kwargs: Any) -> list[ACExecutionResult]:
        dispatched.append(list(kwargs["batch_indices"]))
        return [ACExecutionResult(ac_index=0, ac_content="contract AC", success=True)]

    executor._execute_ac_batch = fake_batch  # type: ignore[method-assign]
    graph = DependencyGraph(
        nodes=(ACNode(index=0, content="contract AC", depends_on=()),),
        execution_levels=((0,),),
    )

    await executor.execute_parallel(
        seed=seed,
        execution_plan=graph.to_execution_plan(),
        session_id="s",
        execution_id="e",
        tools=["Read"],
        tool_catalog=None,
        system_prompt="sys",
        externally_satisfied_acs={0: {"reason": "claims done"}},
    )

    # The failing verify gate forced normal execution instead of skipping.
    assert dispatched == [[0]]


# ---------------------------------------------------------------------------
# V1 gate — expected_artifacts enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_artifacts_only_gate_passes_when_files_exist(tmp_path: Any) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text("guide\n")
    (tmp_path / "README.md").write_text("readme\n")
    executor = _make_executor(working_directory=str(tmp_path))
    spec = AcceptanceCriterionSpec(
        description="docs exist",
        expected_artifacts=("README.md", "docs/guide.md", "docs"),
    )

    outcome = await executor._run_ac_verify_gate(spec=spec, cwd=str(tmp_path))

    assert outcome.passed is True
    assert outcome.missing_artifacts == ()


@pytest.mark.asyncio
async def test_artifacts_only_gate_reports_all_missing(tmp_path: Any) -> None:
    (tmp_path / "present.md").write_text("here\n")
    executor = _make_executor(working_directory=str(tmp_path))
    spec = AcceptanceCriterionSpec(
        description="docs exist",
        expected_artifacts=("present.md", "absent-one.md", "absent/two.md"),
    )

    outcome = await executor._run_ac_verify_gate(spec=spec, cwd=str(tmp_path))

    assert outcome.passed is False
    assert outcome.missing_artifacts == ("absent-one.md", "absent/two.md")
    assert "absent-one.md" in (outcome.reason or "")
    assert "absent/two.md" in (outcome.reason or "")


@pytest.mark.asyncio
async def test_artifact_path_escape_is_treated_as_missing(tmp_path: Any) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # The escape target EXISTS outside the workspace — it still must not count.
    (tmp_path / "outside.md").write_text("outside\n")
    executor = _make_executor(working_directory=str(workspace))
    relative_escape = AcceptanceCriterionSpec(
        description="escape", expected_artifacts=("../outside.md",)
    )
    absolute_escape = AcceptanceCriterionSpec(
        description="escape", expected_artifacts=(str(tmp_path / "outside.md"),)
    )

    for spec in (relative_escape, absolute_escape):
        outcome = await executor._run_ac_verify_gate(spec=spec, cwd=str(workspace))
        assert outcome.passed is False
        assert len(outcome.missing_artifacts) == 1
        assert "escapes workspace" in outcome.missing_artifacts[0]


@pytest.mark.asyncio
async def test_combined_contract_fails_when_either_leg_fails(tmp_path: Any) -> None:
    (tmp_path / "artifact.md").write_text("built\n")
    executor = _make_executor(working_directory=str(tmp_path))

    command_ok_artifact_missing = AcceptanceCriterionSpec(
        description="combined",
        verify_command="exit 0",
        expected_artifacts=("missing.md",),
    )
    artifact_ok_command_fails = AcceptanceCriterionSpec(
        description="combined",
        verify_command="exit 1",
        expected_artifacts=("artifact.md",),
    )
    both_ok = AcceptanceCriterionSpec(
        description="combined",
        verify_command="exit 0",
        expected_artifacts=("artifact.md",),
    )

    missing_leg = await executor._run_ac_verify_gate(
        spec=command_ok_artifact_missing, cwd=str(tmp_path)
    )
    assert missing_leg.passed is False
    assert missing_leg.missing_artifacts == ("missing.md",)

    command_leg = await executor._run_ac_verify_gate(
        spec=artifact_ok_command_fails, cwd=str(tmp_path)
    )
    assert command_leg.passed is False
    assert "status 1" in (command_leg.reason or "")

    assert (await executor._run_ac_verify_gate(spec=both_ok, cwd=str(tmp_path))).passed is True


@pytest.mark.asyncio
async def test_apply_verify_gate_fails_artifacts_only_ac(tmp_path: Any) -> None:
    """An artifacts-only contract (verify: NONE) is enforced, not decorative."""
    executor = _make_executor(working_directory=str(tmp_path))
    seed = _seed_with_specs(
        AcceptanceCriterionSpec(description="docs AC", expected_artifacts=("docs/out.md",))
    )
    result = ACExecutionResult(ac_index=0, ac_content="docs AC", success=True)

    gated = await executor._apply_verify_gate(
        seed=seed, ac_index=0, result=result, session_id="s", execution_id="e"
    )

    assert gated.success is False
    assert gated.outcome == ACExecutionOutcome.FAILED
    assert "expected_artifacts missing" in (gated.error or "")
    assert gated.atomic_verifier_verdict is not None
    assert gated.atomic_verifier_verdict.failure_class == "EVIDENCE_MISSING"

    # And with the artifact present the same AC passes untouched.
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "out.md").write_text("done\n")
    passed = await executor._apply_verify_gate(
        seed=seed, ac_index=0, result=result, session_id="s", execution_id="e"
    )
    assert passed is result


@pytest.mark.asyncio
async def test_sibling_flip_gated_out_by_artifacts_only_contract(tmp_path: Any) -> None:
    executor = _make_executor(working_directory=str(tmp_path))
    (tmp_path / "present.md").write_text("here\n")
    seed = _seed_with_specs(
        "sibling did work",
        AcceptanceCriterionSpec(description="missing docs", expected_artifacts=("absent.md",)),
        AcceptanceCriterionSpec(description="present docs", expected_artifacts=("present.md",)),
    )
    level_results = [
        ACExecutionResult(ac_index=0, ac_content="sibling did work", success=True),
        ACExecutionResult(
            ac_index=1, ac_content="missing docs", success=False, outcome=ACExecutionOutcome.FAILED
        ),
        ACExecutionResult(
            ac_index=2, ac_content="present docs", success=False, outcome=ACExecutionOutcome.FAILED
        ),
    ]

    gated = await executor._compute_sibling_flip_gated_out(
        seed=seed, level_results=level_results, session_id="s", execution_id="e"
    )

    assert gated == frozenset({1})


@pytest.mark.asyncio
async def test_skip_completed_gates_artifacts_only_contract(tmp_path: Any) -> None:
    from ouroboros.orchestrator.dependency_analyzer import ACNode, DependencyGraph

    seed = _seed_with_specs(
        AcceptanceCriterionSpec(description="docs AC", expected_artifacts=("out.md",))
    )
    executor = _make_executor(working_directory=str(tmp_path))
    dispatched: list[list[int]] = []

    async def fake_batch(**kwargs: Any) -> list[ACExecutionResult]:
        dispatched.append(list(kwargs["batch_indices"]))
        return [ACExecutionResult(ac_index=0, ac_content="docs AC", success=True)]

    executor._execute_ac_batch = fake_batch  # type: ignore[method-assign]
    graph = DependencyGraph(
        nodes=(ACNode(index=0, content="docs AC", depends_on=()),),
        execution_levels=((0,),),
    )

    # Missing artifact → the skip is refused and the AC executes normally.
    await executor.execute_parallel(
        seed=seed,
        execution_plan=graph.to_execution_plan(),
        session_id="s1",
        execution_id="e1",
        tools=["Read"],
        tool_catalog=None,
        system_prompt="sys",
        externally_satisfied_acs={0: {"reason": "claims done"}},
    )
    assert dispatched == [[0]]

    # Present artifact → skipped and stamped verified.
    (tmp_path / "out.md").write_text("done\n")
    dispatched.clear()
    result = await executor.execute_parallel(
        seed=seed,
        execution_plan=graph.to_execution_plan(),
        session_id="s2",
        execution_id="e2",
        tools=["Read"],
        tool_catalog=None,
        system_prompt="sys",
        externally_satisfied_acs={0: {"reason": "claims done"}},
    )
    assert dispatched == []
    assert result.externally_satisfied_count == 1
    assert "verification_status=verified" in result.results[0].final_message
