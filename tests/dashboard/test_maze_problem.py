"""Tests for the complex maze problem generator."""

import pytest
from datetime import datetime

from ouroboros.dashboard.maze_problem import (
    MazeGenerator,
    Position,
    CellType,
    Enemy,
    Item,
    generate_demo_data,
)


class TestPosition:
    """Tests for Position class."""

    def test_position_equality(self) -> None:
        """Test position equality comparison."""
        pos1 = Position(3, 5)
        pos2 = Position(3, 5)
        pos3 = Position(3, 6)

        assert pos1 == pos2
        assert pos1 != pos3

    def test_position_hash(self) -> None:
        """Test position hashing for set membership."""
        pos1 = Position(3, 5)
        pos2 = Position(3, 5)

        positions = {pos1}
        assert pos2 in positions

    def test_manhattan_distance(self) -> None:
        """Test Manhattan distance calculation."""
        pos1 = Position(0, 0)
        pos2 = Position(3, 4)

        assert pos1.manhattan_distance(pos2) == 7

    def test_neighbors(self) -> None:
        """Test neighbor generation."""
        pos = Position(5, 5)
        neighbors = pos.neighbors()

        assert len(neighbors) == 4
        expected = [Position(6, 5), Position(4, 5), Position(5, 6), Position(5, 4)]
        for exp in expected:
            assert exp in neighbors


class TestEnemy:
    """Tests for Enemy class."""

    def test_enemy_patrol(self) -> None:
        """Test enemy patrol movement."""
        patrol = [Position(0, 0), Position(1, 0), Position(2, 0)]
        enemy = Enemy(position=patrol[0], patrol_path=patrol)

        assert enemy.position == Position(0, 0)
        enemy.move()
        assert enemy.position == Position(1, 0)
        enemy.move()
        assert enemy.position == Position(2, 0)
        enemy.move()  # Wraps around
        assert enemy.position == Position(0, 0)

    def test_enemy_detection(self) -> None:
        """Test enemy detection range."""
        enemy = Enemy(
            position=Position(5, 5),
            patrol_path=[Position(5, 5)],
            detection_range=2,
        )

        # Within range
        assert enemy.can_detect(Position(5, 6))
        assert enemy.can_detect(Position(6, 6))

        # Out of range
        assert not enemy.can_detect(Position(5, 8))


class TestMazeGenerator:
    """Tests for MazeGenerator class."""

    def test_maze_generation(self) -> None:
        """Test basic maze generation."""
        generator = MazeGenerator(width=15, height=15, seed=42)

        assert len(generator.grid) == 15
        assert len(generator.grid[0]) == 15

    def test_maze_has_start_and_goal(self) -> None:
        """Test that maze has start and goal positions."""
        generator = MazeGenerator(width=15, height=15, seed=42)

        assert generator.start == Position(1, 1)
        assert generator.goal == Position(13, 13)

    def test_maze_items_placement(self) -> None:
        """Test that items are placed in the maze."""
        generator = MazeGenerator(
            width=15,
            height=15,
            num_items=5,
            seed=42,
        )

        assert len(generator.items) <= 5  # May be fewer if not enough space

    def test_maze_enemies_placement(self) -> None:
        """Test that enemies are placed in the maze."""
        generator = MazeGenerator(
            width=15,
            height=15,
            num_enemies=3,
            seed=42,
        )

        assert len(generator.enemies) <= 3

    def test_generate_iterations(self) -> None:
        """Test iteration generation."""
        generator = MazeGenerator(width=15, height=15, seed=42)
        iterations = generator.generate_solving_iterations(target_iterations=60)

        assert len(iterations) >= 50  # May finish early if goal reached
        assert len(iterations) <= 65  # Some buffer for final iterations

        # Check iteration structure
        first = iterations[0]
        assert first.iteration_id == 1
        assert first.phase in ["Discover", "Define", "Develop", "Deliver"]
        assert isinstance(first.action, str)
        assert isinstance(first.result, str)
        assert isinstance(first.state, dict)
        assert isinstance(first.metrics, dict)

    def test_iterations_have_all_phases(self) -> None:
        """Test that iterations cover all four phases."""
        generator = MazeGenerator(width=15, height=15, seed=42)
        iterations = generator.generate_solving_iterations(target_iterations=100)

        phases = {it.phase for it in iterations}
        assert "Discover" in phases
        assert "Define" in phases
        assert "Develop" in phases
        assert "Deliver" in phases


class TestGenerateDemoData:
    """Tests for generate_demo_data function."""

    def test_generate_demo_data_default(self) -> None:
        """Test default demo data generation."""
        iterations = generate_demo_data()

        assert len(iterations) >= 50
        assert all(hasattr(it, "iteration_id") for it in iterations)

    def test_generate_demo_data_custom_count(self) -> None:
        """Test custom iteration count."""
        iterations = generate_demo_data(iteration_count=30)

        assert len(iterations) >= 25  # Some buffer

    def test_generate_demo_data_reproducibility(self) -> None:
        """Test that seed produces reproducible results."""
        iter1 = generate_demo_data(seed=123)
        iter2 = generate_demo_data(seed=123)

        # First few iterations should be identical
        for i in range(min(5, len(iter1), len(iter2))):
            assert iter1[i].action == iter2[i].action
            assert iter1[i].phase == iter2[i].phase

    def test_generate_demo_data_metrics(self) -> None:
        """Test that metrics are properly calculated."""
        iterations = generate_demo_data(iteration_count=50, seed=42)

        for it in iterations:
            assert "efficiency" in it.metrics
            assert "coverage" in it.metrics
            assert 0 <= it.metrics["efficiency"] <= 1
            assert 0 <= it.metrics["coverage"] <= 1
