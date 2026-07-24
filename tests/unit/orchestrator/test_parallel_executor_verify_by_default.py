"""Tests for PR-V verify-by-default: V1 gate, retry, lateral, trust leaks."""

from __future__ import annotations

from dataclasses import replace
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.core.seed import (
    AcceptanceCriterionSpec,
    OntologySchema,
    Seed,
    SeedMetadata,
)
from ouroboros.orchestrator.adapter import ParamSupport, RuntimeCapabilities
from ouroboros.orchestrator.decomposition_policy import (
    DecompositionChild,
    DecompositionDecisionRecord,
    DecompositionDisposition,
    DecompositionSource,
    SemanticAttestationStatus,
    StructuralCheckStatus,
)
from ouroboros.orchestrator.model_routing import ModelRouter, decide_model
from ouroboros.orchestrator.parallel_executor import (
    ACExecutionOutcome,
    ACExecutionResult,
    ParallelACExecutor,
    _build_success_contract_block,
    _complete_sibling_acs_from_evidence,
    _VerifyGateOutcome,
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


@pytest.mark.asyncio
async def test_verify_gate_rejects_commands_that_mutate_the_workspace(tmp_path: Any) -> None:
    target = tmp_path / "target.txt"
    target.write_text("keep", encoding="utf-8")
    executor = _make_executor(working_directory=str(tmp_path))
    spec = AcceptanceCriterionSpec(description="read-only", verify_command="rm target.txt")

    outcome = await executor._run_ac_verify_gate(spec=spec, cwd=str(tmp_path))

    assert outcome.passed is False
    assert outcome.workspace_mutated is True
    assert "mutated the workspace" in (outcome.reason or "")


@pytest.mark.asyncio
async def test_verify_gate_ignores_normalized_exit_code_output_assertion(
    tmp_path: Any,
) -> None:
    executor = _make_executor(working_directory=str(tmp_path))
    spec = AcceptanceCriterionSpec(
        description="exit code is already enforced by verify_command",
        verify_command="exit 0",
        output_assertion="exit code 0",
    )

    assert spec.output_assertion is None
    outcome = await executor._run_ac_verify_gate(spec=spec, cwd=str(tmp_path))

    assert outcome.passed is True
    assert outcome.reason is None


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
async def test_apply_verify_gate_reuses_cached_success_outcome(tmp_path: Any) -> None:
    executor = _make_executor(working_directory=str(tmp_path))
    seed = _seed_with_specs(AcceptanceCriterionSpec(description="ac", verify_command="exit 0"))
    cached = await executor._run_ac_verify_gate(spec=seed.acceptance_criteria[0], cwd=str(tmp_path))
    result = ACExecutionResult(
        ac_index=0,
        ac_content="ac",
        success=True,
        verify_gate_outcome=cached,
    )

    gated = await executor._apply_verify_gate(
        seed=seed, ac_index=0, result=result, session_id="s", execution_id="e"
    )

    assert gated is result


@pytest.mark.asyncio
async def test_apply_verify_gate_rechecks_artifacts_without_replaying_cached_command(
    tmp_path: Any,
) -> None:
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("ready", encoding="utf-8")
    executor = _make_executor(working_directory=str(tmp_path))
    seed = _seed_with_specs(
        AcceptanceCriterionSpec(
            description="ac",
            verify_command="exit 0",
            expected_artifacts=("artifact.txt",),
        )
    )
    cached = await executor._run_ac_verify_gate(spec=seed.acceptance_criteria[0], cwd=str(tmp_path))
    artifact.unlink()
    result = ACExecutionResult(
        ac_index=0,
        ac_content="ac",
        success=True,
        verify_gate_outcome=cached,
    )

    gated = await executor._apply_verify_gate(
        seed=seed, ac_index=0, result=result, session_id="s", execution_id="e"
    )

    assert gated.success is False
    assert gated.verify_gate_outcome is not None
    assert gated.verify_gate_outcome.missing_artifacts == ("artifact.txt",)


@pytest.mark.asyncio
async def test_final_verify_settlement_invalidates_prior_success_after_mutation(
    tmp_path: Any,
) -> None:
    target = tmp_path / "target.txt"
    target.write_text("keep", encoding="utf-8")
    executor = _make_executor(working_directory=str(tmp_path))
    seed = _seed_with_specs(
        AcceptanceCriterionSpec(description="artifact", expected_artifacts=("target.txt",)),
        AcceptanceCriterionSpec(description="mutator", verify_command="rm target.txt"),
        "contractless",
    )
    first_outcome = await executor._run_ac_verify_gate(
        spec=seed.acceptance_criteria[0], cwd=str(tmp_path)
    )
    mutating_outcome = await executor._run_ac_verify_gate(
        spec=seed.acceptance_criteria[1], cwd=str(tmp_path)
    )
    results = await executor._settle_verify_gate_results(
        seed=seed,
        results=[
            ACExecutionResult(
                ac_index=0,
                ac_content="artifact",
                success=True,
                outcome=ACExecutionOutcome.SUCCEEDED,
                verify_gate_outcome=first_outcome,
            ),
            ACExecutionResult(
                ac_index=1,
                ac_content="mutator",
                success=True,
                outcome=ACExecutionOutcome.SUCCEEDED,
                verify_gate_outcome=mutating_outcome,
            ),
            ACExecutionResult(
                ac_index=2,
                ac_content="contractless",
                success=True,
                outcome=ACExecutionOutcome.SUCCEEDED,
            ),
        ],
        session_id="s",
        execution_id="e",
    )

    assert [result.success for result in results] == [False, False, False]
    assert all(result.outcome is ACExecutionOutcome.FAILED for result in results)


@pytest.mark.asyncio
async def test_apply_verify_gate_recovers_failed_result_once(tmp_path: Any) -> None:
    executor = _make_executor(working_directory=str(tmp_path))
    seed = _seed_with_specs(AcceptanceCriterionSpec(description="ac", verify_command="exit 0"))
    result = ACExecutionResult(
        ac_index=0,
        ac_content="ac",
        success=False,
        error="runtime false negative",
        outcome=ACExecutionOutcome.FAILED,
    )

    recovered = await executor._apply_verify_gate(
        seed=seed, ac_index=0, result=result, session_id="s", execution_id="e"
    )

    assert recovered.success is True
    assert recovered.error is None
    assert recovered.outcome == ACExecutionOutcome.SUCCEEDED
    emitted = [call.args[0] for call in executor._event_store.append.await_args_list]
    recovery_events = [event for event in emitted if event.type == "execution.verify.recovered"]
    assert len(recovery_events) == 1
    assert recovery_events[0].data["prior_error"] == "runtime false negative"


@pytest.mark.asyncio
async def test_verify_gate_times_out_hung_command(tmp_path: Any) -> None:
    executor = _make_executor(working_directory=str(tmp_path), verify_command_timeout_seconds=1)
    spec = AcceptanceCriterionSpec(
        description="hung",
        verify_command='python3 -c "import time; time.sleep(10)"',
    )

    started = time.monotonic()
    outcome = await executor._run_ac_verify_gate(spec=spec, cwd=str(tmp_path))

    assert time.monotonic() - started < 5
    assert outcome.passed is False
    assert outcome.reason == "verify_command timed out after 1s"


@pytest.mark.asyncio
async def test_batch_emits_outer_outcome_marker_after_verify_failure(tmp_path: Any) -> None:
    """Provisional leaf proof events cannot outlive a seed-level rejection."""
    executor = _make_executor(working_directory=str(tmp_path), ac_retry_attempts=0)
    seed = _seed_with_specs(AcceptanceCriterionSpec(description="ac", verify_command="exit 1"))

    async def fake_batch(**_kwargs: Any) -> list[ACExecutionResult]:
        return [
            ACExecutionResult(
                ac_index=0,
                ac_content="ac",
                success=True,
                retry_attempt=0,
                is_decomposed=True,
            )
        ]

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
        ac_retry_attempts={0: 0},
        execution_counters=None,
    )

    assert isinstance(results[0], ACExecutionResult)
    assert results[0].success is False
    emitted = [call.args[0] for call in executor._event_store.append.await_args_list]
    markers = [event for event in emitted if event.type == "execution.ac.attempt_judged"]
    assert len(markers) == 1
    assert markers[0].data == {
        "execution_id": "e",
        "session_id": "s",
        "root_ac_index": 0,
        "ac_index": 0,
        "retry_attempt": 0,
        "attempt_number": 1,
        "success": False,
        "outcome": "failed",
        "is_decomposed": True,
        "is_decomposed_child": True,
    }


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
async def test_retry_reaches_pending_native_model_escalation(tmp_path: Any) -> None:
    adapter = _StubAdapter(str(tmp_path))
    adapter.capabilities = RuntimeCapabilities(
        skill_dispatch=True,
        targeted_resume=True,
        structured_output=True,
        model_override_support=ParamSupport.NATIVE,
    )
    executor = ParallelACExecutor(
        adapter=adapter,
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
        run_verify_commands=False,
        ac_retry_attempts=2,
        model_router=ModelRouter(
            tier_models={
                "frugal": "haiku-x",
                "standard": "sonnet-x",
                "frontier": "opus-x",
            },
            runtime_backend="claude",
            child_tier="frugal",
            base_tier="standard",
            escalation_retry_threshold=2,
        ),
    )
    seed = _seed_with_specs("ac")
    ac_retry_attempts = {0: 0}
    calls: list[list[int]] = []

    async def fake_batch(**kwargs: Any) -> list[ACExecutionResult]:
        calls.append(list(kwargs["batch_indices"]))
        return [_fail(0, "EVIDENCE_MISSING")]

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

    assert calls == [[0], [0], [0]]
    assert ac_retry_attempts[0] == 2


