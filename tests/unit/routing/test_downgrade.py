"""Unit tests for downgrade management in Ouroboros.

Tests cover:
- SuccessTracker: consecutive success tracking and reset on failure
- PatternMatcher: Jaccard similarity calculation and pattern matching
- DowngradeManager: downgrade decisions based on success history
- Integration: pattern learning and tier inheritance

Test Coverage Goals:
- All public methods
- Edge cases (empty inputs, boundary values)
- Error handling
- Downgrade threshold behavior (5 consecutive successes)
- Similarity threshold behavior (>=80%)
"""

import pytest

from ouroboros.routing.downgrade import (
    DOWNGRADE_THRESHOLD,
    SIMILARITY_THRESHOLD,
    DowngradeManager,
    DowngradeResult,
    PatternMatcher,
    SuccessTracker,
    _calculate_cost_savings,
    _get_lower_tier,
)
from ouroboros.routing.tiers import Tier


class TestConstants:
    """Test module constants."""

    def test_downgrade_threshold_is_five(self) -> None:
        """Test that downgrade threshold is 5 consecutive successes."""
        assert DOWNGRADE_THRESHOLD == 5

    def test_similarity_threshold_is_eighty_percent(self) -> None:
        """Test that similarity threshold is 80%."""
        assert SIMILARITY_THRESHOLD == 0.80


class TestGetLowerTier:
    """Test the _get_lower_tier helper function."""

    def test_frontier_to_standard(self) -> None:
        """Test Frontier downgrades to Standard."""
        assert _get_lower_tier(Tier.FRONTIER) == Tier.STANDARD

    def test_standard_to_frugal(self) -> None:
        """Test Standard downgrades to Frugal."""
        assert _get_lower_tier(Tier.STANDARD) == Tier.FRUGAL

    def test_frugal_stays_frugal(self) -> None:
        """Test Frugal cannot downgrade further."""
        assert _get_lower_tier(Tier.FRUGAL) == Tier.FRUGAL


class TestCalculateCostSavings:
    """Test the _calculate_cost_savings helper function."""

    def test_frontier_to_standard_savings(self) -> None:
        """Test cost savings from Frontier to Standard."""
        # Frontier (30x) to Standard (10x) = 3x savings
        savings = _calculate_cost_savings(Tier.FRONTIER, Tier.STANDARD)
        assert savings == 3.0

    def test_standard_to_frugal_savings(self) -> None:
        """Test cost savings from Standard to Frugal."""
        # Standard (10x) to Frugal (1x) = 10x savings
        savings = _calculate_cost_savings(Tier.STANDARD, Tier.FRUGAL)
        assert savings == 10.0

    def test_frontier_to_frugal_savings(self) -> None:
        """Test cost savings from Frontier to Frugal."""
        # Frontier (30x) to Frugal (1x) = 30x savings
        savings = _calculate_cost_savings(Tier.FRONTIER, Tier.FRUGAL)
        assert savings == 30.0

    def test_same_tier_no_savings(self) -> None:
        """Test no savings when tier doesn't change."""
        assert _calculate_cost_savings(Tier.STANDARD, Tier.STANDARD) == 1.0
        assert _calculate_cost_savings(Tier.FRUGAL, Tier.FRUGAL) == 1.0


