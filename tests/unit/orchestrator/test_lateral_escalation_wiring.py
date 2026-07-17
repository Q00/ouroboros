"""Tests wiring the lateral-persona escalation ladder (Task 2) into the
parallel executor's ``_maybe_run_lateral_escalation_ladder``. The persona
invocation (``_execute_ac_batch``) and the backoff (``executor._sleep``) are
always mocked — this suite never actually sleeps or dispatches a real agent.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.core.seed import (
    AcceptanceCriterionSpec,
    OntologySchema,
    Seed,
    SeedMetadata,
)
from ouroboros.orchestrator.lateral_escalation import (
    TOTAL_PERSONA_COUNT,
    LateralEscalationState,
)
from ouroboros.orchestrator.model_routing import ModelRouter
from ouroboros.orchestrator.parallel_executor import ACExecutionResult, ParallelACExecutor
from ouroboros.resilience.lateral import ThinkingPersona

_THRESHOLD = 2  # mirrors lateral_escalation._LATERAL_ESCALATION_THRESHOLD


def _make_executor() -> ParallelACExecutor:
    """An executor with NO escalation dial configured (today's default)."""
    executor = ParallelACExecutor(
        adapter=MagicMock(),
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
    )
    executor._coordinator.detect_file_conflicts = MagicMock(return_value=[])
    executor._emit_workflow_progress = AsyncMock()
    executor._emit_level_started = AsyncMock()
    executor._emit_level_completed = AsyncMock()
    executor._emit_subtask_event = AsyncMock()
    executor._emit_ac_outcome_finalized = AsyncMock()
    executor._event_emitter.emit_ac_parked_for_operator = AsyncMock()
    executor._sleep = AsyncMock()
    return executor


def _make_executor_with_active_ladder() -> ParallelACExecutor:
    """An executor with the ladder opted in AND a model-tier router ALREADY
    at the frontier ceiling, so a plain atomic failure is a genuine
    terminal-state failure — the escalation ladder engages. Mirrors a real
    run (``ooo run`` via the runner, which always opts in) with economics
    tiers configured, without needing a full adapter/config round trip."""
    executor = _make_executor()
    executor._lateral_escalation_enabled = True
    executor._model_router = ModelRouter(
        tier_models={"frontier": "gpt-frontier"},
        runtime_backend="claude",
        child_tier="frugal",
        base_tier="frontier",
        escalation_retry_threshold=999,  # never escalates further past frontier
    )
    return executor


def _make_seed() -> Seed:
    return Seed(
        goal="Implement the stubborn widget",
        constraints=(),
        acceptance_criteria=(AcceptanceCriterionSpec(description="Implement the widget"),),
        ontology_schema=OntologySchema(name="Ladder", description="Test schema"),
        metadata=SeedMetadata(ambiguity_score=0.05),
    )


def _failed_result(*, ac_index: int = 0, error: str = "verify_command failed") -> ACExecutionResult:
    return ACExecutionResult(
        ac_index=ac_index,
        ac_content="Implement the widget",
        success=False,
        error=error,
        is_decomposed=False,
    )


def _success_result(*, ac_index: int = 0) -> ACExecutionResult:
    return ACExecutionResult(
        ac_index=ac_index,
        ac_content="Implement the widget",
        success=True,
        is_decomposed=False,
    )


async def _ladder(
    executor: ParallelACExecutor,
    *,
    result: ACExecutionResult | BaseException,
    ac_retry_attempts: dict[int, int] | None = None,
) -> ACExecutionResult | None:
    return await executor._maybe_run_lateral_escalation_ladder(
        seed=_make_seed(),
        ac_idx=0,
        result=result,  # type: ignore[arg-type]
        ac_retry_attempts=ac_retry_attempts or {0: 2},
        session_id="s1",
        execution_id="exec-1",
        tools=[],
        tool_catalog=None,
        system_prompt="",
        level_contexts=[],
        execution_counters=None,
    )


class TestInfraFatalIsExempt:
    @pytest.mark.asyncio
    async def test_raw_exception_never_engages_the_ladder(self) -> None:
        executor = _make_executor()
        executor._execute_ac_batch = AsyncMock()

        outcome = await _ladder(executor, result=RuntimeError("adapter crashed"))

        assert outcome is None
        executor._execute_ac_batch.assert_not_called()

    @pytest.mark.asyncio
    async def test_blocked_result_never_engages_the_ladder(self) -> None:
        """A dependency-blocked AC is not a quality failure; exempt."""
        executor = _make_executor()
        executor._execute_ac_batch = AsyncMock()
        blocked = ACExecutionResult(
            ac_index=0,
            ac_content="Implement the widget",
            success=False,
            error="skipped: dependency failed",
        )

        outcome = await _ladder(executor, result=blocked)

        assert outcome is None
        executor._execute_ac_batch.assert_not_called()


class TestLadderEngagesOnlyAtTerminalState:
    @pytest.mark.asyncio
    async def test_success_result_never_engages(self) -> None:
        executor = _make_executor_with_active_ladder()
        executor._execute_ac_batch = AsyncMock()

        outcome = await _ladder(executor, result=_success_result())

        assert outcome is None
        executor._execute_ac_batch.assert_not_called()

    @pytest.mark.asyncio
    async def test_fully_dormant_routing_never_engages_even_when_opted_in(self) -> None:
        """Regression guard: EVEN WITH the ladder opted in, an executor with
        NO model_router and NO reasoning_effort configured (no escalation
        dial exists at all) must NEVER engage — no matter how many times the
        same retryable failure repeats. Getting this wrong once turned every
        ordinary exhausted-retry failure into a real infinite loop across
        the whole suite (the ladder is ALSO gated off by default via
        ``lateral_escalation_enabled=False``, covered separately below)."""
        executor = _make_executor()
        executor._lateral_escalation_enabled = True
        assert executor._model_router is None
        assert executor._reasoning_effort is None
        executor._execute_ac_batch = AsyncMock()

        outcome = await _ladder(executor, result=_failed_result())

        assert outcome is None
        executor._execute_ac_batch.assert_not_called()

    @pytest.mark.asyncio
    async def test_ladder_disabled_by_default_never_engages(self) -> None:
        """Regression guard: direct/test construction of the executor must
        NOT get the escalation ladder for free, even at a genuine
        terminal-state failure — ``lateral_escalation_enabled`` defaults to
        ``False`` (mirrors ``shadow_replay_enabled``)."""
        executor = _make_executor_with_active_ladder()
        executor._lateral_escalation_enabled = False
        executor._execute_ac_batch = AsyncMock()

        outcome = await _ladder(executor, result=_failed_result())

        assert outcome is None
        executor._execute_ac_batch.assert_not_called()


class TestLadderProgression:
    @pytest.mark.asyncio
    async def test_persona_cycling_never_repeats_and_eventually_succeeds(self) -> None:
        """With the model-tier router already at the frontier ceiling, a
        plain atomic failure is a genuine terminal-state failure, so the
        ladder engages immediately. It should retry identically once (streak
        1->2 below the threshold), then hand a NEW persona each subsequent
        retry, never repeating one, until the dispatch finally succeeds."""
        executor = _make_executor_with_active_ladder()

        seen_prompts: list[str] = []
        call_count = {"n": 0}

        async def fake_execute_ac_batch(**kwargs: object) -> list[ACExecutionResult]:
            call_count["n"] += 1
            retry_prompts = kwargs["retry_prompts"]
            assert isinstance(retry_prompts, dict)
            seen_prompts.append(retry_prompts[0])
            # Succeed on the 5th redispatch (1 identical retry + 3 personas).
            if call_count["n"] >= 5:
                return [_success_result()]
            return [_failed_result(error=f"attempt {call_count['n']} failed")]

        executor._execute_ac_batch = AsyncMock(side_effect=fake_execute_ac_batch)
        executor._apply_verify_gate = AsyncMock(side_effect=lambda **kwargs: kwargs["result"])

        outcome = await _ladder(
            executor, result=_failed_result(error="attempt 0 failed"), ac_retry_attempts={0: 2}
        )

        assert outcome is not None
        assert outcome.success is True
        assert call_count["n"] == 5
        # Every redispatch got a DIFFERENT prompt (identical-retry framing,
        # then a genuinely different persona framing each time).
        assert len(seen_prompts) == len(set(seen_prompts))
        # Escalation state is cleared on breakthrough.
        assert 0 not in executor._lateral_escalation_states

    @pytest.mark.asyncio
    async def test_all_personas_exhausted_emits_parked_event(self) -> None:
        """Seed the ladder state with every persona already tried, so the
        very NEXT terminal failure is the parking transition — no need to
        actually cycle every persona in this test."""
        executor = _make_executor_with_active_ladder()
        executor._lateral_escalation_states[0] = LateralEscalationState(
            consecutive_terminal_failures=_THRESHOLD + TOTAL_PERSONA_COUNT - 1,
            personas_tried=tuple(ThinkingPersona),
            parked=False,
        )

        call_count = {"n": 0}

        async def fails_then_succeeds(**kwargs: object) -> list[ACExecutionResult]:
            call_count["n"] += 1
            # First redispatch (the transition into parking) still fails;
            # the next (parked) redispatch succeeds.
            if call_count["n"] >= 2:
                return [_success_result()]
            return [_failed_result(error="last persona also failed")]

        executor._execute_ac_batch = AsyncMock(side_effect=fails_then_succeeds)
        executor._apply_verify_gate = AsyncMock(side_effect=lambda **kwargs: kwargs["result"])

        outcome = await _ladder(executor, result=_failed_result(error="still broken"))

        assert outcome is not None
        assert outcome.success is True
        executor._event_emitter.emit_ac_parked_for_operator.assert_awaited_once()
        _, kwargs = executor._event_emitter.emit_ac_parked_for_operator.await_args
        assert kwargs["root_ac_index"] == 0
        assert len(kwargs["personas_tried"]) == TOTAL_PERSONA_COUNT
        # Slept the long backoff before BOTH the parking-transition redispatch
        # and the next (successful) parked redispatch.
        assert executor._sleep.await_count == 2
        for call in executor._sleep.await_args_list:
            assert call.args[0] == executor._parked_retry_backoff_seconds

    @pytest.mark.asyncio
    async def test_parked_state_keeps_retrying_with_long_backoff_and_does_not_hard_stop(
        self,
    ) -> None:
        """Once parked, the ladder must keep looping (never hard-stop) and
        sleep the configured long backoff before every redispatch, until it
        finally succeeds."""
        executor = _make_executor_with_active_ladder()
        parked_state = LateralEscalationState(
            consecutive_terminal_failures=50,
            personas_tried=tuple(ThinkingPersona),
            parked=True,
        )
        executor._lateral_escalation_states[0] = parked_state

        call_count = {"n": 0}

        async def fails_twice_then_succeeds(**kwargs: object) -> list[ACExecutionResult]:
            call_count["n"] += 1
            if call_count["n"] >= 3:
                return [_success_result()]
            return [_failed_result(error="parked retry still failing")]

        executor._execute_ac_batch = AsyncMock(side_effect=fails_twice_then_succeeds)
        executor._apply_verify_gate = AsyncMock(side_effect=lambda **kwargs: kwargs["result"])

        outcome = await _ladder(executor, result=_failed_result(error="parked retry still failing"))

        assert outcome is not None
        assert outcome.success is True
        assert call_count["n"] == 3
        # Slept before EVERY parked redispatch (proving the long-backoff
        # cadence is applied, not skipped) and never hard-stopped early.
        assert executor._sleep.await_count == 3
        for call in executor._sleep.await_args_list:
            assert call.args[0] == executor._parked_retry_backoff_seconds
        # Never re-emitted the parked event again (already parked; ``just_parked``
        # only fires once on the original transition).
        executor._event_emitter.emit_ac_parked_for_operator.assert_not_awaited()
