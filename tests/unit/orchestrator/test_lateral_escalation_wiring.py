"""Tests wiring the lateral-persona escalation ladder (Task 2) into the
parallel executor's ``_maybe_run_lateral_escalation_ladder``. The persona
invocation (``_execute_ac_batch``) and the backoff (``executor._sleep``) are
always mocked — this suite never actually sleeps or dispatches a real agent.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

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
        # Round-5 Finding #3 (BLOCKING, superseding round 2's Fix 8 stance):
        # even though this AC never parked, every loop iteration durably
        # emitted a ``progressed`` event before its redispatch — so a success
        # mid-persona-cycle MUST still emit the resolution companion event,
        # or the latest replayable record stays ``progressed`` and
        # projections show a completed AC as "escalating" forever.
        executor._event_emitter.emit_ac_parked_resolved.assert_awaited_once()

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

    @pytest.mark.asyncio
    async def test_reconstructs_in_flight_streak_from_progressed_event_before_parking(
        self, memory_event_store: EventStore
    ) -> None:
        """Fix 5 (round 2, BLOCKING): an AC that accrued streak/persona
        progress but never actually reached full parking must still
        reconstruct that in-flight state from
        ``execution.ac.lateral_escalation_progressed`` on a cold start --
        not silently restart the ladder from scratch."""
        node_id = ExecutionNodeIdentity.root(execution_context_id="exec-1", ac_index=0).node_id
        await memory_event_store.append(
            BaseEvent(
                type="execution.ac.lateral_escalation_progressed",
                aggregate_type="execution",
                aggregate_id="exec-1",
                data={
                    "execution_id": "exec-1",
                    "session_id": "s1",
                    "node_id": node_id,
                    "root_ac_index": 0,
                    "personas_tried": ["hacker"],
                    "consecutive_terminal_failures": 3,
                    "parked": False,
                    "persona": "hacker",
                },
            )
        )
        executor = self._cold_start_executor(memory_event_store)
        assert executor._lateral_escalation_states == {}

        state = await executor._load_lateral_escalation_state(0, execution_id="exec-1")

        assert state.parked is False
        assert state.consecutive_terminal_failures == 3
        assert state.personas_tried == (ThinkingPersona.HACKER,)

    @pytest.mark.asyncio
    async def test_latest_progressed_event_wins_over_earlier_ones(
        self, memory_event_store: EventStore
    ) -> None:
        node_id = ExecutionNodeIdentity.root(execution_context_id="exec-1", ac_index=0).node_id
        for streak, persona in ((1, None), (2, "hacker"), (3, "architect")):
            await memory_event_store.append(
                BaseEvent(
                    type="execution.ac.lateral_escalation_progressed",
                    aggregate_type="execution",
                    aggregate_id="exec-1",
                    data={
                        "execution_id": "exec-1",
                        "session_id": "s1",
                        "node_id": node_id,
                        "root_ac_index": 0,
                        "personas_tried": (
                            []
                            if persona is None
                            else (["hacker"] if persona == "hacker" else ["hacker", "architect"])
                        ),
                        "consecutive_terminal_failures": streak,
                        "parked": False,
                        "persona": persona,
                    },
                )
            )
        executor = self._cold_start_executor(memory_event_store)

        state = await executor._load_lateral_escalation_state(0, execution_id="exec-1")

        assert state.consecutive_terminal_failures == 3
        assert state.personas_tried == (ThinkingPersona.HACKER, ThinkingPersona.ARCHITECT)

    @pytest.mark.asyncio
    async def test_progressed_event_after_resolution_reconstructs_the_new_cycle(
        self, memory_event_store: EventStore
    ) -> None:
        """A parked-then-resolved AC that later re-engages the ladder for a
        FRESH failure cycle must reconstruct that NEW cycle's progress, not
        stay stuck at "resolved -> fresh empty" forever."""
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
                    "consecutive_terminal_failures": 10,
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
        # A brand new failure cycle for the SAME root AC starts accruing
        # progress again after the resolution.
        await memory_event_store.append(
            BaseEvent(
                type="execution.ac.lateral_escalation_progressed",
                aggregate_type="execution",
                aggregate_id="exec-1",
                data={
                    "execution_id": "exec-1",
                    "session_id": "s1",
                    "node_id": node_id,
                    "root_ac_index": 0,
                    "personas_tried": [],
                    "consecutive_terminal_failures": 1,
                    "parked": False,
                    "persona": None,
                },
            )
        )
        executor = self._cold_start_executor(memory_event_store)

        state = await executor._load_lateral_escalation_state(0, execution_id="exec-1")

        assert state.parked is False
        assert state.consecutive_terminal_failures == 1
        assert state.personas_tried == ()

    @pytest.mark.asyncio
    async def test_resume_after_crash_mid_redispatch_continues_ladder_not_restarts(
        self, memory_event_store: EventStore
    ) -> None:
        """End-to-end: a process that crashes DURING a redispatch (nothing
        catches it -- simulating the whole process dying, not just an
        infra-fatal AC result) must not lose the persona/streak progress
        from the iterations that already completed before the crash."""
        executor = _make_executor_with_active_ladder()
        executor._event_store = memory_event_store

        call_count = {"n": 0}

        async def fails_then_crashes(**kwargs: object) -> list[ACExecutionResult]:
            call_count["n"] += 1
            if call_count["n"] in (1, 2):
                return [_failed_result(error=f"attempt {call_count['n']} failed")]
            # Third redispatch: the process itself crashes -- nothing ever
            # returns, and nothing here catches it.
            raise RuntimeError("simulated process crash")

        executor._execute_ac_batch = AsyncMock(side_effect=fails_then_crashes)
        executor._apply_verify_gate = AsyncMock(side_effect=lambda **kwargs: kwargs["result"])

        with pytest.raises(RuntimeError, match="simulated process crash"):
            await _ladder(
                executor,
                result=_failed_result(error="attempt 0 failed"),
                ac_retry_attempts={0: 2},
            )

        # "Resume": a brand-new executor, same durable store, empty in-memory
        # cache -- exactly what a process restart looks like.
        resumed = _make_executor_with_active_ladder()
        resumed._event_store = memory_event_store
        assert resumed._lateral_escalation_states == {}

        state = await resumed._load_lateral_escalation_state(0, execution_id="exec-1")

        # 3 loop iterations ran before the crash on the 3rd redispatch call
        # (streak 1 -> 2 -> 3, with a persona selected on iterations 2 and 3
        # once the streak reached the persona-cycling threshold) -- that
        # progress must have survived even though parking never happened.
        assert state.parked is False
        assert state.consecutive_terminal_failures == 3
        assert len(state.personas_tried) == 2


class TestLateralEscalationStateFailsClosedOnReplayFailure:
    """Fix 5 (round 3, BLOCKING): a genuine READ failure (every
    ``_replay_with_retry`` attempt raises) must NOT be silently treated the
    same as "this AC was never escalated" -- that is fail-OPEN and would let
    a restart repeat already-tried personas or lose a parked AC's long-backoff
    cadence entirely. ``_load_lateral_escalation_state`` must fail closed by
    assuming ``parked=True`` instead of falling through to a fresh, empty
    state.
    """

    @pytest.mark.asyncio
    async def test_replay_exception_fails_closed_to_parked(self) -> None:
        executor = _make_executor()
        executor._lateral_escalation_enabled = True
        executor._event_store.replay = AsyncMock(side_effect=RuntimeError("db unavailable"))

        with patch("ouroboros.orchestrator.parallel_executor.anyio.sleep", AsyncMock()):
            state = await executor._load_lateral_escalation_state(0, execution_id="exec-1")

        assert state.parked is True
        # Fail-closed default is NOT cached -- a later successful read in the
        # same process must not stay poisoned by a transient failure.
        assert 0 not in executor._lateral_escalation_states

    @pytest.mark.asyncio
    async def test_replay_retries_before_failing_closed(self) -> None:
        """``_replay_with_retry`` must actually retry (mirroring
        ``_safe_emit_event``'s pattern) before giving up."""
        executor = _make_executor()
        executor._lateral_escalation_enabled = True
        attempts = {"n": 0}

        async def _flaky_replay(aggregate_type: str, aggregate_id: str) -> list[BaseEvent]:
            attempts["n"] += 1
            raise RuntimeError(f"transient failure {attempts['n']}")

        executor._event_store.replay = AsyncMock(side_effect=_flaky_replay)

        with patch("ouroboros.orchestrator.parallel_executor.anyio.sleep", AsyncMock()):
            events = await executor._replay_with_retry("execution", "exec-1", max_retries=3)

        assert events is None
        assert attempts["n"] == 3

    @pytest.mark.asyncio
    async def test_replay_succeeds_after_transient_failure(self) -> None:
        """A transient failure that recovers before the retry budget is spent
        must return the REAL events, not the fail-closed sentinel."""
        executor = _make_executor()
        attempts = {"n": 0}

        async def _recovers_on_second_try(
            aggregate_type: str, aggregate_id: str
        ) -> list[BaseEvent]:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("transient failure")
            return []

        executor._event_store.replay = AsyncMock(side_effect=_recovers_on_second_try)

        with patch("ouroboros.orchestrator.parallel_executor.anyio.sleep", AsyncMock()):
            events = await executor._replay_with_retry("execution", "exec-1", max_retries=3)

        assert events == []
        assert attempts["n"] == 2


class TestLateralEscalationStateFailsClosedOnMalformedEventData:
    """Round-4 Finding #3 (BLOCKING): replay SUCCEEDS and finds a matching
    escalation event for this node id, but its payload is malformed. The
    previous parse silently "cleaned up" corruption -- an unrecognized
    persona value was discarded, an invalid streak count became ``0``, an
    invalid ``parked`` value became ``False`` -- reconstructing a fresh,
    non-parked, streak-0 state from a record that may have said PARKED.
    That is fail-OPEN: the state directly controls whether already-tried
    personas are repeated and whether parked status survives a restart.
    "Found the event but couldn't validate its contents" must be treated
    the SAME way as "replay failed entirely": a synthetic PARKED=True
    fail-closed sentinel, not cached.
    """

    @pytest.fixture
    async def memory_event_store(self) -> AsyncIterator[EventStore]:
        store = EventStore("sqlite+aiosqlite:///:memory:")
        await store.initialize()
        try:
            yield store
        finally:
            await store.close()

    @staticmethod
    async def _load_with_progressed_payload(
        memory_event_store: EventStore, payload_overrides: dict[str, object]
    ) -> tuple[LateralEscalationState, ParallelACExecutor]:
        node_id = ExecutionNodeIdentity.root(execution_context_id="exec-1", ac_index=0).node_id
        data: dict[str, object] = {
            "execution_id": "exec-1",
            "session_id": "s1",
            "node_id": node_id,
            "root_ac_index": 0,
            "personas_tried": ["hacker"],
            "consecutive_terminal_failures": 3,
            "parked": False,
            "persona": "hacker",
        }
        data.update(payload_overrides)
        await memory_event_store.append(
            BaseEvent(
                type="execution.ac.lateral_escalation_progressed",
                aggregate_type="execution",
                aggregate_id="exec-1",
                data=data,
            )
        )
        executor = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=memory_event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )
        executor._lateral_escalation_enabled = True
        state = await executor._load_lateral_escalation_state(0, execution_id="exec-1")
        return state, executor

    @pytest.mark.asyncio
    async def test_unrecognized_persona_value_fails_closed_to_parked(
        self, memory_event_store: EventStore
    ) -> None:
        """Before the fix: the unknown persona was silently DISCARDED and the
        rest of the state admitted -- letting a restart re-try a persona the
        record says was already tried."""
        state, executor = await self._load_with_progressed_payload(
            memory_event_store, {"personas_tried": ["hacker", "not-a-real-persona"]}
        )

        assert state.parked is True
        # Fail-closed sentinel is NOT cached -- consistent with the
        # replay-failure convention.
        assert 0 not in executor._lateral_escalation_states

    @pytest.mark.asyncio
    async def test_personas_tried_not_a_list_fails_closed_to_parked(
        self, memory_event_store: EventStore
    ) -> None:
        state, executor = await self._load_with_progressed_payload(
            memory_event_store, {"personas_tried": "hacker"}
        )

        assert state.parked is True
        assert 0 not in executor._lateral_escalation_states

    @pytest.mark.asyncio
    async def test_invalid_streak_fails_closed_to_parked(
        self, memory_event_store: EventStore
    ) -> None:
        """Before the fix: an invalid streak silently became 0 -- resetting
        the persona-cycling threshold progress."""
        state, executor = await self._load_with_progressed_payload(
            memory_event_store, {"consecutive_terminal_failures": "three"}
        )

        assert state.parked is True
        assert 0 not in executor._lateral_escalation_states

    @pytest.mark.asyncio
    async def test_negative_streak_fails_closed_to_parked(
        self, memory_event_store: EventStore
    ) -> None:
        state, executor = await self._load_with_progressed_payload(
            memory_event_store, {"consecutive_terminal_failures": -2}
        )

        assert state.parked is True
        assert 0 not in executor._lateral_escalation_states

    @pytest.mark.asyncio
    async def test_invalid_parked_value_fails_closed_to_parked(
        self, memory_event_store: EventStore
    ) -> None:
        """Before the fix: a non-bool ``parked`` silently became ``False`` --
        the exact fail-open direction (a corrupted record of a PARKED AC
        reconstructing as not-parked)."""
        state, executor = await self._load_with_progressed_payload(
            memory_event_store, {"parked": "yes"}
        )

        assert state.parked is True
        assert 0 not in executor._lateral_escalation_states

    @pytest.mark.asyncio
    async def test_malformed_parked_for_operator_payload_fails_closed(
        self, memory_event_store: EventStore
    ) -> None:
        """The ``parked_for_operator`` branch must apply the same strict
        validation to its persona/streak fields (its parked flag is implied
        by the event type, so the sentinel coincidentally matches
        ``parked=True`` -- but the personas/streak must not be silently
        \"cleaned\" either, or the ladder would forget which personas were
        exhausted)."""
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
                    "personas_tried": ["hacker", 42],
                    "consecutive_terminal_failures": 6,
                    "backoff_seconds": 300.0,
                    "reason": "all lateral-thinking personas exhausted",
                },
            )
        )
        executor = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=memory_event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )
        executor._lateral_escalation_enabled = True

        state = await executor._load_lateral_escalation_state(0, execution_id="exec-1")

        assert state.parked is True
        assert 0 not in executor._lateral_escalation_states

    @pytest.mark.asyncio
    async def test_well_formed_payload_still_reconstructs_normally(
        self, memory_event_store: EventStore
    ) -> None:
        """Negative control: strict validation must not reject the exact
        payload the emitter actually writes."""
        state, executor = await self._load_with_progressed_payload(memory_event_store, {})

        assert state.parked is False
        assert state.consecutive_terminal_failures == 3
        assert state.personas_tried == (ThinkingPersona.HACKER,)
        assert executor._lateral_escalation_states[0] is state


