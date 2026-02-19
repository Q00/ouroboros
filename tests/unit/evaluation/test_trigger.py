"""Tests for Consensus Trigger Matrix."""

from ouroboros.evaluation.models import SemanticResult
from ouroboros.evaluation.trigger import (
    ConsensusTrigger,
    TriggerConfig,
    TriggerContext,
    TriggerType,
    check_consensus_trigger,
)


class TestTriggerType:
    """Tests for TriggerType enum."""

    def test_all_trigger_types(self) -> None:
        """All 6 trigger types exist."""
        assert TriggerType.SEED_MODIFICATION == "seed_modification"
        assert TriggerType.ONTOLOGY_EVOLUTION == "ontology_evolution"
        assert TriggerType.GOAL_INTERPRETATION == "goal_interpretation"
        assert TriggerType.SEED_DRIFT_ALERT == "seed_drift_alert"
        assert TriggerType.STAGE2_UNCERTAINTY == "stage2_uncertainty"
        assert TriggerType.LATERAL_THINKING_ADOPTION == "lateral_thinking_adoption"

    def test_trigger_count(self) -> None:
        """Exactly 6 trigger types per FR16."""
        assert len(TriggerType) == 6


class TestTriggerContext:
    """Tests for TriggerContext."""

    def test_default_values(self) -> None:
        """Default values are safe (no triggers)."""
        ctx = TriggerContext(execution_id="exec-1")
        assert ctx.seed_modified is False
        assert ctx.ontology_changed is False
        assert ctx.goal_reinterpreted is False
        assert ctx.drift_score == 0.0
        assert ctx.uncertainty_score == 0.0
        assert ctx.lateral_thinking_adopted is False

    def test_with_semantic_result(self) -> None:
        """Context can include semantic result."""
        semantic = SemanticResult(
            score=0.8,
            ac_compliance=True,
            goal_alignment=0.9,
            drift_score=0.4,
            uncertainty=0.5,
            reasoning="Test",
        )
        ctx = TriggerContext(
            execution_id="exec-1",
            semantic_result=semantic,
        )
        assert ctx.semantic_result.drift_score == 0.4


class TestTriggerConfig:
    """Tests for TriggerConfig."""

    def test_default_thresholds(self) -> None:
        """Default thresholds are 0.3."""
        config = TriggerConfig()
        assert config.drift_threshold == 0.3
        assert config.uncertainty_threshold == 0.3

    def test_custom_thresholds(self) -> None:
        """Custom thresholds can be set."""
        config = TriggerConfig(
            drift_threshold=0.5,
            uncertainty_threshold=0.4,
        )
        assert config.drift_threshold == 0.5
        assert config.uncertainty_threshold == 0.4