class TestSuccessTracker:
    """Test the SuccessTracker dataclass."""

    def test_initial_state_empty(self) -> None:
        """Test tracker starts with empty state."""
        tracker = SuccessTracker()
        assert tracker.get_success_count("any_pattern") == 0
        assert tracker.get_tier("any_pattern") is None
        assert tracker.get_all_patterns() == []

    def test_record_success_increments_count(self) -> None:
        """Test recording success increments counter."""
        tracker = SuccessTracker()

        count = tracker.record_success("pattern_1", Tier.STANDARD)
        assert count == 1

        count = tracker.record_success("pattern_1", Tier.STANDARD)
        assert count == 2

        count = tracker.record_success("pattern_1", Tier.STANDARD)
        assert count == 3

    def test_record_success_tracks_tier(self) -> None:
        """Test recording success tracks the tier."""
        tracker = SuccessTracker()

        tracker.record_success("pattern_1", Tier.STANDARD)
        assert tracker.get_tier("pattern_1") == Tier.STANDARD

        # Tier updates if different tier used
        tracker.record_success("pattern_1", Tier.FRUGAL)
        assert tracker.get_tier("pattern_1") == Tier.FRUGAL

    def test_reset_on_failure_clears_count(self) -> None:
        """Test reset_on_failure sets count to 0."""
        tracker = SuccessTracker()

        tracker.record_success("pattern_1", Tier.STANDARD)
        tracker.record_success("pattern_1", Tier.STANDARD)
        assert tracker.get_success_count("pattern_1") == 2

        tracker.reset_on_failure("pattern_1")
        assert tracker.get_success_count("pattern_1") == 0

    def test_reset_on_failure_preserves_tier(self) -> None:
        """Test reset_on_failure preserves tier history."""
        tracker = SuccessTracker()

        tracker.record_success("pattern_1", Tier.FRONTIER)
        tracker.reset_on_failure("pattern_1")

        # Tier history preserved even after reset
        assert tracker.get_tier("pattern_1") == Tier.FRONTIER

    def test_reset_on_unknown_pattern(self) -> None:
        """Test reset_on_failure on unknown pattern is safe."""
        tracker = SuccessTracker()
        # Should not raise
        tracker.reset_on_failure("unknown_pattern")
        assert tracker.get_success_count("unknown_pattern") == 0

    def test_multiple_patterns_independent(self) -> None:
        """Test multiple patterns are tracked independently."""
        tracker = SuccessTracker()

        tracker.record_success("pattern_a", Tier.STANDARD)
        tracker.record_success("pattern_a", Tier.STANDARD)
        tracker.record_success("pattern_b", Tier.FRUGAL)

        assert tracker.get_success_count("pattern_a") == 2
        assert tracker.get_success_count("pattern_b") == 1
        assert tracker.get_tier("pattern_a") == Tier.STANDARD
        assert tracker.get_tier("pattern_b") == Tier.FRUGAL

    def test_get_all_patterns_returns_tracked(self) -> None:
        """Test get_all_patterns returns all tracked patterns."""
        tracker = SuccessTracker()

        tracker.record_success("alpha", Tier.FRUGAL)
        tracker.record_success("beta", Tier.STANDARD)
        tracker.record_success("gamma", Tier.FRONTIER)

        patterns = tracker.get_all_patterns()
        assert set(patterns) == {"alpha", "beta", "gamma"}

    def test_clear_removes_all_state(self) -> None:
        """Test clear removes all tracking state."""
        tracker = SuccessTracker()

        tracker.record_success("pattern_1", Tier.STANDARD)
        tracker.record_success("pattern_2", Tier.FRUGAL)
        tracker.clear()

        assert tracker.get_all_patterns() == []
        assert tracker.get_success_count("pattern_1") == 0
        assert tracker.get_tier("pattern_1") is None


