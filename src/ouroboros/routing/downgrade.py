"""Downgrade management for tier optimization in Ouroboros.

This module implements automatic tier downgrade after sustained success,
enabling continuous cost optimization while maintaining quality.

Key Components:
- SuccessTracker: Tracks consecutive successes per task pattern
- PatternMatcher: Identifies similar task patterns using Jaccard similarity
- DowngradeManager: Coordinates downgrade decisions based on success history

Downgrade Rules:
- 5 consecutive successes trigger a downgrade
- Frontier -> Standard -> Frugal
- Tasks at Frugal tier remain at Frugal
- Similar patterns (>=80% similarity) inherit tier preferences

Usage:
    from ouroboros.routing.downgrade import DowngradeManager, PatternMatcher

    # Create manager
    manager = DowngradeManager()

    # Record success and check for downgrade
    result = manager.record_success("task_pattern_123", Tier.STANDARD)
    if result.is_ok:
        downgrade_result = result.value
        if downgrade_result.should_downgrade:
            new_tier = downgrade_result.recommended_tier
            print(f"Downgrade to {new_tier.value}")

    # Pattern matching for similar tasks
    matcher = PatternMatcher()
    similarity = matcher.calculate_similarity("fix typo in README", "fix typo in docs")
    if similarity >= 0.8:
        # Apply same tier preference
        pass
"""

from dataclasses import dataclass, field
from typing import TypeAlias

from ouroboros.core.types import Result
from ouroboros.observability.logging import get_logger
from ouroboros.routing.tiers import Tier

log = get_logger(__name__)


# Constants
DOWNGRADE_THRESHOLD = 5  # Consecutive successes needed for downgrade
SIMILARITY_THRESHOLD = 0.80  # Minimum similarity for pattern matching

# Type aliases
PatternId: TypeAlias = str


@dataclass
class SuccessTracker:
    """Tracks consecutive successes per task pattern.

    This class maintains state for tracking how many consecutive successful
    completions have occurred for each unique task pattern. It supports
    both recording successes and resetting on failures.

    Attributes:
        _success_counts: Internal dict mapping pattern IDs to consecutive success counts.
        _tier_history: Internal dict mapping pattern IDs to their current tier.

    Example:
        tracker = SuccessTracker()

        # Record successes
        tracker.record_success("pattern_1", Tier.STANDARD)
        tracker.record_success("pattern_1", Tier.STANDARD)

        # Get current count
        count = tracker.get_success_count("pattern_1")  # Returns 2

        # Reset on failure
        tracker.reset_on_failure("pattern_1")
        count = tracker.get_success_count("pattern_1")  # Returns 0
    """

    _success_counts: dict[PatternId, int] = field(default_factory=dict)
    _tier_history: dict[PatternId, Tier] = field(default_factory=dict)

    def record_success(self, pattern_id: PatternId, tier: Tier) -> int:
        """Record a successful completion for a pattern.

        Increments the consecutive success counter for the given pattern
        and updates the tier history.

        Args:
            pattern_id: Unique identifier for the task pattern.
            tier: The tier at which the task was executed.

        Returns:
            The new consecutive success count for this pattern.

        Example:
            tracker = SuccessTracker()
            count = tracker.record_success("pattern_1", Tier.STANDARD)
            # count is now 1
        """
        current_count = self._success_counts.get(pattern_id, 0)
        new_count = current_count + 1
        self._success_counts[pattern_id] = new_count
        self._tier_history[pattern_id] = tier

        log.debug(
            "success_tracker.recorded",
            pattern_id=pattern_id,
            tier=tier.value,
            consecutive_count=new_count,
        )

        return new_count

    def reset_on_failure(self, pattern_id: PatternId) -> None:
        """Reset the success counter for a pattern after a failure.

        Sets the consecutive success count to 0 while preserving tier history.

        Args:
            pattern_id: Unique identifier for the task pattern.

        Example:
            tracker = SuccessTracker()
            tracker.record_success("pattern_1", Tier.STANDARD)
            tracker.reset_on_failure("pattern_1")
            # Success count is now 0
        """
        previous_count = self._success_counts.get(pattern_id, 0)
        self._success_counts[pattern_id] = 0

        log.debug(
            "success_tracker.reset",
            pattern_id=pattern_id,
            previous_count=previous_count,
        )

    def get_success_count(self, pattern_id: PatternId) -> int:
        """Get the current consecutive success count for a pattern.

        Args:
            pattern_id: Unique identifier for the task pattern.

        Returns:
            The consecutive success count, or 0 if pattern not tracked.
        """
        return self._success_counts.get(pattern_id, 0)

    def get_tier(self, pattern_id: PatternId) -> Tier | None:
        """Get the current tier for a pattern.

        Args:
            pattern_id: Unique identifier for the task pattern.

        Returns:
            The tier for this pattern, or None if not tracked.
        """
        return self._tier_history.get(pattern_id)

    def get_all_patterns(self) -> list[PatternId]:
        """Get all tracked pattern IDs.

        Returns:
            List of all pattern IDs being tracked.
        """
        return list(self._success_counts.keys())

    def clear(self) -> None:
        """Clear all tracking state.

        Resets both success counts and tier history.
        """
        self._success_counts.clear()
        self._tier_history.clear()
        log.debug("success_tracker.cleared")


