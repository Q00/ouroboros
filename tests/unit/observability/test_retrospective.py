"""Unit tests for automatic retrospective.

Tests for Story 6.2: Automatic Retrospective

Covers:
- AC1: Retrospective triggers every 3 iterations
- AC2: Current state compared to original Seed
- AC3: Drift components analyzed
- AC4: Course correction recommendations generated
- AC5: High drift triggers human notification
- AC6: Retrospective results logged
"""

from __future__ import annotations

import pytest

from ouroboros.core.seed import (
    EvaluationPrinciple,
    ExitCondition,
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
)
from ouroboros.observability.drift import DriftMetrics


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def sample_seed() -> Seed:
    """Create a sample seed for testing retrospectives."""
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
def low_drift_metrics() -> DriftMetrics:
    """Metrics showing low drift (acceptable)."""
    return DriftMetrics(
        goal_drift=0.1,
        constraint_drift=0.1,
        ontology_drift=0.1,
    )


@pytest.fixture
def high_drift_metrics() -> DriftMetrics:
    """Metrics showing high drift (exceeds threshold)."""
    return DriftMetrics(
        goal_drift=0.6,
        constraint_drift=0.4,
        ontology_drift=0.3,
    )


# =============================================================================
# AC1: Retrospective Trigger Tests
# =============================================================================


class TestRetrospectiveTrigger:
    """Tests for retrospective triggering every 3 iterations (AC1)."""

    def test_should_trigger_at_iteration_3(self) -> None:
        """Retrospective should trigger at iteration 3."""
        from ouroboros.observability.retrospective import should_trigger_retrospective

        assert should_trigger_retrospective(iteration=3) is True

    def test_should_trigger_at_iteration_6(self) -> None:
        """Retrospective should trigger at iteration 6."""
        from ouroboros.observability.retrospective import should_trigger_retrospective

        assert should_trigger_retrospective(iteration=6) is True

    def test_should_trigger_at_iteration_9(self) -> None:
        """Retrospective should trigger at iteration 9."""
        from ouroboros.observability.retrospective import should_trigger_retrospective

        assert should_trigger_retrospective(iteration=9) is True

    def test_should_not_trigger_at_iteration_1(self) -> None:
        """Retrospective should not trigger at iteration 1."""
        from ouroboros.observability.retrospective import should_trigger_retrospective

        assert should_trigger_retrospective(iteration=1) is False

    def test_should_not_trigger_at_iteration_2(self) -> None:
        """Retrospective should not trigger at iteration 2."""
        from ouroboros.observability.retrospective import should_trigger_retrospective

        assert should_trigger_retrospective(iteration=2) is False

    def test_should_not_trigger_at_iteration_4(self) -> None:
        """Retrospective should not trigger at iteration 4."""
        from ouroboros.observability.retrospective import should_trigger_retrospective

        assert should_trigger_retrospective(iteration=4) is False

    def test_should_not_trigger_at_iteration_0(self) -> None:
        """Retrospective should not trigger at iteration 0."""
        from ouroboros.observability.retrospective import should_trigger_retrospective

        assert should_trigger_retrospective(iteration=0) is False

    def test_custom_interval(self) -> None:
        """Retrospective can use custom interval."""
        from ouroboros.observability.retrospective import should_trigger_retrospective

        # With interval=5, should trigger at 5, 10, 15...
        assert should_trigger_retrospective(iteration=5, interval=5) is True
        assert should_trigger_retrospective(iteration=10, interval=5) is True
        assert should_trigger_retrospective(iteration=3, interval=5) is False


# =============================================================================
# AC2 & AC3: State Comparison and Drift Analysis Tests
# =============================================================================


