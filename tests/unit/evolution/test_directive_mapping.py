"""Unit tests for the StepAction → Directive mapping (slice 1 of #472).

Closes #516 and pins the decision matrix the maintainer alignment in
#476 Q5 agreed for the evolution-first migration.
"""

from __future__ import annotations

from ouroboros.core.directive import Directive
from ouroboros.evolution.directive_mapping import (
    is_terminal_directive,
    step_action_to_directive,
)
from ouroboros.evolution.loop import StepAction


class TestStepActionMapping:
    def test_continue_does_not_emit(self) -> None:
        """A CONTINUE step is the no-op case; no directive event."""
        assert step_action_to_directive(StepAction.CONTINUE) is None

    def test_converged_maps_to_converge(self) -> None:
        assert step_action_to_directive(StepAction.CONVERGED) == Directive.CONVERGE

    def test_stagnated_maps_to_unstuck(self) -> None:
        assert step_action_to_directive(StepAction.STAGNATED) == Directive.UNSTUCK

    def test_exhausted_maps_to_cancel(self) -> None:
        assert step_action_to_directive(StepAction.EXHAUSTED) == Directive.CANCEL

    def test_failed_with_budget_maps_to_retry(self) -> None:
        assert (
            step_action_to_directive(StepAction.FAILED, retry_budget_remaining=2) == Directive.RETRY
        )

    def test_failed_without_budget_maps_to_cancel(self) -> None:
        assert (
            step_action_to_directive(StepAction.FAILED, retry_budget_remaining=0)
            == Directive.CANCEL
        )

    def test_interrupted_maps_to_cancel(self) -> None:
        assert step_action_to_directive(StepAction.INTERRUPTED) == Directive.CANCEL

    def test_string_value_accepted(self) -> None:
        """The function accepts the StepAction value verbatim (StrEnum semantics)."""
        assert step_action_to_directive("converged") == Directive.CONVERGE
        assert step_action_to_directive("stagnated") == Directive.UNSTUCK
        assert step_action_to_directive("continue") is None

    def test_unknown_action_value_returns_none(self) -> None:
        """Forward-compatible: an unrecognized value emits no directive."""
        assert step_action_to_directive("future_step_action_member") is None


class TestTerminalClassification:
    def test_converge_is_terminal(self) -> None:
        assert is_terminal_directive(Directive.CONVERGE) is True

    def test_cancel_is_terminal(self) -> None:
        assert is_terminal_directive(Directive.CANCEL) is True

    def test_retry_is_not_terminal(self) -> None:
        assert is_terminal_directive(Directive.RETRY) is False

    def test_unstuck_is_not_terminal(self) -> None:
        assert is_terminal_directive(Directive.UNSTUCK) is False
