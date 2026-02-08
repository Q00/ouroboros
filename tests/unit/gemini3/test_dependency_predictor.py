"""Tests for Dependency Predictor."""

import pytest
from datetime import datetime, timedelta

from ouroboros.gemini3.dependency_predictor import (
    DependencyPredictor,
    ACDependency,
    BlockingPrediction,
    DependencyType,
    DependencyStrength,
    DependencyTreeNode,
)
from ouroboros.gemini3.convergence_accelerator import (
    IterationData,
    IterationOutcome,
)


@pytest.fixture
def predictor() -> DependencyPredictor:
    """Create a test predictor instance."""
    return DependencyPredictor()


@pytest.fixture
def sample_iterations() -> list[IterationData]:
    """Create sample iterations with dependencies."""
    base_time = datetime.now()
    iterations = []

    # AC_1 succeeds early
    iterations.append(
        IterationData(
            iteration_id="iter_1",
            ac_id="AC_1",
            execution_id="test_exec",
            timestamp=base_time,
            outcome=IterationOutcome.SUCCESS,
        )
    )

    # AC_2 blocked by AC_1 initially, then succeeds
    iterations.append(
        IterationData(
            iteration_id="iter_2",
            ac_id="AC_2",
            execution_id="test_exec",
            timestamp=base_time + timedelta(minutes=1),
            outcome=IterationOutcome.BLOCKED,
            error_message="Blocked by AC_1",
        )
    )

    iterations.append(
        IterationData(
            iteration_id="iter_3",
            ac_id="AC_2",
            execution_id="test_exec",
            timestamp=base_time + timedelta(minutes=2),
            outcome=IterationOutcome.SUCCESS,
        )
    )

    # AC_3 fails with reference to AC_2
    iterations.append(
        IterationData(
            iteration_id="iter_4",
            ac_id="AC_3",
            execution_id="test_exec",
            timestamp=base_time + timedelta(minutes=3),
            outcome=IterationOutcome.FAILURE,
            error_message="Missing dependency from AC_2",
        )
    )

    return iterations


class TestDependencyPredictor:
    """Tests for DependencyPredictor."""

    @pytest.mark.asyncio
    async def test_predict_dependencies_basic(
        self,
        predictor: DependencyPredictor,
        sample_iterations: list[IterationData],
    ) -> None:
        """Should predict basic dependencies."""
        dependencies = await predictor.predict_dependencies(
            sample_iterations,
            ac_ids=["AC_1", "AC_2", "AC_3"],
        )

        assert len(dependencies) > 0
        assert all(isinstance(d, ACDependency) for d in dependencies)

    @pytest.mark.asyncio
    async def test_detect_blocked_dependencies(
        self,
        predictor: DependencyPredictor,
        sample_iterations: list[IterationData],
    ) -> None:
        """Should detect explicit blocked dependencies."""
        dependencies = await predictor.predict_dependencies(
            sample_iterations,
            ac_ids=["AC_1", "AC_2", "AC_3"],
        )

        # Should find AC_2 depends on AC_1
        ac2_deps = [d for d in dependencies if d.source_ac == "AC_2"]
        assert len(ac2_deps) > 0

    @pytest.mark.asyncio
    async def test_get_blockers(
        self,
        predictor: DependencyPredictor,
        sample_iterations: list[IterationData],
    ) -> None:
        """Should return blocking prediction."""
        await predictor.predict_dependencies(
            sample_iterations,
            ac_ids=["AC_1", "AC_2", "AC_3"],
        )

        prediction = predictor.get_blockers("AC_2")

        assert isinstance(prediction, BlockingPrediction)
        assert prediction.blocked_ac == "AC_2"

    @pytest.mark.asyncio
    async def test_get_execution_order(
        self,
        predictor: DependencyPredictor,
        sample_iterations: list[IterationData],
    ) -> None:
        """Should return topologically sorted execution order."""
        await predictor.predict_dependencies(
            sample_iterations,
            ac_ids=["AC_1", "AC_2", "AC_3"],
        )

        order = predictor.get_execution_order()

        assert len(order) == 3
        assert "AC_1" in order
        assert "AC_2" in order
        assert "AC_3" in order

    @pytest.mark.asyncio
    async def test_get_critical_path(
        self,
        predictor: DependencyPredictor,
        sample_iterations: list[IterationData],
    ) -> None:
        """Should return critical path."""
        await predictor.predict_dependencies(
            sample_iterations,
            ac_ids=["AC_1", "AC_2", "AC_3"],
        )

        critical_path = predictor.get_critical_path()

        assert len(critical_path) > 0

    @pytest.mark.asyncio
    async def test_build_dependency_tree(
        self,
        predictor: DependencyPredictor,
        sample_iterations: list[IterationData],
    ) -> None:
        """Should build dependency tree."""
        await predictor.predict_dependencies(
            sample_iterations,
            ac_ids=["AC_1", "AC_2", "AC_3"],
        )

        tree = predictor.build_dependency_tree()

        assert isinstance(tree, DependencyTreeNode)
        assert tree.ac_id == "ROOT"
        assert len(tree.children) > 0

    @pytest.mark.asyncio
    async def test_get_summary(
        self,
        predictor: DependencyPredictor,
        sample_iterations: list[IterationData],
    ) -> None:
        """Should generate dependency summary."""
        await predictor.predict_dependencies(
            sample_iterations,
            ac_ids=["AC_1", "AC_2", "AC_3"],
        )

        summary = predictor.get_summary()

        assert "total_dependencies" in summary
        assert "by_type" in summary
        assert "by_strength" in summary
        assert "critical_path_length" in summary


