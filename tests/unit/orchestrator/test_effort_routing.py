"""Effort routing policy: the pure decision the live executor lays itself on."""

from __future__ import annotations

import pytest

from ouroboros.orchestrator.adapter import ParamSupport
from ouroboros.orchestrator.effort_routing import (
    EFFORT_LADDER,
    EffortDecision,
    decide_effort,
    lower_one_notch,
)


class TestLowerOneNotch:
    def test_drops_one_rung(self) -> None:
        assert lower_one_notch("high") == "medium"
        assert lower_one_notch("xhigh") == "high"

    def test_never_below_floor(self) -> None:
        assert lower_one_notch("low", floor="low") == "low"
        assert lower_one_notch("medium", floor="low") == "low"
        assert lower_one_notch("minimal", floor="low") == "low"  # clamps UP to floor

    def test_custom_floor(self) -> None:
        assert lower_one_notch("medium", floor="medium") == "medium"
        assert lower_one_notch("high", floor="medium") == "medium"

    def test_unknown_level_passthrough(self) -> None:
        assert lower_one_notch("bananas") == "bananas"

    def test_ladder_is_ordered_weak_to_strong(self) -> None:
        assert EFFORT_LADDER.index("low") < EFFORT_LADDER.index("high")


class TestDecideEffort:
    def test_dormant_when_no_base_effort(self) -> None:
        d = decide_effort(ParamSupport.NATIVE, base_effort=None, is_decomposed_child=True)
        assert d == EffortDecision(level=None, mode="none")
        assert d.is_enforced is False

    def test_enforced_on_native_runtime(self) -> None:
        d = decide_effort(ParamSupport.NATIVE, base_effort="high", is_decomposed_child=False)
        assert d.level == "high"
        assert d.mode == "enforced"
        assert d.is_enforced is True

    @pytest.mark.parametrize("support", [ParamSupport.IGNORED, ParamSupport.TRANSLATED])
    def test_advised_on_non_native_runtime(self, support: ParamSupport) -> None:
        d = decide_effort(support, base_effort="high", is_decomposed_child=False)
        assert d.level == "high"
        assert d.mode == "advised"
        assert d.is_enforced is False  # advised never counts as enforced

    def test_decomposed_child_runs_one_notch_lower(self) -> None:
        parent = decide_effort(ParamSupport.NATIVE, base_effort="high", is_decomposed_child=False)
        child = decide_effort(ParamSupport.NATIVE, base_effort="high", is_decomposed_child=True)
        assert parent.level == "high"
        assert child.level == "medium"

    def test_child_respects_floor(self) -> None:
        child = decide_effort(ParamSupport.NATIVE, base_effort="low", is_decomposed_child=True)
        assert child.level == "low"  # floor=low, cannot drop further


class _Caps:
    def __init__(self, support: ParamSupport) -> None:
        self.reasoning_effort_support = support


class _Adapter:
    def __init__(self, support: ParamSupport | None) -> None:
        if support is not None:
            self.capabilities = _Caps(support)


class TestResolveExecuteEffort:
    """The shared helper every live execute_task call site uses."""

    def test_enforced_runtime_yields_kwarg(self) -> None:
        from ouroboros.orchestrator.effort_routing import resolve_execute_effort

        decision, kwargs = resolve_execute_effort(
            _Adapter(ParamSupport.NATIVE), base_effort="high", is_decomposed_child=False
        )
        assert decision.mode == "enforced"
        assert kwargs == {"reasoning_effort": "high"}

    def test_advised_runtime_yields_no_kwarg(self) -> None:
        from ouroboros.orchestrator.effort_routing import resolve_execute_effort

        decision, kwargs = resolve_execute_effort(
            _Adapter(ParamSupport.IGNORED), base_effort="high", is_decomposed_child=False
        )
        assert decision.mode == "advised"
        assert kwargs == {}  # never hand the kwarg to a runtime that ignores it

    def test_adapter_without_capabilities_is_treated_as_advised(self) -> None:
        from ouroboros.orchestrator.effort_routing import resolve_execute_effort

        decision, kwargs = resolve_execute_effort(
            _Adapter(None), base_effort="high", is_decomposed_child=False
        )
        assert decision.mode == "advised"
        assert kwargs == {}

    def test_dormant_yields_no_kwarg(self) -> None:
        from ouroboros.orchestrator.effort_routing import resolve_execute_effort

        decision, kwargs = resolve_execute_effort(
            _Adapter(ParamSupport.NATIVE), base_effort=None, is_decomposed_child=False
        )
        assert decision.mode == "none"
        assert kwargs == {}
