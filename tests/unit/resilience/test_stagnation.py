"""Unit tests for stagnation detection (Story 4.1).

Tests cover:
- StagnationPattern enum
- ExecutionHistory dataclass
- StagnationDetection dataclass
- StagnationDetector with 4 patterns:
  - Spinning (same output repeated)
  - Oscillation (A→B→A→B pattern)
  - No Drift (drift score unchanging)
  - Diminishing Returns (progress slowing)
- Event creation for each pattern
"""

import pytest

from ouroboros.resilience.stagnation import (
    DiminishingReturnsDetectedEvent,
    ExecutionHistory,
    NoDriftDetectedEvent,
    OscillationDetectedEvent,
    SpinningDetectedEvent,
    StagnationDetection,
    StagnationDetector,
    StagnationPattern,
    create_stagnation_event,
)


# =============================================================================
# StagnationPattern Enum Tests
# =============================================================================


class TestStagnationPattern:
    """Test the StagnationPattern enum."""

    def test_pattern_values(self) -> None:
        """Test pattern enum values."""
        assert StagnationPattern.SPINNING.value == "spinning"
        assert StagnationPattern.OSCILLATION.value == "oscillation"
        assert StagnationPattern.NO_DRIFT.value == "no_drift"
        assert StagnationPattern.DIMINISHING_RETURNS.value == "diminishing_returns"

    def test_default_thresholds(self) -> None:
        """Test default threshold values for each pattern."""
        assert StagnationPattern.SPINNING.default_threshold == 3
        assert StagnationPattern.OSCILLATION.default_threshold == 2
        assert StagnationPattern.NO_DRIFT.default_threshold == 3
        assert StagnationPattern.DIMINISHING_RETURNS.default_threshold == 3


# =============================================================================
# ExecutionHistory Tests
# =============================================================================


class TestExecutionHistory:
    """Test the ExecutionHistory dataclass."""

    def test_default_values(self) -> None:
        """Test default initialization."""
        history = ExecutionHistory()

        assert history.phase_outputs == ()
        assert history.error_signatures == ()
        assert history.drift_scores == ()
        assert history.iteration == 0

    def test_custom_initialization(self) -> None:
        """Test initialization with custom values."""
        history = ExecutionHistory(
            phase_outputs=("output1", "output2"),
            error_signatures=("error1",),
            drift_scores=(0.5, 0.4),
            iteration=3,
        )

        assert history.phase_outputs == ("output1", "output2")
        assert history.error_signatures == ("error1",)
        assert history.drift_scores == (0.5, 0.4)
        assert history.iteration == 3

    def test_from_lists_factory(self) -> None:
        """Test from_lists factory method."""
        history = ExecutionHistory.from_lists(
            phase_outputs=["output1", "output2"],
            error_signatures=["error1"],
            drift_scores=[0.5, 0.4],
            iteration=2,
        )

        assert history.phase_outputs == ("output1", "output2")
        assert history.error_signatures == ("error1",)
        assert history.drift_scores == (0.5, 0.4)
        assert history.iteration == 2

    def test_immutability(self) -> None:
        """Test that ExecutionHistory is frozen."""
        history = ExecutionHistory(iteration=1)

        with pytest.raises(AttributeError):
            history.iteration = 2  # type: ignore


# =============================================================================
# StagnationDetection Tests
# =============================================================================


class TestStagnationDetection:
    """Test the StagnationDetection dataclass."""

    def test_not_detected(self) -> None:
        """Test creating a not-detected result."""
        detection = StagnationDetection(
            pattern=StagnationPattern.SPINNING,
            detected=False,
            confidence=0.0,
        )

        assert detection.detected is False
        assert detection.confidence == 0.0
        assert detection.evidence == {}

    def test_detected_with_evidence(self) -> None:
        """Test creating a detected result with evidence."""
        detection = StagnationDetection(
            pattern=StagnationPattern.SPINNING,
            detected=True,
            confidence=1.0,
            evidence={"repeated_output": "error X", "repeat_count": 3},
        )

        assert detection.detected is True
        assert detection.confidence == 1.0
        assert detection.evidence["repeat_count"] == 3

    def test_immutability(self) -> None:
        """Test that StagnationDetection is frozen."""
        detection = StagnationDetection(
            pattern=StagnationPattern.SPINNING,
            detected=True,
            confidence=1.0,
        )

        with pytest.raises(AttributeError):
            detection.detected = False  # type: ignore


# =============================================================================
# StagnationDetector - Spinning Pattern Tests
# =============================================================================