class TestParkedRetryBackoffSecondsFiniteGuard:
    """Fix 7 (round 2, BLOCKING) defense-in-depth: this low-level constructor
    is a direct, unvalidated entry point of its own -- independent of the two
    contract boundaries covered by ``tests/unit/config/test_models.py`` and
    ``tests/unit/orchestrator/test_routing_contract_resume.py``. Without this
    guard, ``max(1.0, parked_retry_backoff_seconds)`` happily lets
    ``float("inf")`` through to ``asyncio.sleep(inf)``, hanging that AC's slot
    forever with no operator-visible signal."""

    def test_infinite_backoff_is_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            ParallelACExecutor(
                adapter=MagicMock(),
                event_store=AsyncMock(),
                console=MagicMock(),
                enable_decomposition=False,
                parked_retry_backoff_seconds=float("inf"),
            )

    def test_nan_backoff_is_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            ParallelACExecutor(
                adapter=MagicMock(),
                event_store=AsyncMock(),
                console=MagicMock(),
                enable_decomposition=False,
                parked_retry_backoff_seconds=float("nan"),
            )

    def test_finite_backoff_still_constructs(self) -> None:
        executor = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=AsyncMock(),
            console=MagicMock(),
            enable_decomposition=False,
            parked_retry_backoff_seconds=900.0,
        )
        assert executor._parked_retry_backoff_seconds == 900.0


class TestInfraFatalMidLadderPropagates:
    """Fix 3 (round 2, BLOCKING): an infra-fatal (or otherwise non-retryable)
    result discovered on a REDISPATCH INSIDE the ladder loop must propagate
    out as the ladder's own return value, not vanish into a ``None`` that
    would make the caller fall back to a stale pre-ladder result."""

    @pytest.mark.asyncio
    async def test_infra_fatal_result_mid_ladder_is_returned_not_none(self) -> None:
        executor = _make_executor_with_active_ladder()

        call_count = {"n": 0}

        async def fake_execute_ac_batch(**kwargs: object) -> list[ACExecutionResult]:
            call_count["n"] += 1
            if call_count["n"] == 1:
                return [_failed_result(error="attempt 1 failed")]
            # The SECOND redispatch (inside the ladder loop) crashes at the
            # infra level -- exactly the scenario that used to vanish into
            # ``None`` and make the caller finalize a stale earlier result.
            return [
                ACExecutionResult(
                    ac_index=0,
                    ac_content="Implement the widget",
                    success=False,
                    error="adapter crashed mid-dispatch",
                    infra_fatal=True,
                )
            ]

        executor._execute_ac_batch = AsyncMock(side_effect=fake_execute_ac_batch)
        executor._apply_verify_gate = AsyncMock(side_effect=lambda **kwargs: kwargs["result"])

        outcome = await _ladder(executor, result=_failed_result(error="attempt 0 failed"))

        assert outcome is not None
        assert outcome.success is False
        assert outcome.infra_fatal is True
        assert outcome.error == "adapter crashed mid-dispatch"

    @pytest.mark.asyncio
    async def test_raw_exception_mid_ladder_is_wrapped_and_returned_not_none(self) -> None:
        """A raw, uncaught exception escaping ``_execute_ac_batch``'s own
        per-AC handling mid-ladder must ALSO propagate out (wrapped as an
        infra-fatal ``ACExecutionResult``, mirroring how the atomic leaf's
        own exception handler wraps this same class of failure), not vanish
        into ``None``."""
        executor = _make_executor_with_active_ladder()

        call_count = {"n": 0}

        async def fake_execute_ac_batch(**kwargs: object) -> list[object]:
            call_count["n"] += 1
            if call_count["n"] == 1:
                return [_failed_result(error="attempt 1 failed")]
            return [RuntimeError("adapter crashed raw")]

        executor._execute_ac_batch = AsyncMock(side_effect=fake_execute_ac_batch)
        executor._apply_verify_gate = AsyncMock(side_effect=lambda **kwargs: kwargs["result"])

        outcome = await _ladder(executor, result=_failed_result(error="attempt 0 failed"))

        assert outcome is not None
        assert outcome.success is False
        assert outcome.infra_fatal is True
        assert "adapter crashed raw" in (outcome.error or "")


class TestLadderFailurePropagatesToRecoveryExhausted:
    """Fix 3 (round 2, BLOCKING): the CALLER (``_run_batch_with_verify_and_retry``)
    must emit recovery-exhausted using the ladder's OWN fresh failed result
    when the ladder returns one, never the stale pre-ladder result captured
    before the ladder ran."""

    @pytest.mark.asyncio
    async def test_caller_uses_fresh_escalated_failure_not_stale_result(self) -> None:
        from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata

        executor = _make_executor_with_active_ladder()
        executor._ac_retry_attempts = 1

        stale_result = _failed_result(error="stale pre-ladder failure")
        fresh_infra_fatal = ACExecutionResult(
            ac_index=0,
            ac_content="Implement the widget",
            success=False,
            error="adapter crashed mid-ladder",
            infra_fatal=True,
        )

        executor._execute_ac_batch = AsyncMock(return_value=[stale_result])
        executor._apply_verify_gate = AsyncMock(side_effect=lambda **kwargs: kwargs["result"])
        executor._maybe_run_lateral_escalation_ladder = AsyncMock(return_value=fresh_infra_fatal)
        executor._emit_recovery_exhausted = AsyncMock()

        seed = Seed(
            goal="Implement the stubborn widget",
            constraints=(),
            acceptance_criteria=(AcceptanceCriterionSpec(description="Implement the widget"),),
            ontology_schema=OntologySchema(name="Ladder", description="Test schema"),
            metadata=SeedMetadata(ambiguity_score=0.05),
        )

        results = await executor._run_batch_with_verify_and_retry(
            seed=seed,
            batch_executable=[0],
            session_id="s1",
            execution_id="exec-1",
            tools=[],
            tool_catalog=None,
            system_prompt="",
            level_contexts=[],
            ac_retry_attempts={0: 0},
            execution_counters=None,
        )

        assert results[0] is fresh_infra_fatal
        executor._emit_recovery_exhausted.assert_awaited_once()
        _, kwargs = executor._emit_recovery_exhausted.await_args
        assert kwargs["result"] is fresh_infra_fatal
        assert kwargs["result"] is not stale_result
        assert kwargs["retry_termination_reason"] == "infra_fatal"

    @pytest.mark.asyncio
    async def test_caller_skips_recovery_exhausted_for_a_breakthrough_success(self) -> None:
        """Unchanged behavior: a genuine ladder SUCCESS must never trigger a
        spurious recovery-exhausted emission."""
        from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata

        executor = _make_executor_with_active_ladder()
        executor._ac_retry_attempts = 1

        stale_result = _failed_result(error="stale pre-ladder failure")
        breakthrough = _success_result()

        executor._execute_ac_batch = AsyncMock(return_value=[stale_result])
        executor._apply_verify_gate = AsyncMock(side_effect=lambda **kwargs: kwargs["result"])
        executor._maybe_run_lateral_escalation_ladder = AsyncMock(return_value=breakthrough)
        executor._emit_recovery_exhausted = AsyncMock()

        seed = Seed(
            goal="Implement the stubborn widget",
            constraints=(),
            acceptance_criteria=(AcceptanceCriterionSpec(description="Implement the widget"),),
            ontology_schema=OntologySchema(name="Ladder", description="Test schema"),
            metadata=SeedMetadata(ambiguity_score=0.05),
        )

        results = await executor._run_batch_with_verify_and_retry(
            seed=seed,
            batch_executable=[0],
            session_id="s1",
            execution_id="exec-1",
            tools=[],
            tool_catalog=None,
            system_prompt="",
            level_contexts=[],
            ac_retry_attempts={0: 0},
            execution_counters=None,
        )

        assert results[0] is breakthrough
        executor._emit_recovery_exhausted.assert_not_awaited()


