"""Unit tests for complexity estimation.

Tests cover:
- TaskContext dataclass validation
- ComplexityScore dataclass
- Complexity estimation with various inputs
- Weight factor calculations
- Normalization of input values
- Error handling for invalid inputs
"""

import pytest

from ouroboros.core.errors import ValidationError
from ouroboros.routing.complexity import (
    MAX_DEPTH_THRESHOLD,
    MAX_TOKEN_THRESHOLD,
    MAX_TOOL_THRESHOLD,
    WEIGHT_AC_DEPTH,
    WEIGHT_TOKEN_COUNT,
    WEIGHT_TOOL_DEPENDENCIES,
    ComplexityScore,
    TaskContext,
    estimate_complexity,
)


class TestTaskContext:
    """Test the TaskContext dataclass."""

    def test_default_values(self) -> None:
        """Test TaskContext has correct default values."""
        context = TaskContext()
        assert context.token_count == 0
        assert context.tool_dependencies == []
        assert context.ac_depth == 0

    def test_create_with_all_fields(self) -> None:
        """Test creating TaskContext with all fields specified."""
        tools = ["git", "npm", "docker"]
        context = TaskContext(
            token_count=1500,
            tool_dependencies=tools,
            ac_depth=3,
        )
        assert context.token_count == 1500
        assert context.tool_dependencies == tools
        assert context.ac_depth == 3

    def test_immutable(self) -> None:
        """Test TaskContext is immutable (frozen dataclass)."""
        context = TaskContext(token_count=100)
        with pytest.raises(AttributeError):
            context.token_count = 200  # type: ignore[misc]

    def test_independent_tool_lists(self) -> None:
        """Test that tool_dependencies default factory creates independent lists."""
        context1 = TaskContext()
        context2 = TaskContext()
        assert context1.tool_dependencies is not context2.tool_dependencies


class TestComplexityScore:
    """Test the ComplexityScore dataclass."""

    def test_create_score(self) -> None:
        """Test creating a ComplexityScore."""
        breakdown = {
            "token_score": 0.5,
            "tool_score": 0.4,
            "depth_score": 0.6,
            "weighted_token": 0.15,
            "weighted_tool": 0.12,
            "weighted_depth": 0.24,
        }
        score = ComplexityScore(score=0.51, breakdown=breakdown)
        assert score.score == 0.51
        assert score.breakdown == breakdown

    def test_immutable(self) -> None:
        """Test ComplexityScore is immutable."""
        score = ComplexityScore(score=0.5, breakdown={})
        with pytest.raises(AttributeError):
            score.score = 0.6  # type: ignore[misc]


class TestWeightConstants:
    """Test weight constant values."""

    def test_weights_sum_to_one(self) -> None:
        """Test that all weights sum to 1.0."""
        total_weight = WEIGHT_TOKEN_COUNT + WEIGHT_TOOL_DEPENDENCIES + WEIGHT_AC_DEPTH
        assert total_weight == pytest.approx(1.0)

    def test_weight_values(self) -> None:
        """Test individual weight values match specification."""
        assert WEIGHT_TOKEN_COUNT == 0.30  # 30%
        assert WEIGHT_TOOL_DEPENDENCIES == 0.30  # 30%
        assert WEIGHT_AC_DEPTH == 0.40  # 40%


