"""Automatic retrospective for Ouroboros.

This module implements Story 6.2: Automatic Retrospective.

Retrospectives run periodically (every 3 iterations by default) to:
1. Compare current state to original Seed
2. Analyze drift components
3. Generate course correction recommendations
4. Notify humans if drift is high

The retrospective is part of Phase 3 resilience, preventing accumulated drift.

Usage:
    from ouroboros.observability.retrospective import (
        RetrospectiveAnalyzer,
        should_trigger_retrospective,
    )

    if should_trigger_retrospective(iteration=3):
        analyzer = RetrospectiveAnalyzer()
        result = analyzer.analyze(
            seed=seed,
            current_output="...",
            constraint_violations=[],
            current_concepts=[],
            iteration=3,
            execution_id="exec-123",
        )
        if result.requires_human_attention:
            # Notify human
            pass
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ouroboros.core.seed import Seed
from ouroboros.events.base import BaseEvent
from ouroboros.observability.drift import (
    DRIFT_THRESHOLD,
    DriftMeasurement,
    DriftMetrics,
)
from ouroboros.observability.logging import get_logger

log = get_logger(__name__)

# =============================================================================
# Constants
# =============================================================================

# Default retrospective interval (every N iterations)
DEFAULT_RETROSPECTIVE_INTERVAL = 3

# Threshold for requiring human attention (default same as drift threshold)
DEFAULT_NOTIFICATION_THRESHOLD = DRIFT_THRESHOLD


# =============================================================================
# Data Models
# =============================================================================


@dataclass(frozen=True, slots=True)
class RetrospectiveResult:
    """Immutable result of a retrospective analysis.

    Attributes:
        execution_id: Execution identifier
        iteration: Iteration number when retrospective was run
        drift_metrics: Measured drift from seed
        recommendations: List of course correction recommendations
        needs_correction: Whether correction is recommended
        notification_threshold: Threshold for human notification
    """

    execution_id: str
    iteration: int
    drift_metrics: DriftMetrics
    recommendations: list[str] = field(default_factory=list)
    needs_correction: bool = False
    notification_threshold: float = DEFAULT_NOTIFICATION_THRESHOLD

    @property
    def requires_human_attention(self) -> bool:
        """Check if human attention is required.

        Returns True if combined drift exceeds notification threshold.

        Returns:
            True if human should be notified
        """
        return self.drift_metrics.combined_drift > self.notification_threshold


# =============================================================================
# Trigger Logic
# =============================================================================


def should_trigger_retrospective(
    iteration: int,
    interval: int = DEFAULT_RETROSPECTIVE_INTERVAL,
) -> bool:
    """Check if retrospective should run at this iteration.

    Retrospectives trigger at multiples of the interval (default: 3).
    First retrospective runs after iteration 3 completes.

    Args:
        iteration: Current iteration number (1-indexed)
        interval: How often to run retrospective (default: 3)

    Returns:
        True if retrospective should run
    """
    if iteration <= 0:
        return False
    return iteration % interval == 0


# =============================================================================
# Retrospective Analyzer
# =============================================================================


class RetrospectiveAnalyzer:
    """Analyzer for periodic retrospectives.

    Compares current state to original Seed and generates
    course correction recommendations if needed.

    Stateless - all state passed via parameters.
    """

    def __init__(
        self,
        notification_threshold: float = DEFAULT_NOTIFICATION_THRESHOLD,
    ) -> None:
        """Initialize analyzer.

        Args:
            notification_threshold: Drift threshold for human notification
        """
        self._notification_threshold = notification_threshold
        self._drift_measurement = DriftMeasurement()

    def analyze(
        self,
        seed: Seed,
        current_output: str,
        constraint_violations: list[str],
        current_concepts: list[str],
        iteration: int,
        execution_id: str,
    ) -> RetrospectiveResult:
        """Analyze current state against original seed.

        Measures drift and generates recommendations for correction.

        Args:
            seed: Original seed specification
            current_output: Current execution output
            constraint_violations: List of constraint violations
            current_concepts: Current ontology concepts
            iteration: Current iteration number
            execution_id: Execution identifier

        Returns:
            RetrospectiveResult with analysis and recommendations
        """
        # Measure drift from seed
        drift_metrics = self._drift_measurement.measure(
            current_output=current_output,
            constraint_violations=constraint_violations,
            current_concepts=current_concepts,
            seed=seed,
        )

        # Generate recommendations based on drift
        recommendations = self._generate_recommendations(
            drift_metrics=drift_metrics,
            seed=seed,
            constraint_violations=constraint_violations,
        )

        # Determine if correction is needed
        needs_correction = (
            not drift_metrics.is_acceptable
            or len(constraint_violations) > 0
            or len(recommendations) > 0
        )

        return RetrospectiveResult(
            execution_id=execution_id,
            iteration=iteration,
            drift_metrics=drift_metrics,
            recommendations=recommendations,
            needs_correction=needs_correction,
            notification_threshold=self._notification_threshold,
        )

    def _generate_recommendations(
        self,
        drift_metrics: DriftMetrics,
        seed: Seed,
        constraint_violations: list[str],
    ) -> list[str]:
        """Generate course correction recommendations.

        Args:
            drift_metrics: Measured drift metrics
            seed: Original seed for reference
            constraint_violations: Current constraint violations

        Returns:
            List of recommendation strings
        """
        recommendations: list[str] = []

        # High goal drift
        if drift_metrics.goal_drift > DRIFT_THRESHOLD:
            # Truncate goal at word boundary
            goal_preview = seed.goal[:100]
            if len(seed.goal) > 100:
                last_space = goal_preview.rfind(" ")
                if last_space > 50:
                    goal_preview = goal_preview[:last_space]
                goal_preview += "..."
            recommendations.append(
                f"High goal drift detected ({drift_metrics.goal_drift:.2f}). "
                f"Refocus on original goal: '{goal_preview}'"
            )

        # Constraint violations
        if constraint_violations:
            recommendations.append(
                f"Constraint violations detected ({len(constraint_violations)}). "
                "Review and address violations to maintain compliance."
            )

        # Ontology drift
        if drift_metrics.ontology_drift > DRIFT_THRESHOLD:
            recommendations.append(
                f"Ontology drift detected ({drift_metrics.ontology_drift:.2f}). "
                "Concepts have evolved significantly from original schema. "
                "Consider realigning with seed ontology."
            )

        # Combined drift exceeds threshold
        if drift_metrics.combined_drift > DRIFT_THRESHOLD:
            recommendations.append(
                f"Combined drift ({drift_metrics.combined_drift:.2f}) exceeds "
                f"threshold ({DRIFT_THRESHOLD}). Major course correction recommended."
            )

        return recommendations


# =============================================================================
# Event Definitions
# =============================================================================


class RetrospectiveCompletedEvent(BaseEvent):
    """Event emitted when a retrospective analysis completes.

    Logs the retrospective results for audit trail.
    """

    def __init__(
        self,
        execution_id: str,
        seed_id: str,
        result: RetrospectiveResult,
    ) -> None:
        """Create RetrospectiveCompletedEvent.

        Args:
            execution_id: Unique execution identifier
            seed_id: Seed identifier being analyzed against
            result: Retrospective analysis result
        """
        super().__init__(
            type="observability.retrospective.completed",
            aggregate_type="execution",
            aggregate_id=execution_id,
            data={
                "seed_id": seed_id,
                "iteration": result.iteration,
                "goal_drift": result.drift_metrics.goal_drift,
                "constraint_drift": result.drift_metrics.constraint_drift,
                "ontology_drift": result.drift_metrics.ontology_drift,
                "combined_drift": result.drift_metrics.combined_drift,
                "needs_correction": result.needs_correction,
                "recommendations_count": len(result.recommendations),
                "requires_human_attention": result.requires_human_attention,
            },
        )


class HumanAttentionRequiredEvent(BaseEvent):
    """Event emitted when human attention is required.

    Indicates high drift that needs manual intervention.
    """

    def __init__(
        self,
        execution_id: str,
        seed_id: str,
        result: RetrospectiveResult,
        reason: str,
    ) -> None:
        """Create HumanAttentionRequiredEvent.

        Args:
            execution_id: Unique execution identifier
            seed_id: Seed identifier
            result: Retrospective analysis result
            reason: Why human attention is needed
        """
        super().__init__(
            type="observability.retrospective.human_attention_required",
            aggregate_type="execution",
            aggregate_id=execution_id,
            data={
                "seed_id": seed_id,
                "iteration": result.iteration,
                "goal_drift": result.drift_metrics.goal_drift,
                "constraint_drift": result.drift_metrics.constraint_drift,
                "ontology_drift": result.drift_metrics.ontology_drift,
                "combined_drift": result.drift_metrics.combined_drift,
                "reason": reason,
                "recommendations": result.recommendations,
            },
        )
