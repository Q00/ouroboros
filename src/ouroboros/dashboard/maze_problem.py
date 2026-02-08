"""Complex Maze Problem Generator for 50+ Iteration Demo.

This module generates realistic maze-solving iteration data that demonstrates
complex decision-making scenarios:
- Shortest path finding
- Item collection
- Enemy avoidance

The generated data provides rich context for Gemini 3's 1M token analysis.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from enum import Enum

from ouroboros.dashboard.gemini_analyzer import IterationData


class CellType(Enum):
    """Types of maze cells."""
    EMPTY = "."
    WALL = "#"
    START = "S"
    GOAL = "G"
    ITEM = "I"
    ENEMY = "E"
    VISITED = "+"


@dataclass
class Position:
    """2D position in the maze."""
    x: int
    y: int

    def __hash__(self) -> int:
        return hash((self.x, self.y))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Position):
            return False
        return self.x == other.x and self.y == other.y

    def manhattan_distance(self, other: Position) -> int:
        """Calculate Manhattan distance to another position."""
        return abs(self.x - other.x) + abs(self.y - other.y)

    def neighbors(self) -> list[Position]:
        """Get adjacent positions."""
        return [
            Position(self.x + 1, self.y),
            Position(self.x - 1, self.y),
            Position(self.x, self.y + 1),
            Position(self.x, self.y - 1),
        ]


@dataclass
class Enemy:
    """Enemy with patrol pattern."""
    position: Position
    patrol_path: list[Position]
    patrol_index: int = 0
    detection_range: int = 2

    def move(self) -> None:
        """Move to next position in patrol."""
        self.patrol_index = (self.patrol_index + 1) % len(self.patrol_path)
        self.position = self.patrol_path[self.patrol_index]

    def can_detect(self, pos: Position) -> bool:
        """Check if enemy can detect a position."""
        return self.position.manhattan_distance(pos) <= self.detection_range


@dataclass
class Item:
    """Collectible item in the maze."""
    position: Position
    item_type: str  # "health", "key", "treasure"
    value: int
    collected: bool = False


@dataclass
class MazeState:
    """Complete state of the maze at a point in time."""
    agent_position: Position
    goal_position: Position
    items: list[Item]
    enemies: list[Enemy]
    visited_cells: set[Position]
    items_collected: list[str]
    enemies_avoided: int
    path_length: int
    grid: list[list[str]]


class MazeGenerator:
    """Generator for complex maze problems.

    This creates realistic maze-solving scenarios with:
    - Procedurally generated mazes
    - Multiple collectible items
    - Patrolling enemies
    - Clear shortest path solutions

    Usage:
        generator = MazeGenerator(width=15, height=15)
        iterations = generator.generate_solving_iterations(target_iterations=60)
    """

    def __init__(
        self,
        width: int = 15,
        height: int = 15,
        num_items: int = 5,
        num_enemies: int = 3,
        seed: int | None = None,
    ) -> None:
        """Initialize the maze generator.

        Args:
            width: Maze width.
            height: Maze height.
            num_items: Number of items to place.
            num_enemies: Number of enemies to place.
            seed: Random seed for reproducibility.
        """
        self.width = width
        self.height = height
        self.num_items = num_items
        self.num_enemies = num_enemies
        self.rng = random.Random(seed)

        # Initialize maze
        self.grid: list[list[str]] = []
        self.start = Position(1, 1)
        self.goal = Position(width - 2, height - 2)
        self.items: list[Item] = []
        self.enemies: list[Enemy] = []

        self._generate_maze()

    def _generate_maze(self) -> None:
        """Generate a solvable maze using recursive backtracking."""
        # Initialize grid with walls
        self.grid = [["#" for _ in range(self.width)] for _ in range(self.height)]

        # Carve paths using recursive backtracking
        self._carve_passages(1, 1)

        # Ensure start and goal are accessible
        self.grid[self.start.y][self.start.x] = "S"
        self.grid[self.goal.y][self.goal.x] = "G"

        # Ensure path exists between start and goal
        self._ensure_path()

        # Place items
        self._place_items()

        # Place enemies with patrol routes
        self._place_enemies()

    def _carve_passages(self, x: int, y: int) -> None:
        """Recursively carve passages through the maze."""
        self.grid[y][x] = "."

        directions = [(0, 2), (2, 0), (0, -2), (-2, 0)]
        self.rng.shuffle(directions)

        for dx, dy in directions:
            nx, ny = x + dx, y + dy
            if 0 < nx < self.width - 1 and 0 < ny < self.height - 1:
                if self.grid[ny][nx] == "#":
                    self.grid[y + dy // 2][x + dx // 2] = "."
                    self._carve_passages(nx, ny)

    def _ensure_path(self) -> None:
        """Ensure there's a path from start to goal."""
        # Simple BFS to check connectivity
        from collections import deque

        visited = set()
        queue = deque([self.start])
        visited.add(self.start)

        while queue:
            pos = queue.popleft()
            if pos == self.goal:
                return  # Path exists

            for neighbor in pos.neighbors():
                if (0 <= neighbor.x < self.width and
                    0 <= neighbor.y < self.height and
                    neighbor not in visited and
                    self.grid[neighbor.y][neighbor.x] != "#"):
                    visited.add(neighbor)
                    queue.append(neighbor)

        # No path found, create one
        x, y = self.start.x, self.start.y
        while x != self.goal.x or y != self.goal.y:
            if x < self.goal.x:
                x += 1
            elif x > self.goal.x:
                x -= 1
            elif y < self.goal.y:
                y += 1
            elif y > self.goal.y:
                y -= 1
            if self.grid[y][x] == "#":
                self.grid[y][x] = "."

    def _place_items(self) -> None:
        """Place collectible items in the maze."""
        item_types = ["health", "key", "treasure"]
        empty_cells = [
            Position(x, y)
            for y in range(self.height)
            for x in range(self.width)
            if self.grid[y][x] == "." and Position(x, y) not in [self.start, self.goal]
        ]

        self.rng.shuffle(empty_cells)
        for i in range(min(self.num_items, len(empty_cells))):
            pos = empty_cells[i]
            item_type = self.rng.choice(item_types)
            self.items.append(Item(
                position=pos,
                item_type=item_type,
                value=self.rng.randint(10, 50),
            ))
            self.grid[pos.y][pos.x] = "I"

    def _place_enemies(self) -> None:
        """Place enemies with patrol routes."""
        empty_cells = [
            Position(x, y)
            for y in range(self.height)
            for x in range(self.width)
            if self.grid[y][x] == "."
        ]

        self.rng.shuffle(empty_cells)
        for i in range(min(self.num_enemies, len(empty_cells) // 3)):
            start_pos = empty_cells[i]
            # Create patrol path
            patrol_path = [start_pos]
            current = start_pos
            for _ in range(self.rng.randint(3, 6)):
                neighbors = [
                    n for n in current.neighbors()
                    if (0 < n.x < self.width - 1 and
                        0 < n.y < self.height - 1 and
                        self.grid[n.y][n.x] in [".", "I"])
                ]
                if neighbors:
                    current = self.rng.choice(neighbors)
                    patrol_path.append(current)

            self.enemies.append(Enemy(
                position=start_pos,
                patrol_path=patrol_path,
                detection_range=2,
            ))
            self.grid[start_pos.y][start_pos.x] = "E"

    def _get_current_state(
        self,
        agent_pos: Position,
        visited: set[Position],
        collected_items: list[str],
        enemies_avoided: int,
        path_length: int,
    ) -> MazeState:
        """Get current maze state."""
        return MazeState(
            agent_position=agent_pos,
            goal_position=self.goal,
            items=self.items,
            enemies=self.enemies,
            visited_cells=visited.copy(),
            items_collected=collected_items.copy(),
            enemies_avoided=enemies_avoided,
            path_length=path_length,
            grid=[row.copy() for row in self.grid],
        )

    def _state_to_dict(self, state: MazeState) -> dict[str, Any]:
        """Convert state to dictionary for serialization."""
        return {
            "agent_position": {"x": state.agent_position.x, "y": state.agent_position.y},
            "goal_position": {"x": state.goal_position.x, "y": state.goal_position.y},
            "visited_count": len(state.visited_cells),
            "items_collected": state.items_collected,
            "enemies_avoided": state.enemies_avoided,
            "path_length": state.path_length,
            "items_remaining": sum(1 for i in state.items if not i.collected),
            "enemies_nearby": sum(
                1 for e in state.enemies
                if e.can_detect(state.agent_position)
            ),
        }

    def generate_solving_iterations(
        self,
        target_iterations: int = 60,
        base_time: datetime | None = None,
    ) -> list[IterationData]:
        """Generate iteration data simulating maze solving.

        This creates realistic iteration data that includes:
        - Exploration and backtracking
        - Item collection decisions
        - Enemy avoidance maneuvers
        - Path optimization attempts

        Args:
            target_iterations: Target number of iterations.
            base_time: Starting timestamp.

        Returns:
            List of IterationData objects.
        """
        if base_time is None:
            base_time = datetime.now() - timedelta(hours=2)

        iterations: list[IterationData] = []
        current_pos = Position(self.start.x, self.start.y)
        visited: set[Position] = {current_pos}
        collected_items: list[str] = []
        enemies_avoided = 0
        path_length = 0
        backtrack_stack: list[Position] = [current_pos]

        # Phase progression based on iteration count
        phase_boundaries = [
            (0, "Discover"),
            (target_iterations // 4, "Define"),
            (target_iterations // 2, "Develop"),
            (3 * target_iterations // 4, "Deliver"),
        ]

        def get_phase(iteration: int) -> str:
            for boundary, phase in reversed(phase_boundaries):
                if iteration >= boundary:
                    return phase
            return "Discover"

        # Action templates
        exploration_actions = [
            "Explore {direction} corridor",
            "Scout {direction} path",
            "Investigate {direction} passage",
            "Map {direction} area",
        ]

        item_actions = [
            "Collect {item_type} at ({x}, {y})",
            "Pick up {item_type}",
            "Acquire {item_type} item",
        ]

        enemy_actions = [
            "Evade enemy patrol",
            "Wait for enemy to pass",
            "Take alternative route to avoid enemy",
            "Hide until enemy moves",
        ]

        optimization_actions = [
            "Recalculate optimal path",
            "Optimize route to goal",
            "Adjust path for efficiency",
            "Re-evaluate shortest path",
        ]

        backtrack_actions = [
            "Backtrack to previous junction",
            "Return to explored area",
            "Retreat from dead end",
        ]

        i = 0
        while i < target_iterations:
            phase = get_phase(i)
            timestamp = base_time + timedelta(minutes=i * 2)

            # Move enemies
            for enemy in self.enemies:
                enemy.move()

            # Determine action based on situation
            neighbors = [
                n for n in current_pos.neighbors()
                if (0 <= n.x < self.width and
                    0 <= n.y < self.height and
                    self.grid[n.y][n.x] != "#")
            ]

            unvisited = [n for n in neighbors if n not in visited]
            enemy_nearby = any(e.can_detect(current_pos) for e in self.enemies)
            item_at_pos = next(
                (item for item in self.items
                 if item.position == current_pos and not item.collected),
                None
            )

            # Choose action
            if item_at_pos:
                # Collect item
                item_at_pos.collected = True
                collected_items.append(item_at_pos.item_type)
                action = self.rng.choice(item_actions).format(
                    item_type=item_at_pos.item_type,
                    x=current_pos.x,
                    y=current_pos.y,
                )
                result = f"Collected {item_at_pos.item_type} (value: {item_at_pos.value})"

            elif enemy_nearby:
                # Avoid enemy
                enemies_avoided += 1
                safe_neighbors = [
                    n for n in neighbors
                    if not any(e.can_detect(n) for e in self.enemies)
                ]
                if safe_neighbors:
                    next_pos = self.rng.choice(safe_neighbors)
                    current_pos = next_pos
                    visited.add(current_pos)
                    path_length += 1
                action = self.rng.choice(enemy_actions)
                result = "Successfully evaded enemy patrol"

            elif unvisited:
                # Explore new area
                next_pos = self.rng.choice(unvisited)
                direction = self._get_direction(current_pos, next_pos)
                current_pos = next_pos
                visited.add(current_pos)
                backtrack_stack.append(current_pos)
                path_length += 1
                action = self.rng.choice(exploration_actions).format(direction=direction)
                result = f"Discovered new area at ({current_pos.x}, {current_pos.y})"

            elif backtrack_stack:
                # Backtrack
                if len(backtrack_stack) > 1:
                    backtrack_stack.pop()
                    current_pos = backtrack_stack[-1]
                    path_length += 1
                action = self.rng.choice(backtrack_actions)
                result = f"Returned to ({current_pos.x}, {current_pos.y})"

            else:
                # Optimize
                action = self.rng.choice(optimization_actions)
                result = "Path recalculated - potential savings identified"

            # Calculate metrics
            distance_to_goal = current_pos.manhattan_distance(self.goal)
            total_cells = (self.width - 2) * (self.height - 2)
            coverage = len(visited) / total_cells

            efficiency = 1.0 - (path_length / (distance_to_goal + path_length + 1))
            items_progress = len(collected_items) / max(len(self.items), 1)

            # Generate reasoning
            reasoning = self._generate_reasoning(
                phase=phase,
                iteration=i,
                current_pos=current_pos,
                distance_to_goal=distance_to_goal,
                items_remaining=sum(1 for item in self.items if not item.collected),
                enemy_nearby=enemy_nearby,
            )

            # Create iteration data
            state = self._get_current_state(
                current_pos, visited, collected_items, enemies_avoided, path_length
            )

            iterations.append(IterationData(
                iteration_id=i + 1,
                timestamp=timestamp,
                phase=phase,
                action=action,
                result=result,
                state=self._state_to_dict(state),
                metrics={
                    "efficiency": round(efficiency, 3),
                    "coverage": round(coverage, 3),
                    "items_progress": round(items_progress, 3),
                    "distance_to_goal": distance_to_goal,
                    "path_length": path_length,
                },
                reasoning=reasoning,
            ))

            i += 1

            # Check if goal reached
            if current_pos == self.goal:
                # Add final delivery iterations
                for j in range(i, min(i + 5, target_iterations)):
                    phase = "Deliver"
                    timestamp = base_time + timedelta(minutes=j * 2)

                    iterations.append(IterationData(
                        iteration_id=j + 1,
                        timestamp=timestamp,
                        phase=phase,
                        action="Verify solution completeness",
                        result=f"Solution verified - Path: {path_length} steps, Items: {len(collected_items)}/{len(self.items)}",
                        state=self._state_to_dict(state),
                        metrics={
                            "efficiency": round(efficiency, 3),
                            "coverage": round(coverage, 3),
                            "items_progress": round(items_progress, 3),
                            "distance_to_goal": 0,
                            "path_length": path_length,
                        },
                        reasoning=f"Final verification at iteration {j + 1}. Goal reached successfully.",
                    ))
                break

        return iterations

    def _get_direction(self, from_pos: Position, to_pos: Position) -> str:
        """Get direction name for movement."""
        dx = to_pos.x - from_pos.x
        dy = to_pos.y - from_pos.y

        if dx > 0:
            return "east"
        if dx < 0:
            return "west"
        if dy > 0:
            return "south"
        if dy < 0:
            return "north"
        return "current"

    def _generate_reasoning(
        self,
        phase: str,
        iteration: int,
        current_pos: Position,
        distance_to_goal: int,
        items_remaining: int,
        enemy_nearby: bool,
    ) -> str:
        """Generate realistic reasoning for the iteration."""
        templates = {
            "Discover": [
                f"At iteration {iteration}, exploring the maze structure. "
                f"Currently at ({current_pos.x}, {current_pos.y}), "
                f"{distance_to_goal} units from goal. "
                f"Priority: Map unknown areas before committing to path.",

                f"Discovery phase iteration {iteration}. "
                f"Building mental model of maze topology. "
                f"{items_remaining} items detected but not yet collected. "
                f"Strategy: Breadth-first exploration.",
            ],
            "Define": [
                f"Define phase at iteration {iteration}. "
                f"Analyzing collected data to identify optimal strategies. "
                f"Distance to goal: {distance_to_goal}. "
                f"Items remaining: {items_remaining}. "
                f"Formulating collection-then-exit vs direct-path approach.",

                f"Iteration {iteration} in Define phase. "
                f"Evaluating tradeoffs between item collection and path efficiency. "
                f"Enemy patrol patterns partially mapped. "
                f"Decision framework: maximize value within time constraints.",
            ],
            "Develop": [
                f"Development iteration {iteration}. "
                f"Executing optimized path strategy. "
                f"Position: ({current_pos.x}, {current_pos.y}). "
                f"{'Enemy detected nearby - evasion required.' if enemy_nearby else 'Path clear.'} "
                f"Adapting to dynamic obstacles.",

                f"Iteration {iteration}: Active path execution. "
                f"Balancing speed ({distance_to_goal} remaining) with item collection. "
                f"Real-time adjustment for enemy movements. "
                f"Confidence in current strategy: "
                f"{'moderate - enemy nearby' if enemy_nearby else 'high - clear path'}.",
            ],
            "Deliver": [
                f"Delivery phase iteration {iteration}. "
                f"Finalizing solution and verifying completeness. "
                f"Path length: optimized. Items: {items_remaining} uncollected. "
                f"Preparing solution documentation.",

                f"Final iteration {iteration}. "
                f"Validating that all acceptance criteria are met. "
                f"Distance to goal: {distance_to_goal}. "
                f"Solution quality assessment: comprehensive analysis complete.",
            ],
        }

        return self.rng.choice(templates.get(phase, templates["Discover"]))


def generate_demo_data(
    iteration_count: int = 60,
    maze_size: int = 15,
    seed: int | None = None,
) -> list[IterationData]:
    """Generate demo iteration data for the Streamlit dashboard.

    This is the main entry point for generating sample data.

    Args:
        iteration_count: Target number of iterations.
        maze_size: Size of the maze (width and height).
        seed: Random seed for reproducibility.

    Returns:
        List of IterationData objects.
    """
    generator = MazeGenerator(
        width=maze_size,
        height=maze_size,
        num_items=5,
        num_enemies=3,
        seed=seed,
    )
    return generator.generate_solving_iterations(target_iterations=iteration_count)


__all__ = [
    "MazeGenerator",
    "generate_demo_data",
    "Position",
    "CellType",
    "Enemy",
    "Item",
    "MazeState",
]