class TestEstimateComplexityBasic:
    """Test basic complexity estimation scenarios."""

    def test_zero_complexity(self) -> None:
        """Test that empty context returns zero complexity."""
        context = TaskContext()
        result = estimate_complexity(context)

        assert result.is_ok
        score = result.value
        assert score.score == 0.0
        assert score.breakdown["token_score"] == 0.0
        assert score.breakdown["tool_score"] == 0.0
        assert score.breakdown["depth_score"] == 0.0

    def test_maximum_complexity(self) -> None:
        """Test that maximum inputs return maximum complexity (1.0)."""
        context = TaskContext(
            token_count=MAX_TOKEN_THRESHOLD,
            tool_dependencies=["t1", "t2", "t3", "t4", "t5"],  # MAX_TOOL_THRESHOLD
            ac_depth=MAX_DEPTH_THRESHOLD,
        )
        result = estimate_complexity(context)

        assert result.is_ok
        score = result.value
        assert score.score == pytest.approx(1.0)
        assert score.breakdown["token_score"] == pytest.approx(1.0)
        assert score.breakdown["tool_score"] == pytest.approx(1.0)
        assert score.breakdown["depth_score"] == pytest.approx(1.0)

    def test_exceeds_maximum_caps_at_one(self) -> None:
        """Test that values exceeding thresholds cap at 1.0 contribution."""
        context = TaskContext(
            token_count=MAX_TOKEN_THRESHOLD * 2,
            tool_dependencies=["t1", "t2", "t3", "t4", "t5", "t6", "t7"],
            ac_depth=MAX_DEPTH_THRESHOLD + 10,
        )
        result = estimate_complexity(context)

        assert result.is_ok
        score = result.value
        # Score should not exceed 1.0
        assert score.score == pytest.approx(1.0)
        assert score.breakdown["token_score"] == 1.0
        assert score.breakdown["tool_score"] == 1.0
        assert score.breakdown["depth_score"] == 1.0


class TestEstimateComplexityIndividualFactors:
    """Test individual factor contributions to complexity."""

    def test_token_count_only(self) -> None:
        """Test complexity with only token count."""
        # 50% of max tokens
        token_count = MAX_TOKEN_THRESHOLD // 2
        context = TaskContext(token_count=token_count)
        result = estimate_complexity(context)

        assert result.is_ok
        score = result.value
        expected_token_score = token_count / MAX_TOKEN_THRESHOLD
        expected_total = expected_token_score * WEIGHT_TOKEN_COUNT

        assert score.breakdown["token_score"] == pytest.approx(expected_token_score)
        assert score.breakdown["tool_score"] == 0.0
        assert score.breakdown["depth_score"] == 0.0
        assert score.score == pytest.approx(expected_total)

    def test_tool_dependencies_only(self) -> None:
        """Test complexity with only tool dependencies."""
        # 60% of max tools (3 out of 5)
        tools = ["git", "npm", "docker"]
        context = TaskContext(tool_dependencies=tools)
        result = estimate_complexity(context)

        assert result.is_ok
        score = result.value
        expected_tool_score = len(tools) / MAX_TOOL_THRESHOLD
        expected_total = expected_tool_score * WEIGHT_TOOL_DEPENDENCIES

        assert score.breakdown["tool_score"] == pytest.approx(expected_tool_score)
        assert score.breakdown["token_score"] == 0.0
        assert score.breakdown["depth_score"] == 0.0
        assert score.score == pytest.approx(expected_total)

    def test_ac_depth_only(self) -> None:
        """Test complexity with only AC depth."""
        # 40% of max depth (2 out of 5)
        ac_depth = 2
        context = TaskContext(ac_depth=ac_depth)
        result = estimate_complexity(context)

        assert result.is_ok
        score = result.value
        expected_depth_score = ac_depth / MAX_DEPTH_THRESHOLD
        expected_total = expected_depth_score * WEIGHT_AC_DEPTH

        assert score.breakdown["depth_score"] == pytest.approx(expected_depth_score)
        assert score.breakdown["token_score"] == 0.0
        assert score.breakdown["tool_score"] == 0.0
        assert score.score == pytest.approx(expected_total)