@dataclass(frozen=True, slots=True)
class DowngradeResult:
    """Result of a downgrade evaluation.

    Contains information about whether a downgrade should occur
    and the recommended tier if so.

    Attributes:
        should_downgrade: Whether a downgrade is recommended.
        current_tier: The tier the task was executed at.
        recommended_tier: The recommended tier after evaluation.
        consecutive_successes: Number of consecutive successes that led to this decision.
        cost_savings_factor: Estimated cost savings if downgrade is applied (ratio).

    Example:
        result = DowngradeResult(
            should_downgrade=True,
            current_tier=Tier.STANDARD,
            recommended_tier=Tier.FRUGAL,
            consecutive_successes=5,
            cost_savings_factor=10.0,  # Standard (10x) to Frugal (1x)
        )
    """

    should_downgrade: bool
    current_tier: Tier
    recommended_tier: Tier
    consecutive_successes: int
    cost_savings_factor: float


def _get_lower_tier(tier: Tier) -> Tier:
    """Get the next lower tier.

    Args:
        tier: Current tier.

    Returns:
        The next lower tier, or the same tier if already at Frugal.
    """
    tier_order = [Tier.FRUGAL, Tier.STANDARD, Tier.FRONTIER]
    current_index = tier_order.index(tier)

    if current_index > 0:
        return tier_order[current_index - 1]
    return tier  # Already at Frugal


def _calculate_cost_savings(from_tier: Tier, to_tier: Tier) -> float:
    """Calculate the cost savings factor from downgrading.

    Args:
        from_tier: Original tier.
        to_tier: Target tier.

    Returns:
        Cost savings factor (ratio of cost multipliers).
    """
    if from_tier == to_tier:
        return 1.0

    from_cost = from_tier.cost_multiplier
    to_cost = to_tier.cost_multiplier

    return from_cost / to_cost


