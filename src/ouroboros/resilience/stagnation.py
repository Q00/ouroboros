"""Stagnation detection for Ouroboros execution cycles.

This module implements Story 4.1: Stagnation Detection (4 Patterns).

Detects 4 stagnation patterns:
1. Spinning: Same output repeated (e.g., same error 3+ times)
2. Oscillation: A→B→A→B alternating pattern
3. No Drift: No progress toward goal (drift score unchanging)
4. Diminishing Returns: Progress rate decreasing

Design:
- Stateless detector: All state passed via ExecutionHistory
- Hash-based comparison: Fast, O(1) for most patterns
- Event emission: Each pattern emits its own event type

Usage:
    from ouroboros.resilience.stagnation import (
        StagnationDetector,
        ExecutionHistory,
    )

    # Build history from execution
    history = ExecutionHistory(
        phase_outputs=["output1", "output2", "output1", "output2"],
        error_signatures=["error_A", "error_A"],
        drift_scores=[0.5, 0.5, 0.5],
        iteration=3,
    )

    # Detect patterns
    detector = StagnationDetector()
    result = detector.detect(history)

    for detection in result.value:
        if detection.detected:
            print(f"Stagnation: {detection.pattern.value}")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
from typing import Any

from ouroboros.core.types import Result
from ouroboros.events.base import BaseEvent
from ouroboros.observability.logging import get_logger

log = get_logger(__name__)


# =============================================================================
# Enums and Data Models
# =============================================================================


class StagnationPattern(str, Enum):
    """Four stagnation patterns detected in execution loops.

    Attributes:
        SPINNING: Identical outputs repeated (same error, same result)
        OSCILLATION: Alternating A→B→A→B pattern (flip-flopping)
        NO_DRIFT: Output generated but no progress toward goal
        DIMINISHING_RETURNS: Progress rate consistently decreasing
    """

    SPINNING = "spinning"
    OSCILLATION = "oscillation"
    NO_DRIFT = "no_drift"
    DIMINISHING_RETURNS = "diminishing_returns"

    @property
    def default_threshold(self) -> int:
        """Return default threshold for this pattern."""
        thresholds = {
            StagnationPattern.SPINNING: 3,
            StagnationPattern.OSCILLATION: 2,
            StagnationPattern.NO_DRIFT: 3,
            StagnationPattern.DIMINISHING_RETURNS: 3,
        }
        return thresholds[self]


@dataclass(frozen=True, slots=True)
class StagnationDetection:
    """Result of stagnation pattern detection.

    Attributes:
        pattern: The type of stagnation pattern checked.
        detected: Whether stagnation was detected.
        confidence: Confidence score (0.0-1.0).
        evidence: Pattern-specific evidence supporting detection.
    """

    pattern: StagnationPattern
    detected: bool
    confidence: float
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ExecutionHistory:
    """Historical execution data for stagnation detection.

    Contains recent outputs and metrics needed for pattern analysis.
    All collections are tuples to ensure immutability.

    Attributes:
        phase_outputs: Recent phase outputs (strings, for hash comparison).
        error_signatures: Recent error messages (for spinning detection).
        drift_scores: Recent drift measurements (for no_drift/diminishing).
        iteration: Current iteration number.
    """

    phase_outputs: tuple[str, ...] = field(default_factory=tuple)
    error_signatures: tuple[str, ...] = field(default_factory=tuple)
    drift_scores: tuple[float, ...] = field(default_factory=tuple)
    iteration: int = 0

    @classmethod
    def from_lists(
        cls,
        phase_outputs: list[str],
        error_signatures: list[str],
        drift_scores: list[float],
        iteration: int,
    ) -> ExecutionHistory:
        """Create ExecutionHistory from mutable lists.

        Args:
            phase_outputs: Recent phase output strings.
            error_signatures: Recent error messages.
            drift_scores: Recent drift score values.
            iteration: Current iteration number.

        Returns:
            Immutable ExecutionHistory instance.
        """
        return cls(
            phase_outputs=tuple(phase_outputs),
            error_signatures=tuple(error_signatures),
            drift_scores=tuple(drift_scores),
            iteration=iteration,
        )


# =============================================================================
# Stagnation Detector
# =============================================================================


class StagnationDetector:
    """Stateless detector for stagnation patterns.

    Analyzes ExecutionHistory to detect 4 stagnation patterns.
    All detection methods are pure functions operating on history.

    Attributes:
        spinning_threshold: Repetitions needed for spinning detection.
        oscillation_cycles: Complete A→B cycles needed.
        no_drift_epsilon: Maximum drift change to consider "no progress".
        no_drift_iterations: Iterations with no drift to trigger detection.
        diminishing_threshold: Improvement rate below this triggers detection.
    """

    # Default thresholds (can be overridden via __init__)
    DEFAULT_SPINNING_THRESHOLD = 3
    DEFAULT_OSCILLATION_CYCLES = 2
    DEFAULT_NO_DRIFT_EPSILON = 0.01
    DEFAULT_NO_DRIFT_ITERATIONS = 3
    DEFAULT_DIMINISHING_THRESHOLD = 0.01

    def __init__(
        self,
        *,
        spinning_threshold: int | None = None,
        oscillation_cycles: int | None = None,
        no_drift_epsilon: float | None = None,
        no_drift_iterations: int | None = None,
        diminishing_threshold: float | None = None,
    ) -> None:
        """Initialize StagnationDetector with configurable thresholds.

        Args:
            spinning_threshold: Repetitions for spinning (default: 3).
            oscillation_cycles: A→B cycles for oscillation (default: 2).
            no_drift_epsilon: Max drift delta for no_drift (default: 0.01).
            no_drift_iterations: Iterations for no_drift (default: 3).
            diminishing_threshold: Min improvement rate (default: 0.01).
        """
        self._spinning_threshold = spinning_threshold or self.DEFAULT_SPINNING_THRESHOLD
        self._oscillation_cycles = oscillation_cycles or self.DEFAULT_OSCILLATION_CYCLES
        self._no_drift_epsilon = no_drift_epsilon or self.DEFAULT_NO_DRIFT_EPSILON
        self._no_drift_iterations = no_drift_iterations or self.DEFAULT_NO_DRIFT_ITERATIONS
        self._diminishing_threshold = diminishing_threshold or self.DEFAULT_DIMINISHING_THRESHOLD

    def _compute_hash(self, text: str) -> str:
        """Compute SHA-256 hash of text for fast comparison.

        Args:
            text: Text to hash.

        Returns:
            First 16 characters of hex digest (enough for collision avoidance).
        """
        return hashlib.sha256(text.encode()).hexdigest()[:16]

    def detect(self, history: ExecutionHistory) -> Result[list[StagnationDetection], None]:
        """Detect all stagnation patterns from execution history.

        Runs all 4 pattern detectors and returns results.
        Each detector is independent and non-blocking.

        Args:
            history: ExecutionHistory with recent outputs and metrics.

        Returns:
            Result containing list of StagnationDetection for each pattern.
            Always returns Ok (detection never fails, just detects or not).
        """
        log.debug(
            "resilience.stagnation.detection_started",
            iteration=history.iteration,
            outputs_count=len(history.phase_outputs),
            errors_count=len(history.error_signatures),
            drifts_count=len(history.drift_scores),
        )

        detections = [
            self._detect_spinning(history),
            self._detect_oscillation(history),
            self._detect_no_drift(history),
            self._detect_diminishing_returns(history),
        ]

        detected_count = sum(1 for d in detections if d.detected)
        if detected_count > 0:
            log.info(
                "resilience.stagnation.patterns_detected",
                count=detected_count,
                patterns=[d.pattern.value for d in detections if d.detected],
            )

        return Result.ok(detections)

    def _detect_spinning(self, history: ExecutionHistory) -> StagnationDetection:
        """Detect spinning pattern: same output repeated N times.

        Checks both phase_outputs and error_signatures for repetition.

        Args:
            history: ExecutionHistory to analyze.

        Returns:
            StagnationDetection with detected=True if spinning found.
        """
        # Check phase outputs
        outputs = history.phase_outputs
        if len(outputs) >= self._spinning_threshold:
            recent = outputs[-self._spinning_threshold :]
            hashes = [self._compute_hash(o) for o in recent]

            if len(set(hashes)) == 1:
                return StagnationDetection(
                    pattern=StagnationPattern.SPINNING,
                    detected=True,
                    confidence=1.0,
                    evidence={
                        "repeated_output_sample": recent[-1][:200],
                        "repeat_count": len(recent),
                        "source": "phase_outputs",
                    },
                )

        # Check error signatures
        errors = history.error_signatures
        if len(errors) >= self._spinning_threshold:
            recent_errors = errors[-self._spinning_threshold :]
            error_hashes = [self._compute_hash(e) for e in recent_errors]

            if len(set(error_hashes)) == 1:
                return StagnationDetection(
                    pattern=StagnationPattern.SPINNING,
                    detected=True,
                    confidence=1.0,
                    evidence={
                        "repeated_error": recent_errors[-1][:200],
                        "repeat_count": len(recent_errors),
                        "source": "error_signatures",
                    },
                )

        return StagnationDetection(
            pattern=StagnationPattern.SPINNING,
            detected=False,
            confidence=0.0,
        )

    def _detect_oscillation(self, history: ExecutionHistory) -> StagnationDetection:
        """Detect oscillation pattern: A→B→A→B alternating states.

        Looks for alternating pattern where even indices match each other
        and odd indices match each other, but even ≠ odd.

        Args:
            history: ExecutionHistory to analyze.

        Returns:
            StagnationDetection with detected=True if oscillation found.
        """
        min_length = self._oscillation_cycles * 2
        outputs = history.phase_outputs

        if len(outputs) < min_length:
            return StagnationDetection(
                pattern=StagnationPattern.OSCILLATION,
                detected=False,
                confidence=0.0,
            )

        recent = outputs[-min_length:]
        hashes = [self._compute_hash(o) for o in recent]

        # Split into even and odd indices
        even_hashes = hashes[::2]  # [0, 2, 4, ...]
        odd_hashes = hashes[1::2]  # [1, 3, 5, ...]

        # Check: all evens same, all odds same, but even ≠ odd
        evens_same = len(set(even_hashes)) == 1
        odds_same = len(set(odd_hashes)) == 1
        different_states = even_hashes[0] != odd_hashes[0]

        if evens_same and odds_same and different_states:
            return StagnationDetection(
                pattern=StagnationPattern.OSCILLATION,
                detected=True,
                confidence=0.9,
                evidence={
                    "state_a_sample": recent[0][:100],
                    "state_b_sample": recent[1][:100],
                    "cycles_detected": self._oscillation_cycles,
                },
            )

        return StagnationDetection(
            pattern=StagnationPattern.OSCILLATION,
            detected=False,
            confidence=0.0,
        )

    def _detect_no_drift(self, history: ExecutionHistory) -> StagnationDetection:
        """Detect no drift pattern: drift score not improving.

        Checks if drift scores have changed by less than epsilon
        over the required number of iterations.

        Args:
            history: ExecutionHistory with drift_scores.

        Returns:
            StagnationDetection with detected=True if no drift found.
        """
        scores = history.drift_scores

        if len(scores) < self._no_drift_iterations:
            return StagnationDetection(
                pattern=StagnationPattern.NO_DRIFT,
                detected=False,
                confidence=0.0,
            )

        recent = scores[-self._no_drift_iterations :]

        # Calculate deltas between consecutive scores
        deltas = [abs(recent[i] - recent[i - 1]) for i in range(1, len(recent))]

        # Check if all deltas are below epsilon
        if all(delta < self._no_drift_epsilon for delta in deltas):
            avg_score = sum(recent) / len(recent)
            return StagnationDetection(
                pattern=StagnationPattern.NO_DRIFT,
                detected=True,
                confidence=1.0 - (sum(deltas) / len(deltas) / self._no_drift_epsilon),
                evidence={
                    "drift_scores": list(recent),
                    "deltas": deltas,
                    "epsilon_threshold": self._no_drift_epsilon,
                    "average_drift": avg_score,
                    "stagnant_iterations": len(recent),
                },
            )

        return StagnationDetection(
            pattern=StagnationPattern.NO_DRIFT,
            detected=False,
            confidence=0.0,
        )

    def _detect_diminishing_returns(self, history: ExecutionHistory) -> StagnationDetection:
        """Detect diminishing returns: improvement rate decreasing.

        Analyzes drift scores to check if improvements are getting smaller.

        Args:
            history: ExecutionHistory with drift_scores.

        Returns:
            StagnationDetection with detected=True if diminishing returns found.
        """
        scores = history.drift_scores

        if len(scores) < self._no_drift_iterations + 1:
            return StagnationDetection(
                pattern=StagnationPattern.DIMINISHING_RETURNS,
                detected=False,
                confidence=0.0,
            )

        recent = scores[-(self._no_drift_iterations + 1) :]

        # Calculate improvement rates (positive = improving toward goal)
        # Assuming lower drift = better (closer to goal)
        improvements = [recent[i - 1] - recent[i] for i in range(1, len(recent))]

        # Check if all improvements are below threshold
        if all(imp < self._diminishing_threshold for imp in improvements):
            # Additional check: are improvements decreasing?
            is_decreasing = all(
                improvements[i] >= improvements[i + 1] for i in range(len(improvements) - 1)
            )

            confidence = 0.8 if is_decreasing else 0.6

            return StagnationDetection(
                pattern=StagnationPattern.DIMINISHING_RETURNS,
                detected=True,
                confidence=confidence,
                evidence={
                    "improvement_rates": improvements,
                    "threshold": self._diminishing_threshold,
                    "monotonically_decreasing": is_decreasing,
                },
            )

        return StagnationDetection(
            pattern=StagnationPattern.DIMINISHING_RETURNS,
            detected=False,
            confidence=0.0,
        )


# =============================================================================
# Event Classes
# =============================================================================


class SpinningDetectedEvent(BaseEvent):
    """Event emitted when spinning pattern detected.

    Spinning occurs when the same output is repeated multiple times,
    indicating the system is stuck in a loop.
    """

    def __init__(
        self,
        execution_id: str,
        repeated_output_sample: str,
        repeat_count: int,
        source: str,
        *,
        seed_id: str | None = None,
        confidence: float = 1.0,
        iteration: int = 0,
    ) -> None:
        """Create SpinningDetectedEvent.

        Args:
            execution_id: Execution identifier.
            repeated_output_sample: Sample of repeated output (truncated).
            repeat_count: Number of repetitions detected.
            source: Where repetition was found ("phase_outputs" or "error_signatures").
            seed_id: Optional seed identifier.
            confidence: Confidence score (0.0-1.0).
            iteration: Current iteration number.
        """
        super().__init__(
            type="resilience.stagnation.spinning.detected",
            aggregate_type="execution",
            aggregate_id=execution_id,
            data={
                "pattern": StagnationPattern.SPINNING.value,
                "repeated_output_sample": repeated_output_sample[:200],
                "repeat_count": repeat_count,
                "source": source,
                "seed_id": seed_id,
                "confidence": confidence,
                "iteration": iteration,
            },
        )


class OscillationDetectedEvent(BaseEvent):
    """Event emitted when oscillation pattern detected.

    Oscillation occurs when execution alternates between two states
    in an A→B→A→B pattern.
    """

    def __init__(
        self,
        execution_id: str,
        state_a_sample: str,
        state_b_sample: str,
        cycles_detected: int,
        *,
        seed_id: str | None = None,
        confidence: float = 0.9,
        iteration: int = 0,
    ) -> None:
        """Create OscillationDetectedEvent.

        Args:
            execution_id: Execution identifier.
            state_a_sample: Sample of state A (truncated).
            state_b_sample: Sample of state B (truncated).
            cycles_detected: Number of A→B cycles detected.
            seed_id: Optional seed identifier.
            confidence: Confidence score (0.0-1.0).
            iteration: Current iteration number.
        """
        super().__init__(
            type="resilience.stagnation.oscillation.detected",
            aggregate_type="execution",
            aggregate_id=execution_id,
            data={
                "pattern": StagnationPattern.OSCILLATION.value,
                "state_a_sample": state_a_sample[:100],
                "state_b_sample": state_b_sample[:100],
                "cycles_detected": cycles_detected,
                "seed_id": seed_id,
                "confidence": confidence,
                "iteration": iteration,
            },
        )


class NoDriftDetectedEvent(BaseEvent):
    """Event emitted when no drift pattern detected.

    No drift occurs when the drift score (distance from goal) is not
    improving over multiple iterations.
    """

    def __init__(
        self,
        execution_id: str,
        drift_scores: list[float],
        average_drift: float,
        stagnant_iterations: int,
        *,
        seed_id: str | None = None,
        confidence: float = 1.0,
        iteration: int = 0,
    ) -> None:
        """Create NoDriftDetectedEvent.

        Args:
            execution_id: Execution identifier.
            drift_scores: Recent drift score values.
            average_drift: Average drift score over period.
            stagnant_iterations: Number of iterations with no progress.
            seed_id: Optional seed identifier.
            confidence: Confidence score (0.0-1.0).
            iteration: Current iteration number.
        """
        super().__init__(
            type="resilience.stagnation.no_drift.detected",
            aggregate_type="execution",
            aggregate_id=execution_id,
            data={
                "pattern": StagnationPattern.NO_DRIFT.value,
                "drift_scores": drift_scores,
                "average_drift": average_drift,
                "stagnant_iterations": stagnant_iterations,
                "seed_id": seed_id,
                "confidence": confidence,
                "iteration": iteration,
            },
        )


class DiminishingReturnsDetectedEvent(BaseEvent):
    """Event emitted when diminishing returns pattern detected.

    Diminishing returns occurs when the improvement rate is consistently
    decreasing, indicating progress is slowing.
    """

    def __init__(
        self,
        execution_id: str,
        improvement_rates: list[float],
        monotonically_decreasing: bool,
        *,
        seed_id: str | None = None,
        confidence: float = 0.8,
        iteration: int = 0,
    ) -> None:
        """Create DiminishingReturnsDetectedEvent.

        Args:
            execution_id: Execution identifier.
            improvement_rates: Recent improvement rate values.
            monotonically_decreasing: Whether rates are strictly decreasing.
            seed_id: Optional seed identifier.
            confidence: Confidence score (0.0-1.0).
            iteration: Current iteration number.
        """
        super().__init__(
            type="resilience.stagnation.diminishing_returns.detected",
            aggregate_type="execution",
            aggregate_id=execution_id,
            data={
                "pattern": StagnationPattern.DIMINISHING_RETURNS.value,
                "improvement_rates": improvement_rates,
                "monotonically_decreasing": monotonically_decreasing,
                "seed_id": seed_id,
                "confidence": confidence,
                "iteration": iteration,
            },
        )


# =============================================================================
# Helper Functions
# =============================================================================


def create_stagnation_event(
    detection: StagnationDetection,
    execution_id: str,
    *,
    seed_id: str | None = None,
    iteration: int = 0,
) -> BaseEvent:
    """Create appropriate event for a stagnation detection.

    Factory function that creates the correct event type based on
    the detected pattern.

    Args:
        detection: StagnationDetection result.
        execution_id: Execution identifier.
        seed_id: Optional seed identifier.
        iteration: Current iteration number.

    Returns:
        Appropriate event type for the detected pattern.

    Raises:
        ValueError: If detection.detected is False.
    """
    if not detection.detected:
        raise ValueError("Cannot create event for non-detected stagnation")

    evidence = detection.evidence

    match detection.pattern:
        case StagnationPattern.SPINNING:
            return SpinningDetectedEvent(
                execution_id=execution_id,
                repeated_output_sample=str(
                    evidence.get("repeated_output_sample", evidence.get("repeated_error", ""))
                ),
                repeat_count=int(evidence.get("repeat_count", 0)),
                source=str(evidence.get("source", "unknown")),
                seed_id=seed_id,
                confidence=detection.confidence,
                iteration=iteration,
            )

        case StagnationPattern.OSCILLATION:
            return OscillationDetectedEvent(
                execution_id=execution_id,
                state_a_sample=str(evidence.get("state_a_sample", "")),
                state_b_sample=str(evidence.get("state_b_sample", "")),
                cycles_detected=int(evidence.get("cycles_detected", 0)),
                seed_id=seed_id,
                confidence=detection.confidence,
                iteration=iteration,
            )

        case StagnationPattern.NO_DRIFT:
            return NoDriftDetectedEvent(
                execution_id=execution_id,
                drift_scores=list(evidence.get("drift_scores", [])),
                average_drift=float(evidence.get("average_drift", 0.0)),
                stagnant_iterations=int(evidence.get("stagnant_iterations", 0)),
                seed_id=seed_id,
                confidence=detection.confidence,
                iteration=iteration,
            )

        case StagnationPattern.DIMINISHING_RETURNS:
            return DiminishingReturnsDetectedEvent(
                execution_id=execution_id,
                improvement_rates=list(evidence.get("improvement_rates", [])),
                monotonically_decreasing=bool(evidence.get("monotonically_decreasing", False)),
                seed_id=seed_id,
                confidence=detection.confidence,
                iteration=iteration,
            )

        case _:
            # Fallback - should never reach here
            return BaseEvent(
                type=f"resilience.stagnation.{detection.pattern.value}.detected",
                aggregate_type="execution",
                aggregate_id=execution_id,
                data={
                    "pattern": detection.pattern.value,
                    "evidence": evidence,
                    "confidence": detection.confidence,
                    "seed_id": seed_id,
                    "iteration": iteration,
                },
            )
