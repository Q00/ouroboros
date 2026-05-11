"""Unit tests for the StepAction → Directive mapping (slice 1 of #472).

Closes #516 and pins the decision matrix the maintainer alignment in
#476 Q5 agreed for the evolution-first migration.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

from ouroboros.core.directive import Directive
from ouroboros.evolution.directive_mapping import (
    WATCHDOG_TIMEOUT_KINDS,
    is_terminal_directive,
    step_action_to_directive,
    watchdog_timeout_to_directive,
)
from ouroboros.evolution.loop import StepAction
from ouroboros.evolution.watchdog import GenerationProgressWatchdog


class TestStepActionMapping:
    def test_continue_does_not_emit(self) -> None:
        """A CONTINUE step is the no-op case; no directive event."""
        assert step_action_to_directive(StepAction.CONTINUE) is None

    def test_converged_maps_to_converge(self) -> None:
        assert step_action_to_directive(StepAction.CONVERGED) == Directive.CONVERGE

    def test_stagnated_maps_to_unstuck_not_terminal_success(self) -> None:
        directive = step_action_to_directive(StepAction.STAGNATED)

        assert directive == Directive.UNSTUCK
        assert directive is not Directive.CONVERGE
        assert directive is not None
        assert directive.is_terminal is False

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


class TestWatchdogTimeoutMapping:
    """Pin the watchdog-timeout → directive matrix for #578.

    Conservative-by-design mapping documented in
    ``ouroboros.evolution.directive_mapping``: safety/idle ⇒ CANCEL,
    no-material-progress ⇒ UNSTUCK. WAIT and RETRY are intentionally
    not produced — the watchdog has already waited past its budget,
    and retry budgeting lives at the step layer below.
    """

    def test_safety_timeout_maps_to_cancel(self) -> None:
        directive = watchdog_timeout_to_directive("safety_timeout")
        assert directive == Directive.CANCEL
        assert directive is not None and directive.is_terminal is True

    def test_idle_timeout_maps_to_cancel(self) -> None:
        directive = watchdog_timeout_to_directive("idle_timeout")
        assert directive == Directive.CANCEL
        assert directive is not None and directive.is_terminal is True

    def test_no_material_progress_timeout_maps_to_unstuck(self) -> None:
        """No-progress is the canonical lateral-thinking trigger; the
        directive must be non-terminal so the runtime can route the
        lineage through an UNSTUCK persona rather than aborting."""
        directive = watchdog_timeout_to_directive("no_material_progress_timeout")
        assert directive == Directive.UNSTUCK
        assert directive is not None and directive.is_terminal is False

    def test_unknown_timeout_kind_returns_none(self) -> None:
        """Forward-compatible: an unrecognized timeout name silently
        maps to None so a future watchdog threshold lands without
        breaking older callers."""
        assert watchdog_timeout_to_directive("future_threshold_we_have_not_named") is None
        assert watchdog_timeout_to_directive("") is None

    def test_no_watchdog_timeout_produces_wait_or_retry(self) -> None:
        """Pin the intentional absence of WAIT/RETRY in the watchdog
        mapping — see module docstring rationale. WAIT would ask the
        runtime to wait longer (the very thing the watchdog exists to
        cut short); RETRY belongs at the step layer below."""
        produced = {watchdog_timeout_to_directive(kind) for kind in WATCHDOG_TIMEOUT_KINDS}
        assert Directive.WAIT not in produced
        assert Directive.RETRY not in produced

    def test_every_kind_in_the_alphabet_has_a_mapping(self) -> None:
        """``WATCHDOG_TIMEOUT_KINDS`` is the public alphabet of timeout
        names the watchdog can raise. Adding a name without an entry
        in the lookup table would silently fall back to ``None`` and
        skip directive emission, which masks the watchdog decision on
        the control plane. The mapping table must therefore cover
        every kind in the constant."""
        for kind in WATCHDOG_TIMEOUT_KINDS:
            assert watchdog_timeout_to_directive(kind) is not None, kind

    def test_timeout_alphabet_matches_watchdog_raise_sites(self) -> None:
        """Pin the mapping alphabet to the watchdog's actual timeout raises.

        This catches drift in either direction: adding a new
        ``_raise_timeout("...")`` call without extending the directive
        alphabet, or leaving a stale mapping kind after the watchdog stops
        raising it.
        """
        tree = ast.parse(
            textwrap.dedent(
                inspect.getsource(GenerationProgressWatchdog._raise_if_threshold_exceeded)
            )
        )
        raised_kinds = {
            call.args[0].value
            for call in ast.walk(tree)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "_raise_timeout"
            and call.args
            and isinstance(call.args[0], ast.Constant)
            and isinstance(call.args[0].value, str)
        }

        assert raised_kinds == set(WATCHDOG_TIMEOUT_KINDS)