class TestZeroRetryBudgetStillEngagesLadder:
    """Fix 6 (round 3, BLOCKING): ``ac_retry_attempts=0`` is a valid,
    documented configuration (no same-runtime retries before escalation).
    Before this fix, ``_run_batch_with_verify_and_retry``'s
    ``if self._ac_retry_attempts <= 0:`` early return emitted
    recovery-exhausted directly and returned, completely bypassing
    ``_maybe_run_lateral_escalation_ladder`` -- only reachable through the
    ``while pending:`` loop, which a zero-budget config never enters. Even
    with ``lateral_escalation_enabled=True``, a zero ordinary retry budget
    meant the AC surfaced as exhausted/failed immediately: exactly the
    "give up" behavior this whole feature exists to prevent.
    """

    @pytest.mark.asyncio
    async def test_zero_budget_exhausted_ac_is_handed_to_the_ladder(self) -> None:
        from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata

        executor = _make_executor_with_active_ladder()
        executor._ac_retry_attempts = 0

        initial_failure = _failed_result(error="stuck at maximum strength")
        breakthrough = _success_result()

        executor._execute_ac_batch = AsyncMock(return_value=[initial_failure])
        executor._apply_verify_gate = AsyncMock(side_effect=lambda **kwargs: kwargs["result"])
        executor._maybe_run_lateral_escalation_ladder = AsyncMock(return_value=breakthrough)
        executor._emit_recovery_exhausted = AsyncMock()

        seed = Seed(
            goal="Implement the stubborn widget",
            constraints=(),
            acceptance_criteria=(AcceptanceCriterionSpec(description="Implement the widget"),),
            ontology_schema=OntologySchema(name="Ladder", description="Test schema"),
            metadata=SeedMetadata(ambiguity_score=0.05),
        )

        results = await executor._run_batch_with_verify_and_retry(
            seed=seed,
            batch_executable=[0],
            session_id="s1",
            execution_id="exec-1",
            tools=[],
            tool_catalog=None,
            system_prompt="",
            level_contexts=[],
            ac_retry_attempts={0: 0},
            execution_counters=None,
        )

        # The ladder MUST have been consulted even though the ordinary retry
        # budget is zero -- the actual reported bug.
        executor._maybe_run_lateral_escalation_ladder.assert_awaited_once()
        # A breakthrough success from the ladder must win over the initial
        # exhausted failure, and must never trigger recovery-exhausted.
        assert results[0] is breakthrough
        executor._emit_recovery_exhausted.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_zero_budget_ladder_failure_still_emits_recovery_exhausted_with_fresh_result(
        self,
    ) -> None:
        """When the ladder itself terminates in failure (infra-fatal or
        otherwise non-retryable) for a zero-budget AC, recovery-exhausted
        must still fire using the ladder's OWN fresh result, not the stale
        pre-ladder one -- the same guarantee Fix 3 (round 2) already gives
        the nonzero-budget path."""
        from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata

        executor = _make_executor_with_active_ladder()
        executor._ac_retry_attempts = 0

        stale_result = _failed_result(error="stale pre-ladder failure")
        fresh_infra_fatal = ACExecutionResult(
            ac_index=0,
            ac_content="Implement the widget",
            success=False,
            error="adapter crashed mid-ladder",
            infra_fatal=True,
        )

        executor._execute_ac_batch = AsyncMock(return_value=[stale_result])
        executor._apply_verify_gate = AsyncMock(side_effect=lambda **kwargs: kwargs["result"])
        executor._maybe_run_lateral_escalation_ladder = AsyncMock(return_value=fresh_infra_fatal)
        executor._emit_recovery_exhausted = AsyncMock()

        seed = Seed(
            goal="Implement the stubborn widget",
            constraints=(),
            acceptance_criteria=(AcceptanceCriterionSpec(description="Implement the widget"),),
            ontology_schema=OntologySchema(name="Ladder", description="Test schema"),
            metadata=SeedMetadata(ambiguity_score=0.05),
        )

        results = await executor._run_batch_with_verify_and_retry(
            seed=seed,
            batch_executable=[0],
            session_id="s1",
            execution_id="exec-1",
            tools=[],
            tool_catalog=None,
            system_prompt="",
            level_contexts=[],
            ac_retry_attempts={0: 0},
            execution_counters=None,
        )

        assert results[0] is fresh_infra_fatal
        executor._emit_recovery_exhausted.assert_awaited_once()
        _, kwargs = executor._emit_recovery_exhausted.await_args
        assert kwargs["result"] is fresh_infra_fatal
        assert kwargs["result"] is not stale_result
        assert kwargs["retry_termination_reason"] == "infra_fatal"

    @pytest.mark.asyncio
    async def test_zero_budget_ladder_disabled_preserves_original_behavior(self) -> None:
        """Sanity control: with the ladder OFF (today's default), a zero
        retry budget must behave exactly as before -- immediate
        recovery-exhausted, no ladder consultation side effects beyond the
        already-guarded no-op ``_maybe_run_lateral_escalation_ladder`` call."""
        from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata

        executor = _make_executor()  # lateral escalation NOT enabled
        executor._ac_retry_attempts = 0

        initial_failure = _failed_result(error="stuck at maximum strength")

        executor._execute_ac_batch = AsyncMock(return_value=[initial_failure])
        executor._apply_verify_gate = AsyncMock(side_effect=lambda **kwargs: kwargs["result"])
        executor._emit_recovery_exhausted = AsyncMock()

        seed = Seed(
            goal="Implement the stubborn widget",
            constraints=(),
            acceptance_criteria=(AcceptanceCriterionSpec(description="Implement the widget"),),
            ontology_schema=OntologySchema(name="Ladder", description="Test schema"),
            metadata=SeedMetadata(ambiguity_score=0.05),
        )

        results = await executor._run_batch_with_verify_and_retry(
            seed=seed,
            batch_executable=[0],
            session_id="s1",
            execution_id="exec-1",
            tools=[],
            tool_catalog=None,
            system_prompt="",
            level_contexts=[],
            ac_retry_attempts={0: 0},
            execution_counters=None,
        )

        assert results[0] is initial_failure
        executor._emit_recovery_exhausted.assert_awaited_once()
        _, kwargs = executor._emit_recovery_exhausted.await_args
        assert kwargs["result"] is initial_failure
        assert kwargs["retry_termination_reason"] == "budget_exhausted"

    @pytest.mark.asyncio
    async def test_zero_budget_ladder_genuinely_engages_with_realistic_router(self) -> None:
        """Fix 6 redo (round 3 follow-up review): every test above MOCKS
        ``_maybe_run_lateral_escalation_ladder`` -- they only prove it gets
        CALLED, never that it actually ENGAGES. A mocked-call test would NOT
        have caught the actual bug: ``_root_ac_terminal_state`` gates ladder
        eligibility on ``is_terminal_state_failure(..., retry_attempt=0)``,
        and for a REALISTICALLY configured router (base tier below the
        frontier ceiling -- the common case, unlike
        ``_make_executor_with_active_ladder``'s fixture which starts
        ALREADY at the ceiling, the edge case the finding notes accidentally
        worked before this fix) that resolves to a non-ceiling tier at
        ``retry_attempt=0`` -- exactly what a zero-same-runtime-retry-budget
        AC's one and only dispatch always is. Before this redo, the ladder
        declined on its very first check and fell straight through to plain
        recovery-exhausted, never redispatching at all.

        This test drives the REAL, unmocked ladder through
        ``_run_batch_with_verify_and_retry`` and asserts a genuine SECOND
        dispatch happens with a real breakthrough success -- the only way
        that is observable is if the ladder actually engaged.
        """
        from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata

        executor = _make_executor()
        executor._lateral_escalation_enabled = True
        executor._ac_retry_attempts = 0
        # A REALISTIC router: base tier is "standard", one notch below the
        # "frontier" ceiling, with an escalation threshold high enough that
        # retry_attempt=0 -- the only attempt a zero-budget config's
        # ORDINARY dispatch ever makes -- does not itself resolve to the
        # ceiling tier.
        executor._model_router = ModelRouter(
            tier_models={
                "frugal": "model-frugal",
                "standard": "model-standard",
                "frontier": "model-frontier",
            },
            runtime_backend="claude",
            child_tier="frugal",
            base_tier="standard",
            escalation_retry_threshold=5,
        )
        executor._adapter.runtime_backend = "claude"
        executor._reasoning_effort = "medium"  # also below the "xhigh" ceiling

        initial_failure = _failed_result(error="stuck at maximum strength")
        breakthrough = _success_result()

        dispatch_calls: list[int] = []

        async def fake_execute_ac_batch(**kwargs: object) -> list[ACExecutionResult]:
            dispatch_calls.append(len(dispatch_calls) + 1)
            if len(dispatch_calls) == 1:
                return [initial_failure]
            return [breakthrough]

        executor._execute_ac_batch = AsyncMock(side_effect=fake_execute_ac_batch)
        executor._apply_verify_gate = AsyncMock(side_effect=lambda **kwargs: kwargs["result"])
        executor._emit_recovery_exhausted = AsyncMock()

        seed = Seed(
            goal="Implement the stubborn widget",
            constraints=(),
            acceptance_criteria=(AcceptanceCriterionSpec(description="Implement the widget"),),
            ontology_schema=OntologySchema(name="Ladder", description="Test schema"),
            metadata=SeedMetadata(ambiguity_score=0.05),
        )

        results = await executor._run_batch_with_verify_and_retry(
            seed=seed,
            batch_executable=[0],
            session_id="s1",
            execution_id="exec-1",
            tools=[],
            tool_catalog=None,
            system_prompt="",
            level_contexts=[],
            ac_retry_attempts={0: 0},
            execution_counters=None,
        )

        # The genuine unmocked engagement signal: a SECOND dispatch actually
        # happened. Before this redo, the ladder would have declined on its
        # first (and only) check, so ``dispatch_calls`` would have stayed
        # ``[1]`` and ``results[0]`` would have been the STALE
        # ``initial_failure``, finalized as plain recovery-exhausted.
        assert len(dispatch_calls) >= 2
        assert results[0] is breakthrough
        executor._emit_recovery_exhausted.assert_not_awaited()


