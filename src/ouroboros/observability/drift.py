"""Drift measurement engine for Ouroboros.

This module implements Story 6.1: Drift Measurement Engine.

Drift measures how far the current execution state has deviated from
the original Seed specification. Three components are tracked:
1. Goal drift: Deviation from the stated objective
2. Constraint drift: Constraint violations accumulated
3. Ontology drift: Evolution of the concept space

The combined drift uses weighted formula (PRD 13.1):
    combined = (goal * 0.5) + (constraint * 0.3) + (ontology * 0.2)

NFR5 requires combined drift ≤ 0.3 to be acceptable.

Usage:
    from ouroboros.observability.drift import (
        DriftMeasurement,
        DriftMetrics,
        DriftMeasuredEvent,
    )

    measurement = DriftMeasurement()
    metrics = measurement.measure(
        current_output="CLI task manager",
        constraint_violations=[],
        current_concepts=["tasks", "status"],
        seed=seed,
    )

    if not metrics.is_acceptable:
        # Combined drift > 0.3 - may need consensus
        event = DriftThresholdExceededEvent(...)
"""

from __future__ import annotations

from dataclasses import dataclass

from ouroboros.core.seed import Seed
from ouroboros.events.base import BaseEvent
from ouroboros.observability.logging import get_logger

log = get_logger(__name__)

# =============================================================================
# Constants
# =============================================================================

# Weights from PRD 13.1
GOAL_DRIFT_WEIGHT = 0.5
CONSTRAINT_DRIFT_WEIGHT = 0.3
ONTOLOGY_DRIFT_WEIGHT = 0.2

# NFR5: Acceptable drift threshold
DRIFT_THRESHOLD = 0.3

# Constraint violation penalty per violation
CONSTRAINT_VIOLATION_PENALTY = 0.1


# =============================================================================
# Data Models
# =============================================================================


@dataclass(frozen=True, slots=True)
class DriftMetrics:
    """Immutable drift measurement result.

    Contains individual drift components and computed combined drift.
    Uses weighted formula from PRD 13.1.

    Attributes:
        goal_drift: Deviation from seed goal (0.0-1.0)
        constraint_drift: Constraint violations score (0.0-1.0)
        ontology_drift: Concept space deviation (0.0-1.0)
    """

    goal_drift: float
    constraint_drift: float
    ontology_drift: float

    def __post_init__(self) -> None:
        """Validate value ranges."""
        for attr in ("goal_drift", "constraint_drift", "ontology_drift"):
            value = getattr(self, attr)
            if not 0.0 <= value <= 1.0:
                msg = f"{attr} must be between 0.0 and 1.0, got {value}"
                raise ValueError(msg)

    @property
    def combined_drift(self) -> float:
        """Calculate combined drift using weighted formula.

        Formula from PRD 13.1:
            combined = (goal * 0.5) + (constraint * 0.3) + (ontology * 0.2)

        Returns:
            Combined drift score (0.0-1.0)
        """
        return (
            self.goal_drift * GOAL_DRIFT_WEIGHT
            + self.constraint_drift * CONSTRAINT_DRIFT_WEIGHT
            + self.ontology_drift * ONTOLOGY_DRIFT_WEIGHT
        )

    @property
    def is_acceptable(self) -> bool:
        """Check if drift is within acceptable threshold.

        NFR5 requires combined drift ≤ 0.3.

        Returns:
            True if combined drift ≤ 0.3
        """
        return self.combined_drift <= DRIFT_THRESHOLD


# =============================================================================
# Drift Calculation Functions
# =============================================================================


def calculate_goal_drift(current_output: str, seed: Seed) -> float:
    """Calculate goal drift using text similarity.

    Measures how far the current output has drifted from the seed goal.
    Uses Jaccard similarity of word sets as a simple baseline.

    Args:
        current_output: Current execution output text
        seed: Original seed specification

    Returns:
        Goal drift score (0.0 = identical, 1.0 = completely different)
    """
    if not current_output or not current_output.strip():
        return 1.0

    # Tokenize and normalize
    goal_words = _tokenize(seed.goal)
    output_words = _tokenize(current_output)

    if not goal_words:
        return 1.0

    # Jaccard similarity: |intersection| / |union|
    intersection = goal_words & output_words
    union = goal_words | output_words

    if not union:
        return 1.0

    similarity = len(intersection) / len(union)
    return 1.0 - similarity


def calculate_constraint_drift(constraint_violations: list[str], seed: Seed) -> float:  # noqa: ARG001
    """Calculate constraint drift based on violations.

    Each violation adds 0.1 to drift, capped at 1.0.
    Formula from PRD 13.1: min(violations * 0.1, 1.0)

    Args:
        constraint_violations: List of violation descriptions
        seed: Original seed specification (for context)

    Returns:
        Constraint drift score (0.0-1.0)
    """
    if not constraint_violations:
        return 0.0

    drift = len(constraint_violations) * CONSTRAINT_VIOLATION_PENALTY
    return min(drift, 1.0)


