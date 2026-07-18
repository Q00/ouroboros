"""Tests wiring the lateral-persona escalation ladder (Task 2) into the
parallel executor's ``_maybe_run_lateral_escalation_ladder``. The persona
invocation (``_execute_ac_batch``) and the backoff (``executor._sleep``) are
always mocked — this suite never actually sleeps or dispatches a real agent.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.core.seed import (
    AcceptanceCriterionSpec,
    InvestmentSpec,
    OntologySchema,
    Seed,
    SeedMetadata,
)
from ouroboros.events.base import BaseEvent
from ouroboros.orchestrator.effort_routing import assess_investment, resolve_execute_effort
from ouroboros.orchestrator.execution_runtime_scope import ExecutionNodeIdentity
from ouroboros.orchestrator.lateral_escalation import (
    TOTAL_PERSONA_COUNT,
    LateralEscalationState,
)
from ouroboros.orchestrator.model_routing import ModelRouter
from ouroboros.orchestrator.parallel_executor import ACExecutionResult, ParallelACExecutor
from ouroboros.persistence.event_store import EventStore
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
    executor._event_emitter.emit_ac_parked_resolved = AsyncMock()
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
    # resolve_execute_model() treats a router built for a different backend
    # than the currently-configured adapter as absent (the cross-harness
    # redispatch guard) — the adapter must report the SAME backend the
    # router above declares for the model axis to actually be "configured"
    # from a live dispatch's point of view, exactly as it must at runtime.
    executor._adapter.runtime_backend = "claude"
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
        # Fix 8: this AC was never parked, so there is no badge to clear —
        # emitting a resolution event here would be a spurious no-op event.
        executor._event_emitter.emit_ac_parked_resolved.assert_not_awaited()

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
        # Fix 8: this AC WAS parked before the breakthrough, so the durable
        # resolution companion event must fire exactly once so Kanban/HUD/
        # conductor can clear the parked badge.
        executor._event_emitter.emit_ac_parked_resolved.assert_awaited_once()
        _, resolved_kwargs = executor._event_emitter.emit_ac_parked_resolved.await_args
        assert resolved_kwargs["root_ac_index"] == 0

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
        # Fix 8: the eventual breakthrough still resolves the (pre-seeded)
        # parked state exactly once.
        executor._event_emitter.emit_ac_parked_resolved.assert_awaited_once()


class TestRootAcTerminalStateMatchesLiveDispatch:
    """Fix 3 (BLOCKING, PR #1648 review): ``_root_ac_terminal_state`` must
    reproduce the EXACT model/effort resolution a live dispatch would use for
    the NEXT attempt of this same root AC — same entry points
    (``resolve_execute_model``/``resolve_execute_effort``), same inputs
    (investment authority, profile-suggested tier, backend-matched router) —
    never a parallel/incomplete reconstruction that can silently diverge from
    what actually gets dispatched.
    """

    @pytest.mark.asyncio
    async def test_investment_authorized_cheapening_is_reflected_not_overshot(self) -> None:
        """A measured low/low high-confidence investment assessment
        authorizes ONE notch of real-dispatch cheapening; from
        ``EFFORT_RAISE_RETRY_THRESHOLD`` onward the retry-raise then adds one
        notch back, netting "high" — not "xhigh". Before the fix, the helper
        ignored the investment assessment entirely and reported "xhigh": at
        the effort ceiling, wrongly satisfying ``at_max_effort`` and
        reporting this AC terminal a retry early. The model axis alone is
        already at the frontier ceiling (see ``_make_executor_with_active_ladder``),
        so with the fix this case must NOT be terminal (effort axis has real
        headroom the live dispatch would still use), and the bug this
        regression guards against would have reported it terminal."""
        executor = _make_executor_with_active_ladder()
        executor._reasoning_effort = "high"
        seed = Seed(
            goal="Implement the stubborn widget",
            constraints=(),
            acceptance_criteria=(
                AcceptanceCriterionSpec(
                    description="Implement the widget",
                    investment=InvestmentSpec(
                        difficulty="low",
                        stakes="low",
                        provenance="measured",
                        confidence="high",
                    ),
                ),
            ),
            ontology_schema=OntologySchema(name="Ladder", description="Test schema"),
            metadata=SeedMetadata(ambiguity_score=0.05),
        )

        # Ground truth: what the REAL dispatch would resolve for this exact
        # retry attempt, given the exact same investment assessment.
        investment_assessment = assess_investment(seed.acceptance_criteria[0].investment)
        real_effort_decision, _ = resolve_execute_effort(
            executor._adapter,
            base_effort="high",
            is_decomposed_child=False,
            retry_attempt=3,
            investment_assessment=investment_assessment,
        )
        assert real_effort_decision.level == "high"  # cheapen-then-raise nets back to "high"

        terminal = await executor._root_ac_terminal_state(
            seed=seed,
            ac_idx=0,
            result=_failed_result(),
            retry_attempt=3,
        )

        # The effort axis still has real headroom under the fix (matches the
        # live dispatch's own "high", short of the "xhigh" ceiling), so this
        # is correctly NOT yet a terminal-state failure.
        assert terminal is False

    @pytest.mark.asyncio
    async def test_no_investment_authority_reaches_true_ceiling_and_is_terminal(self) -> None:
        """Without investment authority to cheapen, the retry-raise alone
        carries a "high" base to the "xhigh" ceiling — genuinely terminal,
        same as before the fix. Regression guard proving the fix did not
        just flip every case to non-terminal."""
        executor = _make_executor_with_active_ladder()
        executor._reasoning_effort = "high"
        seed = _make_seed()  # no investment on the AC spec

        terminal = await executor._root_ac_terminal_state(
            seed=seed,
            ac_idx=0,
            result=_failed_result(),
            retry_attempt=3,
        )

        assert terminal is True

    @pytest.mark.asyncio
    async def test_model_router_backend_mismatch_treated_as_dormant_like_live_dispatch(
        self,
    ) -> None:
        """``resolve_execute_model`` treats a router built for a DIFFERENT
        backend than the currently-configured adapter as absent (the
        cross-harness redispatch guard). The terminal check must observe the
        identical treatment — calling the lower-level ``decide_model``
        directly (the pre-fix shape) would ignore this guard and use the
        router anyway."""
        executor = _make_executor_with_active_ladder()
        executor._adapter.runtime_backend = "codex"  # no longer matches the router's "claude"
        seed = _make_seed()

        # With the model axis forced dormant by the mismatch and no effort
        # axis configured either, there is no ladder to have exhausted.
        terminal = await executor._root_ac_terminal_state(
            seed=seed,
            ac_idx=0,
            result=_failed_result(),
            retry_attempt=3,
        )

        assert terminal is False


class TestLateralEscalationStateDurability:
    """Fix 6 (BLOCKING, PR #1648 review): a process restart/resume recreates
    the executor with ``self._lateral_escalation_states`` EMPTY. A parked
    AC's escalation streak/personas-tried/parked cadence must be
    reconstructed from its own durable event history on first access, not
    silently dropped (restarting the persona cycle from scratch and losing
    the long-backoff parked cadence)."""

    @pytest.fixture
    async def memory_event_store(self) -> AsyncIterator[EventStore]:
        store = EventStore("sqlite+aiosqlite:///:memory:")
        await store.initialize()
        try:
            yield store
        finally:
            await store.close()

    @staticmethod
    def _cold_start_executor(event_store: EventStore) -> ParallelACExecutor:
        """A FRESH executor instance — as if just recreated after a restart —
        wired to a durable event store that may already carry this run's
        history."""
        executor = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )
        executor._lateral_escalation_enabled = True
        return executor

    @pytest.mark.asyncio
    async def test_reconstructs_parked_state_from_durable_event_on_cold_start(
        self, memory_event_store: EventStore
    ) -> None:
        node_id = ExecutionNodeIdentity.root(execution_context_id="exec-1", ac_index=0).node_id
        await memory_event_store.append(
            BaseEvent(
                type="execution.ac.parked_for_operator",
                aggregate_type="execution",
                aggregate_id="exec-1",
                data={
                    "execution_id": "exec-1",
                    "session_id": "s1",
                    "node_id": node_id,
                    "root_ac_index": 0,
                    "personas_tried": ["hacker", "researcher"],
                    "consecutive_terminal_failures": 6,
                    "backoff_seconds": 300.0,
                    "reason": "all lateral-thinking personas exhausted",
                },
            )
        )
        executor = self._cold_start_executor(memory_event_store)
        assert executor._lateral_escalation_states == {}  # cold start: nothing cached yet

        state = await executor._load_lateral_escalation_state(0, execution_id="exec-1")

        assert state.parked is True
        assert state.consecutive_terminal_failures == 6
        assert state.personas_tried == (ThinkingPersona.HACKER, ThinkingPersona.RESEARCHER)
        # Cached for subsequent same-process calls (no repeat replay).
        assert executor._lateral_escalation_states[0] is state

    @pytest.mark.asyncio
    async def test_resolved_park_reconstructs_as_fresh_not_still_parked(
        self, memory_event_store: EventStore
    ) -> None:
        """A parked-then-succeeded AC (Fix 8's resolution event present)
        must NOT reconstruct as still-parked after a restart."""
        node_id = ExecutionNodeIdentity.root(execution_context_id="exec-1", ac_index=0).node_id
        await memory_event_store.append(
            BaseEvent(
                type="execution.ac.parked_for_operator",
                aggregate_type="execution",
                aggregate_id="exec-1",
                data={
                    "execution_id": "exec-1",
                    "session_id": "s1",
                    "node_id": node_id,
                    "root_ac_index": 0,
                    "personas_tried": ["hacker"],
                    "consecutive_terminal_failures": 5,
                    "backoff_seconds": 300.0,
                    "reason": "all lateral-thinking personas exhausted",
                },
            )
        )
        await memory_event_store.append(
            BaseEvent(
                type="execution.ac.parked_resolved",
                aggregate_type="execution",
                aggregate_id="exec-1",
                data={
                    "execution_id": "exec-1",
                    "session_id": "s1",
                    "node_id": node_id,
                    "root_ac_index": 0,
                },
            )
        )
        executor = self._cold_start_executor(memory_event_store)

        state = await executor._load_lateral_escalation_state(0, execution_id="exec-1")

        assert state == LateralEscalationState()

    @pytest.mark.asyncio
    async def test_no_durable_event_reconstructs_fresh_state(
        self, memory_event_store: EventStore
    ) -> None:
        executor = self._cold_start_executor(memory_event_store)

        state = await executor._load_lateral_escalation_state(0, execution_id="exec-1")

        assert state == LateralEscalationState()

    @pytest.mark.asyncio
    async def test_in_memory_cache_hit_skips_replay(self, memory_event_store: EventStore) -> None:
        executor = self._cold_start_executor(memory_event_store)
        seeded = LateralEscalationState(consecutive_terminal_failures=2, parked=False)
        executor._lateral_escalation_states[0] = seeded

        state = await executor._load_lateral_escalation_state(0, execution_id="exec-1")

        assert state is seeded

    @pytest.mark.asyncio
    async def test_reconstructed_parked_state_applies_long_backoff_immediately(
        self, memory_event_store: EventStore
    ) -> None:
        """End-to-end through the actual ladder entry point: a 'cold start'
        AC that was durably parked before a restart must resume the
        long-backoff parked cadence on its VERY FIRST post-restart dispatch —
        not restart persona cycling from scratch, and not lose the long
        backoff."""
        node_id = ExecutionNodeIdentity.root(execution_context_id="exec-1", ac_index=0).node_id
        await memory_event_store.append(
            BaseEvent(
                type="execution.ac.parked_for_operator",
                aggregate_type="execution",
                aggregate_id="exec-1",
                data={
                    "execution_id": "exec-1",
                    "session_id": "s1",
                    "node_id": node_id,
                    "root_ac_index": 0,
                    "personas_tried": [p.value for p in ThinkingPersona],
                    "consecutive_terminal_failures": 20,
                    "backoff_seconds": 300.0,
                    "reason": "all lateral-thinking personas exhausted",
                },
            )
        )
        executor = _make_executor_with_active_ladder()
        # Swap in the durable store carrying this AC's parked history, as if
        # this executor were recreated after a restart against the same run.
        executor._event_store = memory_event_store

        call_count = {"n": 0}

        async def succeeds_immediately(**kwargs: object) -> list[ACExecutionResult]:
            call_count["n"] += 1
            return [_success_result()]

        executor._execute_ac_batch = AsyncMock(side_effect=succeeds_immediately)
        executor._apply_verify_gate = AsyncMock(side_effect=lambda **kwargs: kwargs["result"])

        outcome = await _ladder(executor, result=_failed_result(error="still broken"))

        assert outcome is not None
        assert outcome.success is True
        assert call_count["n"] == 1
        # The long-backoff parked cadence applied on the FIRST post-restart
        # dispatch — proving the reconstructed state was actually consulted,
        # not a fresh LateralEscalationState() that would instead do "one
        # more identical retry" without sleeping at all.
        executor._sleep.assert_awaited_once_with(executor._parked_retry_backoff_seconds)
