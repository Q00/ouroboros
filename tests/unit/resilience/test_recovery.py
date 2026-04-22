"""Unit tests for run recovery planning."""

from __future__ import annotations

from ouroboros.resilience.lateral import ThinkingPersona
from ouroboros.resilience.recovery import (
    RecoveryActionKind,
    RecoveryPlanner,
    RecoverySnapshot,
    suggest_lateral_persona_for_pattern,
)
from ouroboros.resilience.stagnation import StagnationPattern


def test_suggest_lateral_persona_skips_failed_attempt_names() -> None:
    persona = suggest_lateral_persona_for_pattern(
        StagnationPattern.NO_DRIFT,
        failed_attempts=("researcher", "not-a-persona"),
    )

    assert persona == ThinkingPersona.ARCHITECT


def test_recovery_planner_injects_lateral_directive_for_failure() -> None:
    planner = RecoveryPlanner()

    action = planner.plan(
        RecoverySnapshot(
            problem_context="Goal: fix tests\nPrevious final message: tests failed",
            current_approach="The run retried the same failing test.",
            final_error="tests failed",
        )
    )

    assert action.kind == RecoveryActionKind.INJECT_LATERAL_DIRECTIVE
    assert action.pattern == StagnationPattern.SPINNING
    assert action.persona == ThinkingPersona.HACKER
    assert "Lateral Recovery Directive" in action.directive
    assert "Selected persona: hacker" in action.directive


def test_recovery_planner_respects_intervention_budget() -> None:
    planner = RecoveryPlanner(max_interventions=1)

    action = planner.plan(
        RecoverySnapshot(
            problem_context="Goal: fix tests",
            current_approach="Already tried recovery.",
            final_error="tests failed",
            interventions_used=1,
        )
    )

    assert action.kind == RecoveryActionKind.STAGNATED
    assert "budget exhausted" in action.reason


def test_recovery_planner_stagnates_when_all_personas_excluded() -> None:
    planner = RecoveryPlanner()

    action = planner.plan(
        RecoverySnapshot(
            problem_context="Goal: fix tests",
            current_approach="Tried every lateral path.",
            final_error="tests failed",
            failed_attempts=tuple(persona.value for persona in ThinkingPersona),
        )
    )

    assert action.kind == RecoveryActionKind.STAGNATED
    assert "No available" in action.reason