class TestRetrospectiveAnalysis:
    """Tests for state comparison and drift analysis (AC2, AC3)."""

    def test_analyze_returns_retrospective_result(
        self, sample_seed: Seed, low_drift_metrics: DriftMetrics
    ) -> None:
        """Analyze should return RetrospectiveResult."""
        from ouroboros.observability.retrospective import (
            RetrospectiveAnalyzer,
            RetrospectiveResult,
        )

        analyzer = RetrospectiveAnalyzer()
        result = analyzer.analyze(
            seed=sample_seed,
            current_output="CLI task manager with CRUD",
            constraint_violations=[],
            current_concepts=["tasks", "task_id"],
            iteration=3,
            execution_id="exec-123",
        )

        assert isinstance(result, RetrospectiveResult)

    def test_analyze_includes_drift_metrics(
        self, sample_seed: Seed
    ) -> None:
        """Analysis result includes drift metrics."""
        from ouroboros.observability.retrospective import RetrospectiveAnalyzer

        analyzer = RetrospectiveAnalyzer()
        result = analyzer.analyze(
            seed=sample_seed,
            current_output="CLI task manager",
            constraint_violations=[],
            current_concepts=["tasks"],
            iteration=3,
            execution_id="exec-123",
        )

        assert result.drift_metrics is not None
        assert 0.0 <= result.drift_metrics.goal_drift <= 1.0
        assert 0.0 <= result.drift_metrics.constraint_drift <= 1.0
        assert 0.0 <= result.drift_metrics.ontology_drift <= 1.0

    def test_analyze_includes_iteration_number(
        self, sample_seed: Seed
    ) -> None:
        """Analysis result includes iteration number."""
        from ouroboros.observability.retrospective import RetrospectiveAnalyzer

        analyzer = RetrospectiveAnalyzer()
        result = analyzer.analyze(
            seed=sample_seed,
            current_output="CLI task manager",
            constraint_violations=[],
            current_concepts=["tasks"],
            iteration=6,
            execution_id="exec-123",
        )

        assert result.iteration == 6

    def test_analyze_compares_to_original_seed(
        self, sample_seed: Seed
    ) -> None:
        """Analysis compares current state to original seed."""
        from ouroboros.observability.retrospective import RetrospectiveAnalyzer

        analyzer = RetrospectiveAnalyzer()
        result = analyzer.analyze(
            seed=sample_seed,
            current_output="Completely different web scraping bot",
            constraint_violations=["Uses external database"],
            current_concepts=["users", "sessions"],
            iteration=3,
            execution_id="exec-123",
        )

        # High drift from original seed
        assert result.drift_metrics.combined_drift > 0.3


# =============================================================================
# AC4: Course Correction Recommendations Tests
# =============================================================================


class TestCourseCorrection:
    """Tests for course correction recommendations (AC4)."""

    def test_no_recommendations_for_low_drift(
        self, sample_seed: Seed
    ) -> None:
        """No correction needed when drift is low."""
        from ouroboros.observability.retrospective import RetrospectiveAnalyzer

        analyzer = RetrospectiveAnalyzer()
        result = analyzer.analyze(
            seed=sample_seed,
            current_output="CLI task management tool with CRUD operations",
            constraint_violations=[],
            current_concepts=["tasks", "task_id"],
            iteration=3,
            execution_id="exec-123",
        )

        # Low drift - recommendations should be empty or minimal
        assert result.needs_correction is False or len(result.recommendations) == 0

    def test_recommendations_for_high_goal_drift(
        self, sample_seed: Seed
    ) -> None:
        """Recommendations generated for high goal drift."""
        from ouroboros.observability.retrospective import RetrospectiveAnalyzer

        analyzer = RetrospectiveAnalyzer()
        result = analyzer.analyze(
            seed=sample_seed,
            current_output="Web scraping bot for news aggregation",
            constraint_violations=[],
            current_concepts=["tasks"],
            iteration=3,
            execution_id="exec-123",
        )

        assert result.needs_correction is True
        assert len(result.recommendations) > 0
        assert any("goal" in r.lower() for r in result.recommendations)

    def test_recommendations_for_constraint_violations(
        self, sample_seed: Seed
    ) -> None:
        """Recommendations generated for constraint violations."""
        from ouroboros.observability.retrospective import RetrospectiveAnalyzer

        analyzer = RetrospectiveAnalyzer()
        result = analyzer.analyze(
            seed=sample_seed,
            current_output="CLI task manager",
            constraint_violations=[
                "Uses external PostgreSQL database",
                "Response time is 500ms",
            ],
            current_concepts=["tasks"],
            iteration=3,
            execution_id="exec-123",
        )

        assert result.needs_correction is True
        assert len(result.recommendations) > 0
        assert any("constraint" in r.lower() for r in result.recommendations)

    def test_recommendations_for_ontology_drift(
        self, sample_seed: Seed
    ) -> None:
        """Recommendations generated for ontology drift."""
        from ouroboros.observability.retrospective import RetrospectiveAnalyzer

        analyzer = RetrospectiveAnalyzer()
        result = analyzer.analyze(
            seed=sample_seed,
            current_output="CLI task manager",
            constraint_violations=[],
            current_concepts=["appointments", "calendar", "schedule", "notifications"],
            iteration=3,
            execution_id="exec-123",
        )

        assert result.needs_correction is True
        assert any("ontology" in r.lower() or "concept" in r.lower() for r in result.recommendations)


# =============================================================================
# AC5: Human Notification Tests
# =============================================================================


