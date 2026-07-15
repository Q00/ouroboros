"""Effort routing policy: the pure decision the live executor lays itself on."""

from __future__ import annotations

import pytest

from ouroboros.core.seed import InvestmentSpec
from ouroboros.orchestrator.adapter import ParamSupport
from ouroboros.orchestrator.effort_routing import (
    EFFORT_LADDER,
    EffortDecision,
    InvestmentAssessment,
    assess_investment,
    decide_effort,
    lower_one_notch,
    raise_one_notch,
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


class TestRaiseOneNotch:
    def test_lifts_one_rung(self) -> None:
        assert raise_one_notch("low") == "medium"
        assert raise_one_notch("medium") == "high"
        assert raise_one_notch("high") == "xhigh"

    def test_never_above_ceiling(self) -> None:
        assert raise_one_notch("xhigh") == "xhigh"  # ladder top, cannot rise further
        assert raise_one_notch("high", ceiling="high") == "high"
        assert raise_one_notch("low", ceiling="medium") == "medium"

    def test_unknown_level_passthrough(self) -> None:
        # Claude-only 'max' is off the shared ladder and is returned unchanged.
        assert raise_one_notch("max") == "max"
        assert raise_one_notch("bananas") == "bananas"


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

    def test_decomposed_child_inherits_parent_tier_unchanged(self) -> None:
        # V5: a decomposed child no longer runs one notch lower — a harder,
        # verified-MECE child inherits the parent tier unchanged.
        parent = decide_effort(ParamSupport.NATIVE, base_effort="high", is_decomposed_child=False)
        child = decide_effort(ParamSupport.NATIVE, base_effort="high", is_decomposed_child=True)
        assert parent.level == "high"
        assert child.level == "high"

    def test_child_at_low_base_also_inherits_unchanged(self) -> None:
        child = decide_effort(ParamSupport.NATIVE, base_effort="low", is_decomposed_child=True)
        assert child.level == "low"

    def test_second_retry_raises_one_notch(self) -> None:
        # retry_attempt: 0 initial, 1 first retry, 2 second retry -> raise.
        initial = decide_effort(
            ParamSupport.NATIVE, base_effort="medium", is_decomposed_child=False, retry_attempt=0
        )
        first = decide_effort(
            ParamSupport.NATIVE, base_effort="medium", is_decomposed_child=False, retry_attempt=1
        )
        second = decide_effort(
            ParamSupport.NATIVE, base_effort="medium", is_decomposed_child=False, retry_attempt=2
        )
        assert initial.level == "medium"
        assert first.level == "medium"  # first retry does not raise yet
        assert second.level == "high"  # second retry earns one extra notch

    def test_retry_raise_caps_at_ladder_top(self) -> None:
        d = decide_effort(
            ParamSupport.NATIVE, base_effort="xhigh", is_decomposed_child=False, retry_attempt=3
        )
        assert d.level == "xhigh"  # already at the top, cannot rise further

    def test_retry_raise_applies_to_decomposed_child_too(self) -> None:
        # Children inherit the parent tier AND still earn the retry raise.
        d = decide_effort(
            ParamSupport.NATIVE, base_effort="low", is_decomposed_child=True, retry_attempt=2
        )
        assert d.level == "medium"


class TestInvestmentAssessment:
    def test_absent_metadata_is_unknown_and_cannot_cheapen(self) -> None:
        assessment = assess_investment(None)

        assert assessment == InvestmentAssessment(
            difficulty="unknown",
            stakes="unknown",
            provenance="absent",
            confidence="low",
            used_signals=(),
            missing_signals=("difficulty", "stakes"),
            can_cheapen=False,
            minimum_effort=None,
            rationale="missing difficulty and stakes; base effort preserved",
        )

    def test_declared_low_axes_never_authorize_cheaper_execution(self) -> None:
        low_confidence = assess_investment(
            InvestmentSpec(
                difficulty="low",
                stakes="low",
                provenance="declared",
                confidence="low",
            )
        )
        high_confidence = assess_investment(
            InvestmentSpec(
                difficulty="low",
                stakes="low",
                provenance="declared",
                confidence="high",
            )
        )

        assert low_confidence.can_cheapen is False
        assert high_confidence.can_cheapen is False
        decision = decide_effort(
            ParamSupport.NATIVE,
            base_effort="high",
            is_decomposed_child=False,
            investment_assessment=high_confidence,
        )
        assert decision.level == "high"

    @pytest.mark.parametrize("provenance", ["inferred", "absent"])
    def test_inferred_or_absent_inputs_never_authorize_cheaper_execution(
        self, provenance: str
    ) -> None:
        assessment = assess_investment(
            InvestmentSpec.model_construct(
                difficulty="low",
                stakes="low",
                provenance=provenance,
                confidence="high",
            )
        )

        assert assessment.can_cheapen is False

    def test_high_axis_imposes_high_effort_floor(self) -> None:
        assessment = assess_investment(
            InvestmentSpec(
                difficulty="low",
                stakes="high",
                provenance="measured",
                confidence="high",
            )
        )
        decision = decide_effort(
            ParamSupport.NATIVE,
            base_effort="low",
            is_decomposed_child=False,
            investment_assessment=assessment,
        )

        assert assessment.minimum_effort == "high"
        assert assessment.can_cheapen is False
        assert decision.level == "high"

    def test_authorized_low_investment_lowers_exactly_one_notch(self) -> None:
        assessment = assess_investment(
            InvestmentSpec(
                difficulty="low",
                stakes="low",
                provenance="measured",
                confidence="high",
            )
        )
        decision = decide_effort(
            ParamSupport.NATIVE,
            base_effort="high",
            is_decomposed_child=False,
            investment_assessment=assessment,
        )

        assert decision.level == "medium"

    def test_authorized_low_investment_never_raises_an_already_minimal_base(self) -> None:
        assessment = assess_investment(
            InvestmentSpec(
                difficulty="low",
                stakes="low",
                provenance="measured",
                confidence="high",
            )
        )

        decision = decide_effort(
            ParamSupport.NATIVE,
            base_effort="minimal",
            is_decomposed_child=False,
            investment_assessment=assessment,
        )

        assert decision.level == "minimal"

    def test_retry_raise_applies_after_investment_policy(self) -> None:
        assessment = assess_investment(
            InvestmentSpec(
                difficulty="low",
                stakes="high",
                provenance="declared",
                confidence="high",
            )
        )
        decision = decide_effort(
            ParamSupport.NATIVE,
            base_effort="medium",
            is_decomposed_child=False,
            retry_attempt=2,
            investment_assessment=assessment,
        )

        assert decision.level == "xhigh"

    def test_no_base_effort_keeps_routing_dormant_even_with_assessment(self) -> None:
        assessment = assess_investment(
            InvestmentSpec(
                difficulty="high",
                stakes="high",
                provenance="declared",
                confidence="high",
            )
        )

        assert decide_effort(
            ParamSupport.NATIVE,
            base_effort=None,
            is_decomposed_child=False,
            investment_assessment=assessment,
        ) == EffortDecision(level=None, mode="none")


class TestDecideEffortEnforceableLevels:
    """A NATIVE runtime only enforces the levels its backend actually accepts."""

    def test_level_outside_vocabulary_is_advised_not_enforced(self) -> None:
        # Codex drops 'max' silently — declaring it enforced would be untruthful.
        codex_levels = frozenset({"minimal", "low", "medium", "high", "xhigh"})
        d = decide_effort(
            ParamSupport.NATIVE,
            base_effort="max",
            is_decomposed_child=False,
            enforceable_levels=codex_levels,
        )
        assert d.level == "max"
        assert d.mode == "advised"
        assert not d.is_enforced

    def test_level_inside_vocabulary_is_enforced(self) -> None:
        codex_levels = frozenset({"minimal", "low", "medium", "high", "xhigh"})
        d = decide_effort(
            ParamSupport.NATIVE,
            base_effort="high",
            is_decomposed_child=False,
            enforceable_levels=codex_levels,
        )
        assert d.mode == "enforced"

    def test_claude_only_minimal_is_advised(self) -> None:
        claude_levels = frozenset({"low", "medium", "high", "xhigh", "max"})
        d = decide_effort(
            ParamSupport.NATIVE,
            base_effort="minimal",
            is_decomposed_child=False,
            enforceable_levels=claude_levels,
        )
        assert d.mode == "advised"

    def test_none_vocabulary_imposes_no_restriction(self) -> None:
        d = decide_effort(
            ParamSupport.NATIVE,
            base_effort="max",
            is_decomposed_child=False,
            enforceable_levels=None,
        )
        assert d.mode == "enforced"


class _Caps:
    def __init__(self, support: ParamSupport, enforceable: frozenset[str] | None = None) -> None:
        self.reasoning_effort_support = support
        self.enforceable_reasoning_efforts = enforceable


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

    def test_second_retry_raises_the_enforced_kwarg(self) -> None:
        from ouroboros.orchestrator.effort_routing import resolve_execute_effort

        decision, kwargs = resolve_execute_effort(
            _Adapter(ParamSupport.NATIVE),
            base_effort="medium",
            is_decomposed_child=False,
            retry_attempt=2,
        )
        assert decision.level == "high"
        assert kwargs == {"reasoning_effort": "high"}

    def test_dormant_yields_no_kwarg(self) -> None:
        from ouroboros.orchestrator.effort_routing import resolve_execute_effort

        decision, kwargs = resolve_execute_effort(
            _Adapter(ParamSupport.NATIVE), base_effort=None, is_decomposed_child=False
        )
        assert decision.mode == "none"
        assert kwargs == {}

    def test_unenforceable_level_is_advised_with_no_kwarg(self) -> None:
        # A NATIVE runtime that cannot enforce the chosen level (declared via
        # enforceable_reasoning_efforts) records it advised and is not handed the kwarg.
        from ouroboros.orchestrator.effort_routing import resolve_execute_effort

        adapter = _Adapter(ParamSupport.NATIVE)
        adapter.capabilities = _Caps(
            ParamSupport.NATIVE, enforceable=frozenset({"low", "medium", "high", "xhigh"})
        )
        decision, kwargs = resolve_execute_effort(
            adapter, base_effort="minimal", is_decomposed_child=False
        )
        assert decision.mode == "advised"
        assert kwargs == {}