class TestBudgetExhaustionAloneReachesLadder:
    """Round-4 Finding #2 (BLOCKING): effort escalation raises exactly ONE
    notch total (``EFFORT_RAISE_RETRY_THRESHOLD``), so a ``low``/``medium``
    base can NEVER literally reach the ``xhigh`` ceiling no matter how many
    same-runtime retries run. Gating ladder eligibility on the ceiling being
    reached therefore let a small retry budget (e.g. 2) starve the ladder
    out entirely: the AC exhausted as an ordinary FAILED result with every
    persona untried -- the exact "give up while escalation remains
    available" outcome this feature forbids. Exhausting the same-runtime
    retry budget must be SUFFICIENT to engage the ladder (the ladder's own
    logic then decides bail-out via success/is_decomposed), extending the
    same ``same_runtime_retry_available`` mechanism the zero-budget Fix 6
    redo introduced.
    """

    @staticmethod
    def _medium_effort_executor() -> ParallelACExecutor:
        """Ladder opted in, effort axis configured at ``medium`` (which can
        only ever climb to ``high`` -- one notch), model axis dormant."""
        executor = _make_executor()
        executor._lateral_escalation_enabled = True
        executor._reasoning_effort = "medium"
        assert executor._model_router is None
        return executor

    @pytest.mark.asyncio
    async def test_medium_effort_budget_exhausted_engages_ladder_despite_never_reaching_xhigh(
        self,
    ) -> None:
        """Direct reproduction of the review's scenario via the unmocked
        ladder: ``medium`` base effort, ``ac_retry_attempts=2`` spent
        (``retry_attempt=2`` resolves to ``high``, not ``xhigh``). Before
        the fix, the first terminal check saw effort below ceiling and
        returned ``None`` -- the AC surfaced FAILED with zero personas
        tried."""
        executor = self._medium_effort_executor()
        executor._ac_retry_attempts = 2

        call_count = {"n": 0}

        async def fake_execute_ac_batch(**kwargs: object) -> list[ACExecutionResult]:
            call_count["n"] += 1
            if call_count["n"] >= 3:
                return [_success_result()]
            return [_failed_result(error=f"ladder attempt {call_count['n']} failed")]

        executor._execute_ac_batch = AsyncMock(side_effect=fake_execute_ac_batch)
        executor._apply_verify_gate = AsyncMock(side_effect=lambda **kwargs: kwargs["result"])

        outcome = await _ladder(
            executor,
            result=_failed_result(error="budget exhausted at effort=high"),
            ac_retry_attempts={0: 2},  # configured budget of 2 fully spent
        )

        # Before the fix: ``outcome is None`` (ladder declined -- effort
        # never reached "xhigh") and ``_execute_ac_batch`` was never called.
        assert outcome is not None
        assert outcome.success is True
        assert call_count["n"] >= 1

    @pytest.mark.asyncio
    async def test_medium_effort_end_to_end_retry_loop_hands_off_to_ladder(self) -> None:
        """End-to-end through ``_run_batch_with_verify_and_retry``: initial
        dispatch + 2 budget-funded retries all fail, then the ladder's own
        redispatches continue until a breakthrough -- never a FAILED
        finalization. Before the fix the third failure finalized as plain
        recovery-exhausted."""
        executor = self._medium_effort_executor()
        executor._ac_retry_attempts = 2
        executor._emit_recovery_exhausted = AsyncMock()

        call_count = {"n": 0}

        async def fake_execute_ac_batch(**kwargs: object) -> list[ACExecutionResult]:
            call_count["n"] += 1
            if call_count["n"] >= 5:
                return [_success_result()]
            return [_failed_result(error=f"attempt {call_count['n']} failed")]

        executor._execute_ac_batch = AsyncMock(side_effect=fake_execute_ac_batch)
        executor._apply_verify_gate = AsyncMock(side_effect=lambda **kwargs: kwargs["result"])

        results = await executor._run_batch_with_verify_and_retry(
            seed=_make_seed(),
            batch_executable=[0],
            session_id="s1",
            execution_id="exec-1",
            tools=[],
            tool_catalog=None,
            system_prompt="",
            level_contexts=[],
            ac_retry_attempts={0: 0},
            execution_counters=None,
        )

        # Dispatches 1-3 are the ordinary budget (initial + 2 retries);
        # dispatches 4-5 can only be the engaged ladder's own.
        assert call_count["n"] == 5
        assert isinstance(results[0], ACExecutionResult)
        assert results[0].success is True
        executor._emit_recovery_exhausted.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_zero_budget_ladder_survives_its_own_failed_redispatches(self) -> None:
        """The review's second sub-claim: the zero-budget Fix 6 redo bypass
        fired only on the FIRST terminal check (``engaged or
        self._ac_retry_attempts > 0``). Once the ladder had redispatched
        once, normal ceiling semantics resumed -- and a ``medium`` base
        (never able to reach ``xhigh``) made the recheck report
        "not terminal", so the ladder returned the current FAILED result
        with personas still untried. The ladder must keep going across its
        OWN failed redispatches."""
        executor = self._medium_effort_executor()
        executor._ac_retry_attempts = 0

        call_count = {"n": 0}

        async def fake_execute_ac_batch(**kwargs: object) -> list[ACExecutionResult]:
            call_count["n"] += 1
            if call_count["n"] >= 4:
                return [_success_result()]
            return [_failed_result(error=f"ladder attempt {call_count['n']} failed")]

        executor._execute_ac_batch = AsyncMock(side_effect=fake_execute_ac_batch)
        executor._apply_verify_gate = AsyncMock(side_effect=lambda **kwargs: kwargs["result"])

        outcome = await _ladder(
            executor,
            result=_failed_result(error="single zero-budget dispatch failed"),
            ac_retry_attempts={0: 0},
        )

        # Before the fix: the ladder bailed with the FAILED result of its
        # first redispatch (call_count would stop at 1 and
        # ``outcome.success`` would be False).
        assert outcome is not None
        assert outcome.success is True
        assert call_count["n"] == 4

    @pytest.mark.asyncio
    async def test_dormant_axes_still_never_engage_after_budget_exhaustion(self) -> None:
        """Negative control: the fix must not turn budget exhaustion alone
        into ladder entry when NO escalation axis is configured at all --
        that dormant-run opt-out (unchanged give-up-after-N-retries) is
        deliberate and load-bearing for every unconfigured run."""
        executor = _make_executor()
        executor._lateral_escalation_enabled = True
        assert executor._model_router is None
        assert executor._reasoning_effort is None
        executor._execute_ac_batch = AsyncMock()

        outcome = await _ladder(
            executor,
            result=_failed_result(error="budget exhausted"),
            ac_retry_attempts={0: 2},
        )

        assert outcome is None
        executor._execute_ac_batch.assert_not_called()


class TestTerminalEligibilityRecheckedEachIteration:
    """Fix 4 (round 2, BLOCKING): terminal-state eligibility must be
    rechecked after EVERY redispatch inside the ladder loop, not just once
    at entry. A redispatch that bounces into decomposition (non-atomic) is
    no longer a "stuck at maximum strength" failure -- there is cheaper room
    left (namely, not decomposing) that was not tried."""

    @pytest.mark.asyncio
    async def test_bounce_into_decomposition_mid_ladder_stops_the_ladder(self) -> None:
        executor = _make_executor_with_active_ladder()

        call_count = {"n": 0}

        def _decomposed_result() -> ACExecutionResult:
            return ACExecutionResult(
                ac_index=0,
                ac_content="Implement the widget",
                success=False,
                error="bounced into decomposition",
                is_decomposed=True,
            )

        async def fake_execute_ac_batch(**kwargs: object) -> list[ACExecutionResult]:
            call_count["n"] += 1
            if call_count["n"] == 1:
                return [_failed_result(error="attempt 1 failed")]
            # Second redispatch bounces into decomposition -- no longer
            # atomic, so no longer "at maximum strength".
            return [_decomposed_result()]

        executor._execute_ac_batch = AsyncMock(side_effect=fake_execute_ac_batch)
        executor._apply_verify_gate = AsyncMock(side_effect=lambda **kwargs: kwargs["result"])

        outcome = await _ladder(executor, result=_failed_result(error="attempt 0 failed"))

        # The ladder must stop -- NOT keep cycling personas on a non-terminal
        # result -- and hand back the CURRENT (decomposed) result rather than
        # None (which would make the caller fall back to stale data).
        assert outcome is not None
        assert outcome.is_decomposed is True
        assert outcome.error == "bounced into decomposition"
        # Only 2 redispatches happened: the ladder did not loop a 3rd time
        # trying to advance the persona cycle on the non-terminal bounce.
        assert call_count["n"] == 2

    @pytest.mark.asyncio
    async def test_still_terminal_every_iteration_keeps_cycling_as_before(self) -> None:
        """Regression guard: when every redispatch STAYS terminal (atomic,
        frontier tier, max effort), the now-per-iteration recheck must not
        change existing persona-cycling/success behavior."""
        executor = _make_executor_with_active_ladder()

        call_count = {"n": 0}

        async def fake_execute_ac_batch(**kwargs: object) -> list[ACExecutionResult]:
            call_count["n"] += 1
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


