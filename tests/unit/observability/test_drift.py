"""Unit tests for drift measurement engine.

Tests for Story 6.1: Drift Measurement Engine

Covers:
- AC1: Goal drift calculation
- AC2: Constraint drift calculation
- AC3: Ontology drift calculation
- AC4: Combined drift weighted formula
- AC5: Threshold checking (≤0.3)
- AC6: Drift measurement after each iteration
- AC7: Drift events stored in event log
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from ouroboros.core.seed import (
    EvaluationPrinciple,
    ExitCondition,
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def sample_seed() -> Seed:
    """Create a sample seed for testing drift calculations."""
    return Seed(
        goal="Build a CLI task management tool with CRUD operations",
        constraints=(
            "Must use Python 3.14+",
            "No external database",
            "Response time under 100ms",
        ),
        acceptance_criteria=(
            "Tasks can be created",
            "Tasks can be listed",
            "Tasks can be deleted",
        ),
        ontology_schema=OntologySchema(
            name="TaskManager",
            description="Task management ontology",
            fields=(
                OntologyField(
                    name="tasks",
                    field_type="array",
                    description="List of tasks",
                ),
                OntologyField(
                    name="task_id",
                    field_type="string",
                    description="Unique task identifier",
                ),
                OntologyField(
                    name="status",
                    field_type="string",
                    description="Task status",
                ),
            ),
        ),
        evaluation_principles=(
            EvaluationPrinciple(
                name="completeness",
                description="All requirements are met",
            ),
        ),
        exit_conditions=(
            ExitCondition(
                name="all_criteria_met",
                description="All acceptance criteria satisfied",
                evaluation_criteria="100% criteria pass",
            ),
        ),
        metadata=SeedMetadata(ambiguity_score=0.15),
    )


@pytest.fixture
def current_state_aligned() -> dict:
    """Current state that is well-aligned with seed."""
    return {
        "output": "CLI task management tool with create, list, delete operations",
        "constraint_violations": [],
        "ontology_concepts": ["tasks", "task_id", "status", "priority"],
    }


@pytest.fixture
def current_state_drifted() -> dict:
    """Current state that has drifted from seed."""
    return {
        "output": "Web-based calendar application with scheduling features",
        "constraint_violations": ["Uses external PostgreSQL database"],
        "ontology_concepts": ["appointments", "calendar", "schedule", "notifications"],
    }


# =============================================================================
# AC1: Goal Drift Calculation Tests
# =============================================================================


class TestGoalDriftCalculation:
    """Tests for calculate_goal_drift() (AC1)."""

    def test_goal_drift_identical_output_is_zero(self, sample_seed: Seed) -> None:
        """Identical goal produces zero drift."""
        from ouroboros.observability.drift import calculate_goal_drift

        result = calculate_goal_drift(
            current_output=sample_seed.goal,
            seed=sample_seed,
        )

        assert result == pytest.approx(0.0, abs=0.1)

    def test_goal_drift_similar_output_is_low(self, sample_seed: Seed) -> None:
        """Similar output produces lower drift than unrelated output."""
        from ouroboros.observability.drift import calculate_goal_drift

        # Similar output (shares many words with seed goal)
        similar_result = calculate_goal_drift(
            current_output="CLI task management tool with CRUD operations for tasks",
            seed=sample_seed,
        )

        # Unrelated output
        unrelated_result = calculate_goal_drift(
            current_output="Web scraping bot for news aggregation",
            seed=sample_seed,
        )

        # Similar should have less drift than unrelated
        assert similar_result < unrelated_result
        assert 0.0 <= similar_result < 1.0

    def test_goal_drift_unrelated_output_is_high(self, sample_seed: Seed) -> None:
        """Unrelated output produces high drift."""
        from ouroboros.observability.drift import calculate_goal_drift

        result = calculate_goal_drift(
            current_output="Web scraping bot for news aggregation",
            seed=sample_seed,
        )

        assert result > 0.5

    def test_goal_drift_returns_float_between_0_and_1(self, sample_seed: Seed) -> None:
        """Goal drift always returns value between 0.0 and 1.0."""
        from ouroboros.observability.drift import calculate_goal_drift

        result = calculate_goal_drift(
            current_output="Any random output",
            seed=sample_seed,
        )

        assert 0.0 <= result <= 1.0


# =============================================================================
# AC2: Constraint Drift Calculation Tests
# =============================================================================


class TestConstraintDriftCalculation:
    """Tests for calculate_constraint_drift() (AC2)."""

    def test_constraint_drift_no_violations_is_zero(self, sample_seed: Seed) -> None:
        """No constraint violations produces zero drift."""
        from ouroboros.observability.drift import calculate_constraint_drift

        result = calculate_constraint_drift(
            constraint_violations=[],
            seed=sample_seed,
        )

        assert result == 0.0

    def test_constraint_drift_one_violation(self, sample_seed: Seed) -> None:
        """One constraint violation produces 0.1 drift."""
        from ouroboros.observability.drift import calculate_constraint_drift

        result = calculate_constraint_drift(
            constraint_violations=["Uses external database"],
            seed=sample_seed,
        )

        assert result == pytest.approx(0.1, abs=0.01)

    def test_constraint_drift_multiple_violations(self, sample_seed: Seed) -> None:
        """Multiple violations produce cumulative drift (capped at 1.0)."""
        from ouroboros.observability.drift import calculate_constraint_drift

        result = calculate_constraint_drift(
            constraint_violations=[
                "Uses external database",
                "Response time is 500ms",
                "Uses Python 3.10",
            ],
            seed=sample_seed,
        )

        assert result == pytest.approx(0.3, abs=0.01)

    def test_constraint_drift_capped_at_1(self, sample_seed: Seed) -> None:
        """Drift is capped at 1.0 even with many violations."""
        from ouroboros.observability.drift import calculate_constraint_drift

        result = calculate_constraint_drift(
            constraint_violations=[f"violation_{i}" for i in range(15)],
            seed=sample_seed,
        )

        assert result == 1.0


# =============================================================================
# AC3: Ontology Drift Calculation Tests
# =============================================================================


class TestOntologyDriftCalculation:
    """Tests for calculate_ontology_drift() (AC3)."""

    def test_ontology_drift_identical_concepts_is_zero(self, sample_seed: Seed) -> None:
        """Identical ontology concepts produce zero drift."""
        from ouroboros.observability.drift import calculate_ontology_drift

        result = calculate_ontology_drift(
            current_concepts=["tasks", "task_id", "status"],
            seed=sample_seed,
        )

        assert result == pytest.approx(0.0, abs=0.1)

    def test_ontology_drift_partial_overlap(self, sample_seed: Seed) -> None:
        """Partial concept overlap produces moderate drift."""
        from ouroboros.observability.drift import calculate_ontology_drift

        result = calculate_ontology_drift(
            current_concepts=["tasks", "task_id", "priority", "deadline"],
            seed=sample_seed,
        )

        # Some overlap, some new concepts
        assert 0.0 < result < 0.7

    def test_ontology_drift_no_overlap_is_high(self, sample_seed: Seed) -> None:
        """Completely different concepts produce high drift."""
        from ouroboros.observability.drift import calculate_ontology_drift

        result = calculate_ontology_drift(
            current_concepts=["users", "authentication", "sessions", "tokens"],
            seed=sample_seed,
        )

        assert result > 0.7

    def test_ontology_drift_returns_float_between_0_and_1(
        self, sample_seed: Seed
    ) -> None:
        """Ontology drift always returns value between 0.0 and 1.0."""
        from ouroboros.observability.drift import calculate_ontology_drift

        result = calculate_ontology_drift(
            current_concepts=["random", "concepts"],
            seed=sample_seed,
        )

        assert 0.0 <= result <= 1.0


# =============================================================================
# AC4: Combined Drift Weighted Formula Tests
# =============================================================================


class TestCombinedDriftCalculation:
    """Tests for combined drift using weighted formula (AC4)."""

    def test_combined_drift_weights(self, sample_seed: Seed) -> None:
        """Combined drift uses correct weights: goal=0.5, constraint=0.3, ontology=0.2."""
        from ouroboros.observability.drift import DriftMetrics

        # Known component values
        metrics = DriftMetrics(
            goal_drift=0.4,
            constraint_drift=0.2,
            ontology_drift=0.1,
        )

        # Expected: (0.4 * 0.5) + (0.2 * 0.3) + (0.1 * 0.2) = 0.2 + 0.06 + 0.02 = 0.28
        assert metrics.combined_drift == pytest.approx(0.28, abs=0.001)

    def test_combined_drift_all_zero(self) -> None:
        """All zero components produce zero combined drift."""
        from ouroboros.observability.drift import DriftMetrics

        metrics = DriftMetrics(
            goal_drift=0.0,
            constraint_drift=0.0,
            ontology_drift=0.0,
        )

        assert metrics.combined_drift == 0.0

    def test_combined_drift_all_max(self) -> None:
        """All max components produce max combined drift (1.0)."""
        from ouroboros.observability.drift import DriftMetrics

        metrics = DriftMetrics(
            goal_drift=1.0,
            constraint_drift=1.0,
            ontology_drift=1.0,
        )

        assert metrics.combined_drift == 1.0


# =============================================================================
# AC5: Threshold Checking Tests
# =============================================================================


class TestDriftThresholdChecking:
    """Tests for drift threshold checking (AC5)."""

    def test_drift_below_threshold_is_acceptable(self) -> None:
        """Drift ≤0.3 is acceptable (NFR5)."""
        from ouroboros.observability.drift import DriftMetrics

        metrics = DriftMetrics(
            goal_drift=0.2,
            constraint_drift=0.2,
            ontology_drift=0.2,
        )

        # Combined: (0.2 * 0.5) + (0.2 * 0.3) + (0.2 * 0.2) = 0.2
        assert metrics.is_acceptable is True
        assert metrics.combined_drift <= 0.3

    def test_drift_at_threshold_is_acceptable(self) -> None:
        """Drift exactly at 0.3 is still acceptable."""
        from ouroboros.observability.drift import DriftMetrics

        metrics = DriftMetrics(
            goal_drift=0.3,
            constraint_drift=0.3,
            ontology_drift=0.3,
        )

        # Combined: 0.3
        assert metrics.is_acceptable is True

    def test_drift_above_threshold_is_not_acceptable(self) -> None:
        """Drift >0.3 is not acceptable."""
        from ouroboros.observability.drift import DriftMetrics

        metrics = DriftMetrics(
            goal_drift=0.5,
            constraint_drift=0.4,
            ontology_drift=0.3,
        )

        # Combined: (0.5 * 0.5) + (0.4 * 0.3) + (0.3 * 0.2) = 0.25 + 0.12 + 0.06 = 0.43
        assert metrics.is_acceptable is False
        assert metrics.combined_drift > 0.3


# =============================================================================
# AC6 & AC7: Drift Measurement & Event Storage Tests
# =============================================================================


class TestDriftMeasurement:
    """Tests for drift measurement and event emission (AC6, AC7)."""

    def test_measure_drift_returns_metrics(
        self, sample_seed: Seed, current_state_aligned: dict
    ) -> None:
        """measure_drift returns DriftMetrics with all components."""
        from ouroboros.observability.drift import DriftMeasurement, DriftMetrics

        measurement = DriftMeasurement()
        result = measurement.measure(
            current_output=current_state_aligned["output"],
            constraint_violations=current_state_aligned["constraint_violations"],
            current_concepts=current_state_aligned["ontology_concepts"],
            seed=sample_seed,
        )

        assert isinstance(result, DriftMetrics)
        assert 0.0 <= result.goal_drift <= 1.0
        assert 0.0 <= result.constraint_drift <= 1.0
        assert 0.0 <= result.ontology_drift <= 1.0
        assert 0.0 <= result.combined_drift <= 1.0

    def test_measure_drift_aligned_state_produces_low_drift(
        self, sample_seed: Seed, current_state_aligned: dict
    ) -> None:
        """Well-aligned state produces lower drift than drifted state."""
        from ouroboros.observability.drift import DriftMeasurement

        measurement = DriftMeasurement()
        aligned_result = measurement.measure(
            current_output=current_state_aligned["output"],
            constraint_violations=current_state_aligned["constraint_violations"],
            current_concepts=current_state_aligned["ontology_concepts"],
            seed=sample_seed,
        )

        # Compare with drifted state
        drifted_result = measurement.measure(
            current_output="Web-based calendar application with scheduling",
            constraint_violations=["Uses external database"],
            current_concepts=["appointments", "calendar"],
            seed=sample_seed,
        )

        # Aligned state should have lower combined drift
        assert aligned_result.combined_drift < drifted_result.combined_drift
        # Aligned state should be within acceptable range
        assert aligned_result.combined_drift <= 0.5

    def test_measure_drift_drifted_state_produces_high_drift(
        self, sample_seed: Seed, current_state_drifted: dict
    ) -> None:
        """Drifted state produces high combined drift."""
        from ouroboros.observability.drift import DriftMeasurement

        measurement = DriftMeasurement()
        result = measurement.measure(
            current_output=current_state_drifted["output"],
            constraint_violations=current_state_drifted["constraint_violations"],
            current_concepts=current_state_drifted["ontology_concepts"],
            seed=sample_seed,
        )

        assert result.combined_drift > 0.3


class TestDriftEvents:
    """Tests for drift event creation (AC7)."""

    def test_drift_measured_event_created(self, sample_seed: Seed) -> None:
        """DriftMeasuredEvent is created with all component scores."""
        from ouroboros.observability.drift import DriftMeasuredEvent, DriftMetrics

        metrics = DriftMetrics(
            goal_drift=0.2,
            constraint_drift=0.1,
            ontology_drift=0.05,
        )

        event = DriftMeasuredEvent(
            execution_id="exec-123",
            seed_id=sample_seed.metadata.seed_id,
            iteration=1,
            metrics=metrics,
        )

        assert event.type == "observability.drift.measured"
        assert event.aggregate_type == "execution"
        assert event.aggregate_id == "exec-123"
        assert event.data["goal_drift"] == 0.2
        assert event.data["constraint_drift"] == 0.1
        assert event.data["ontology_drift"] == 0.05
        assert event.data["combined_drift"] == metrics.combined_drift
        assert event.data["iteration"] == 1

    def test_drift_threshold_exceeded_event(self) -> None:
        """DriftThresholdExceededEvent created when threshold breached."""
        from ouroboros.observability.drift import (
            DriftMetrics,
            DriftThresholdExceededEvent,
        )

        metrics = DriftMetrics(
            goal_drift=0.5,
            constraint_drift=0.4,
            ontology_drift=0.3,
        )

        event = DriftThresholdExceededEvent(
            execution_id="exec-123",
            seed_id="seed-456",
            iteration=5,
            metrics=metrics,
            threshold=0.3,
        )

        assert event.type == "observability.drift.threshold_exceeded"
        assert event.data["combined_drift"] > 0.3
        assert event.data["threshold"] == 0.3


# =============================================================================
# Edge Cases and Error Handling
# =============================================================================


class TestDriftEdgeCases:
    """Tests for edge cases in drift calculations."""

    def test_empty_output_handling(self, sample_seed: Seed) -> None:
        """Empty output produces maximum goal drift."""
        from ouroboros.observability.drift import calculate_goal_drift

        result = calculate_goal_drift(
            current_output="",
            seed=sample_seed,
        )

        assert result == 1.0

    def test_empty_concepts_handling(self, sample_seed: Seed) -> None:
        """Empty concepts list produces maximum ontology drift."""
        from ouroboros.observability.drift import calculate_ontology_drift

        result = calculate_ontology_drift(
            current_concepts=[],
            seed=sample_seed,
        )

        assert result == 1.0

    def test_drift_metrics_immutable(self) -> None:
        """DriftMetrics is immutable (frozen dataclass)."""
        from ouroboros.observability.drift import DriftMetrics

        metrics = DriftMetrics(
            goal_drift=0.2,
            constraint_drift=0.1,
            ontology_drift=0.05,
        )

        with pytest.raises(AttributeError):
            metrics.goal_drift = 0.5  # type: ignore

    def test_drift_metrics_validation(self) -> None:
        """DriftMetrics validates value ranges."""
        from ouroboros.observability.drift import DriftMetrics

        with pytest.raises(ValueError, match="must be between 0.0 and 1.0"):
            DriftMetrics(
                goal_drift=1.5,  # Invalid
                constraint_drift=0.1,
                ontology_drift=0.05,
            )