class TestACDependency:
    """Tests for ACDependency model."""

    def test_to_dict(self) -> None:
        """Should convert to dictionary."""
        dep = ACDependency(
            dependency_id="dep_001",
            source_ac="AC_2",
            target_ac="AC_1",
            dependency_type=DependencyType.HARD,
            strength=DependencyStrength.BLOCKING,
            confidence=0.9,
        )

        dep_dict = dep.to_dict()

        assert dep_dict["dependency_id"] == "dep_001"
        assert dep_dict["source_ac"] == "AC_2"
        assert dep_dict["target_ac"] == "AC_1"
        assert dep_dict["dependency_type"] == "hard"
        assert dep_dict["strength"] == "blocking"


class TestDependencyTypes:
    """Tests for dependency enums."""

    def test_all_types_exist(self) -> None:
        """All expected types should exist."""
        expected = ["hard", "soft", "implicit", "explicit"]

        for type_name in expected:
            assert DependencyType(type_name) is not None

    def test_all_strengths_exist(self) -> None:
        """All expected strengths should exist."""
        expected = ["blocking", "inhibiting", "affecting"]

        for strength_name in expected:
            assert DependencyStrength(strength_name) is not None


class TestDependencyTreeNode:
    """Tests for DependencyTreeNode model."""

    def test_to_dict(self) -> None:
        """Should convert to dictionary."""
        node = DependencyTreeNode(
            ac_id="AC_1",
            depth=1,
            is_satisfied=True,
            is_blocked=False,
            blocker_count=0,
        )

        node_dict = node.to_dict()

        assert node_dict["ac_id"] == "AC_1"
        assert node_dict["depth"] == 1
        assert node_dict["is_satisfied"] is True
        assert node_dict["children"] == []

    def test_nested_to_dict(self) -> None:
        """Should handle nested nodes."""
        child = DependencyTreeNode(
            ac_id="AC_2",
            depth=2,
        )
        parent = DependencyTreeNode(
            ac_id="AC_1",
            depth=1,
            children=[child],
        )

        parent_dict = parent.to_dict()

        assert len(parent_dict["children"]) == 1
        assert parent_dict["children"][0]["ac_id"] == "AC_2"