def _native_escalation_executor(tmp_path: Any, *, ac_retry_attempts: int) -> ParallelACExecutor:
    """A verify-off executor whose adapter enforces model overrides natively and
    whose router escalates from the ``standard`` base at retry threshold 2.

    Shared by the ladder-walk regressions below. The three-tier ladder plus a
    NATIVE ``model_override_support`` are exactly the conditions the retry loop's
    ``pending_enforced_escalation`` branch requires before it lets an identical
    failure class keep dispatching instead of early-stopping.
    """
    adapter = _StubAdapter(str(tmp_path))
    adapter.capabilities = RuntimeCapabilities(
        skill_dispatch=True,
        targeted_resume=True,
        structured_output=True,
        model_override_support=ParamSupport.NATIVE,
    )
    return ParallelACExecutor(
        adapter=adapter,
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
        run_verify_commands=False,
        ac_retry_attempts=ac_retry_attempts,
        model_router=ModelRouter(
            tier_models={"frugal": "haiku-x", "standard": "sonnet-x", "frontier": "opus-x"},
            runtime_backend="claude",
            child_tier="frugal",
            base_tier="standard",
            escalation_retry_threshold=2,
        ),
    )


@pytest.mark.asyncio
async def test_retry_top_level_walks_whole_ladder_to_frontier(tmp_path: Any) -> None:
    """Executor-level pin for the ``escalation_threshold`` doc claim
    (docs/config-reference.md) that "a persistently failing unit walks the whole
    ladder rather than stalling one tier up" — the early-stop truncation is part
    of the *effective* runtime contract, not just the pure routing policy.

    ac_retry_attempts=3, threshold=2, identical failure class every attempt. A
    TOP-LEVEL unit starts at ``standard`` and, under the model ladder, would be
    routed ``standard`` (retry 0) -> ``standard`` (retry 1) -> ``frontier``
    (retry 2). The retry loop must:
      * defeat early-stop while a stronger tier is still pending (retry 1's
        next attempt is ``frontier``), so it keeps dispatching, and
      * resume early-stop once the frontier ceiling is dispatched — retry 3
        would still be ``frontier`` (no escalation pending beyond the cap), so a
        4th identical-class attempt must NOT be burned.

    So exactly 3 dispatches occur and the final one lands at ``frontier``.
    """
    executor = _native_escalation_executor(tmp_path, ac_retry_attempts=3)
    router = executor._model_router
    assert router is not None
    seed = _seed_with_specs("ac")
    ac_retry_attempts = {0: 0}
    calls: list[list[int]] = []
    routed_tiers: list[str | None] = []

    async def fake_batch(**kwargs: Any) -> list[ACExecutionResult]:
        calls.append(list(kwargs["batch_indices"]))
        # Mirror the production seam: the tier a top-level unit would be routed
        # to for this same dispatch is a pure function of the retry_attempt the
        # loop advanced before dispatching.
        routed_tiers.append(
            decide_model(
                ParamSupport.NATIVE,
                router=router,
                is_decomposed_child=False,
                retry_attempt=kwargs["ac_retry_attempts"][0],
            ).tier
        )
        return [_fail(0, "EVIDENCE_MISSING")]

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

    # Ladder walked to the ceiling, then early-stop resumed: no 4th burn.
    assert calls == [[0], [0], [0]]
    assert ac_retry_attempts[0] == 2
    assert routed_tiers == ["standard", "standard", "frontier"]