class TestLadderRedispatchForcesFrontierRouting:
    """Round-5 Finding #2 (BLOCKING): once the ladder deems an AC eligible
    (its eligibility check treats every ACTIVE routing axis as at ceiling),
    the ladder's ACTUAL persona redispatches must run at the true frontier
    tier + max effort — not the incremental one-notch-per-attempt climb the
    pre-ladder retry loop uses. Before this fix a medium-effort-configured
    AC could cycle every persona and end up parked having only ever
    dispatched at "high", never "xhigh"."""

    @pytest.mark.asyncio
    async def test_every_ladder_redispatch_requests_forced_frontier_routing(self) -> None:
        executor = _make_executor_with_active_ladder()

        captured_kwargs: list[dict[str, object]] = []
        call_count = {"n": 0}

        async def capture_batch(**kwargs: object) -> list[ACExecutionResult]:
            call_count["n"] += 1
            captured_kwargs.append(kwargs)
            if call_count["n"] >= 3:
                return [_success_result()]
            return [_failed_result(error=f"attempt {call_count['n']} failed")]

        executor._execute_ac_batch = AsyncMock(side_effect=capture_batch)
        executor._apply_verify_gate = AsyncMock(side_effect=lambda **kwargs: kwargs["result"])

        outcome = await _ladder(executor, result=_failed_result(error="attempt 0 failed"))

        assert outcome is not None
        assert outcome.success is True
        assert len(captured_kwargs) == 3
        for kwargs in captured_kwargs:
            assert kwargs["force_frontier_routing"] is True

    @pytest.mark.asyncio
    async def test_forced_routing_dispatches_at_true_ceiling_not_one_notch(self) -> None:
        """End-to-end through ``_execute_atomic_ac``: with a ``medium``
        effort base and a standard-tier router, a ladder-owned dispatch
        (``force_frontier_routing=True``) must hand the runtime
        ``reasoning_effort="xhigh"`` and the FRONTIER model — while the
        ordinary incremental path at the same retry attempt only reaches
        ``high`` (one notch), reproducing the exact reported gap."""
        from datetime import UTC, datetime

        from ouroboros.events.base import BaseEvent as _BaseEvent
        from ouroboros.orchestrator.adapter import (
            CLAUDE_REASONING_EFFORT_LEVELS,
            AgentMessage,
            ParamSupport,
            RuntimeCapabilities,
        )

        class _CapturingRuntime:
            _cwd = "/tmp/project"

            def __init__(self) -> None:
                self.captured: list[dict[str, object]] = []
                self.capabilities = RuntimeCapabilities(
                    skill_dispatch=False,
                    targeted_resume=False,
                    structured_output=True,
                    reasoning_effort_support=ParamSupport.NATIVE,
                    enforceable_reasoning_efforts=CLAUDE_REASONING_EFFORT_LEVELS,
                    model_override_support=ParamSupport.NATIVE,
                )

            @property
            def runtime_backend(self) -> str:
                return "claude"

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return "acceptEdits"

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: object = None,
                resume_session_id: str | None = None,
                reasoning_effort: str | None = None,
                model: str | None = None,
            ) -> AsyncIterator[AgentMessage]:
                del prompt, tools, system_prompt, resume_handle, resume_session_id
                self.captured.append({"reasoning_effort": reasoning_effort, "model": model})
                yield AgentMessage(
                    type="result",
                    content="done",
                    data={"subtype": "success"},
                )

        def _make_dispatch_executor() -> tuple[ParallelACExecutor, _CapturingRuntime]:
            runtime = _CapturingRuntime()
            event_store = AsyncMock()
            appended: list[_BaseEvent] = []

            async def _append(event: _BaseEvent) -> None:
                appended.append(event)

            async def _replay(aggregate_type: str, aggregate_id: str) -> list[_BaseEvent]:
                return [
                    event
                    for event in appended
                    if event.aggregate_type == aggregate_type and event.aggregate_id == aggregate_id
                ]

            event_store.append.side_effect = _append
            event_store.replay.side_effect = _replay
            executor = ParallelACExecutor(
                adapter=runtime,
                event_store=event_store,
                console=MagicMock(),
                enable_decomposition=False,
                reasoning_effort="medium",
                model_router=ModelRouter(
                    tier_models={
                        "standard": "claude-standard",
                        "frontier": "claude-frontier",
                    },
                    runtime_backend="claude",
                    child_tier="frugal",
                    base_tier="standard",
                    escalation_retry_threshold=999,  # incremental climb never fires
                ),
            )
            return executor, runtime

        # Ladder-owned dispatch: BOTH active axes at their true ceilings.
        executor, runtime = _make_dispatch_executor()
        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement the widget",
            session_id="s1",
            tools=[],
            system_prompt="",
            seed_goal="Implement the stubborn widget",
            depth=0,
            start_time=datetime.now(UTC),
            execution_id="exec-forced",
            retry_attempt=3,
            force_frontier_routing=True,
        )
        assert result.success is True
        assert runtime.captured == [{"reasoning_effort": "xhigh", "model": "claude-frontier"}]

        # Control — the ordinary incremental path at the SAME retry attempt:
        # effort reaches only ONE notch above base ("high", never "xhigh")
        # and the tier stays wherever the incremental climb left it. This is
        # the exact pre-fix behavior the ladder's redispatches were stuck on.
        executor, runtime = _make_dispatch_executor()
        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement the widget",
            session_id="s1",
            tools=[],
            system_prompt="",
            seed_goal="Implement the stubborn widget",
            depth=0,
            start_time=datetime.now(UTC),
            execution_id="exec-ordinary",
            retry_attempt=3,
        )
        assert result.success is True
        assert runtime.captured == [{"reasoning_effort": "high", "model": "claude-standard"}]


class TestPrePakingSuccessEmitsResolution:
    """Round-5 Finding #3 (BLOCKING): a persona can succeed partway through
    the cycle, BEFORE the AC ever parks. The durable resolution event used
    to be gated on ``state.parked`` — so in that case the node's LATEST
    replayable event stayed ``lateral_escalation_progressed`` ("trying
    persona X") and every replay-based projection reported a COMPLETED AC as
    still actively escalating, forever. Resolution must fire on EVERY
    successful ladder exit."""

    @pytest.fixture
    async def memory_event_store(self) -> AsyncIterator[EventStore]:
        store = EventStore("sqlite+aiosqlite:///:memory:")
        await store.initialize()
        try:
            yield store
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_persona_success_before_parking_resolves_durable_state(
        self, memory_event_store: EventStore
    ) -> None:
        executor = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=memory_event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )
        executor._lateral_escalation_enabled = True
        executor._model_router = ModelRouter(
            tier_models={"frontier": "gpt-frontier"},
            runtime_backend="claude",
            child_tier="frugal",
            base_tier="frontier",
            escalation_retry_threshold=999,
        )
        executor._adapter.runtime_backend = "claude"
        executor._emit_ac_outcome_finalized = AsyncMock()
        executor._sleep = AsyncMock()
        # The REAL emitter + REAL durable store: this test verifies the
        # durable event log itself, not a mocked emitter call.

        call_count = {"n": 0}

        async def fails_once_then_succeeds_under_first_persona(
            **kwargs: object,
        ) -> list[ACExecutionResult]:
            call_count["n"] += 1
            # Redispatch 1: the identical below-threshold retry fails.
            # Redispatch 2: the FIRST persona attempt succeeds — well before
            # parking (4 personas remain untried).
            if call_count["n"] >= 2:
                return [_success_result()]
            return [_failed_result(error=f"attempt {call_count['n']} failed")]

        executor._execute_ac_batch = AsyncMock(
            side_effect=fails_once_then_succeeds_under_first_persona
        )
        executor._apply_verify_gate = AsyncMock(side_effect=lambda **kwargs: kwargs["result"])

        outcome = await _ladder(executor, result=_failed_result(error="attempt 0 failed"))

        assert outcome is not None
        assert outcome.success is True
        assert call_count["n"] == 2  # succeeded mid-cycle, never parked

        node_id = ExecutionNodeIdentity.root(execution_context_id="exec-1", ac_index=0).node_id
        events = await memory_event_store.replay("execution", "exec-1")
        escalation_events = [
            event
            for event in events
            if event.type
            in {
                "execution.ac.lateral_escalation_progressed",
                "execution.ac.parked_for_operator",
                "execution.ac.parked_resolved",
            }
            and event.data.get("node_id") == node_id
        ]
        # The ladder durably recorded its in-flight progress...
        assert any(
            event.type == "execution.ac.lateral_escalation_progressed"
            for event in escalation_events
        )
        # ...and the LATEST durable event is the resolution — not a stale
        # ``progressed`` record claiming the AC is still escalating.
        assert escalation_events[-1].type == "execution.ac.parked_resolved"

        # A cold-start reconstruction (fresh executor, same durable store —
        # i.e. any later resume) must see a FRESH state, not a stale
        # mid-ladder streak.
        cold = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=memory_event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )
        cold._lateral_escalation_enabled = True
        state = await cold._load_lateral_escalation_state(0, execution_id="exec-1")
        assert state == LateralEscalationState()

        # And the replay-based board projection (dashboard/HUD/conductor all
        # fold the same reducer) shows NO escalation badge for the node —
        # the exact "status=completed, escalation_state=escalating" stuck
        # pairing the finding reproduced.
        from ouroboros.dashboard.board import reduce_board

        board = reduce_board(
            [{"event_type": event.type, "payload": event.data} for event in events],
            execution_id="exec-1",
        )
        all_cards = [card for column in board["columns"].values() for card in column]
        matching = [card for card in all_cards if card.get("id") == node_id]
        assert matching, f"no board card produced for node {node_id!r}"
        for card in matching:
            assert "escalation_state" not in card


class TestLadderWriteFailureFailsClosed:
    """Round-5 Finding #4 (BLOCKING): a correctness-bearing write whose only
    consequence on failure is a log line is fail-OPEN — execution continues
    exactly as if the write succeeded, and after a restart a clean replay
    cannot distinguish "never happened" from "failed and was logged". The
    ladder must not act on (redispatch under) a streak/persona/parked
    advancement the durable log cannot corroborate: it reverts to the
    pre-advance state, holds at the parked cadence, and retries the SAME
    step until the write lands."""

    @pytest.mark.asyncio
    async def test_progressed_write_failure_defers_redispatch_and_retries_same_step(
        self,
    ) -> None:
        executor = _make_executor_with_active_ladder()
        executor._event_emitter.emit_lateral_escalation_progressed = AsyncMock(
            side_effect=[False, True]
        )

        dispatch_count = {"n": 0}

        async def succeeds_immediately(**kwargs: object) -> list[ACExecutionResult]:
            dispatch_count["n"] += 1
            return [_success_result()]

        executor._execute_ac_batch = AsyncMock(side_effect=succeeds_immediately)
        executor._apply_verify_gate = AsyncMock(side_effect=lambda **kwargs: kwargs["result"])

        outcome = await _ladder(executor, result=_failed_result(error="attempt 0 failed"))

        assert outcome is not None
        assert outcome.success is True
        # No dispatch happened while the step was un-persisted: exactly one
        # redispatch, and only AFTER the write finally landed.
        assert dispatch_count["n"] == 1
        progressed_mock = executor._event_emitter.emit_lateral_escalation_progressed
        assert progressed_mock.await_count == 2
        # The failed write did NOT advance the ladder: the retried write
        # describes the IDENTICAL step (no phantom streak increment, no
        # persona skipped).
        first_kwargs = progressed_mock.await_args_list[0].kwargs
        second_kwargs = progressed_mock.await_args_list[1].kwargs
        assert first_kwargs["personas_tried"] == second_kwargs["personas_tried"] == ()
        assert (
            first_kwargs["consecutive_terminal_failures"]
            == second_kwargs["consecutive_terminal_failures"]
            == 1
        )
        # Held at the operator-visible parked cadence while un-persisted.
        assert executor._sleep.await_count == 1
        assert executor._sleep.await_args_list[0].args[0] == (
            executor._parked_retry_backoff_seconds
        )

    @pytest.mark.asyncio
    async def test_parked_write_failure_defers_parking_and_retries_until_durable(
        self,
    ) -> None:
        """The full-parking transition gets the same treatment: the parked
        redispatch must not run until ``parked_for_operator`` is durably
        recorded — otherwise a restart loses parked status entirely and
        re-cycles exhausted personas from scratch."""
        executor = _make_executor_with_active_ladder()
        executor._lateral_escalation_states[0] = LateralEscalationState(
            consecutive_terminal_failures=_THRESHOLD + TOTAL_PERSONA_COUNT - 1,
            personas_tried=tuple(ThinkingPersona),
            parked=False,
        )
        executor._event_emitter.emit_ac_parked_for_operator = AsyncMock(side_effect=[False, True])

        dispatch_count = {"n": 0}

        async def succeeds_immediately(**kwargs: object) -> list[ACExecutionResult]:
            dispatch_count["n"] += 1
            return [_success_result()]

        executor._execute_ac_batch = AsyncMock(side_effect=succeeds_immediately)
        executor._apply_verify_gate = AsyncMock(side_effect=lambda **kwargs: kwargs["result"])

        outcome = await _ladder(executor, result=_failed_result(error="last persona failed"))

        assert outcome is not None
        assert outcome.success is True
        assert dispatch_count["n"] == 1
        # The parking transition was re-attempted until it landed.
        assert executor._event_emitter.emit_ac_parked_for_operator.await_count == 2
        # Slept the parked cadence twice: once holding the un-persisted
        # parking step, once as the parked redispatch's own long backoff.
        assert executor._sleep.await_count == 2
        for call in executor._sleep.await_args_list:
            assert call.args[0] == executor._parked_retry_backoff_seconds


