"""Tests for the pure lateral-persona escalation ladder decision points."""

from __future__ import annotations

from ouroboros.orchestrator.lateral_escalation import (
    _LATERAL_ESCALATION_THRESHOLD,
    TOTAL_PERSONA_COUNT,
    LateralEscalationState,
    advance_lateral_escalation,
    build_persona_retry_prompt,
    is_terminal_state_failure,
)
from ouroboros.resilience.lateral import ThinkingPersona


class TestIsTerminalStateFailure:
    def test_success_is_never_terminal_failure(self) -> None:
        assert (
            is_terminal_state_failure(
                success=True, is_decomposed=False, model_tier="frontier", effort_level="xhigh"
            )
            is False
        )

    def test_decomposed_node_is_never_terminal_failure(self) -> None:
        """A decomposed node has a cheaper lever left: not decomposing."""
        assert (
            is_terminal_state_failure(
                success=False, is_decomposed=True, model_tier="frontier", effort_level="xhigh"
            )
            is False
        )

    def test_frontier_tier_and_max_effort_is_terminal(self) -> None:
        assert (
            is_terminal_state_failure(
                success=False, is_decomposed=False, model_tier="frontier", effort_level="xhigh"
            )
            is True
        )

    def test_fully_dormant_routing_never_engages_the_ladder(self) -> None:
        """No model_router and no reasoning_effort configured at all: there is
        no escalation dial to have maxed out, so this must stay False — an
        executor with no ladder configured keeps its unmodified
        give-up-after-N-retries behavior instead of retrying forever."""
        assert (
            is_terminal_state_failure(
                success=False, is_decomposed=False, model_tier=None, effort_level=None
            )
            is False
        )

    def test_one_dormant_axis_with_the_other_at_ceiling_is_terminal(self) -> None:
        """Effort routing dormant, but model routing IS configured and
        already at the frontier tier: the one active dial has maxed out."""
        assert (
            is_terminal_state_failure(
                success=False, is_decomposed=False, model_tier="frontier", effort_level=None
            )
            is True
        )

    def test_below_frontier_tier_is_not_terminal(self) -> None:
        assert (
            is_terminal_state_failure(
                success=False, is_decomposed=False, model_tier="standard", effort_level="xhigh"
            )
            is False
        )

    def test_below_max_effort_is_not_terminal(self) -> None:
        assert (
            is_terminal_state_failure(
                success=False, is_decomposed=False, model_tier="frontier", effort_level="medium"
            )
            is False
        )