class TestEstimateComplexityWeightedCombinations:
    """Test weighted combinations of factors."""

    def test_balanced_medium_complexity(self) -> None:
        """Test medium complexity with balanced factors."""
        # 50% of each factor
        context = TaskContext(
            token_count=MAX_TOKEN_THRESHOLD // 2,
            tool_dependencies=["git", "npm"],  # ~40% of max (2/5)
            ac_depth=MAX_DEPTH_THRESHOLD // 2,  # 50% of max
        )
        result = estimate_complexity(context)

        assert result.is_ok
        score = result.value
        # Score should be around 0.5 (medium complexity)
        assert 0.3 < score.score < 0.7

    def test_high_token_low_other_factors(self) -> None:
        """Test high token count with low other factors."""
        context = TaskContext(
            token_count=MAX_TOKEN_THRESHOLD,  # 100%
            tool_dependencies=[],  # 0%
            ac_depth=0,  # 0%
        )
        result = estimate_complexity(context)

        assert result.is_ok
        score = result.value
        # Only token weight contributes (30%)
        assert score.score == pytest.approx(WEIGHT_TOKEN_COUNT)

    def test_low_token_high_other_factors(self) -> None:
        """Test low token count with high other factors."""
        context = TaskContext(
            token_count=0,  # 0%
            tool_dependencies=["t1", "t2", "t3", "t4", "t5"],  # 100%
            ac_depth=MAX_DEPTH_THRESHOLD,  # 100%
        )
        result = estimate_complexity(context)

        assert result.is_ok
        score = result.value
        # Tool (30%) + Depth (40%) weights contribute
        expected = WEIGHT_TOOL_DEPENDENCIES + WEIGHT_AC_DEPTH
        assert score.score == pytest.approx(expected)

    def test_breakdown_weighted_values(self) -> None:
        """Test that breakdown includes both raw and weighted values."""
        context = TaskContext(
            token_count=2000,  # 50% of 4000
            tool_dependencies=["git", "npm"],  # 40% of 5
            ac_depth=3,  # 60% of 5
        )
        result = estimate_complexity(context)

        assert result.is_ok
        score = result.value
        breakdown = score.breakdown

        # Verify breakdown has all required keys
        assert "token_score" in breakdown
        assert "tool_score" in breakdown
        assert "depth_score" in breakdown
        assert "weighted_token" in breakdown
        assert "weighted_tool" in breakdown
        assert "weighted_depth" in breakdown

        # Verify weighted = raw * weight
        assert breakdown["weighted_token"] == pytest.approx(
            breakdown["token_score"] * WEIGHT_TOKEN_COUNT
        )
        assert breakdown["weighted_tool"] == pytest.approx(
            breakdown["tool_score"] * WEIGHT_TOOL_DEPENDENCIES
        )
        assert breakdown["weighted_depth"] == pytest.approx(
            breakdown["depth_score"] * WEIGHT_AC_DEPTH
        )


class TestEstimateComplexityEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_exactly_at_threshold_values(self) -> None:
        """Test inputs exactly at threshold values."""
        context = TaskContext(
            token_count=MAX_TOKEN_THRESHOLD,
            tool_dependencies=["t1", "t2", "t3", "t4", "t5"],
            ac_depth=MAX_DEPTH_THRESHOLD,
        )
        result = estimate_complexity(context)

        assert result.is_ok
        score = result.value
        assert score.score == pytest.approx(1.0)

    def test_one_below_threshold(self) -> None:
        """Test inputs one below threshold values."""
        context = TaskContext(
            token_count=MAX_TOKEN_THRESHOLD - 1,
            tool_dependencies=["t1", "t2", "t3", "t4"],  # One below max (4/5 = 0.8)
            ac_depth=MAX_DEPTH_THRESHOLD - 1,  # One below max (4/5 = 0.8)
        )
        result = estimate_complexity(context)

        assert result.is_ok
        score = result.value
        # Token: ~1.0 * 0.3 = 0.3
        # Tools: 0.8 * 0.3 = 0.24
        # Depth: 0.8 * 0.4 = 0.32
        # Total: ~0.86
        assert score.score < 1.0
        assert score.score > 0.8  # Adjusted to match actual calculation

    def test_single_token(self) -> None:
        """Test with single token."""
        context = TaskContext(token_count=1)
        result = estimate_complexity(context)

        assert result.is_ok
        score = result.value
        expected_token_score = 1 / MAX_TOKEN_THRESHOLD
        assert score.breakdown["token_score"] == pytest.approx(expected_token_score)

    def test_single_tool(self) -> None:
        """Test with single tool dependency."""
        context = TaskContext(tool_dependencies=["git"])
        result = estimate_complexity(context)

        assert result.is_ok
        score = result.value
        expected_tool_score = 1 / MAX_TOOL_THRESHOLD
        assert score.breakdown["tool_score"] == pytest.approx(expected_tool_score)

    def test_depth_of_one(self) -> None:
        """Test with AC depth of 1."""
        context = TaskContext(ac_depth=1)
        result = estimate_complexity(context)

        assert result.is_ok
        score = result.value
        expected_depth_score = 1 / MAX_DEPTH_THRESHOLD
        assert score.breakdown["depth_score"] == pytest.approx(expected_depth_score)

    def test_empty_tool_list(self) -> None:
        """Test with explicitly empty tool list."""
        context = TaskContext(tool_dependencies=[])
        result = estimate_complexity(context)

        assert result.is_ok
        assert result.value.breakdown["tool_score"] == 0.0

    def test_duplicate_tools_count_separately(self) -> None:
        """Test that duplicate tools in the list count separately."""
        # If user passes duplicates, they contribute to complexity
        context = TaskContext(tool_dependencies=["git", "git", "npm"])
        result = estimate_complexity(context)

        assert result.is_ok
        score = result.value
        # 3 items in list, even though 2 unique
        expected_tool_score = 3 / MAX_TOOL_THRESHOLD
        assert score.breakdown["tool_score"] == pytest.approx(expected_tool_score)


class TestEstimateComplexityValidation:
    """Test input validation for complexity estimation."""

    def test_negative_token_count_error(self) -> None:
        """Test that negative token count returns error."""
        context = TaskContext(token_count=-100)
        result = estimate_complexity(context)

        assert result.is_err
        error = result.error
        assert isinstance(error, ValidationError)
        assert "non-negative" in error.message.lower()
        assert error.field == "token_count"
        assert error.value == -100

    def test_negative_ac_depth_error(self) -> None:
        """Test that negative AC depth returns error."""
        context = TaskContext(ac_depth=-1)
        result = estimate_complexity(context)

        assert result.is_err
        error = result.error
        assert isinstance(error, ValidationError)
        assert "non-negative" in error.message.lower()
        assert error.field == "ac_depth"
        assert error.value == -1


class TestEstimateComplexityRoutingThresholds:
    """Test complexity scores against routing thresholds."""

    def test_frugal_tier_range(self) -> None:
        """Test inputs that should route to Frugal tier (< 0.4)."""
        # Very simple task
        context = TaskContext(
            token_count=500,  # ~12.5% of max
            tool_dependencies=["git"],  # 20% of max
            ac_depth=1,  # 20% of max
        )
        result = estimate_complexity(context)

        assert result.is_ok
        score = result.value
        assert score.score < 0.4

    def test_standard_tier_range(self) -> None:
        """Test inputs that should route to Standard tier (0.4-0.7)."""
        # Medium complexity task
        context = TaskContext(
            token_count=2000,  # 50% of max
            tool_dependencies=["git", "npm"],  # 40% of max
            ac_depth=3,  # 60% of max
        )
        result = estimate_complexity(context)

        assert result.is_ok
        score = result.value
        assert 0.4 <= score.score <= 0.7

    def test_frontier_tier_range(self) -> None:
        """Test inputs that should route to Frontier tier (> 0.7)."""
        # Complex task
        context = TaskContext(
            token_count=3500,  # ~87.5% of max
            tool_dependencies=["git", "npm", "docker", "aws"],  # 80% of max
            ac_depth=4,  # 80% of max
        )
        result = estimate_complexity(context)

        assert result.is_ok
        score = result.value
        assert score.score > 0.7