class TestPatternMatcher:
    """Test the PatternMatcher class."""

    def test_default_threshold(self) -> None:
        """Test default similarity threshold is 80%."""
        matcher = PatternMatcher()
        assert matcher.similarity_threshold == 0.80

    def test_custom_threshold(self) -> None:
        """Test custom similarity threshold can be set."""
        matcher = PatternMatcher(similarity_threshold=0.90)
        assert matcher.similarity_threshold == 0.90

    def test_identical_patterns_similarity_one(self) -> None:
        """Test identical patterns have similarity of 1.0."""
        matcher = PatternMatcher()
        sim = matcher.calculate_similarity("fix bug", "fix bug")
        assert sim == 1.0

    def test_completely_different_patterns_similarity_zero(self) -> None:
        """Test completely different patterns have similarity of 0.0."""
        matcher = PatternMatcher()
        sim = matcher.calculate_similarity("fix bug", "add feature test")
        assert sim == 0.0

    def test_partial_overlap_similarity(self) -> None:
        """Test partial overlap gives partial similarity."""
        matcher = PatternMatcher()
        # "fix bug" -> {fix, bug}
        # "fix typo" -> {fix, typo}
        # Intersection: {fix}, Union: {fix, bug, typo}
        # Similarity: 1/3 = 0.333...
        sim = matcher.calculate_similarity("fix bug", "fix typo")
        assert sim == pytest.approx(1 / 3)

    def test_case_insensitive_matching(self) -> None:
        """Test similarity is case insensitive."""
        matcher = PatternMatcher()
        sim = matcher.calculate_similarity("FIX Bug", "fix bug")
        assert sim == 1.0

    def test_punctuation_stripped(self) -> None:
        """Test punctuation is stripped from tokens."""
        matcher = PatternMatcher()
        sim = matcher.calculate_similarity("fix, bug!", "fix bug")
        assert sim == 1.0

    def test_empty_patterns_similarity_one(self) -> None:
        """Test two empty patterns have similarity of 1.0."""
        matcher = PatternMatcher()
        sim = matcher.calculate_similarity("", "")
        assert sim == 1.0

    def test_one_empty_pattern_similarity_zero(self) -> None:
        """Test one empty pattern has similarity of 0.0."""
        matcher = PatternMatcher()
        assert matcher.calculate_similarity("fix bug", "") == 0.0
        assert matcher.calculate_similarity("", "fix bug") == 0.0

    def test_whitespace_only_treated_as_empty(self) -> None:
        """Test whitespace-only patterns treated as empty."""
        matcher = PatternMatcher()
        # Both effectively empty after tokenization
        sim = matcher.calculate_similarity("   ", "   ")
        assert sim == 1.0

    def test_is_similar_above_threshold(self) -> None:
        """Test is_similar returns True when above threshold."""
        matcher = PatternMatcher(similarity_threshold=0.5)
        # Similarity = 1/3, threshold = 0.5
        assert not matcher.is_similar("fix bug", "fix typo")

        # Same pattern = 1.0 similarity
        assert matcher.is_similar("fix bug", "fix bug")

    def test_is_similar_exactly_at_threshold(self) -> None:
        """Test is_similar returns True when exactly at threshold."""
        matcher = PatternMatcher(similarity_threshold=0.5)
        # "a b" vs "a c" = {a} / {a, b, c} = 1/3 = 0.333
        # "a b c" vs "a b d" = {a, b} / {a, b, c, d} = 2/4 = 0.5
        assert matcher.is_similar("a b c", "a b d")

    def test_find_similar_patterns_returns_matches(self) -> None:
        """Test find_similar_patterns finds matching patterns."""
        matcher = PatternMatcher(similarity_threshold=0.3)

        candidates = [
            "fix typo in README",  # Shares "fix", "in" with target
            "add new feature",  # No overlap
            "fix bug in code",  # Shares "fix", "in" with target
        ]

        # Target: "fix error in module"
        # Similarity with "fix typo in README" = {fix, in} / {fix, typo, in, readme, error, module} = 2/6 = 0.33
        similar = matcher.find_similar_patterns("fix error in module", candidates)

        # Both "fix...in" patterns should match with threshold 0.3
        assert len(similar) >= 1
        # Results should be sorted by similarity descending
        if len(similar) > 1:
            assert similar[0][1] >= similar[1][1]

    def test_find_similar_patterns_empty_candidates(self) -> None:
        """Test find_similar_patterns with empty candidates list."""
        matcher = PatternMatcher()
        similar = matcher.find_similar_patterns("fix bug", [])
        assert similar == []

    def test_find_similar_patterns_no_matches(self) -> None:
        """Test find_similar_patterns with no matching patterns."""
        matcher = PatternMatcher(similarity_threshold=0.9)
        candidates = ["add feature", "refactor code", "update docs"]
        similar = matcher.find_similar_patterns("fix bug", candidates)
        assert similar == []

    def test_high_similarity_patterns(self) -> None:
        """Test patterns with high similarity."""
        matcher = PatternMatcher()
        # "fix typo in README" vs "fix typo in documentation"
        # Tokens: {fix, typo, in, readme} vs {fix, typo, in, documentation}
        # Intersection: {fix, typo, in} = 3
        # Union: {fix, typo, in, readme, documentation} = 5
        # Similarity: 3/5 = 0.6
        sim = matcher.calculate_similarity(
            "fix typo in README",
            "fix typo in documentation",
        )
        assert sim == 0.6


