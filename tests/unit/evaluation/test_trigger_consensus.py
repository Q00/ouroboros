"""Tests for trigger_consensus wiring — Bug 1 of #230.

Verifies that trigger_consensus flows from MCP handler → EvaluationContext
→ TriggerContext → ConsensusTrigger and correctly bypasses the Stage 2
threshold gate.

See: https://github.com/Q00/ouroboros/issues/230
"""

from __future__ import annotations

from ouroboros.evaluation.models import EvaluationContext
from ouroboros.evaluation.trigger import (
    ConsensusTrigger,
    TriggerContext,
    TriggerType,
)


class TestTriggerConsensusField:
    """EvaluationContext and TriggerContext accept trigger_consensus."""

    def test_evaluation_context_defaults_to_false(self) -> None:
        ctx = EvaluationContext(
            execution_id="e1",
            seed_id="s1",
            current_ac="ac1",
            artifact="code",
        )
        assert ctx.trigger_consensus is False

    def test_evaluation_context_accepts_true(self) -> None:
        ctx = EvaluationContext(
            execution_id="e1",
            seed_id="s1",
            current_ac="ac1",
            artifact="code",
            trigger_consensus=True,
        )
        assert ctx.trigger_consensus is True

    def test_trigger_context_defaults_to_false(self) -> None:
        ctx = TriggerContext(execution_id="e1")
        assert ctx.manual_consensus_request is False

    def test_trigger_context_accepts_true(self) -> None:
        ctx = TriggerContext(execution_id="e1", manual_consensus_request=True)
        assert ctx.manual_consensus_request is True


class TestManualConsensusRequest:
    """ConsensusTrigger fires immediately on manual_consensus_request."""

    def test_manual_request_triggers_consensus(self) -> None:
        ctx = TriggerContext(execution_id="e1", manual_consensus_request=True)
        trigger = ConsensusTrigger()
        result = trigger.evaluate(ctx)

        assert result.is_ok
        decision, events = result.value
        assert decision.should_trigger is True
        assert decision.trigger_type == TriggerType.MANUAL_REQUEST
        assert len(events) == 1

    def test_manual_request_takes_priority_over_other_conditions(self) -> None:
        """Manual request fires even when no other trigger conditions are met."""
        ctx = TriggerContext(
            execution_id="e1",
            manual_consensus_request=True,
            seed_modified=False,
            drift_score=0.0,
            uncertainty_score=0.0,
        )
        trigger = ConsensusTrigger()
        result = trigger.evaluate(ctx)

        assert result.is_ok
        decision, _ = result.value
        assert decision.should_trigger is True
        assert decision.trigger_type == TriggerType.MANUAL_REQUEST

    def test_no_manual_request_falls_through_to_normal_checks(self) -> None:
        """Without manual request, normal trigger logic applies."""
        ctx = TriggerContext(
            execution_id="e1",
            manual_consensus_request=False,
            seed_modified=False,
            drift_score=0.0,
            uncertainty_score=0.0,
        )
        trigger = ConsensusTrigger()
        result = trigger.evaluate(ctx)

        assert result.is_ok
        decision, _ = result.value
        assert decision.should_trigger is False
