"""LLM-based AC dependency analysis.

Analyzes acceptance criteria to determine execution order and parallelization.
Uses topological sort to group independent ACs for parallel execution.

Example:
    analyzer = DependencyAnalyzer(llm_adapter)
    result = await analyzer.analyze(acceptance_criteria)

    if result.is_ok:
        graph = result.value
        # graph.execution_levels: ((0, 2), (1, 3), (4,))
        # Level 0: AC 0 and 2 can run in parallel
        # Level 1: AC 1 and 3 depend on level 0
        # Level 2: AC 4 depends on level 1
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ouroboros.core.types import Result
from ouroboros.observability.logging import get_logger

if TYPE_CHECKING:
    from ouroboros.providers.base import LLMAdapter

log = get_logger(__name__)


# =============================================================================
# Data Models
# =============================================================================


@dataclass(frozen=True, slots=True)
class ACNode:
    """Represents an AC in the dependency graph.

    Attributes:
        index: 0-based AC index.
        content: AC description text.
        depends_on: Indices of ACs this depends on.
    """

    index: int
    content: str
    depends_on: tuple[int, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class DependencyGraph:
    """Dependency graph for AC execution.

    Attributes:
        nodes: All AC nodes with their dependencies.
        execution_levels: Groups of AC indices that can run in parallel.
            Example: ((0, 2), (1,), (3, 4)) means:
            - Level 0: AC 0 and 2 run in parallel
            - Level 1: AC 1 runs after level 0
            - Level 2: AC 3 and 4 run after level 1
    """

    nodes: tuple[ACNode, ...]
    execution_levels: tuple[tuple[int, ...], ...] = field(default_factory=tuple)

    @property
    def total_levels(self) -> int:
        """Number of sequential execution levels."""
        return len(self.execution_levels)

    @property
    def is_parallelizable(self) -> bool:
        """True if any level has multiple ACs (can benefit from parallelization)."""
        return any(len(level) > 1 for level in self.execution_levels)

    def get_dependencies(self, index: int) -> tuple[int, ...]:
        """Get dependencies for a specific AC."""
        for node in self.nodes:
            if node.index == index:
                return node.depends_on
        return ()


# =============================================================================
# Analysis Errors
# =============================================================================


class DependencyAnalysisError(Exception):
    """Error during dependency analysis."""

    pass


# =============================================================================
# LLM Prompts
# =============================================================================


DEPENDENCY_ANALYSIS_PROMPT = """Analyze the following acceptance criteria and determine their dependencies.

Acceptance Criteria:
{ac_list}

Instructions:
1. For each AC, identify which OTHER ACs it depends on (if any)
2. An AC depends on another if:
   - It requires files/code created by the other AC
   - It needs functionality implemented by the other AC
   - It builds upon or extends the other AC's work
3. If ACs are independent (can be done in any order), they have no dependencies

Return ONLY a valid JSON object in this exact format:
{{
  "dependencies": [
    {{"ac_index": 0, "depends_on": []}},
    {{"ac_index": 1, "depends_on": [0]}},
    {{"ac_index": 2, "depends_on": []}}
  ]
}}