class TestDowngradeResult:
    """Test the DowngradeResult dataclass."""

    def test_create_downgrade_result(self) -> None:
        """Test creating a DowngradeResult."""
        result = DowngradeResult(
            should_downgrade=True,
            current_tier=Tier.STANDARD,
            recommended_tier=Tier.FRUGAL,
            consecutive_successes=5,
            cost_savings_factor=10.0,
        )

        assert result.should_downgrade is True
        assert result.current_tier == Tier.STANDARD
        assert result.recommended_tier == Tier.FRUGAL
        assert result.consecutive_successes == 5
        assert result.cost_savings_factor == 10.0

    def test_immutable(self) -> None:
        """Test DowngradeResult is immutable."""
        result = DowngradeResult(
            should_downgrade=False,
            current_tier=Tier.FRUGAL,
            recommended_tier=Tier.FRUGAL,
            consecutive_successes=3,
            cost_savings_factor=1.0,
        )

        with pytest.raises(AttributeError):
            result.should_downgrade = True  # type: ignore[misc]


class TestDowngradeManager:
    """Test the DowngradeManager class."""

    def test_default_threshold(self) -> None:
        """Test default downgrade threshold is 5."""
        manager = DowngradeManager()
        assert manager.downgrade_threshold == 5

    def test_record_success_returns_result(self) -> None:
        """Test record_success returns a Result with DowngradeResult."""
        manager = DowngradeManager()

        result = manager.record_success("pattern_1", Tier.STANDARD)

        assert result.is_ok
        assert isinstance(result.value, DowngradeResult)

    def test_no_downgrade_before_threshold(self) -> None:
        """Test no downgrade recommended before threshold reached."""
        manager = DowngradeManager()

        # Record 4 successes (below threshold of 5)
        for i in range(4):
            result = manager.record_success("pattern_1", Tier.STANDARD)
            assert result.is_ok
            assert result.value.should_downgrade is False
            assert result.value.consecutive_successes == i + 1

    def test_downgrade_at_threshold(self) -> None:
        """Test downgrade recommended at threshold (5 successes)."""
        manager = DowngradeManager()

        # Record 5 successes
        for i in range(5):
            result = manager.record_success("pattern_1", Tier.STANDARD)

        assert result.is_ok
        assert result.value.should_downgrade is True
        assert result.value.current_tier == Tier.STANDARD
        assert result.value.recommended_tier == Tier.FRUGAL
        assert result.value.consecutive_successes == 5
        assert result.value.cost_savings_factor == 10.0  # Standard(10x) to Frugal(1x)

    def test_downgrade_above_threshold(self) -> None:
        """Test downgrade continues to be recommended above threshold."""
        manager = DowngradeManager()

        # Record 7 successes (above threshold)
        for _ in range(7):
            result = manager.record_success("pattern_1", Tier.FRONTIER)

        assert result.is_ok
        assert result.value.should_downgrade is True
        assert result.value.recommended_tier == Tier.STANDARD
        assert result.value.consecutive_successes == 7

    def test_no_downgrade_at_frugal_tier(self) -> None:
        """Test no downgrade when already at Frugal tier."""
        manager = DowngradeManager()

        # Record 10 successes at Frugal
        for _ in range(10):
            result = manager.record_success("pattern_1", Tier.FRUGAL)

        assert result.is_ok
        assert result.value.should_downgrade is False
        assert result.value.current_tier == Tier.FRUGAL
        assert result.value.recommended_tier == Tier.FRUGAL

    def test_frontier_downgrades_to_standard(self) -> None:
        """Test Frontier tier downgrades to Standard."""
        manager = DowngradeManager()

        for _ in range(5):
            result = manager.record_success("pattern_1", Tier.FRONTIER)

        assert result.is_ok
        assert result.value.should_downgrade is True
        assert result.value.recommended_tier == Tier.STANDARD
        assert result.value.cost_savings_factor == 3.0  # Frontier(30x) to Standard(10x)

    def test_record_failure_resets_counter(self) -> None:
        """Test recording failure resets the success counter."""
        manager = DowngradeManager()

        # Record 4 successes
        for _ in range(4):
            manager.record_success("pattern_1", Tier.STANDARD)

        # Record failure
        manager.record_failure("pattern_1")

        # Counter should be reset
        result = manager.record_success("pattern_1", Tier.STANDARD)
        assert result.is_ok
        assert result.value.consecutive_successes == 1

    def test_failure_after_downgrade_threshold(self) -> None:
        """Test failure resets counter even after threshold reached."""
        manager = DowngradeManager()

        # Reach threshold
        for _ in range(5):
            manager.record_success("pattern_1", Tier.STANDARD)

        # Failure resets everything
        manager.record_failure("pattern_1")

        result = manager.record_success("pattern_1", Tier.STANDARD)
        assert result.value.consecutive_successes == 1
        assert result.value.should_downgrade is False

    def test_multiple_patterns_independent(self) -> None:
        """Test multiple patterns are tracked independently."""
        manager = DowngradeManager()

        # Pattern A: 5 successes -> should downgrade
        for _ in range(5):
            manager.record_success("pattern_a", Tier.STANDARD)

        # Pattern B: 2 successes -> no downgrade
        for _ in range(2):
            manager.record_success("pattern_b", Tier.FRONTIER)

        # Check pattern A
        result_a = manager.record_success("pattern_a", Tier.STANDARD)
        assert result_a.value.should_downgrade is True

        # Check pattern B
        result_b = manager.record_success("pattern_b", Tier.FRONTIER)
        assert result_b.value.should_downgrade is False
        assert result_b.value.consecutive_successes == 3

    def test_apply_downgrade_resets_counter(self) -> None:
        """Test apply_downgrade resets the success counter."""
        manager = DowngradeManager()

        # Reach threshold
        for _ in range(5):
            manager.record_success("pattern_1", Tier.STANDARD)

        # Apply downgrade
        manager.apply_downgrade("pattern_1")

        # Counter should be reset
        assert manager.tracker.get_success_count("pattern_1") == 0

    def test_apply_downgrade_updates_tier(self) -> None:
        """Test apply_downgrade updates the tier history."""
        manager = DowngradeManager()

        # Record at Standard
        manager.record_success("pattern_1", Tier.STANDARD)
        assert manager.tracker.get_tier("pattern_1") == Tier.STANDARD

        # Apply downgrade
        manager.apply_downgrade("pattern_1")

        # Tier should be updated to Frugal
        assert manager.tracker.get_tier("pattern_1") == Tier.FRUGAL

    def test_get_cost_savings_estimate(self) -> None:
        """Test cost savings estimation."""
        manager = DowngradeManager()

        manager.record_success("pattern_1", Tier.FRONTIER)
        savings = manager.get_cost_savings_estimate("pattern_1")
        assert savings == 3.0  # Frontier to Standard

        manager.record_success("pattern_2", Tier.STANDARD)
        savings = manager.get_cost_savings_estimate("pattern_2")
        assert savings == 10.0  # Standard to Frugal

        manager.record_success("pattern_3", Tier.FRUGAL)
        savings = manager.get_cost_savings_estimate("pattern_3")
        assert savings == 1.0  # No savings at Frugal

    def test_get_cost_savings_unknown_pattern(self) -> None:
        """Test cost savings for unknown pattern returns 1.0."""
        manager = DowngradeManager()
        savings = manager.get_cost_savings_estimate("unknown")
        assert savings == 1.0

    def test_clear_removes_all_state(self) -> None:
        """Test clear removes all tracking state."""
        manager = DowngradeManager()

        manager.record_success("pattern_1", Tier.STANDARD)
        manager.record_success("pattern_2", Tier.FRONTIER)
        manager.clear()

        assert manager.tracker.get_all_patterns() == []