class TestAdvanceLateralEscalation:
    def test_non_terminal_failure_resets_streak(self) -> None:
        state = LateralEscalationState(consecutive_terminal_failures=5, parked=False)
        step = advance_lateral_escalation(state, terminal_state_failure=False)

        assert step.state == LateralEscalationState()
        assert step.persona is None
        assert step.apply_long_backoff is False

    def test_streak_below_threshold_offers_no_persona_yet(self) -> None:
        state = LateralEscalationState()
        step = advance_lateral_escalation(state, terminal_state_failure=True)

        assert step.state.consecutive_terminal_failures == 1
        assert step.persona is None
        assert step.just_parked is False
        assert step.apply_long_backoff is False

    def test_threshold_crossing_offers_first_persona(self) -> None:
        state = LateralEscalationState(
            consecutive_terminal_failures=_LATERAL_ESCALATION_THRESHOLD - 1
        )
        step = advance_lateral_escalation(state, terminal_state_failure=True)

        assert step.state.consecutive_terminal_failures == _LATERAL_ESCALATION_THRESHOLD
        assert step.persona is not None
        assert step.state.personas_tried == (step.persona,)

    def test_persona_cycling_never_repeats(self) -> None:
        """Cycling through the ladder must visit each of the 5 personas
        exactly once before parking — no repeats."""
        state = LateralEscalationState(
            consecutive_terminal_failures=_LATERAL_ESCALATION_THRESHOLD - 1
        )
        seen: list[ThinkingPersona] = []

        for _ in range(TOTAL_PERSONA_COUNT):
            step = advance_lateral_escalation(state, terminal_state_failure=True)
            assert step.persona is not None
            assert step.persona not in seen
            seen.append(step.persona)
            state = step.state

        assert len(seen) == len(set(seen)) == TOTAL_PERSONA_COUNT
        assert state.parked is False  # not parked until personas are EXHAUSTED

    def test_generic_failure_persona_order_is_pattern_aware_not_fixed_linear(self) -> None:
        """Fix 8 (P2, round 2 review): documents/locks in the REAL
        persona-cycling order for a generic/unclassified failure (no
        ``failure_text``, or text with no pattern-specific keyword match --
        both classify as the SPINNING fallback pattern). Persona selection
        reuses ``select_persona_for_qa_failure`` verbatim, whose order is
        pattern-primary first, THEN ``contrarian`` as the universal
        fallback, THEN the rest of the deterministic chain -- NOT the fixed
        linear list (hacker -> architect -> researcher -> simplifier ->
        contrarian) an earlier PR description stated. See the module
        docstring's "actual persona-cycling order" section for the full
        explanation of why the DESCRIPTION was corrected instead of this
        selector."""
        state = LateralEscalationState(
            consecutive_terminal_failures=_LATERAL_ESCALATION_THRESHOLD - 1
        )
        order: list[ThinkingPersona] = []
        for _ in range(TOTAL_PERSONA_COUNT):
            step = advance_lateral_escalation(state, terminal_state_failure=True)
            assert step.persona is not None
            order.append(step.persona)
            state = step.state

        assert order == [
            ThinkingPersona.HACKER,
            ThinkingPersona.CONTRARIAN,
            ThinkingPersona.ARCHITECT,
            ThinkingPersona.RESEARCHER,
            ThinkingPersona.SIMPLIFIER,
        ]

    def test_all_personas_exhausted_parks(self) -> None:
        state = LateralEscalationState(
            consecutive_terminal_failures=_LATERAL_ESCALATION_THRESHOLD - 1
        )
        for _ in range(TOTAL_PERSONA_COUNT):
            step = advance_lateral_escalation(state, terminal_state_failure=True)
            state = step.state

        # One more terminal failure after all 5 personas tried: parks.
        final_step = advance_lateral_escalation(state, terminal_state_failure=True)

        assert final_step.just_parked is True
        assert final_step.state.parked is True
        assert final_step.persona is None
        assert final_step.apply_long_backoff is True

    def test_parked_state_keeps_advancing_and_never_hard_stops(self) -> None:
        """Once parked, repeated terminal failures keep producing a NEW step
        with ``apply_long_backoff=True`` — the ladder never raises, never
        returns a terminal/absorbing 'stop' sentinel, and the streak keeps
        counting, proving the caller can loop forever without the ladder
        itself hard-stopping."""
        parked_state = LateralEscalationState(
            consecutive_terminal_failures=10,
            personas_tried=tuple(ThinkingPersona),
            parked=True,
        )

        for i in range(5):
            step = advance_lateral_escalation(parked_state, terminal_state_failure=True)
            assert step.state.parked is True
            assert step.apply_long_backoff is True
            assert step.just_parked is False  # only true on the transition step
            assert step.state.consecutive_terminal_failures == 10 + i + 1
            parked_state = step.state


class TestBuildPersonaRetryPrompt:
    def test_prompt_mentions_persona_and_problem(self) -> None:
        prompt = build_persona_retry_prompt(
            persona=ThinkingPersona.HACKER,
            ac_content="Implement the widget",
            current_approach="Tried X and it failed",
            failed_attempts=("verify_gate_failed",),
        )

        assert "Hacker" in prompt
        assert "Implement the widget" in prompt

    def test_different_personas_produce_different_prompts(self) -> None:
        prompts = {
            persona: build_persona_retry_prompt(
                persona=persona,
                ac_content="Implement the widget",
                current_approach="Tried X and it failed",
            )
            for persona in ThinkingPersona
        }

        assert len(set(prompts.values())) == len(ThinkingPersona)