class TestResumeReentersLadderAtRestoredPhase:
    """Round-5 Finding #1 (BLOCKING): checkpoints only save after a level
    completes, so a crash mid-ladder restarts that level with every AC's
    retry counter reset to zero — and the ordinary un-backed-off retry path
    used to run BEFORE any escalation state was loaded (loading only
    happened inside the ladder itself). A resumed AC that was mid-ladder or
    parked must re-enter at its durably recorded phase/cadence, and a
    success on that resumed path must still fire the resolution transition
    the non-crash path fires."""

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
        """A fresh executor, as recreated after a crash/restart, with a real
        emitter over the run's durable store and a nonzero fresh retry
        budget configured."""
        executor = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )
        executor._lateral_escalation_enabled = True
        executor._ac_retry_attempts = 2
        executor._model_router = ModelRouter(
            tier_models={"frontier": "gpt-frontier"},
            runtime_backend="claude",
            child_tier="frugal",
            base_tier="frontier",
            escalation_retry_threshold=999,
        )
        executor._adapter.runtime_backend = "claude"
        executor._emit_ac_outcome_finalized = AsyncMock()
        executor._sleep = AsyncMock()
        executor._apply_verify_gate = AsyncMock(side_effect=lambda **kwargs: kwargs["result"])
        return executor

    async def _run_batch(
        self, executor: ParallelACExecutor, *, ac_retry_attempts: dict[int, int]
    ) -> list[ACExecutionResult | BaseException]:
        return await executor._run_batch_with_verify_and_retry(
            seed=_make_seed(),
            batch_executable=[0],
            session_id="s1",
            execution_id="exec-1",
            tools=[],
            tool_catalog=None,
            system_prompt="",
            level_contexts=[],
            ac_retry_attempts=ac_retry_attempts,
            execution_counters=None,
        )

    @pytest.mark.asyncio
    async def test_parked_ac_resumes_at_parked_cadence_and_resolves_on_success(
        self, memory_event_store: EventStore
    ) -> None:
        """The exact reported crash-mid-parked-cycle scenario: on resume the
        AC must NOT restart the fresh un-backed-off retry budget, must honor
        the parked cadence before its dispatch, and a success must leave the
        durable log resolved — not stuck on parked."""
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
                    "consecutive_terminal_failures": 9,
                    "backoff_seconds": 300.0,
                    "reason": "all lateral-thinking personas exhausted",
                },
            )
        )
        executor = self._cold_start_executor(memory_event_store)

        dispatch_calls: list[dict[str, object]] = []

        async def succeeds_immediately(**kwargs: object) -> list[ACExecutionResult]:
            dispatch_calls.append(kwargs)
            return [_success_result()]

        executor._execute_ac_batch = AsyncMock(side_effect=succeeds_immediately)

        ac_retry_attempts = {0: 0}  # the post-crash reset the finding describes
        results = await self._run_batch(executor, ac_retry_attempts=ac_retry_attempts)

        assert isinstance(results[0], ACExecutionResult)
        assert results[0].success is True
        # ONE dispatch — the fresh 2-attempt retry budget was never granted.
        assert len(dispatch_calls) == 1
        # Re-entered as a ladder-owned attempt: post-budget, max strength,
        # with a re-entry retry prompt.
        assert dispatch_calls[0]["same_runtime_budget_exhausted"] is True
        assert dispatch_calls[0]["force_frontier_routing"] is True
        retry_prompts = dispatch_calls[0]["retry_prompts"]
        assert isinstance(retry_prompts, dict) and retry_prompts[0]
        assert ac_retry_attempts[0] == executor._ac_retry_attempts
        # The parked cadence governed pacing: slept the long backoff BEFORE
        # the resumed dispatch.
        executor._sleep.assert_awaited_once_with(executor._parked_retry_backoff_seconds)
        # Durable state converged: the latest replayable escalation event is
        # the resolution, and a later cold start reconstructs FRESH state.
        events = await memory_event_store.replay("execution", "exec-1")
        escalation_events = [
            event
            for event in events
            if event.type
            in {
                "execution.ac.lateral_escalation_progressed",
                "execution.ac.parked_for_operator",
                "execution.ac.parked_resolved",
            }
            and event.data.get("node_id") == node_id
        ]
        assert escalation_events[-1].type == "execution.ac.parked_resolved"
        later = self._cold_start_executor(memory_event_store)
        assert (
            await later._load_lateral_escalation_state(0, execution_id="exec-1")
            == LateralEscalationState()
        )

    @pytest.mark.asyncio
    async def test_mid_persona_crash_resumes_in_flight_persona_then_continues_cycle(
        self, memory_event_store: EventStore
    ) -> None:
        """A crash during a persona redispatch (the ``progressed`` event is
        emitted BEFORE the redispatch) must resume by re-running THAT
        persona's attempt — not a fresh top-of-ladder identical retry — and
        a subsequent failure must continue the cycle from the restored
        personas, never repeating or restarting."""
        node_id = ExecutionNodeIdentity.root(execution_context_id="exec-1", ac_index=0).node_id
        await memory_event_store.append(
            BaseEvent(
                type="execution.ac.lateral_escalation_progressed",
                aggregate_type="execution",
                aggregate_id="exec-1",
                data={
                    "execution_id": "exec-1",
                    "session_id": "s1",
                    "node_id": node_id,
                    "root_ac_index": 0,
                    "personas_tried": [ThinkingPersona.HACKER.value],
                    "consecutive_terminal_failures": 3,
                    "parked": False,
                    "persona": ThinkingPersona.HACKER.value,
                },
            )
        )
        executor = self._cold_start_executor(memory_event_store)

        dispatch_calls: list[dict[str, object]] = []

        async def fails_then_succeeds(**kwargs: object) -> list[ACExecutionResult]:
            dispatch_calls.append(kwargs)
            if len(dispatch_calls) >= 2:
                return [_success_result()]
            return [_failed_result(error="resumed persona attempt failed")]

        executor._execute_ac_batch = AsyncMock(side_effect=fails_then_succeeds)

        ac_retry_attempts = {0: 0}
        results = await self._run_batch(executor, ac_retry_attempts=ac_retry_attempts)

        assert isinstance(results[0], ACExecutionResult)
        assert results[0].success is True
        # Two dispatches total: the resumed in-flight-persona attempt, then
        # ONE ladder redispatch — never the fresh 2-attempt ordinary budget.
        assert len(dispatch_calls) == 2
        # The resumed attempt re-ran the interrupted persona's framing.
        first_prompt = dispatch_calls[0]["retry_prompts"][0]  # type: ignore[index]
        assert ThinkingPersona.HACKER.value in first_prompt.lower()
        # The ladder continued from the restored phase: the next durable
        # ``progressed`` record extends the persona history (hacker + a new
        # persona) instead of restarting from scratch or repeating hacker.
        events = await memory_event_store.replay("execution", "exec-1")
        progressed_after_resume = [
            event
            for event in events[1:]  # skip the pre-seeded crash-era event
            if event.type == "execution.ac.lateral_escalation_progressed"
            and event.data.get("node_id") == node_id
        ]
        assert progressed_after_resume, "ladder never durably progressed after resume"
        resumed_personas = progressed_after_resume[-1].data["personas_tried"]
        assert resumed_personas[0] == ThinkingPersona.HACKER.value
        assert len(resumed_personas) == 2
        assert len(set(resumed_personas)) == 2
        # And the eventual success still resolved the episode durably.
        assert any(
            event.type == "execution.ac.parked_resolved" and event.data.get("node_id") == node_id
            for event in events
        )

    @pytest.mark.asyncio
    async def test_fresh_ac_without_escalation_history_keeps_ordinary_path(
        self, memory_event_store: EventStore
    ) -> None:
        """Control: an AC with NO durable escalation history must get the
        unchanged ordinary path — fresh budget, no forced routing, no
        spurious resolution event."""
        executor = self._cold_start_executor(memory_event_store)

        dispatch_calls: list[dict[str, object]] = []

        async def fails_then_succeeds(**kwargs: object) -> list[ACExecutionResult]:
            dispatch_calls.append(kwargs)
            if len(dispatch_calls) >= 2:
                return [_success_result()]
            return [_failed_result(error="first attempt failed")]

        executor._execute_ac_batch = AsyncMock(side_effect=fails_then_succeeds)

        ac_retry_attempts = {0: 0}
        results = await self._run_batch(executor, ac_retry_attempts=ac_retry_attempts)

        assert isinstance(results[0], ACExecutionResult)
        assert results[0].success is True
        # Ordinary path: initial dispatch + one budget-funded retry.
        assert len(dispatch_calls) == 2
        assert dispatch_calls[0]["same_runtime_budget_exhausted"] is False
        assert dispatch_calls[0].get("force_frontier_routing", False) is False
        assert ac_retry_attempts[0] == 1
        # No escalation episode existed, so no resolution event may appear.
        events = await memory_event_store.replay("execution", "exec-1")
        assert not any(event.type == "execution.ac.parked_resolved" for event in events)