def calculate_ontology_drift(current_concepts: list[str], seed: Seed) -> float:
    """Calculate ontology drift based on concept evolution.

    Measures how much the current concept space has drifted from
    the original ontology schema defined in the seed.

    Uses Jaccard distance between concept sets.

    Args:
        current_concepts: Current list of concepts in use
        seed: Original seed with ontology schema

    Returns:
        Ontology drift score (0.0-1.0)
    """
    if not current_concepts:
        return 1.0

    # Extract seed ontology field names
    seed_concepts = {field.name.lower() for field in seed.ontology_schema.fields}

    if not seed_concepts:
        # No seed ontology defined - any concepts are acceptable
        return 0.0

    current_set = {c.lower() for c in current_concepts}

    # Jaccard distance: 1 - (|intersection| / |union|)
    intersection = seed_concepts & current_set
    union = seed_concepts | current_set

    if not union:
        return 0.0

    similarity = len(intersection) / len(union)
    return 1.0 - similarity


def _tokenize(text: str) -> set[str]:
    """Tokenize text into normalized word set.

    Args:
        text: Input text

    Returns:
        Set of lowercase words (alphanumeric only)
    """
    # Simple word tokenization
    words = text.lower().split()
    # Keep only alphanumeric characters
    return {"".join(c for c in word if c.isalnum()) for word in words if word}


# =============================================================================
# Drift Measurement Service
# =============================================================================


class DriftMeasurement:
    """Service for measuring drift from seed.

    Combines all three drift components into a unified measurement.
    Stateless - all state passed via parameters.

    Usage:
        measurement = DriftMeasurement()
        metrics = measurement.measure(
            current_output="...",
            constraint_violations=[...],
            current_concepts=[...],
            seed=seed,
        )
    """

    def measure(
        self,
        current_output: str,
        constraint_violations: list[str],
        current_concepts: list[str],
        seed: Seed,
    ) -> DriftMetrics:
        """Measure all drift components.

        Args:
            current_output: Current execution output
            constraint_violations: List of constraint violations
            current_concepts: Current ontology concepts
            seed: Original seed specification

        Returns:
            DriftMetrics with all components
        """
        goal_drift = calculate_goal_drift(current_output, seed)
        constraint_drift = calculate_constraint_drift(constraint_violations, seed)
        ontology_drift = calculate_ontology_drift(current_concepts, seed)

        metrics = DriftMetrics(
            goal_drift=goal_drift,
            constraint_drift=constraint_drift,
            ontology_drift=ontology_drift,
        )

        log.debug(
            "observability.drift.measured",
            seed_id=seed.metadata.seed_id,
            goal_drift=goal_drift,
            constraint_drift=constraint_drift,
            ontology_drift=ontology_drift,
            combined_drift=metrics.combined_drift,
            is_acceptable=metrics.is_acceptable,
        )

        return metrics


# =============================================================================
# Event Definitions
# =============================================================================


class DriftMeasuredEvent(BaseEvent):
    """Event emitted when drift is measured after an iteration.

    Stores all drift components for audit trail and analysis.
    """

    def __init__(
        self,
        execution_id: str,
        seed_id: str,
        iteration: int,
        metrics: DriftMetrics,
    ) -> None:
        """Create DriftMeasuredEvent.

        Args:
            execution_id: Unique execution identifier
            seed_id: Seed identifier being measured against
            iteration: Current iteration number
            metrics: Drift measurement results
        """
        super().__init__(
            type="observability.drift.measured",
            aggregate_type="execution",
            aggregate_id=execution_id,
            data={
                "seed_id": seed_id,
                "iteration": iteration,
                "goal_drift": metrics.goal_drift,
                "constraint_drift": metrics.constraint_drift,
                "ontology_drift": metrics.ontology_drift,
                "combined_drift": metrics.combined_drift,
                "is_acceptable": metrics.is_acceptable,
            },
        )


class DriftThresholdExceededEvent(BaseEvent):
    """Event emitted when drift exceeds acceptable threshold.

    Indicates combined drift > 0.3 (NFR5), may trigger consensus.
    """

    def __init__(
        self,
        execution_id: str,
        seed_id: str,
        iteration: int,
        metrics: DriftMetrics,
        threshold: float = DRIFT_THRESHOLD,
    ) -> None:
        """Create DriftThresholdExceededEvent.

        Args:
            execution_id: Unique execution identifier
            seed_id: Seed identifier being measured against
            iteration: Current iteration number
            metrics: Drift measurement results
            threshold: The threshold that was exceeded (default: 0.3)
        """
        super().__init__(
            type="observability.drift.threshold_exceeded",
            aggregate_type="execution",
            aggregate_id=execution_id,
            data={
                "seed_id": seed_id,
                "iteration": iteration,
                "goal_drift": metrics.goal_drift,
                "constraint_drift": metrics.constraint_drift,
                "ontology_drift": metrics.ontology_drift,
                "combined_drift": metrics.combined_drift,
                "threshold": threshold,
                "exceeded_by": metrics.combined_drift - threshold,
            },
        )