@pytest.mark.asyncio
async def test_retry_decomposed_child_reaches_retry3_frontier(tmp_path: Any) -> None:
    """A decomposed CHILD (routed one tier below top-level) must also walk its
    whole ladder to ``frontier`` — the finding's expectation.

    The batch retry loop carries top-level indices; the child start tier is not a
    loop input but a property of the routing seam (``resolve_execute_model`` /
    ``decide_model`` with ``is_decomposed_child=True``). A decomposed parent
    re-runs its children — routed one tier cheaper and sharing the parent's retry
    counter — on every retry, so the early-stop predicate reads ``is_decomposed``
    off the dispatched result and probes the CHILD ladder for a pending escalation.
    This mirrors reality by returning a decomposed failing result and computing the
    child tier the loop's per-attempt ``retry_attempt`` would route to, exactly as
    the executor does inside ``_execute_single_ac``.

    ac_retry_attempts=3, threshold=2, identical failure class every attempt. The
    child ladder is frugal, frugal, standard, frontier (retry 0..3), so reaching
    ``frontier`` requires a 4th dispatch at retry 3. The ladder-truth predicate
    keeps dispatching while the next retry resolves to a stronger enforced model
    and resumes early-stop only once the frontier ceiling is reached.
    """
    executor = _native_escalation_executor(tmp_path, ac_retry_attempts=3)
    router = executor._model_router
    assert router is not None
    seed = _seed_with_specs("ac")
    ac_retry_attempts = {0: 0}
    calls: list[list[int]] = []
    routed_tiers: list[str | None] = []

    def _decomposed_fail() -> ACExecutionResult:
        # A decomposed parent whose children (routed one tier cheaper) failed:
        # the predicate requires both child status and a trusted split record.
        base = _fail(0, "EVIDENCE_MISSING")
        return replace(
            base,
            is_decomposed=True,
            decomposition_decision=DecompositionDecisionRecord(
                node_id="trusted-decomposed-parent",
                source=DecompositionSource.PREFLIGHT,
                disposition=DecompositionDisposition.SPLIT,
                children=(
                    DecompositionChild("child a", ("scope a",), "verify a"),
                    DecompositionChild("child b", ("scope b",), "verify b"),
                ),
                structural_status=StructuralCheckStatus.PASSED,
                semantic_status=SemanticAttestationStatus.ESTABLISHED,
                trustworthy=True,
            ),
        )

    async def fake_batch(**kwargs: Any) -> list[ACExecutionResult]:
        calls.append(list(kwargs["batch_indices"]))
        routed_tiers.append(
            decide_model(
                ParamSupport.NATIVE,
                router=router,
                is_decomposed_child=True,
                decomposition_trustworthy=True,
                retry_attempt=kwargs["ac_retry_attempts"][0],
            ).tier
        )
        return [_decomposed_fail()]

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

    # The child is re-dispatched through retry 3 so its ladder reaches the
    # frontier ceiling despite the repeated failure class.
    assert calls == [[0], [0], [0], [0]]
    assert routed_tiers[-1] == "frontier"