class TestDowngradeManagerPatternLearning:
    """Test pattern learning in DowngradeManager."""

    def test_get_recommended_tier_no_patterns(self) -> None:
        """Test recommended tier with no tracked patterns returns default."""
        manager = DowngradeManager()

        tier = manager.get_recommended_tier_for_pattern("fix bug")
        assert tier == Tier.FRUGAL  # Default

        tier = manager.get_recommended_tier_for_pattern(
            "fix bug", default_tier=Tier.STANDARD
        )
        assert tier == Tier.STANDARD

    def test_get_recommended_tier_no_similar_patterns(self) -> None:
        """Test recommended tier with no similar patterns returns default."""
        manager = DowngradeManager()

        # Add some patterns with low similarity
        manager.record_success("add new feature", Tier.FRONTIER)
        manager.record_success("refactor database", Tier.STANDARD)

        # Query a dissimilar pattern
        tier = manager.get_recommended_tier_for_pattern("fix critical bug")
        assert tier == Tier.FRUGAL  # Default

    def test_get_recommended_tier_similar_pattern_found(self) -> None:
        """Test recommended tier inherits from similar pattern."""
        manager = DowngradeManager()

        # Use matcher with lower threshold for test
        manager._pattern_matcher = PatternMatcher(similarity_threshold=0.5)

        # Track a pattern at Frugal tier
        manager.record_success("fix typo in README", Tier.FRUGAL)

        # Query a similar pattern
        tier = manager.get_recommended_tier_for_pattern("fix typo in docs")
        # Should inherit Frugal tier from similar pattern
        # Similarity: {fix, typo, in} / {fix, typo, in, readme, docs} = 3/5 = 0.6 >= 0.5
        assert tier == Tier.FRUGAL

    def test_get_recommended_tier_best_match_used(self) -> None:
        """Test the best matching pattern's tier is used."""
        manager = DowngradeManager()
        manager._pattern_matcher = PatternMatcher(similarity_threshold=0.3)

        # Track patterns at different tiers
        manager.record_success("fix small bug", Tier.FRUGAL)
        manager.record_success("fix critical bug in production", Tier.FRONTIER)

        # Query should match the more similar one
        tier = manager.get_recommended_tier_for_pattern("fix small typo")
        # "fix small typo" is more similar to "fix small bug"
        assert tier == Tier.FRUGAL