class TestSpinningDetection:
    """Test spinning pattern detection."""

    def test_no_spinning_insufficient_data(self) -> None:
        """Test no spinning when not enough outputs."""
        detector = StagnationDetector(spinning_threshold=3)
        history = ExecutionHistory(
            phase_outputs=("output1", "output2"),  # Only 2, need 3
            iteration=1,
        )

        result = detector.detect(history)

        assert result.is_ok
        spinning = next(d for d in result.value if d.pattern == StagnationPattern.SPINNING)
        assert spinning.detected is False

    def test_spinning_detected_identical_outputs(self) -> None:
        """Test spinning detected when identical outputs."""
        detector = StagnationDetector(spinning_threshold=3)
        history = ExecutionHistory(
            phase_outputs=("error X", "error X", "error X"),
            iteration=3,
        )

        result = detector.detect(history)

        assert result.is_ok
        spinning = next(d for d in result.value if d.pattern == StagnationPattern.SPINNING)
        assert spinning.detected is True
        assert spinning.confidence == 1.0
        assert spinning.evidence["repeat_count"] == 3

    def test_no_spinning_different_outputs(self) -> None:
        """Test no spinning when outputs differ."""
        detector = StagnationDetector(spinning_threshold=3)
        history = ExecutionHistory(
            phase_outputs=("output1", "output2", "output3"),
            iteration=3,
        )

        result = detector.detect(history)

        assert result.is_ok
        spinning = next(d for d in result.value if d.pattern == StagnationPattern.SPINNING)
        assert spinning.detected is False

    def test_spinning_from_error_signatures(self) -> None:
        """Test spinning detected from repeated errors."""
        detector = StagnationDetector(spinning_threshold=3)
        history = ExecutionHistory(
            phase_outputs=("out1", "out2", "out3"),  # Different outputs
            error_signatures=("RateLimitError", "RateLimitError", "RateLimitError"),
            iteration=3,
        )

        result = detector.detect(history)

        assert result.is_ok
        spinning = next(d for d in result.value if d.pattern == StagnationPattern.SPINNING)
        assert spinning.detected is True
        assert spinning.evidence["source"] == "error_signatures"


# =============================================================================
# StagnationDetector - Oscillation Pattern Tests
# =============================================================================


class TestOscillationDetection:
    """Test oscillation pattern detection."""

    def test_no_oscillation_insufficient_data(self) -> None:
        """Test no oscillation when not enough outputs."""
        detector = StagnationDetector(oscillation_cycles=2)
        history = ExecutionHistory(
            phase_outputs=("A", "B", "A"),  # Only 3, need 4 for 2 cycles
            iteration=1,
        )

        result = detector.detect(history)

        assert result.is_ok
        oscillation = next(d for d in result.value if d.pattern == StagnationPattern.OSCILLATION)
        assert oscillation.detected is False

    def test_oscillation_detected_ab_pattern(self) -> None:
        """Test oscillation detected for A→B→A→B pattern."""
        detector = StagnationDetector(oscillation_cycles=2)
        history = ExecutionHistory(
            phase_outputs=("state A", "state B", "state A", "state B"),
            iteration=4,
        )

        result = detector.detect(history)

        assert result.is_ok
        oscillation = next(d for d in result.value if d.pattern == StagnationPattern.OSCILLATION)
        assert oscillation.detected is True
        assert oscillation.confidence == 0.9
        assert oscillation.evidence["cycles_detected"] == 2

    def test_no_oscillation_same_state(self) -> None:
        """Test no oscillation when all states are same."""
        detector = StagnationDetector(oscillation_cycles=2)
        history = ExecutionHistory(
            phase_outputs=("same", "same", "same", "same"),
            iteration=4,
        )

        result = detector.detect(history)

        assert result.is_ok
        oscillation = next(d for d in result.value if d.pattern == StagnationPattern.OSCILLATION)
        # Note: This is detected as spinning, not oscillation
        assert oscillation.detected is False

    def test_no_oscillation_random_pattern(self) -> None:
        """Test no oscillation for random pattern."""
        detector = StagnationDetector(oscillation_cycles=2)
        history = ExecutionHistory(
            phase_outputs=("A", "B", "C", "D"),
            iteration=4,
        )

        result = detector.detect(history)

        assert result.is_ok
        oscillation = next(d for d in result.value if d.pattern == StagnationPattern.OSCILLATION)
        assert oscillation.detected is False


# =============================================================================
# StagnationDetector - No Drift Pattern Tests
# =============================================================================