@pytest.mark.asyncio
async def test_retry_non_native_runtime_plain_early_stops(tmp_path: Any) -> None:
    """A router whose model override is only advised (not NATIVE) cannot enforce a
    stronger model, so the ladder-truth probe must degrade to the plain early-stop:
    an identical failure class stops at 2 dispatches even though the tier ladder
    WOULD escalate on the next retry under a native runtime.
    """
    adapter = _StubAdapter(str(tmp_path))
    adapter.capabilities = RuntimeCapabilities(
        skill_dispatch=True,
        targeted_resume=True,
        structured_output=True,
        model_override_support=ParamSupport.TRANSLATED,
    )
    executor = ParallelACExecutor(
        adapter=adapter,
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
        run_verify_commands=False,
        ac_retry_attempts=2,
        model_router=ModelRouter(
            tier_models={"frugal": "haiku-x", "standard": "sonnet-x", "frontier": "opus-x"},
            runtime_backend="claude",
            child_tier="frugal",
            base_tier="standard",
            # Threshold 1: under NATIVE the retry-1 dispatch would escalate to
            # ``frontier`` and defeat early-stop — the ADVISED guard must suppress that.
            escalation_retry_threshold=1,
        ),
    )
    seed = _seed_with_specs("ac")
    ac_retry_attempts = {0: 0}
    calls: list[list[int]] = []

    async def fake_batch(**kwargs: Any) -> list[ACExecutionResult]:
        calls.append(list(kwargs["batch_indices"]))
        return [_fail(0, "EVIDENCE_MISSING")]

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