Rules:
- Use 0-based indexing (AC 0, AC 1, etc.)
- If an AC has no dependencies, use empty array []
- Return ONLY valid JSON, no explanations or markdown
- Every AC must appear in the dependencies array
"""


# =============================================================================
# Dependency Analyzer
# =============================================================================


class DependencyAnalyzer:
    """Analyzes AC dependencies using LLM."""

    def __init__(
        self,
        llm_adapter: "LLMAdapter | None" = None,
        model: str | None = None,
    ):
        """Initialize analyzer.

        Args:
            llm_adapter: LLM adapter for dependency analysis (LiteLLMAdapter or ClaudeCodeAdapter).
                        If None, creates default ClaudeCodeAdapter.
            model: Model to use for analysis. If None, uses adapter's default.
        """
        self._llm = llm_adapter
        self._model = model

    async def analyze(
        self,
        acceptance_criteria: tuple[str, ...] | list[str],
    ) -> Result[DependencyGraph, DependencyAnalysisError]:
        """Analyze AC dependencies.

        Args:
            acceptance_criteria: List of AC strings.

        Returns:
            Result containing DependencyGraph on success.
        """
        criteria = tuple(acceptance_criteria)
        count = len(criteria)

        log.info(
            "dependency_analyzer.analysis.started",
            ac_count=count,
        )

        # Single AC or none - no dependencies
        if count <= 1:
            nodes = tuple(
                ACNode(index=i, content=ac, depends_on=())
                for i, ac in enumerate(criteria)
            )
            levels: tuple[tuple[int, ...], ...] = (tuple(range(count)),) if count > 0 else ()

            log.info(
                "dependency_analyzer.analysis.completed",
                ac_count=count,
                levels=1,
                method="trivial",
            )

            return Result.ok(DependencyGraph(nodes=nodes, execution_levels=levels))

        # Use LLM to analyze dependencies
        try:
            dependencies = await self._analyze_with_llm(criteria)
            nodes = tuple(
                ACNode(
                    index=i,
                    content=criteria[i],
                    depends_on=tuple(dependencies.get(i, [])),
                )
                for i in range(count)
            )

            # Compute execution levels via topological sort
            levels = self._compute_execution_levels(nodes)

            graph = DependencyGraph(nodes=nodes, execution_levels=levels)

            log.info(
                "dependency_analyzer.analysis.completed",
                ac_count=count,
                levels=graph.total_levels,
                parallelizable=graph.is_parallelizable,
                method="llm",
            )

            return Result.ok(graph)

        except Exception as e:
            log.warning(
                "dependency_analyzer.analysis.failed",
                error=str(e),
                ac_count=count,
            )

            # Fallback: assume all ACs are independent
            nodes = tuple(
                ACNode(index=i, content=ac, depends_on=())
                for i, ac in enumerate(criteria)
            )
            levels = (tuple(range(count)),)

            log.info(
                "dependency_analyzer.analysis.fallback",
                ac_count=count,
                method="all_parallel",
            )

            return Result.ok(DependencyGraph(nodes=nodes, execution_levels=levels))

    async def _analyze_with_llm(
        self,
        criteria: tuple[str, ...],
    ) -> dict[int, list[int]]:
        """Use LLM to analyze dependencies.

        Returns dict mapping AC index to list of dependent AC indices.
        """
        # Lazy import to avoid circular dependencies
        from ouroboros.providers.base import CompletionConfig, Message, MessageRole

        if self._llm is None:
            # Default to ClaudeCodeAdapter (orchestrator mode)
            from ouroboros.providers.claude_code_adapter import ClaudeCodeAdapter
            self._llm = ClaudeCodeAdapter(max_turns=1)

        # Build prompt
        ac_list = "\n".join(f"AC {i}: {ac}" for i, ac in enumerate(criteria))
        prompt = DEPENDENCY_ANALYSIS_PROMPT.format(ac_list=ac_list)

        # Call LLM with proper interface
        messages = [Message(role=MessageRole.USER, content=prompt)]

        # Build config - use provided model or default
        # Note: ClaudeCodeAdapter ignores the model and uses Claude Code's default
        config = CompletionConfig(
            model=self._model or "claude-sonnet-4-20250514",
            temperature=0.0,  # Deterministic
            max_tokens=1000,
        )

        response = await self._llm.complete(messages=messages, config=config)

        if response.is_err:
            raise DependencyAnalysisError(f"LLM call failed: {response.error}")

        content = response.value.content.strip()

        # Parse JSON response
        try:
            # Try to extract JSON from response
            if content.startswith("```"):
                # Remove markdown code block
                lines = content.split("\n")
                json_lines = []
                in_block = False
                for line in lines:
                    if line.startswith("```"):
                        in_block = not in_block
                        continue
                    if in_block or not line.startswith("```"):
                        json_lines.append(line)
                content = "\n".join(json_lines)

            data = json.loads(content)

            # Build dependency dict
            dependencies: dict[int, list[int]] = {}
            for item in data.get("dependencies", []):
                ac_idx = item.get("ac_index", 0)
                deps = item.get("depends_on", [])

                # Validate dependencies
                valid_deps = [
                    d for d in deps
                    if isinstance(d, int) and 0 <= d < len(criteria) and d != ac_idx
                ]
                dependencies[ac_idx] = valid_deps

            return dependencies

        except json.JSONDecodeError as e:
            raise DependencyAnalysisError(f"Failed to parse LLM response: {e}")

    def _compute_execution_levels(
        self,
        nodes: tuple[ACNode, ...],
    ) -> tuple[tuple[int, ...], ...]:
        """Compute parallel execution levels using Kahn's algorithm.

        Returns tuple of levels, where each level contains AC indices
        that can be executed in parallel.
        """
        count = len(nodes)
        if count == 0:
            return ()

        # Build adjacency list and in-degree count
        in_degree = [0] * count
        dependents: dict[int, list[int]] = {i: [] for i in range(count)}

        for node in nodes:
            for dep in node.depends_on:
                if 0 <= dep < count:
                    in_degree[node.index] += 1
                    dependents[dep].append(node.index)

        levels: list[tuple[int, ...]] = []
        remaining = set(range(count))

        while remaining:
            # Find all nodes with no remaining dependencies
            ready = tuple(
                i for i in remaining
                if in_degree[i] == 0
            )

            if not ready:
                # Circular dependency detected - add all remaining as one level
                log.warning(
                    "dependency_analyzer.circular_dependency_detected",
                    remaining=list(remaining),
                )
                ready = tuple(remaining)

            levels.append(ready)

            # Remove ready nodes and update in-degrees
            for node_idx in ready:
                remaining.discard(node_idx)
                for dependent in dependents[node_idx]:
                    in_degree[dependent] -= 1

        return tuple(levels)


__all__ = [
    "ACNode",
    "DependencyGraph",
    "DependencyAnalyzer",
    "DependencyAnalysisError",
]