class TestNoDriftDetection:
    """Test no drift pattern detection."""

    def test_no_drift_insufficient_data(self) -> None:
        """Test no drift when not enough scores."""
        detector = StagnationDetector(no_drift_iterations=3)
        history = ExecutionHistory(
            drift_scores=(0.5, 0.5),  # Only 2, need 3
            iteration=2,
        )

        result = detector.detect(history)

        assert result.is_ok
        no_drift = next(d for d in result.value if d.pattern == StagnationPattern.NO_DRIFT)
        assert no_drift.detected is False

    def test_no_drift_detected_flat_scores(self) -> None:
        """Test no drift detected when scores are flat."""
        detector = StagnationDetector(no_drift_iterations=3, no_drift_epsilon=0.02)
        history = ExecutionHistory(
            drift_scores=(0.5, 0.51, 0.5),  # Deltas: 0.01, 0.01 (< 0.02)
            iteration=3,
        )

        result = detector.detect(history)

        assert result.is_ok
        no_drift = next(d for d in result.value if d.pattern == StagnationPattern.NO_DRIFT)
        assert no_drift.detected is True
        assert no_drift.evidence["stagnant_iterations"] == 3

    def test_no_drift_not_detected_improving(self) -> None:
        """Test no drift not detected when improving."""
        detector = StagnationDetector(no_drift_iterations=3, no_drift_epsilon=0.01)
        history = ExecutionHistory(
            drift_scores=(0.5, 0.4, 0.3),  # Deltas: 0.1, 0.1 (> 0.01)
            iteration=3,
        )

        result = detector.detect(history)

        assert result.is_ok
        no_drift = next(d for d in result.value if d.pattern == StagnationPattern.NO_DRIFT)
        assert no_drift.detected is False


# =============================================================================
# StagnationDetector - Diminishing Returns Pattern Tests
# =============================================================================


class TestDiminishingReturnsDetection:
    """Test diminishing returns pattern detection."""

    def test_diminishing_insufficient_data(self) -> None:
        """Test no diminishing when not enough scores."""
        detector = StagnationDetector(no_drift_iterations=3)
        history = ExecutionHistory(
            drift_scores=(0.5, 0.4, 0.3),  # Only 3, need 4 for 3 improvements
            iteration=3,
        )

        result = detector.detect(history)

        assert result.is_ok
        diminishing = next(
            d for d in result.value if d.pattern == StagnationPattern.DIMINISHING_RETURNS
        )
        assert diminishing.detected is False

    def test_diminishing_detected_decreasing_improvements(self) -> None:
        """Test diminishing detected when improvements decrease."""
        detector = StagnationDetector(no_drift_iterations=3, diminishing_threshold=0.02)
        # Improvements: 0.01, 0.005, 0.001 (all < 0.02, and decreasing)
        history = ExecutionHistory(
            drift_scores=(0.5, 0.49, 0.485, 0.484),
            iteration=4,
        )

        result = detector.detect(history)

        assert result.is_ok
        diminishing = next(
            d for d in result.value if d.pattern == StagnationPattern.DIMINISHING_RETURNS
        )
        assert diminishing.detected is True
        assert diminishing.evidence["monotonically_decreasing"] is True
        assert diminishing.confidence == 0.8

    def test_diminishing_not_detected_good_progress(self) -> None:
        """Test diminishing not detected with good progress."""
        detector = StagnationDetector(no_drift_iterations=3, diminishing_threshold=0.01)
        # Improvements: 0.1, 0.1, 0.1 (all > 0.01)
        history = ExecutionHistory(
            drift_scores=(0.5, 0.4, 0.3, 0.2),
            iteration=4,
        )

        result = detector.detect(history)

        assert result.is_ok
        diminishing = next(
            d for d in result.value if d.pattern == StagnationPattern.DIMINISHING_RETURNS
        )
        assert diminishing.detected is False


# =============================================================================
# StagnationDetector - Multiple Patterns Tests
# =============================================================================


class TestMultiplePatterns:
    """Test detection of multiple patterns simultaneously."""

    def test_detect_returns_all_patterns(self) -> None:
        """Test that detect() returns results for all 4 patterns."""
        detector = StagnationDetector()
        history = ExecutionHistory(iteration=1)

        result = detector.detect(history)

        assert result.is_ok
        assert len(result.value) == 4
        patterns = {d.pattern for d in result.value}
        assert patterns == {
            StagnationPattern.SPINNING,
            StagnationPattern.OSCILLATION,
            StagnationPattern.NO_DRIFT,
            StagnationPattern.DIMINISHING_RETURNS,
        }

    def test_multiple_patterns_detected(self) -> None:
        """Test that multiple patterns can be detected simultaneously."""
        detector = StagnationDetector(
            spinning_threshold=3,
            no_drift_iterations=3,
            no_drift_epsilon=0.02,
        )
        # Spinning (same output) + No Drift (flat scores)
        history = ExecutionHistory(
            phase_outputs=("same", "same", "same"),
            drift_scores=(0.5, 0.5, 0.5),
            iteration=3,
        )

        result = detector.detect(history)

        assert result.is_ok
        detected = [d for d in result.value if d.detected]
        assert len(detected) >= 2
        detected_patterns = {d.pattern for d in detected}
        assert StagnationPattern.SPINNING in detected_patterns
        assert StagnationPattern.NO_DRIFT in detected_patterns