def test_retry_prompt_redacts_secret_like_failure_values() -> None:
    executor = _make_executor()
    long_secret = "s" * 505
    result = ACExecutionResult(
        ac_index=0,
        ac_content="build the thing",
        success=False,
        error=(
            f"provider failed with password=hunter2 and API_KEY=secret-value token={long_secret}"
        ),
    )

    prompt = executor._build_ac_retry_prompt(
        result=result,
        ac_content="build the thing",
        is_final_attempt=False,
    )

    assert "hunter2" not in prompt
    assert "secret-value" not in prompt
    assert long_secret[-100:] not in prompt
    assert prompt.count("[REDACTED]") == 3


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


@pytest.mark.asyncio
async def test_sibling_flip_reuses_cached_failed_verify_gate(tmp_path: Any) -> None:
    counter = tmp_path / "verify-count.txt"
    command = (
        "python3 -c \"from pathlib import Path; p=Path('verify-count.txt'); "
        "n=int(p.read_text()) if p.exists() else 0; p.write_text(str(n+1)); "
        'raise SystemExit(1)"'
    )
    executor = _make_executor(working_directory=str(tmp_path))
    seed = _seed_with_specs(AcceptanceCriterionSpec(description="contract", verify_command=command))
    result = ACExecutionResult(ac_index=0, ac_content="contract", success=True)

    failed = await executor._apply_verify_gate(
        seed=seed, ac_index=0, result=result, session_id="s", execution_id="e"
    )
    assert failed.success is False
    assert counter.read_text(encoding="utf-8") == "1"

    gated = await executor._compute_sibling_flip_gated_out(
        seed=seed, level_results=[failed], session_id="s", execution_id="e"
    )

    assert gated == frozenset({0})
    assert counter.read_text(encoding="utf-8") == "1"


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

    # And with the artifact present the same AC passes with durable evidence.
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "out.md").write_text("done\n")
    passed = await executor._apply_verify_gate(
        seed=seed, ac_index=0, result=result, session_id="s", execution_id="e"
    )
    assert passed.success is True
    assert passed.verify_gate_outcome is not None


@pytest.mark.asyncio
async def test_final_workspace_revalidation_does_not_reuse_cached_command_result(
    tmp_path: Any,
) -> None:
    counter = tmp_path / "final-verify-count.txt"
    command = (
        "python3 -c \"from pathlib import Path; p=Path('final-verify-count.txt'); "
        "n=int(p.read_text()) if p.exists() else 0; p.write_text(str(n+1)); "
        'raise SystemExit(0 if n == 0 else 7)"'
    )
    executor = _make_executor(working_directory=str(tmp_path))
    seed = _seed_with_specs(AcceptanceCriterionSpec(description="ac", verify_command=command))
    cached = await executor._run_ac_verify_gate(spec=seed.acceptance_criteria[0], cwd=str(tmp_path))
    result = ACExecutionResult(
        ac_index=0,
        ac_content="ac",
        success=True,
        verify_gate_outcome=cached,
    )

    revalidated = await executor._revalidate_results_after_coordinator(
        seed=seed,
        results=[result],
        session_id="s",
        execution_id="e",
    )

    # Coordinator revalidation must not replay an arbitrary shell command;
    # external effects are not observable through the workspace digest.
    assert counter.read_text(encoding="utf-8") == "1"
    assert revalidated[0].success is False
    assert revalidated[0].outcome is ACExecutionOutcome.FAILED
    assert "replaying verify_command is not permitted" in (revalidated[0].error or "")