class TestDowngradeManagerIntegration:
    """Integration tests for downgrade scenarios."""

    def test_complete_downgrade_cycle(self) -> None:
        """Test a complete downgrade cycle from Frontier to Frugal."""
        manager = DowngradeManager()

        # Start at Frontier, record 5 successes
        for _ in range(5):
            result = manager.record_success("pattern_1", Tier.FRONTIER)
        assert result.value.should_downgrade is True
        assert result.value.recommended_tier == Tier.STANDARD

        # Apply downgrade and continue at Standard
        manager.apply_downgrade("pattern_1")

        # Record 5 more successes at Standard
        for _ in range(5):
            result = manager.record_success("pattern_1", Tier.STANDARD)
        assert result.value.should_downgrade is True
        assert result.value.recommended_tier == Tier.FRUGAL

        # Apply downgrade to Frugal
        manager.apply_downgrade("pattern_1")

        # At Frugal, no more downgrades even after many successes
        for _ in range(10):
            result = manager.record_success("pattern_1", Tier.FRUGAL)
        assert result.value.should_downgrade is False

    def test_failure_interrupts_downgrade_progress(self) -> None:
        """Test that failure interrupts downgrade progress."""
        manager = DowngradeManager()

        # 4 successes, then failure
        for _ in range(4):
            manager.record_success("pattern_1", Tier.STANDARD)
        manager.record_failure("pattern_1")

        # Need 5 more successes for downgrade
        for i in range(5):
            result = manager.record_success("pattern_1", Tier.STANDARD)
            if i < 4:
                assert result.value.should_downgrade is False
            else:
                assert result.value.should_downgrade is True

    def test_mixed_tier_successes(self) -> None:
        """Test pattern tracked across different tiers."""
        manager = DowngradeManager()

        # Start at Frontier
        for _ in range(3):
            manager.record_success("pattern_1", Tier.FRONTIER)

        # Switch to Standard (simulating manual intervention)
        for _ in range(2):
            result = manager.record_success("pattern_1", Tier.STANDARD)

        # Should reach threshold (total 5 successes)
        assert result.value.should_downgrade is True
        assert result.value.current_tier == Tier.STANDARD
        assert result.value.recommended_tier == Tier.FRUGAL

    def test_cost_savings_tracking(self) -> None:
        """Test total cost savings can be tracked over multiple downgrades."""
        manager = DowngradeManager()
        total_savings = 1.0

        # Frontier -> Standard: 3x savings
        for _ in range(5):
            result = manager.record_success("pattern_1", Tier.FRONTIER)
        if result.value.should_downgrade:
            total_savings *= result.value.cost_savings_factor
            manager.apply_downgrade("pattern_1")

        assert total_savings == 3.0

        # Standard -> Frugal: 10x savings
        for _ in range(5):
            result = manager.record_success("pattern_1", Tier.STANDARD)
        if result.value.should_downgrade:
            total_savings *= result.value.cost_savings_factor

        assert total_savings == 30.0  # 3 * 10


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_empty_pattern_id(self) -> None:
        """Test handling of empty pattern ID."""
        manager = DowngradeManager()

        # Should handle empty string pattern ID
        result = manager.record_success("", Tier.STANDARD)
        assert result.is_ok
        assert result.value.consecutive_successes == 1

    def test_very_long_pattern_id(self) -> None:
        """Test handling of very long pattern ID."""
        manager = DowngradeManager()
        long_pattern = "x" * 10000

        result = manager.record_success(long_pattern, Tier.STANDARD)
        assert result.is_ok

    def test_special_characters_in_pattern(self) -> None:
        """Test handling of special characters in pattern."""
        manager = DowngradeManager()
        special_pattern = "fix bug #123 @user <script>alert('xss')</script>"

        result = manager.record_success(special_pattern, Tier.STANDARD)
        assert result.is_ok

    def test_unicode_in_pattern(self) -> None:
        """Test handling of unicode characters in pattern."""
        manager = DowngradeManager()
        unicode_pattern = "fix bug 버그 수정"

        result = manager.record_success(unicode_pattern, Tier.STANDARD)
        assert result.is_ok

        # Pattern matcher should handle unicode
        matcher = PatternMatcher()
        sim = matcher.calculate_similarity("버그 수정", "버그 수정")
        assert sim == 1.0

    def test_concurrent_pattern_updates(self) -> None:
        """Test multiple rapid updates to same pattern."""
        manager = DowngradeManager()

        # Rapid success/failure cycles
        for _ in range(100):
            manager.record_success("pattern_1", Tier.STANDARD)
            if manager.tracker.get_success_count("pattern_1") >= 3:
                manager.record_failure("pattern_1")

        # Should still be in consistent state
        count = manager.tracker.get_success_count("pattern_1")
        assert 0 <= count <= 2

    def test_exact_threshold_boundary(self) -> None:
        """Test exact behavior at threshold boundary."""
        manager = DowngradeManager()

        # 4 successes: no downgrade
        for _ in range(4):
            result = manager.record_success("pattern_1", Tier.STANDARD)
        assert result.value.should_downgrade is False
        assert result.value.consecutive_successes == 4

        # 5th success: downgrade
        result = manager.record_success("pattern_1", Tier.STANDARD)
        assert result.value.should_downgrade is True
        assert result.value.consecutive_successes == 5


class TestAccessors:
    """Test accessor methods and properties."""

    def test_manager_tracker_accessor(self) -> None:
        """Test DowngradeManager.tracker property."""
        manager = DowngradeManager()
        assert isinstance(manager.tracker, SuccessTracker)

    def test_manager_pattern_matcher_accessor(self) -> None:
        """Test DowngradeManager.pattern_matcher property."""
        manager = DowngradeManager()
        assert isinstance(manager.pattern_matcher, PatternMatcher)

    def test_tracker_get_all_patterns_order(self) -> None:
        """Test get_all_patterns returns patterns in insertion order."""
        tracker = SuccessTracker()

        # Add in specific order
        tracker.record_success("first", Tier.FRUGAL)
        tracker.record_success("second", Tier.STANDARD)
        tracker.record_success("third", Tier.FRONTIER)

        patterns = tracker.get_all_patterns()
        # Dictionary order is preserved in Python 3.7+
        assert patterns == ["first", "second", "third"]