# =============================================================================
# Event Creation Tests
# =============================================================================


class TestSpinningEvent:
    """Test SpinningDetectedEvent creation."""

    def test_event_creation(self) -> None:
        """Test creating SpinningDetectedEvent."""
        event = SpinningDetectedEvent(
            execution_id="exec-123",
            repeated_output_sample="error message",
            repeat_count=3,
            source="phase_outputs",
            seed_id="seed-456",
            iteration=5,
        )

        assert event.type == "resilience.stagnation.spinning.detected"
        assert event.aggregate_type == "execution"
        assert event.aggregate_id == "exec-123"
        assert event.data["pattern"] == "spinning"
        assert event.data["repeat_count"] == 3
        assert event.data["seed_id"] == "seed-456"

    def test_output_truncation(self) -> None:
        """Test that output is truncated to 200 chars."""
        long_output = "x" * 500
        event = SpinningDetectedEvent(
            execution_id="exec-123",
            repeated_output_sample=long_output,
            repeat_count=3,
            source="phase_outputs",
        )

        assert len(event.data["repeated_output_sample"]) == 200


class TestOscillationEvent:
    """Test OscillationDetectedEvent creation."""

    def test_event_creation(self) -> None:
        """Test creating OscillationDetectedEvent."""
        event = OscillationDetectedEvent(
            execution_id="exec-123",
            state_a_sample="state A",
            state_b_sample="state B",
            cycles_detected=2,
        )

        assert event.type == "resilience.stagnation.oscillation.detected"
        assert event.data["pattern"] == "oscillation"
        assert event.data["cycles_detected"] == 2


class TestNoDriftEvent:
    """Test NoDriftDetectedEvent creation."""

    def test_event_creation(self) -> None:
        """Test creating NoDriftDetectedEvent."""
        event = NoDriftDetectedEvent(
            execution_id="exec-123",
            drift_scores=[0.5, 0.5, 0.5],
            average_drift=0.5,
            stagnant_iterations=3,
        )

        assert event.type == "resilience.stagnation.no_drift.detected"
        assert event.data["pattern"] == "no_drift"
        assert event.data["average_drift"] == 0.5


class TestDiminishingReturnsEvent:
    """Test DiminishingReturnsDetectedEvent creation."""

    def test_event_creation(self) -> None:
        """Test creating DiminishingReturnsDetectedEvent."""
        event = DiminishingReturnsDetectedEvent(
            execution_id="exec-123",
            improvement_rates=[0.01, 0.005, 0.001],
            monotonically_decreasing=True,
        )

        assert event.type == "resilience.stagnation.diminishing_returns.detected"
        assert event.data["pattern"] == "diminishing_returns"
        assert event.data["monotonically_decreasing"] is True


# =============================================================================
# Event Factory Tests
# =============================================================================