class TestResumeRestoresPersistedRetryAttempt:
    """Round-6 Finding #1 (BLOCKING): ``lateral_escalation_progressed`` did
    not persist the in-flight dispatch-attempt counter, and the resume path
    reset the counter to the CONFIGURED retry cap instead of the actual
    attempt in flight at the crash. Runtime handles and frugality-proof
    telemetry are attempt-scoped, so after multiple ladder attempts the
    cap-reset could resume an OLDER attempt's stale runtime handle and
    double-count that attempt's telemetry."""

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
        executor = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )
        executor._lateral_escalation_enabled = True
        executor._ac_retry_attempts = 2
        executor._model_router = ModelRouter(
            tier_models={"frontier": "gpt-frontier"},
            runtime_backend="claude",
            child_tier="frugal",
            base_tier="frontier",
            escalation_retry_threshold=999,
        )
        executor._adapter.runtime_backend = "claude"
        executor._emit_ac_outcome_finalized = AsyncMock()
        executor._sleep = AsyncMock()
        executor._apply_verify_gate = AsyncMock(side_effect=lambda **kwargs: kwargs["result"])
        return executor

    @staticmethod
    def _progressed_event(
        node_id: str, *, personas: list[str], streak: int, retry_attempt: int | None
    ) -> BaseEvent:
        data: dict[str, object] = {
            "execution_id": "exec-1",
            "session_id": "s1",
            "node_id": node_id,
            "root_ac_index": 0,
            "personas_tried": personas,
            "consecutive_terminal_failures": streak,
            "parked": False,
            "persona": personas[-1] if personas else None,
        }
        if retry_attempt is not None:
            data["retry_attempt"] = retry_attempt
        return BaseEvent(
            type="execution.ac.lateral_escalation_progressed",
            aggregate_type="execution",
            aggregate_id="exec-1",
            data=data,
        )

    async def _run_batch(
        self, executor: ParallelACExecutor, *, ac_retry_attempts: dict[int, int]
    ) -> list[ACExecutionResult | BaseException]:
        return await executor._run_batch_with_verify_and_retry(
            seed=_make_seed(),
            batch_executable=[0],
            session_id="s1",
            execution_id="exec-1",
            tools=[],
            tool_catalog=None,
            system_prompt="",
            level_contexts=[],
            ac_retry_attempts=ac_retry_attempts,
            execution_counters=None,
        )

    @pytest.mark.asyncio
    async def test_ladder_persists_each_redispatch_attempt_number(self) -> None:
        """Every ``progressed`` event must carry the attempt number the
        redispatch it precedes runs under — the value a cold resume needs to
        re-enter at the exact in-flight attempt."""
        executor = _make_executor_with_active_ladder()
        executor._event_emitter.emit_lateral_escalation_progressed = AsyncMock(return_value=True)

        call_count = {"n": 0}

        async def fails_once_then_succeeds(**kwargs: object) -> list[ACExecutionResult]:
            call_count["n"] += 1
            if call_count["n"] >= 2:
                return [_success_result()]
            return [_failed_result(error="attempt failed")]

        executor._execute_ac_batch = AsyncMock(side_effect=fails_once_then_succeeds)
        executor._apply_verify_gate = AsyncMock(side_effect=lambda **kwargs: kwargs["result"])

        ac_retry_attempts = {0: 2}
        outcome = await _ladder(
            executor,
            result=_failed_result(error="attempt 0 failed"),
            ac_retry_attempts=ac_retry_attempts,
        )

        assert outcome is not None and outcome.success is True
        progressed_mock = executor._event_emitter.emit_lateral_escalation_progressed
        emitted_attempts = [
            call.kwargs["retry_attempt"] for call in progressed_mock.await_args_list
        ]
        # Entered at attempt 2 (budget spent): the two ladder redispatches
        # ran as attempts 3 and 4, and each preceding event recorded exactly
        # the attempt its redispatch would run under.
        assert emitted_attempts == [3, 4]
        assert ac_retry_attempts[0] == 4

    @pytest.mark.asyncio
    async def test_resume_restores_actual_attempt_not_configured_cap(
        self, memory_event_store: EventStore
    ) -> None:
        """The reported scenario: after 3 durably-recorded ladder attempts
        (latest in-flight attempt 5), a cold resume must restore attempt 5 —
        not reset to the configured cap (2), which an earlier attempt
        already used."""
        node_id = ExecutionNodeIdentity.root(execution_context_id="exec-1", ac_index=0).node_id
        for personas, streak, attempt in (
            (["hacker"], 3, 3),
            (["hacker", "architect"], 4, 4),
            (["hacker", "architect", "researcher"], 5, 5),
        ):
            await memory_event_store.append(
                self._progressed_event(
                    node_id, personas=personas, streak=streak, retry_attempt=attempt
                )
            )
        executor = self._cold_start_executor(memory_event_store)
        executor._execute_ac_batch = AsyncMock(return_value=[_success_result()])

        ac_retry_attempts = {0: 0}  # the post-crash reset
        results = await self._run_batch(executor, ac_retry_attempts=ac_retry_attempts)

        assert isinstance(results[0], ACExecutionResult)
        assert results[0].success is True
        # Restored to the durably-recorded in-flight attempt, not the cap.
        assert ac_retry_attempts[0] == 5
        assert ac_retry_attempts[0] != executor._ac_retry_attempts

    @pytest.mark.asyncio
    async def test_legacy_event_without_attempt_falls_back_to_cap(
        self, memory_event_store: EventStore
    ) -> None:
        """Negative control: a durable record written before the field
        existed keeps the pre-fix configured-cap restore — never worse."""
        node_id = ExecutionNodeIdentity.root(execution_context_id="exec-1", ac_index=0).node_id
        await memory_event_store.append(
            self._progressed_event(node_id, personas=["hacker"], streak=3, retry_attempt=None)
        )
        executor = self._cold_start_executor(memory_event_store)
        executor._execute_ac_batch = AsyncMock(return_value=[_success_result()])

        ac_retry_attempts = {0: 0}
        results = await self._run_batch(executor, ac_retry_attempts=ac_retry_attempts)

        assert isinstance(results[0], ACExecutionResult)
        assert results[0].success is True
        assert ac_retry_attempts[0] == executor._ac_retry_attempts

    @pytest.mark.asyncio
    async def test_wrong_typed_attempt_fails_closed_to_parked(
        self, memory_event_store: EventStore
    ) -> None:
        """The emitter always writes a non-negative int, so a wrong-typed
        value is corruption — reconstruction fails closed to the parked
        sentinel like any other malformed field (round-4 convention)."""
        node_id = ExecutionNodeIdentity.root(execution_context_id="exec-1", ac_index=0).node_id
        await memory_event_store.append(
            self._progressed_event(node_id, personas=["hacker"], streak=3, retry_attempt=None)
        )
        # Corrupt the field in a fresh event (latest wins).
        corrupted = self._progressed_event(
            node_id, personas=["hacker"], streak=3, retry_attempt=None
        )
        corrupted.data["retry_attempt"] = "seven"
        await memory_event_store.append(corrupted)
        executor = self._cold_start_executor(memory_event_store)

        state = await executor._load_lateral_escalation_state(0, execution_id="exec-1")

        assert state.parked is True


class TestNonSuccessLadderExitsResolveDurableState:
    """Round-6 Finding #2 (BLOCKING): two ladder exits are terminal but NOT
    successes — a redispatch that came back decomposed, and a redispatch
    that produced a non-retryable result. Neither emitted any terminal
    transition, so the node's latest replayable escalation record stayed
    ``progressed``: replay presented a terminally-done AC as still actively
    escalating forever, and a later resume re-entered stale ladder state.
    Both exits must durably close the episode (with the distinct
    ``lateral_escalation_interrupted`` signal — ``parked_resolved`` means
    SUCCESS per round 5)."""

    @pytest.fixture
    async def memory_event_store(self) -> AsyncIterator[EventStore]:
        store = EventStore("sqlite+aiosqlite:///:memory:")
        await store.initialize()
        try:
            yield store
        finally:
            await store.close()

    @staticmethod
    def _durable_ladder_executor(event_store: EventStore) -> ParallelACExecutor:
        """Active-ladder executor with the REAL emitter over a REAL durable
        store — these tests verify the durable event log itself."""
        executor = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )
        executor._lateral_escalation_enabled = True
        executor._model_router = ModelRouter(
            tier_models={"frontier": "gpt-frontier"},
            runtime_backend="claude",
            child_tier="frugal",
            base_tier="frontier",
            escalation_retry_threshold=999,
        )
        executor._adapter.runtime_backend = "claude"
        executor._emit_ac_outcome_finalized = AsyncMock()
        executor._sleep = AsyncMock()
        executor._apply_verify_gate = AsyncMock(side_effect=lambda **kwargs: kwargs["result"])
        return executor

    async def _escalation_events(self, store: EventStore, node_id: str) -> list[BaseEvent]:
        events = await store.replay("execution", "exec-1")
        return [
            event
            for event in events
            if event.type
            in {
                "execution.ac.lateral_escalation_progressed",
                "execution.ac.parked_for_operator",
                "execution.ac.parked_resolved",
                "execution.ac.lateral_escalation_interrupted",
            }
            and event.data.get("node_id") == node_id
        ]

    @pytest.mark.asyncio
    async def test_decomposed_redispatch_exit_durably_closes_episode(
        self, memory_event_store: EventStore
    ) -> None:
        executor = self._durable_ladder_executor(memory_event_store)
        decomposed_failure = ACExecutionResult(
            ac_index=0,
            ac_content="Implement the widget",
            success=False,
            error="decomposition children failed",
            is_decomposed=True,
        )
        executor._execute_ac_batch = AsyncMock(return_value=[decomposed_failure])

        outcome = await _ladder(executor, result=_failed_result(error="attempt 0 failed"))

        # The ladder handed back the CURRENT decomposed result (exit a).
        assert outcome is not None
        assert outcome.is_decomposed is True
        assert outcome.success is False

        node_id = ExecutionNodeIdentity.root(execution_context_id="exec-1", ac_index=0).node_id
        escalation_events = await self._escalation_events(memory_event_store, node_id)
        # The episode durably began...
        assert any(
            event.type == "execution.ac.lateral_escalation_progressed"
            for event in escalation_events
        )
        # ...and the LATEST durable record closes it — no stale "escalating".
        assert escalation_events[-1].type == "execution.ac.lateral_escalation_interrupted"
        assert escalation_events[-1].data["reason"] == "redispatch_decomposed"

        # A later cold start reconstructs FRESH state (ordinary path), not a
        # stale mid-ladder streak.
        cold = self._durable_ladder_executor(memory_event_store)
        state = await cold._load_lateral_escalation_state(0, execution_id="exec-1")
        assert state == LateralEscalationState()

        # And the board projection shows no escalation badge for the node.
        from ouroboros.dashboard.board import reduce_board

        events = await memory_event_store.replay("execution", "exec-1")
        board = reduce_board(
            [{"event_type": event.type, "payload": event.data} for event in events],
            execution_id="exec-1",
        )
        all_cards = [card for column in board["columns"].values() for card in column]
        for card in all_cards:
            if card.get("id") == node_id:
                assert "escalation_state" not in card

    @pytest.mark.asyncio
    async def test_non_retryable_redispatch_exit_durably_closes_episode(
        self, memory_event_store: EventStore
    ) -> None:
        executor = self._durable_ladder_executor(memory_event_store)
        infra_fatal = ACExecutionResult(
            ac_index=0,
            ac_content="Implement the widget",
            success=False,
            error="adapter crashed mid-redispatch",
            infra_fatal=True,
        )
        executor._execute_ac_batch = AsyncMock(return_value=[infra_fatal])

        outcome = await _ladder(executor, result=_failed_result(error="attempt 0 failed"))

        # The ladder propagated the fresh non-retryable result (exit b).
        assert outcome is not None
        assert outcome.success is False
        assert outcome.infra_fatal is True

        node_id = ExecutionNodeIdentity.root(execution_context_id="exec-1", ac_index=0).node_id
        escalation_events = await self._escalation_events(memory_event_store, node_id)
        assert any(
            event.type == "execution.ac.lateral_escalation_progressed"
            for event in escalation_events
        )
        assert escalation_events[-1].type == "execution.ac.lateral_escalation_interrupted"
        assert escalation_events[-1].data["reason"] == "not_retryable"

        # A later resume must NOT auto-redispatch this AC through the
        # resumed-ladder path: reconstruction is fresh.
        cold = self._durable_ladder_executor(memory_event_store)
        state = await cold._load_lateral_escalation_state(0, execution_id="exec-1")
        assert state == LateralEscalationState()