class PatternMatcher:
    """Matches similar task patterns using Jaccard similarity.

    This class provides pattern similarity calculation using Jaccard similarity
    on tokenized task descriptions. Similar patterns (>=80% similarity) can
    inherit tier preferences from successful completions.

    For MVP, we use simple word-based tokenization and Jaccard similarity
    instead of embeddings. This provides a fast, interpretable similarity
    measure suitable for initial implementation.

    Jaccard Similarity:
        J(A, B) = |A intersection B| / |A union B|

    Example:
        matcher = PatternMatcher()

        # Check similarity between patterns
        sim = matcher.calculate_similarity(
            "fix typo in README",
            "fix typo in documentation",
        )
        # sim ~= 0.4 (shares "fix", "typo", "in")

        # Find similar patterns
        patterns = ["fix typo", "add feature", "fix bug"]
        similar = matcher.find_similar_patterns("fix issue", patterns)
        # Returns patterns with similarity >= 0.8
    """

    def __init__(self, similarity_threshold: float = SIMILARITY_THRESHOLD) -> None:
        """Initialize the pattern matcher.

        Args:
            similarity_threshold: Minimum similarity for patterns to be considered similar.
                Defaults to 0.80 (80%).
        """
        self._similarity_threshold = similarity_threshold

    @property
    def similarity_threshold(self) -> float:
        """Get the similarity threshold."""
        return self._similarity_threshold

    def _tokenize(self, text: str) -> set[str]:
        """Tokenize a text string into a set of lowercase words.

        Args:
            text: The text to tokenize.

        Returns:
            Set of lowercase word tokens.
        """
        # Simple word tokenization: split on whitespace and punctuation
        # Convert to lowercase for case-insensitive matching
        words = text.lower().split()
        # Remove common punctuation from tokens
        cleaned_words = set()
        for word in words:
            # Strip punctuation from start and end
            cleaned = word.strip(".,;:!?\"'()-[]{}/<>")
            if cleaned:  # Only add non-empty tokens
                cleaned_words.add(cleaned)
        return cleaned_words

    def calculate_similarity(self, pattern_a: str, pattern_b: str) -> float:
        """Calculate Jaccard similarity between two patterns.

        Jaccard similarity is defined as:
            J(A, B) = |A intersection B| / |A union B|

        Args:
            pattern_a: First pattern string.
            pattern_b: Second pattern string.

        Returns:
            Similarity score between 0.0 and 1.0.

        Example:
            matcher = PatternMatcher()
            sim = matcher.calculate_similarity("fix bug", "fix typo")
            # sim = 1/3 (intersection={fix}, union={fix, bug, typo})
        """
        tokens_a = self._tokenize(pattern_a)
        tokens_b = self._tokenize(pattern_b)

        if not tokens_a and not tokens_b:
            return 1.0  # Both empty = identical

        if not tokens_a or not tokens_b:
            return 0.0  # One empty, one not = no similarity

        intersection = tokens_a & tokens_b
        union = tokens_a | tokens_b

        similarity = len(intersection) / len(union)

        log.debug(
            "pattern_matcher.similarity_calculated",
            pattern_a=pattern_a[:50],  # Truncate for logging
            pattern_b=pattern_b[:50],
            similarity=similarity,
            intersection_size=len(intersection),
            union_size=len(union),
        )

        return similarity

    def is_similar(self, pattern_a: str, pattern_b: str) -> bool:
        """Check if two patterns are similar based on threshold.

        Args:
            pattern_a: First pattern string.
            pattern_b: Second pattern string.

        Returns:
            True if similarity >= threshold.
        """
        return self.calculate_similarity(pattern_a, pattern_b) >= self._similarity_threshold

    def find_similar_patterns(
        self,
        target_pattern: str,
        candidate_patterns: list[str],
    ) -> list[tuple[str, float]]:
        """Find all candidate patterns similar to the target.

        Args:
            target_pattern: The pattern to match against.
            candidate_patterns: List of patterns to compare.

        Returns:
            List of (pattern, similarity) tuples for patterns meeting threshold,
            sorted by similarity in descending order.
        """
        similar: list[tuple[str, float]] = []

        for candidate in candidate_patterns:
            similarity = self.calculate_similarity(target_pattern, candidate)
            if similarity >= self._similarity_threshold:
                similar.append((candidate, similarity))

        # Sort by similarity descending
        similar.sort(key=lambda x: x[1], reverse=True)

        if similar:
            log.debug(
                "pattern_matcher.similar_patterns_found",
                target_pattern=target_pattern[:50],
                match_count=len(similar),
                best_match_similarity=similar[0][1] if similar else 0.0,
            )

        return similar