class TestCreateStagnationEvent:
    """Test create_stagnation_event factory function."""

    def test_creates_spinning_event(self) -> None:
        """Test factory creates SpinningDetectedEvent."""
        detection = StagnationDetection(
            pattern=StagnationPattern.SPINNING,
            detected=True,
            confidence=1.0,
            evidence={
                "repeated_output_sample": "error",
                "repeat_count": 3,
                "source": "phase_outputs",
            },
        )

        event = create_stagnation_event(detection, "exec-123", seed_id="seed-456", iteration=5)

        assert isinstance(event, SpinningDetectedEvent)
        assert event.data["repeat_count"] == 3

    def test_creates_oscillation_event(self) -> None:
        """Test factory creates OscillationDetectedEvent."""
        detection = StagnationDetection(
            pattern=StagnationPattern.OSCILLATION,
            detected=True,
            confidence=0.9,
            evidence={
                "state_a_sample": "A",
                "state_b_sample": "B",
                "cycles_detected": 2,
            },
        )

        event = create_stagnation_event(detection, "exec-123")

        assert isinstance(event, OscillationDetectedEvent)
        assert event.data["cycles_detected"] == 2

    def test_creates_no_drift_event(self) -> None:
        """Test factory creates NoDriftDetectedEvent."""
        detection = StagnationDetection(
            pattern=StagnationPattern.NO_DRIFT,
            detected=True,
            confidence=1.0,
            evidence={
                "drift_scores": [0.5, 0.5, 0.5],
                "average_drift": 0.5,
                "stagnant_iterations": 3,
            },
        )

        event = create_stagnation_event(detection, "exec-123")

        assert isinstance(event, NoDriftDetectedEvent)
        assert event.data["stagnant_iterations"] == 3

    def test_creates_diminishing_returns_event(self) -> None:
        """Test factory creates DiminishingReturnsDetectedEvent."""
        detection = StagnationDetection(
            pattern=StagnationPattern.DIMINISHING_RETURNS,
            detected=True,
            confidence=0.8,
            evidence={
                "improvement_rates": [0.01, 0.005],
                "monotonically_decreasing": True,
            },
        )

        event = create_stagnation_event(detection, "exec-123")

        assert isinstance(event, DiminishingReturnsDetectedEvent)
        assert event.data["monotonically_decreasing"] is True

    def test_raises_for_not_detected(self) -> None:
        """Test factory raises ValueError for non-detected."""
        detection = StagnationDetection(
            pattern=StagnationPattern.SPINNING,
            detected=False,
            confidence=0.0,
        )

        with pytest.raises(ValueError, match="non-detected"):
            create_stagnation_event(detection, "exec-123")


# =============================================================================
# Configuration Tests
# =============================================================================


class TestDetectorConfiguration:
    """Test StagnationDetector configuration."""

    def test_default_thresholds(self) -> None:
        """Test default threshold values."""
        detector = StagnationDetector()

        assert detector._spinning_threshold == 3
        assert detector._oscillation_cycles == 2
        assert detector._no_drift_epsilon == 0.01
        assert detector._no_drift_iterations == 3
        assert detector._diminishing_threshold == 0.01

    def test_custom_thresholds(self) -> None:
        """Test custom threshold values."""
        detector = StagnationDetector(
            spinning_threshold=5,
            oscillation_cycles=3,
            no_drift_epsilon=0.05,
            no_drift_iterations=5,
            diminishing_threshold=0.02,
        )

        assert detector._spinning_threshold == 5
        assert detector._oscillation_cycles == 3
        assert detector._no_drift_epsilon == 0.05
        assert detector._no_drift_iterations == 5
        assert detector._diminishing_threshold == 0.02


# =============================================================================
# Edge Case Tests
# =============================================================================


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_empty_history(self) -> None:
        """Test detection with empty history."""
        detector = StagnationDetector()
        history = ExecutionHistory()

        result = detector.detect(history)

        assert result.is_ok
        # No patterns should be detected with empty data
        detected = [d for d in result.value if d.detected]
        assert len(detected) == 0

    def test_hash_consistency(self) -> None:
        """Test that hash is consistent for same input."""
        detector = StagnationDetector()

        hash1 = detector._compute_hash("test string")
        hash2 = detector._compute_hash("test string")
        hash3 = detector._compute_hash("different string")

        assert hash1 == hash2
        assert hash1 != hash3

    def test_very_long_output(self) -> None:
        """Test detection with very long outputs."""
        detector = StagnationDetector(spinning_threshold=2)
        long_output = "x" * 10000
        history = ExecutionHistory(
            phase_outputs=(long_output, long_output),
            iteration=2,
        )

        result = detector.detect(history)

        assert result.is_ok
        spinning = next(d for d in result.value if d.pattern == StagnationPattern.SPINNING)
        assert spinning.detected is True

    def test_unicode_outputs(self) -> None:
        """Test detection with unicode outputs."""
        detector = StagnationDetector(spinning_threshold=2)
        history = ExecutionHistory(
            phase_outputs=("日本語テスト", "日本語テスト"),
            iteration=2,
        )

        result = detector.detect(history)

        assert result.is_ok
        spinning = next(d for d in result.value if d.pattern == StagnationPattern.SPINNING)
        assert spinning.detected is True

    def test_negative_drift_scores(self) -> None:
        """Test detection with negative drift scores."""
        detector = StagnationDetector(no_drift_iterations=3, no_drift_epsilon=0.1)
        history = ExecutionHistory(
            drift_scores=(-0.5, -0.5, -0.5),
            iteration=3,
        )

        result = detector.detect(history)

        assert result.is_ok
        no_drift = next(d for d in result.value if d.pattern == StagnationPattern.NO_DRIFT)
        assert no_drift.detected is True