@pytest.mark.asyncio
async def test_final_verify_mutation_invalidates_prior_successes(tmp_path: Any) -> None:
    executor = _make_executor(working_directory=str(tmp_path))
    seed = _seed_with_specs(
        AcceptanceCriterionSpec(description="stable", verify_command="exit 0"),
        AcceptanceCriterionSpec(
            description="mutating verifier",
            verify_command=(
                'python3 -c "from pathlib import Path; '
                "Path('verify-side-effect.txt').write_text('side effect')\""
            ),
        ),
    )
    results = [
        ACExecutionResult(
            ac_index=0,
            ac_content="stable",
            success=True,
            outcome=ACExecutionOutcome.SUCCEEDED,
        ),
        ACExecutionResult(
            ac_index=1,
            ac_content="mutating verifier",
            success=True,
            outcome=ACExecutionOutcome.SUCCEEDED,
        ),
    ]

    revalidated = await executor._revalidate_results_after_coordinator(
        seed=seed,
        results=results,
        session_id="s",
        execution_id="e",
    )

    assert [result.outcome for result in revalidated] == [
        ACExecutionOutcome.FAILED,
        ACExecutionOutcome.FAILED,
    ]
    assert all(
        "replaying verify_command is not permitted" in (result.error or "")
        for result in revalidated
    )


@pytest.mark.asyncio
async def test_final_settlement_rejects_stale_command_without_replay(
    tmp_path: Any,
) -> None:
    counter = tmp_path.parent / f"final-settlement-count-{tmp_path.name}.txt"
    target = tmp_path / "condition.txt"
    target.write_text("before", encoding="utf-8")
    command = (
        'python3 -c "from pathlib import Path; '
        f"counter=Path({str(counter)!r}); "
        "n=int(counter.read_text()) if counter.exists() else 0; "
        "counter.write_text(str(n+1)); "
        "raise SystemExit(0 if Path('condition.txt').read_text() == 'before' else 7)\""
    )
    executor = _make_executor(working_directory=str(tmp_path))
    seed = _seed_with_specs(AcceptanceCriterionSpec(description="ac", verify_command=command))
    cached = await executor._run_ac_verify_gate(spec=seed.acceptance_criteria[0], cwd=str(tmp_path))
    target.write_text("after", encoding="utf-8")

    settled = await executor._settle_verify_gate_results(
        seed=seed,
        results=[
            ACExecutionResult(
                ac_index=0,
                ac_content="ac",
                success=True,
                outcome=ACExecutionOutcome.SUCCEEDED,
                verify_gate_outcome=cached,
            )
        ],
        session_id="s",
        execution_id="e",
    )

    assert counter.read_text(encoding="utf-8") == "1"
    assert settled[0].success is False
    assert settled[0].outcome is ACExecutionOutcome.FAILED
    assert "replaying verify_command is not permitted" in (settled[0].error or "")