class TestResolvedWriteExhaustionRetriesInBackground:
    """Round-6 Finding #3 (BLOCKING): when the ``parked_resolved`` write's
    bounded foreground retries were ALL exhausted, the executor logged,
    cleared its in-memory state, and returned success anyway — leaving the
    durable projections (board/HUD/conductor) stuck on parked/escalating
    forever with nothing left even trying to fix it. The write must keep
    retrying at the long parked cadence in the background, and the
    in-memory state may only clear once the durable log actually
    corroborates the resolution."""

    @pytest.mark.asyncio
    async def test_exhausted_resolved_write_keeps_state_and_retries_in_background(
        self,
    ) -> None:
        import asyncio

        executor = _make_executor_with_active_ladder()
        executor._lateral_escalation_states[0] = LateralEscalationState(
            consecutive_terminal_failures=3,
            personas_tried=(ThinkingPersona.HACKER,),
        )
        # All 3 foreground retries fail; the FIRST background retry lands.
        executor._event_emitter.emit_ac_parked_resolved = AsyncMock(
            side_effect=[False, False, False, True]
        )

        await executor._resolve_escalation_success(ac_idx=0, execution_id="exec-1", session_id="s1")

        # The foreground call returned (a genuine success is never held
        # hostage) but the in-memory state was NOT silently cleared: the
        # durable log still says escalating, and the live process stays
        # honest about that.
        assert 0 in executor._lateral_escalation_states
        assert executor._deferred_durable_write_tasks

        await asyncio.gather(*executor._deferred_durable_write_tasks)

        # Once the background write landed, the state cleared — same as the
        # foreground success path.
        assert 0 not in executor._lateral_escalation_states
        assert executor._event_emitter.emit_ac_parked_resolved.await_count == 4

    @pytest.mark.asyncio
    async def test_background_gives_up_loudly_but_never_clears_unresolved_state(
        self,
    ) -> None:
        import asyncio

        executor = _make_executor_with_active_ladder()
        executor._lateral_escalation_states[0] = LateralEscalationState(
            consecutive_terminal_failures=3,
            personas_tried=(ThinkingPersona.HACKER,),
        )
        executor._event_emitter.emit_ac_parked_resolved = AsyncMock(return_value=False)

        await executor._resolve_escalation_success(ac_idx=0, execution_id="exec-1", session_id="s1")
        await asyncio.gather(*executor._deferred_durable_write_tasks)

        # Bounded give-up: the in-memory state stays "escalating" (the
        # fail-closed direction) rather than silently pretending the
        # durable log was fixed.
        assert 0 in executor._lateral_escalation_states
        from ouroboros.orchestrator.parallel_executor import (
            _DEFERRED_DURABLE_WRITE_MAX_ATTEMPTS,
        )

        assert executor._event_emitter.emit_ac_parked_resolved.await_count == (
            3 + _DEFERRED_DURABLE_WRITE_MAX_ATTEMPTS
        )

    @pytest.mark.asyncio
    async def test_persisted_resolution_still_clears_state_immediately(self) -> None:
        """Negative control: the ordinary foreground success path is
        unchanged — no background task, state cleared immediately."""
        executor = _make_executor_with_active_ladder()
        executor._lateral_escalation_states[0] = LateralEscalationState(
            consecutive_terminal_failures=3,
            personas_tried=(ThinkingPersona.HACKER,),
        )
        executor._event_emitter.emit_ac_parked_resolved = AsyncMock(return_value=True)

        await executor._resolve_escalation_success(ac_idx=0, execution_id="exec-1", session_id="s1")

        assert 0 not in executor._lateral_escalation_states
        assert not executor._deferred_durable_write_tasks


class TestDeferredWriteFirstAttemptIsImmediate:
    """Adversarial-review Bug #1: the deferred retry loop slept the FULL
    parked cadence (default 300s) BEFORE its first attempt, while the
    primary CLI entrypoint's ``asyncio.run`` teardown cancels all pending
    tasks the moment the run coroutine returns. Deferred writes are
    scheduled disproportionately near the END of a run, so in the common
    CLI case the write got ZERO real attempts — silently, since
    ``CancelledError`` bypassed the recovered/gave-up logging too. The
    first attempt must run immediately; only SUBSEQUENT attempts hold the
    parked cadence."""

    @pytest.mark.asyncio
    async def test_first_background_attempt_runs_before_any_cadence_sleep(self) -> None:
        import asyncio

        executor = _make_executor_with_active_ladder()
        write = AsyncMock(return_value=True)
        on_persisted = MagicMock()

        executor._schedule_deferred_durable_write(
            write=write, on_persisted=on_persisted, log_key="test.deferred"
        )
        await asyncio.gather(*executor._deferred_durable_write_tasks)

        write.assert_awaited_once()
        on_persisted.assert_called_once()
        # The write landed with NO sleep at all — a short-lived run tears
        # down long before the parked cadence would have elapsed.
        executor._sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_subsequent_attempts_still_hold_parked_cadence(self) -> None:
        import asyncio

        executor = _make_executor_with_active_ladder()
        write = AsyncMock(side_effect=[False, False, True])

        executor._schedule_deferred_durable_write(
            write=write, on_persisted=None, log_key="test.deferred"
        )
        await asyncio.gather(*executor._deferred_durable_write_tasks)

        assert write.await_count == 3
        # Exactly one cadence sleep BETWEEN each pair of attempts — never
        # before the first.
        assert executor._sleep.await_count == 2
        executor._sleep.assert_awaited_with(executor._parked_retry_backoff_seconds)

    @pytest.mark.asyncio
    async def test_cancellation_is_logged_not_silent(self) -> None:
        import asyncio

        import structlog.testing

        executor = _make_executor_with_active_ladder()
        started = asyncio.Event()

        async def hanging_write() -> bool:
            started.set()
            await asyncio.Event().wait()  # pragma: no cover - cancelled mid-wait
            return True

        executor._schedule_deferred_durable_write(
            write=hanging_write, on_persisted=None, log_key="test.deferred"
        )
        (task,) = executor._deferred_durable_write_tasks
        with structlog.testing.capture_logs() as captured:
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        cancelled_logs = [
            entry
            for entry in captured
            if entry["event"] == "test.deferred.deferred_write_cancelled"
        ]
        assert cancelled_logs, "cancellation must be loud, never silent"
        assert cancelled_logs[0]["attempts_remaining"] > 0
        assert "stale" in cancelled_logs[0]["detail"]


class TestDeferredWritesDrainAtRunCompletion:
    """Adversarial-review Bug #1 (part b): when the run's top-level flow
    completes, in-flight deferred durable writes must get a bounded final
    shot (``asyncio.wait`` with a timeout) before teardown cancels them —
    and anything still pending after the timeout must be cancelled
    explicitly (so the loop's own handler logs) rather than left for the
    event loop to kill silently."""

    @pytest.mark.asyncio
    async def test_drain_waits_for_inflight_write_to_land(self) -> None:
        import asyncio

        executor = _make_executor_with_active_ladder()
        landed = asyncio.Event()

        async def slow_write() -> bool:
            await asyncio.sleep(0.05)
            landed.set()
            return True

        executor._schedule_deferred_durable_write(
            write=slow_write, on_persisted=None, log_key="test.deferred"
        )
        await executor._drain_deferred_durable_writes()

        assert landed.is_set()
        await asyncio.sleep(0)  # let done-callbacks discard finished tasks
        assert not executor._deferred_durable_write_tasks

    @pytest.mark.asyncio
    async def test_drain_cancels_stalled_writes_after_bounded_timeout(self) -> None:
        import asyncio

        import structlog.testing

        executor = _make_executor_with_active_ladder()
        started = asyncio.Event()

        async def hanging_write() -> bool:
            started.set()
            await asyncio.Event().wait()  # pragma: no cover - cancelled mid-wait
            return True

        executor._schedule_deferred_durable_write(
            write=hanging_write, on_persisted=None, log_key="test.deferred"
        )
        with (
            patch(
                "ouroboros.orchestrator.parallel_executor"
                "._DEFERRED_DURABLE_WRITE_DRAIN_TIMEOUT_SECONDS",
                0.05,
            ),
            structlog.testing.capture_logs() as captured,
        ):
            await executor._drain_deferred_durable_writes()

        assert started.is_set()
        await asyncio.sleep(0)
        assert not executor._deferred_durable_write_tasks
        # The drain's explicit cancel routed through the loop's own loud
        # cancellation handler.
        assert any(entry["event"] == "test.deferred.deferred_write_cancelled" for entry in captured)

    @pytest.mark.asyncio
    async def test_drain_is_a_noop_with_no_pending_writes(self) -> None:
        executor = _make_executor_with_active_ladder()
        await executor._drain_deferred_durable_writes()
        assert not executor._deferred_durable_write_tasks