class TestConsensusTrigger:
    """Tests for ConsensusTrigger class."""

    def test_no_trigger_default_context(self) -> None:
        """No trigger with default context."""
        trigger = ConsensusTrigger()
        ctx = TriggerContext(execution_id="exec-1")
        result = trigger.evaluate(ctx)

        assert result.is_ok
        trigger_result, events = result.value
        assert trigger_result.should_trigger is False
        assert len(events) == 0

    def test_seed_modification_trigger(self) -> None:
        """Seed modification triggers consensus."""
        trigger = ConsensusTrigger()
        ctx = TriggerContext(
            execution_id="exec-1",
            seed_modified=True,
        )
        result = trigger.evaluate(ctx)

        assert result.is_ok
        trigger_result, events = result.value
        assert trigger_result.should_trigger is True
        assert trigger_result.trigger_type == TriggerType.SEED_MODIFICATION
        assert len(events) == 1
        assert events[0].type == "evaluation.consensus.triggered"

    def test_ontology_evolution_trigger(self) -> None:
        """Ontology change triggers consensus."""
        trigger = ConsensusTrigger()
        ctx = TriggerContext(
            execution_id="exec-1",
            ontology_changed=True,
        )
        result = trigger.evaluate(ctx)

        assert result.is_ok
        trigger_result, _ = result.value
        assert trigger_result.should_trigger is True
        assert trigger_result.trigger_type == TriggerType.ONTOLOGY_EVOLUTION

    def test_goal_interpretation_trigger(self) -> None:
        """Goal reinterpretation triggers consensus."""
        trigger = ConsensusTrigger()
        ctx = TriggerContext(
            execution_id="exec-1",
            goal_reinterpreted=True,
        )
        result = trigger.evaluate(ctx)

        assert result.is_ok
        trigger_result, _ = result.value
        assert trigger_result.should_trigger is True
        assert trigger_result.trigger_type == TriggerType.GOAL_INTERPRETATION

    def test_drift_alert_trigger(self) -> None:
        """High drift triggers consensus."""
        trigger = ConsensusTrigger()
        ctx = TriggerContext(
            execution_id="exec-1",
            drift_score=0.4,  # > 0.3 threshold
        )
        result = trigger.evaluate(ctx)

        assert result.is_ok
        trigger_result, _ = result.value
        assert trigger_result.should_trigger is True
        assert trigger_result.trigger_type == TriggerType.SEED_DRIFT_ALERT
        assert "0.4" in trigger_result.reason

    def test_drift_from_semantic_result(self) -> None:
        """Drift from semantic result is used."""
        semantic = SemanticResult(
            score=0.8,
            ac_compliance=True,
            goal_alignment=0.9,
            drift_score=0.5,  # High drift
            uncertainty=0.1,  # Low uncertainty
            reasoning="Test",
        )
        trigger = ConsensusTrigger()
        ctx = TriggerContext(
            execution_id="exec-1",
            drift_score=0.1,  # Context has low drift
            semantic_result=semantic,  # But semantic result has high
        )
        result = trigger.evaluate(ctx)

        assert result.is_ok
        trigger_result, _ = result.value
        assert trigger_result.should_trigger is True
        assert trigger_result.trigger_type == TriggerType.SEED_DRIFT_ALERT

    def test_drift_at_threshold_no_trigger(self) -> None:
        """Drift at exactly threshold does NOT trigger."""
        trigger = ConsensusTrigger()
        ctx = TriggerContext(
            execution_id="exec-1",
            drift_score=0.3,  # Exactly at threshold
        )
        result = trigger.evaluate(ctx)

        assert result.is_ok
        trigger_result, _ = result.value
        assert trigger_result.should_trigger is False

    def test_uncertainty_trigger(self) -> None:
        """High uncertainty triggers consensus."""
        trigger = ConsensusTrigger()
        ctx = TriggerContext(
            execution_id="exec-1",
            uncertainty_score=0.4,  # > 0.3 threshold
        )
        result = trigger.evaluate(ctx)

        assert result.is_ok
        trigger_result, _ = result.value
        assert trigger_result.should_trigger is True
        assert trigger_result.trigger_type == TriggerType.STAGE2_UNCERTAINTY

    def test_uncertainty_from_semantic_result(self) -> None:
        """Uncertainty from semantic result is used."""
        semantic = SemanticResult(
            score=0.8,
            ac_compliance=True,
            goal_alignment=0.9,
            drift_score=0.1,
            uncertainty=0.5,  # High uncertainty
            reasoning="Test",
        )
        trigger = ConsensusTrigger()
        ctx = TriggerContext(
            execution_id="exec-1",
            uncertainty_score=0.1,  # Context has low uncertainty
            semantic_result=semantic,
        )
        result = trigger.evaluate(ctx)

        assert result.is_ok
        trigger_result, _ = result.value
        assert trigger_result.should_trigger is True
        assert trigger_result.trigger_type == TriggerType.STAGE2_UNCERTAINTY

    def test_lateral_thinking_trigger(self) -> None:
        """Lateral thinking adoption triggers consensus."""
        trigger = ConsensusTrigger()
        ctx = TriggerContext(
            execution_id="exec-1",
            lateral_thinking_adopted=True,
        )
        result = trigger.evaluate(ctx)

        assert result.is_ok
        trigger_result, _ = result.value
        assert trigger_result.should_trigger is True
        assert trigger_result.trigger_type == TriggerType.LATERAL_THINKING_ADOPTION

    def test_priority_order(self) -> None:
        """First matching trigger is returned (priority order)."""
        trigger = ConsensusTrigger()
        ctx = TriggerContext(
            execution_id="exec-1",
            seed_modified=True,
            ontology_changed=True,  # Both true
        )
        result = trigger.evaluate(ctx)

        assert result.is_ok
        trigger_result, _ = result.value
        # Seed modification has higher priority
        assert trigger_result.trigger_type == TriggerType.SEED_MODIFICATION

    def test_custom_thresholds(self) -> None:
        """Custom thresholds are respected."""
        config = TriggerConfig(
            drift_threshold=0.5,
            uncertainty_threshold=0.5,
        )
        trigger = ConsensusTrigger(config)

        # 0.4 drift doesn't trigger with 0.5 threshold
        ctx = TriggerContext(
            execution_id="exec-1",
            drift_score=0.4,
        )
        result = trigger.evaluate(ctx)

        assert result.is_ok
        trigger_result, _ = result.value
        assert trigger_result.should_trigger is False


class TestCheckConsensusTrigger:
    """Tests for convenience function."""

    def test_convenience_function(self) -> None:
        """Convenience function works correctly."""
        ctx = TriggerContext(
            execution_id="exec-1",
            seed_modified=True,
        )
        result = check_consensus_trigger(ctx)

        assert result.is_ok
        trigger_result, _ = result.value
        assert trigger_result.should_trigger is True

    def test_with_custom_config(self) -> None:
        """Convenience function accepts config."""
        config = TriggerConfig(drift_threshold=0.9)
        ctx = TriggerContext(
            execution_id="exec-1",
            drift_score=0.5,  # Below 0.9 threshold
        )
        result = check_consensus_trigger(ctx, config)

        assert result.is_ok
        trigger_result, _ = result.value
        assert trigger_result.should_trigger is False