@pytest.mark.asyncio
async def test_checkpoint_restores_verify_evidence_and_result_retry_identity(tmp_path: Any) -> None:
    from ouroboros.orchestrator.dependency_analyzer import ACNode, DependencyGraph

    seed = _seed_with_specs(AcceptanceCriterionSpec(description="ac", verify_command="exit 0"))
    plan = DependencyGraph(
        nodes=(ACNode(index=0, content="ac", depends_on=()),),
        execution_levels=((0,),),
    ).to_execution_plan()
    checkpoint_store = MagicMock()
    checkpoint_store.load.return_value = type("LoadResult", (), {"is_ok": False})()
    checkpoint_store.save.return_value = type("SaveResult", (), {"is_ok": True})()
    executor = _make_executor(working_directory=str(tmp_path))
    executor._checkpoint_store = checkpoint_store
    gate = await executor._run_ac_verify_gate(spec=seed.acceptance_criteria[0], cwd=str(tmp_path))
    executor._execute_ac_batch = AsyncMock(
        return_value=[
            ACExecutionResult(
                ac_index=0,
                ac_content="ac",
                success=True,
                retry_attempt=3,
                verify_gate_outcome=gate,
            )
        ]
    )

    first = await executor.execute_parallel(
        seed=seed,
        execution_plan=plan,
        session_id="session-checkpoint-verify",
        execution_id="execution-checkpoint-verify",
        tools=["Read"],
        tool_catalog=None,
        system_prompt="system",
    )
    assert first.results[0].success is True
    checkpoint = checkpoint_store.save.call_args.args[0]
    assert checkpoint.state["result_retry_attempts"] == {"0": 3}
    assert checkpoint.state["verify_gate_outcomes"]["0"]["workspace_digest"] == (
        gate.workspace_digest
    )

    restore_store = MagicMock()
    restore_store.load.return_value = type("LoadResult", (), {"is_ok": True, "value": checkpoint})()
    restore_store.save.return_value = type("SaveResult", (), {"is_ok": True})()
    restored = _make_executor(working_directory=str(tmp_path))
    restored._checkpoint_store = restore_store
    restored._execute_ac_batch = AsyncMock()

    recovered = await restored.execute_parallel(
        seed=seed,
        execution_plan=plan,
        session_id="session-checkpoint-verify",
        execution_id="execution-checkpoint-verify",
        tools=["Read"],
        tool_catalog=None,
        system_prompt="system",
    )

    assert recovered.results[0].success is True
    assert recovered.results[0].retry_attempt == 3
    assert isinstance(recovered.results[0].verify_gate_outcome, _VerifyGateOutcome)
    restored._execute_ac_batch.assert_not_awaited()


def test_workspace_digest_includes_empty_directories(tmp_path: Any) -> None:
    empty_directory = tmp_path / "expected-artifact-directory"
    empty_directory.mkdir()
    before = ParallelACExecutor._workspace_content_digest(str(tmp_path))

    empty_directory.rmdir()

    after = ParallelACExecutor._workspace_content_digest(str(tmp_path))
    assert before is not None
    assert after is not None
    assert before != after


@pytest.mark.asyncio
async def test_final_workspace_revalidation_fails_closed_without_contract(tmp_path: Any) -> None:
    executor = _make_executor(working_directory=str(tmp_path))
    seed = _seed_with_specs("description-only AC")
    result = ACExecutionResult(ac_index=0, ac_content="description-only AC", success=True)

    revalidated = await executor._revalidate_results_after_coordinator(
        seed=seed,
        results=[result],
        session_id="s",
        execution_id="e",
    )

    assert revalidated[0].success is False
    assert revalidated[0].outcome is ACExecutionOutcome.FAILED
    assert "no deterministic post-coordinator success contract" in (revalidated[0].error or "")


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


class TestSuccessContractBlock:
    """The worker-facing SUCCESS CONTRACT block surfaced in the leaf prompt."""

    def test_none_spec_yields_empty_block(self) -> None:
        assert _build_success_contract_block(None) == ""

    def test_contract_less_spec_yields_empty_block(self) -> None:
        spec = AcceptanceCriterionSpec(description="just a description")
        assert _build_success_contract_block(spec) == ""

    def test_full_contract_renders_all_three_lines(self) -> None:
        spec = AcceptanceCriterionSpec(
            description="build succeeds",
            verify_command="make build",
            expected_artifacts=("dist/app", "dist/app.map"),
            output_assertion="BUILD OK",
        )
        block = _build_success_contract_block(spec)
        assert block.startswith("SUCCESS CONTRACT for this AC:")
        assert (
            "- Run locally before completion: make build. "
            "The verify gate re-runs it and records authoritative evidence." in block
        )
        assert (
            "- Expected artifacts: dist/app, dist/app.map — ensure they exist in the workspace"
            in block
        )
        assert "- Expected output: BUILD OK" in block

    def test_partial_contract_only_renders_present_fields(self) -> None:
        spec = AcceptanceCriterionSpec(description="verify only", verify_command="pytest -q")
        block = _build_success_contract_block(spec)
        assert (
            "- Run locally before completion: pytest -q. "
            "The verify gate re-runs it and records authoritative evidence." in block
        )
        assert "Expected artifacts" not in block
        assert "Expected output" not in block