@dataclass
class DowngradeManager:
    """Manages tier downgrade decisions based on success history.

    The DowngradeManager coordinates success tracking and pattern matching
    to determine when tasks should be downgraded to a lower (cheaper) tier.

    Downgrade Rules:
    - 5 consecutive successes at a tier trigger a downgrade evaluation
    - Tiers downgrade: Frontier -> Standard -> Frugal
    - Frugal tier tasks cannot be downgraded further
    - Similar patterns inherit tier preferences

    Example:
        manager = DowngradeManager()

        # Record successes
        for i in range(5):
            result = manager.record_success("pattern_1", Tier.STANDARD)

        # After 5 successes, should_downgrade will be True
        assert result.is_ok
        assert result.value.should_downgrade
        assert result.value.recommended_tier == Tier.FRUGAL

        # Record failure resets the counter
        manager.record_failure("pattern_1")
    """

    _tracker: SuccessTracker = field(default_factory=SuccessTracker)
    _pattern_matcher: PatternMatcher = field(default_factory=PatternMatcher)
    _downgrade_threshold: int = DOWNGRADE_THRESHOLD

    @property
    def downgrade_threshold(self) -> int:
        """Get the downgrade threshold (consecutive successes needed)."""
        return self._downgrade_threshold

    @property
    def tracker(self) -> SuccessTracker:
        """Get the internal success tracker."""
        return self._tracker

    @property
    def pattern_matcher(self) -> PatternMatcher:
        """Get the internal pattern matcher."""
        return self._pattern_matcher

    def record_success(
        self,
        pattern_id: PatternId,
        tier: Tier,
    ) -> Result[DowngradeResult, None]:
        """Record a successful task completion and evaluate for downgrade.

        Increments the success counter for the pattern and checks if the
        threshold for downgrade has been met.

        Args:
            pattern_id: Unique identifier for the task pattern.
            tier: The tier at which the task was executed.

        Returns:
            Result containing DowngradeResult with the evaluation.
            Always succeeds (Result.ok) with downgrade information.

        Example:
            manager = DowngradeManager()

            # Record first success - no downgrade
            result = manager.record_success("pattern_1", Tier.FRONTIER)
            assert not result.value.should_downgrade

            # After 5 successes, downgrade recommended
            for _ in range(4):
                result = manager.record_success("pattern_1", Tier.FRONTIER)
            assert result.value.should_downgrade
        """
        # Record the success
        success_count = self._tracker.record_success(pattern_id, tier)

        # Check if downgrade threshold met
        should_downgrade = (
            success_count >= self._downgrade_threshold and tier != Tier.FRUGAL
        )

        # Determine recommended tier
        if should_downgrade:
            recommended_tier = _get_lower_tier(tier)
            cost_savings = _calculate_cost_savings(tier, recommended_tier)

            log.info(
                "downgrade.recommended",
                pattern_id=pattern_id,
                current_tier=tier.value,
                recommended_tier=recommended_tier.value,
                consecutive_successes=success_count,
                cost_savings_factor=cost_savings,
            )
        else:
            recommended_tier = tier
            cost_savings = 1.0

            log.debug(
                "downgrade.not_ready",
                pattern_id=pattern_id,
                tier=tier.value,
                consecutive_successes=success_count,
                threshold=self._downgrade_threshold,
            )

        downgrade_result = DowngradeResult(
            should_downgrade=should_downgrade,
            current_tier=tier,
            recommended_tier=recommended_tier,
            consecutive_successes=success_count,
            cost_savings_factor=cost_savings,
        )

        return Result.ok(downgrade_result)

    def record_failure(self, pattern_id: PatternId) -> None:
        """Record a failed task completion.

        Resets the consecutive success counter for the pattern.

        Args:
            pattern_id: Unique identifier for the task pattern.

        Example:
            manager = DowngradeManager()

            # Record some successes
            manager.record_success("pattern_1", Tier.STANDARD)
            manager.record_success("pattern_1", Tier.STANDARD)

            # Failure resets the counter
            manager.record_failure("pattern_1")
            # Success count is now 0
        """
        self._tracker.reset_on_failure(pattern_id)

        log.info(
            "downgrade.failure_recorded",
            pattern_id=pattern_id,
        )

    def get_recommended_tier_for_pattern(
        self,
        pattern_description: str,
        default_tier: Tier = Tier.FRUGAL,
    ) -> Tier:
        """Get the recommended tier for a pattern based on similar patterns.

        Looks for similar tracked patterns and returns the tier of the most
        similar successful pattern, allowing new tasks to benefit from
        learned tier preferences.

        Args:
            pattern_description: Description of the task pattern.
            default_tier: Tier to return if no similar patterns found.
                Defaults to FRUGAL (optimistic for cost savings).

        Returns:
            The recommended tier based on similar patterns.

        Example:
            manager = DowngradeManager()

            # After tracking "fix typo in README" at Frugal tier
            manager.record_success("fix typo in README", Tier.FRUGAL)

            # Similar pattern gets same tier recommendation
            tier = manager.get_recommended_tier_for_pattern("fix typo in docs")
            # tier == Tier.FRUGAL (if similarity >= 80%)
        """
        tracked_patterns = self._tracker.get_all_patterns()

        if not tracked_patterns:
            log.debug(
                "downgrade.no_patterns_tracked",
                pattern_description=pattern_description[:50],
                default_tier=default_tier.value,
            )
            return default_tier

        # Find similar patterns
        similar_patterns = self._pattern_matcher.find_similar_patterns(
            pattern_description,
            tracked_patterns,
        )

        if not similar_patterns:
            log.debug(
                "downgrade.no_similar_patterns",
                pattern_description=pattern_description[:50],
                default_tier=default_tier.value,
            )
            return default_tier

        # Get the tier of the most similar pattern
        best_match, best_similarity = similar_patterns[0]
        matched_tier = self._tracker.get_tier(best_match)

        if matched_tier is None:
            return default_tier

        log.info(
            "downgrade.tier_inherited",
            pattern_description=pattern_description[:50],
            matched_pattern=best_match[:50],
            similarity=best_similarity,
            inherited_tier=matched_tier.value,
        )

        return matched_tier

    def apply_downgrade(self, pattern_id: PatternId) -> None:
        """Apply a downgrade by resetting the success counter.

        After a downgrade is applied, the success counter is reset
        to allow the pattern to be evaluated again at the new tier.

        Args:
            pattern_id: Unique identifier for the task pattern.
        """
        current_tier = self._tracker.get_tier(pattern_id)
        self._tracker._success_counts[pattern_id] = 0

        if current_tier:
            new_tier = _get_lower_tier(current_tier)
            self._tracker._tier_history[pattern_id] = new_tier

            log.info(
                "downgrade.applied",
                pattern_id=pattern_id,
                from_tier=current_tier.value,
                to_tier=new_tier.value,
            )

    def get_cost_savings_estimate(self, pattern_id: PatternId) -> float:
        """Estimate cost savings if downgrade is applied.

        Args:
            pattern_id: Unique identifier for the task pattern.

        Returns:
            Cost savings factor (>1.0 means savings).
        """
        current_tier = self._tracker.get_tier(pattern_id)
        if current_tier is None or current_tier == Tier.FRUGAL:
            return 1.0

        new_tier = _get_lower_tier(current_tier)
        return _calculate_cost_savings(current_tier, new_tier)

    def clear(self) -> None:
        """Clear all tracking state."""
        self._tracker.clear()
        log.info("downgrade.manager_cleared")