class TestHumanNotification:
    """Tests for high drift human notification (AC5)."""

    def test_requires_human_attention_for_high_drift(
        self, sample_seed: Seed
    ) -> None:
        """High drift should require human attention."""
        from ouroboros.observability.retrospective import RetrospectiveAnalyzer

        analyzer = RetrospectiveAnalyzer()
        result = analyzer.analyze(
            seed=sample_seed,
            current_output="Completely unrelated output",
            constraint_violations=["violation1", "violation2", "violation3"],
            current_concepts=["unrelated", "concepts"],
            iteration=3,
            execution_id="exec-123",
        )

        assert result.requires_human_attention is True

    def test_no_human_attention_for_low_drift(
        self, sample_seed: Seed
    ) -> None:
        """Low drift should not require human attention."""
        from ouroboros.observability.retrospective import RetrospectiveAnalyzer

        analyzer = RetrospectiveAnalyzer()
        result = analyzer.analyze(
            seed=sample_seed,
            current_output="CLI task management tool with CRUD operations for tasks",
            constraint_violations=[],
            current_concepts=["tasks", "task_id"],
            iteration=3,
            execution_id="exec-123",
        )

        assert result.requires_human_attention is False

    def test_notification_threshold_configurable(self) -> None:
        """Notification threshold can be configured."""
        from ouroboros.observability.retrospective import RetrospectiveResult
        from ouroboros.observability.drift import DriftMetrics

        metrics = DriftMetrics(goal_drift=0.4, constraint_drift=0.3, ontology_drift=0.2)

        result = RetrospectiveResult(
            execution_id="exec-123",
            iteration=3,
            drift_metrics=metrics,
            recommendations=[],
            needs_correction=True,
            notification_threshold=0.5,  # Higher threshold
        )

        # Combined drift is 0.35, below 0.5 threshold
        assert result.requires_human_attention is False


# =============================================================================
# AC6: Event Logging Tests
# =============================================================================


class TestRetrospectiveEvents:
    """Tests for retrospective event logging (AC6)."""

    def test_retrospective_completed_event(self, sample_seed: Seed) -> None:
        """RetrospectiveCompletedEvent is created with analysis results."""
        from ouroboros.observability.retrospective import (
            RetrospectiveCompletedEvent,
            RetrospectiveResult,
        )
        from ouroboros.observability.drift import DriftMetrics

        metrics = DriftMetrics(goal_drift=0.2, constraint_drift=0.1, ontology_drift=0.1)
        result = RetrospectiveResult(
            execution_id="exec-123",
            iteration=3,
            drift_metrics=metrics,
            recommendations=["Refocus on original goal"],
            needs_correction=False,
        )

        event = RetrospectiveCompletedEvent(
            execution_id="exec-123",
            seed_id=sample_seed.metadata.seed_id,
            result=result,
        )

        assert event.type == "observability.retrospective.completed"
        assert event.aggregate_type == "execution"
        assert event.aggregate_id == "exec-123"
        assert event.data["iteration"] == 3
        assert event.data["combined_drift"] == metrics.combined_drift
        assert event.data["needs_correction"] is False

    def test_human_attention_required_event(self, sample_seed: Seed) -> None:
        """HumanAttentionRequiredEvent is created for high drift."""
        from ouroboros.observability.retrospective import (
            HumanAttentionRequiredEvent,
            RetrospectiveResult,
        )
        from ouroboros.observability.drift import DriftMetrics

        metrics = DriftMetrics(goal_drift=0.6, constraint_drift=0.5, ontology_drift=0.4)
        result = RetrospectiveResult(
            execution_id="exec-123",
            iteration=6,
            drift_metrics=metrics,
            recommendations=["Major course correction needed"],
            needs_correction=True,
        )

        event = HumanAttentionRequiredEvent(
            execution_id="exec-123",
            seed_id=sample_seed.metadata.seed_id,
            result=result,
            reason="Combined drift exceeds threshold",
        )

        assert event.type == "observability.retrospective.human_attention_required"
        assert event.data["iteration"] == 6
        assert event.data["combined_drift"] == metrics.combined_drift
        assert event.data["reason"] == "Combined drift exceeds threshold"


# =============================================================================
# Edge Cases and Error Handling
# =============================================================================


class TestRetrospectiveEdgeCases:
    """Tests for edge cases in retrospective."""

    def test_result_is_immutable(self) -> None:
        """RetrospectiveResult is immutable."""
        from ouroboros.observability.retrospective import RetrospectiveResult
        from ouroboros.observability.drift import DriftMetrics

        metrics = DriftMetrics(goal_drift=0.2, constraint_drift=0.1, ontology_drift=0.1)
        result = RetrospectiveResult(
            execution_id="exec-123",
            iteration=3,
            drift_metrics=metrics,
            recommendations=["Test"],
            needs_correction=False,
        )

        with pytest.raises(AttributeError):
            result.iteration = 5  # type: ignore

    def test_negative_iteration_handling(self) -> None:
        """Negative iteration should not trigger retrospective."""
        from ouroboros.observability.retrospective import should_trigger_retrospective

        assert should_trigger_retrospective(iteration=-1) is False
        assert should_trigger_retrospective(iteration=-3) is False

    def test_very_large_iteration(self) -> None:
        """Very large iteration should trigger at multiples of 3."""
        from ouroboros.observability.retrospective import should_trigger_retrospective

        assert should_trigger_retrospective(iteration=999) is True  # 999 % 3 == 0
        assert should_trigger_retrospective(iteration=1000) is False  # 1000 % 3 == 1
