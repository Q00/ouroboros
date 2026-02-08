"""Tests for Pattern Analyzer."""

import pytest
from datetime import datetime, timedelta

from ouroboros.gemini3.pattern_analyzer import (
    PatternAnalyzer,
    FailurePattern,
    PatternCategory,
    PatternSeverity,
    PatternCluster,
    PatternNetwork,
)
from ouroboros.gemini3.convergence_accelerator import (
    IterationData,
    IterationOutcome,
)


@pytest.fixture
def analyzer() -> PatternAnalyzer:
    """Create a test analyzer instance."""
    return PatternAnalyzer()


@pytest.fixture
def sample_failures() -> list[IterationData]:
    """Create sample failure iterations for testing."""
    base_time = datetime.now()
    failures = []

    # Spinning pattern: Same ImportError 3 times
    for i in range(3):
        failures.append(
            IterationData(
                iteration_id=f"spin_{i}",
                ac_id="AC_1",
                execution_id="test_exec",
                timestamp=base_time + timedelta(minutes=i),
                outcome=IterationOutcome.FAILURE,
                error_message="ImportError: No module named 'utils'",
            )
        )

    # Add some other failures
    failures.append(
        IterationData(
            iteration_id="other_1",
            ac_id="AC_2",
            execution_id="test_exec",
            timestamp=base_time + timedelta(minutes=5),
            outcome=IterationOutcome.FAILURE,
            error_message="TypeError: expected str, got int",
        )
    )

    # Add blocked iteration
    failures.append(
        IterationData(
            iteration_id="blocked_1",
            ac_id="AC_3",
            execution_id="test_exec",
            timestamp=base_time + timedelta(minutes=6),
            outcome=IterationOutcome.BLOCKED,
            error_message="Blocked by AC_1",
        )
    )

    return failures


class TestPatternAnalyzer:
    """Tests for PatternAnalyzer."""

    @pytest.mark.asyncio
    async def test_analyze_patterns_insufficient_data(
        self,
        analyzer: PatternAnalyzer,
    ) -> None:
        """Should return empty for insufficient iterations."""
        iterations = [
            IterationData(
                iteration_id="iter_1",
                ac_id="AC_1",
                execution_id="test_exec",
                timestamp=datetime.now(),
                outcome=IterationOutcome.FAILURE,
            )
        ]

        patterns = await analyzer.analyze_patterns(iterations)
        assert len(patterns) == 0

    @pytest.mark.asyncio
    async def test_analyze_patterns_no_failures(
        self,
        analyzer: PatternAnalyzer,
    ) -> None:
        """Should return empty when no failures."""
        iterations = [
            IterationData(
                iteration_id=f"iter_{i}",
                ac_id="AC_1",
                execution_id="test_exec",
                timestamp=datetime.now() + timedelta(minutes=i),
                outcome=IterationOutcome.SUCCESS,
            )
            for i in range(10)
        ]

        patterns = await analyzer.analyze_patterns(iterations)
        assert len(patterns) == 0

    @pytest.mark.asyncio
    async def test_detect_spinning_pattern(
        self,
        analyzer: PatternAnalyzer,
        sample_failures: list[IterationData],
    ) -> None:
        """Should detect spinning pattern."""
        patterns = await analyzer.analyze_patterns(sample_failures)

        spinning_patterns = [
            p for p in patterns if p.category == PatternCategory.SPINNING
        ]

        assert len(spinning_patterns) >= 1

    @pytest.mark.asyncio
    async def test_detect_error_patterns(
        self,
        analyzer: PatternAnalyzer,
        sample_failures: list[IterationData],
    ) -> None:
        """Should detect error signature patterns."""
        patterns = await analyzer.analyze_patterns(sample_failures)

        # Should find import_error pattern
        import_patterns = [
            p for p in patterns if "import" in p.error_signature.lower()
        ]

        assert len(import_patterns) >= 1

    @pytest.mark.asyncio
    async def test_cluster_patterns(
        self,
        analyzer: PatternAnalyzer,
        sample_failures: list[IterationData],
    ) -> None:
        """Should cluster similar patterns."""
        await analyzer.analyze_patterns(sample_failures)
        clusters = await analyzer.cluster_patterns()

        assert len(clusters) > 0
        assert all(isinstance(c, PatternCluster) for c in clusters)

    def test_build_pattern_network_empty(
        self,
        analyzer: PatternAnalyzer,
    ) -> None:
        """Should return empty network when no patterns."""
        network = analyzer.build_pattern_network()

        assert len(network.nodes) == 0
        assert len(network.edges) == 0

    @pytest.mark.asyncio
    async def test_build_pattern_network(
        self,
        analyzer: PatternAnalyzer,
        sample_failures: list[IterationData],
    ) -> None:
        """Should build pattern network from detected patterns."""
        await analyzer.analyze_patterns(sample_failures)
        network = analyzer.build_pattern_network()

        assert len(network.nodes) > 0
        # Network dict should be serializable
        network_dict = network.to_dict()
        assert "nodes" in network_dict
        assert "edges" in network_dict

    @pytest.mark.asyncio
    async def test_get_summary(
        self,
        analyzer: PatternAnalyzer,
        sample_failures: list[IterationData],
    ) -> None:
        """Should generate pattern summary."""
        await analyzer.analyze_patterns(sample_failures)
        summary = analyzer.get_summary()

        assert "total_patterns" in summary
        assert "patterns_by_category" in summary
        assert "patterns_by_severity" in summary
        assert "top_patterns" in summary

    @pytest.mark.asyncio
    async def test_socratic_questions_added(
        self,
        analyzer: PatternAnalyzer,
        sample_failures: list[IterationData],
    ) -> None:
        """Should add Socratic questions to patterns."""
        patterns = await analyzer.analyze_patterns(sample_failures)

        # All patterns should have questions
        patterns_with_questions = [
            p for p in patterns if len(p.socratic_questions) > 0
        ]

        assert len(patterns_with_questions) == len(patterns)


class TestFailurePattern:
    """Tests for FailurePattern model."""

    def test_to_dict(self) -> None:
        """Should convert to dictionary."""
        pattern = FailurePattern(
            pattern_id="test_pattern",
            category=PatternCategory.SPINNING,
            severity=PatternSeverity.HIGH,
            description="Test pattern",
            occurrence_count=5,
            confidence=0.9,
        )

        pattern_dict = pattern.to_dict()

        assert pattern_dict["pattern_id"] == "test_pattern"
        assert pattern_dict["category"] == "spinning"
        assert pattern_dict["severity"] == "high"
        assert pattern_dict["occurrence_count"] == 5


class TestPatternCategories:
    """Tests for pattern categories."""

    def test_all_categories_exist(self) -> None:
        """All expected categories should exist."""
        expected = [
            "spinning",
            "oscillation",
            "dependency",
            "root_cause",
            "symptom",
            "stagnation",
            "regression",
            "complexity",
            "ambiguity",
        ]

        for cat_name in expected:
            assert PatternCategory(cat_name) is not None

    def test_all_severities_exist(self) -> None:
        """All expected severities should exist."""
        expected = ["critical", "high", "medium", "low"]

        for sev_name in expected:
            assert PatternSeverity(sev_name) is not None
