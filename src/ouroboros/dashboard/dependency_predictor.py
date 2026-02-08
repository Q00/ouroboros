"""AC Blocking Dependency Predictor.

This module implements graph-based dependency prediction to identify
which Acceptance Criteria (ACs) block others from completion.

Key Features:
1. Predicts blocking dependencies between ACs
2. Detects implicit dependency chains
3. Identifies critical path ACs
4. Generates dependency tree for visualization
5. Suggests optimal execution ordering

Design Philosophy:
- Ontological analysis: Understands essential relationships
- Graph-based reasoning: Models dependencies as directed graph
- HOTL convergence: Accelerates by resolving blockers first

Usage:
    from ouroboros.dashboard.dependency_predictor import DependencyPredictor

    predictor = DependencyPredictor(llm_adapter)

    # Predict dependencies from iteration history
    dependencies = await predictor.predict_dependencies(iterations, acs)

    # Get blocking predictions
    blockers = predictor.get_blockers(ac_id)

    # Get optimal execution order
    order = predictor.get_execution_order()

    # Generate dependency tree for visualization
    tree = predictor.build_dependency_tree()
"""

from __future__ import annotations

import hashlib
import re
import sys
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

# Python 3.11+ has StrEnum, for earlier versions use string mixin
if sys.version_info >= (3, 11):
    from enum import StrEnum
else:
    class StrEnum(str, Enum):
        """String enum for Python < 3.11 compatibility."""
        pass

# Python 3.10+ supports slots=True in dataclass
DATACLASS_SLOTS = {"slots": True} if sys.version_info >= (3, 10) else {}

from ouroboros.dashboard.models import IterationData, IterationOutcome

if TYPE_CHECKING:
    from ouroboros.providers.base import LLMAdapter

try:
    from ouroboros.observability.logging import get_logger
    log = get_logger(__name__)
except ImportError:
    import logging
    log = logging.getLogger(__name__)


# =============================================================================
# Constants
# =============================================================================

# Confidence thresholds
HIGH_CONFIDENCE_THRESHOLD = 0.8
MEDIUM_CONFIDENCE_THRESHOLD = 0.5

# Dependency detection keywords
DEPENDENCY_KEYWORDS = {
    "requires": r"requires?\s+\w+",
    "depends": r"depends?\s+on",
    "needs": r"needs?\s+\w+",
    "after": r"after\s+\w+",
    "before": r"before\s+\w+",
    "imports": r"import\s+\w+",
    "uses": r"uses?\s+\w+",
    "calls": r"calls?\s+\w+",
    "missing": r"missing\s+\w+",
    "not_found": r"not\s+found",
    "undefined": r"undefined\s+\w+",
}


# =============================================================================
# Enums and Data Models
# =============================================================================


class DependencyType(StrEnum):
    """Type of dependency relationship.

    Attributes:
        HARD: Strict dependency - cannot proceed without
        SOFT: Preference - better with but can work around
        IMPLICIT: Inferred from failure patterns
        EXPLICIT: Stated in AC requirements
    """

    HARD = "hard"
    SOFT = "soft"
    IMPLICIT = "implicit"
    EXPLICIT = "explicit"


class DependencyStrength(StrEnum):
    """Strength of the dependency.

    Attributes:
        BLOCKING: Completely blocks progress
        INHIBITING: Significantly slows progress
        AFFECTING: Minor impact on progress
    """

    BLOCKING = "blocking"
    INHIBITING = "inhibiting"
    AFFECTING = "affecting"


@dataclass(frozen=True, **DATACLASS_SLOTS)
class ACDependency:
    """A dependency relationship between two ACs.

    Attributes:
        dependency_id: Unique identifier
        source_ac: The AC that depends on another
        target_ac: The AC that is depended upon (blocker)
        dependency_type: Type of dependency
        strength: Strength of the dependency
        confidence: Confidence in this prediction (0-1)
        evidence: Evidence supporting this dependency
        detected_from: How dependency was detected
        first_detected: When first detected
    """

    dependency_id: str
    source_ac: str  # This AC is blocked
    target_ac: str  # This AC is the blocker
    dependency_type: DependencyType
    strength: DependencyStrength
    confidence: float = 0.5
    evidence: tuple[str, ...] = ()
    detected_from: str = "iteration_analysis"
    first_detected: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "dependency_id": self.dependency_id,
            "source_ac": self.source_ac,
            "target_ac": self.target_ac,
            "dependency_type": self.dependency_type.value,
            "strength": self.strength.value,
            "confidence": self.confidence,
            "evidence": list(self.evidence),
            "detected_from": self.detected_from,
            "first_detected": self.first_detected.isoformat() if self.first_detected else None,
        }


@dataclass
class BlockingPrediction:
    """Prediction of which ACs block a given AC.

    Attributes:
        blocked_ac: The AC that is blocked
        blockers: List of blocking ACs with confidence
        total_blocking_confidence: Combined confidence
        critical_path_position: Position on critical path (0 = start)
        recommended_action: Suggested action to unblock
    """

    blocked_ac: str
    blockers: list[tuple[str, float]] = field(default_factory=list)  # (ac_id, confidence)
    total_blocking_confidence: float = 0.0
    critical_path_position: int = -1
    recommended_action: str = ""


@dataclass
class DependencyTreeNode:
    """Node in the dependency tree.

    Used for visualization of AC dependencies.
    """

    ac_id: str
    depth: int
    children: list[DependencyTreeNode] = field(default_factory=list)
    is_satisfied: bool = False
    is_blocked: bool = False
    blocker_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for visualization."""
        return {
            "ac_id": self.ac_id,
            "depth": self.depth,
            "is_satisfied": self.is_satisfied,
            "is_blocked": self.is_blocked,
            "blocker_count": self.blocker_count,
            "children": [c.to_dict() for c in self.children],
        }


@dataclass
class DependencyGraph:
    """Graph representation of AC dependencies.

    Nodes are ACs, edges are dependencies.
    """

    nodes: dict[str, dict[str, Any]] = field(default_factory=dict)
    edges: list[ACDependency] = field(default_factory=list)
    adjacency: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    reverse_adjacency: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))

    def add_node(self, ac_id: str, **metadata: Any) -> None:
        """Add an AC node to the graph."""
        self.nodes[ac_id] = metadata

    def add_edge(self, dependency: ACDependency) -> None:
        """Add a dependency edge to the graph."""
        self.edges.append(dependency)
        self.adjacency[dependency.source_ac].append(dependency.target_ac)
        self.reverse_adjacency[dependency.target_ac].append(dependency.source_ac)


# =============================================================================
# Dependency Predictor
# =============================================================================


class DependencyPredictor:
    """AC Blocking Dependency Predictor.

    Uses iteration history and semantic analysis to predict
    which ACs block others from completion.

    Attributes:
        llm_adapter: LLM adapter for semantic analysis
    """

    def __init__(
        self,
        llm_adapter: LLMAdapter | None = None,
    ) -> None:
        """Initialize DependencyPredictor.

        Args:
            llm_adapter: LLM adapter for semantic analysis
        """
        self._llm_adapter = llm_adapter

        # Dependency graph
        self._graph = DependencyGraph()
        self._dependencies: list[ACDependency] = []

        # AC status tracking
        self._ac_status: dict[str, bool] = {}  # ac_id -> is_satisfied
        self._ac_iterations: dict[str, list[IterationData]] = {}

        # Execution order cache
        self._execution_order: list[str] = []
        self._critical_path: list[str] = []

    async def predict_dependencies(
        self,
        iterations: list[IterationData],
        ac_ids: list[str] | None = None,
    ) -> list[ACDependency]:
        """Predict dependencies from iteration history.

        Analyzes failures and blocked iterations to infer
        dependency relationships between ACs.

        Args:
            iterations: List of iteration data
            ac_ids: List of all AC IDs (inferred if None)

        Returns:
            List of predicted dependencies
        """
        # Extract AC IDs if not provided
        if ac_ids is None:
            ac_ids = list(set(i.ac_id for i in iterations if i.ac_id))

        # Build graph structure
        self._graph = DependencyGraph()
        for ac_id in ac_ids:
            self._graph.add_node(ac_id)

        # Group iterations by AC
        self._ac_iterations = {}
        for iteration in iterations:
            if iteration.ac_id:
                if iteration.ac_id not in self._ac_iterations:
                    self._ac_iterations[iteration.ac_id] = []
                self._ac_iterations[iteration.ac_id].append(iteration)

        # Update AC status
        self._ac_status = {}
        for ac_id, ac_iters in self._ac_iterations.items():
            self._ac_status[ac_id] = any(
                i.outcome == IterationOutcome.SUCCESS for i in ac_iters
            )

        dependencies: list[ACDependency] = []

        # 1. Detect explicit blocked dependencies
        blocked_deps = self._detect_blocked_dependencies(iterations)
        dependencies.extend(blocked_deps)

        # 2. Detect implicit dependencies from error messages
        implicit_deps = self._detect_implicit_dependencies(iterations, ac_ids)
        dependencies.extend(implicit_deps)

        # 3. Detect temporal dependencies
        temporal_deps = self._detect_temporal_dependencies(iterations, ac_ids)
        dependencies.extend(temporal_deps)

        # 4. Detect shared resource dependencies
        shared_deps = self._detect_shared_dependencies(iterations, ac_ids)
        dependencies.extend(shared_deps)

        # Add to graph
        for dep in dependencies:
            self._graph.add_edge(dep)

        # Cache dependencies
        self._dependencies = dependencies

        # Calculate execution order
        self._execution_order = self._topological_sort()
        self._critical_path = self._find_critical_path()

        log.info(
            "dependency.prediction_complete",
            extra={
                "total_acs": len(ac_ids),
                "dependencies_found": len(dependencies),
                "execution_order_length": len(self._execution_order),
            },
        )

        return dependencies

    def get_blockers(self, ac_id: str) -> BlockingPrediction:
        """Get blocking ACs for a given AC.

        Args:
            ac_id: AC to check

        Returns:
            BlockingPrediction with blocker information
        """
        blockers: list[tuple[str, float]] = []

        for dep in self._dependencies:
            if dep.source_ac == ac_id:
                # Check if target is not satisfied
                if not self._ac_status.get(dep.target_ac, False):
                    blockers.append((dep.target_ac, dep.confidence))

        # Sort by confidence
        blockers.sort(key=lambda x: x[1], reverse=True)

        total_confidence = min(1.0, sum(c for _, c in blockers))

        # Find position on critical path
        critical_position = -1
        if ac_id in self._critical_path:
            critical_position = self._critical_path.index(ac_id)

        # Generate recommendation
        if blockers:
            top_blocker = blockers[0][0]
            recommended = f"Complete AC '{top_blocker}' first (confidence: {blockers[0][1]:.2f})"
        else:
            recommended = "No blocking dependencies detected"

        return BlockingPrediction(
            blocked_ac=ac_id,
            blockers=blockers,
            total_blocking_confidence=total_confidence,
            critical_path_position=critical_position,
            recommended_action=recommended,
        )

    def get_execution_order(self) -> list[str]:
        """Get optimal AC execution order.

        Returns:
            List of AC IDs in recommended execution order
        """
        return list(self._execution_order)

    def get_critical_path(self) -> list[str]:
        """Get the critical path of dependencies.

        The critical path is the longest chain of dependencies.

        Returns:
            List of AC IDs on critical path
        """
        return list(self._critical_path)

    def build_dependency_tree(self) -> DependencyTreeNode:
        """Build dependency tree for visualization.

        Returns:
            Root node of dependency tree
        """
        # Find root ACs (no incoming dependencies)
        all_sources = set(dep.source_ac for dep in self._dependencies)
        all_targets = set(dep.target_ac for dep in self._dependencies)
        roots = all_targets - all_sources

        if not roots:
            # No clear root, use first AC in execution order
            roots = {self._execution_order[0]} if self._execution_order else set()

        # Build tree from roots
        root = DependencyTreeNode(
            ac_id="ROOT",
            depth=0,
        )

        visited: set[str] = set()

        def build_subtree(ac_id: str, depth: int) -> DependencyTreeNode:
            if ac_id in visited:
                return DependencyTreeNode(ac_id=ac_id, depth=depth)
            visited.add(ac_id)

            node = DependencyTreeNode(
                ac_id=ac_id,
                depth=depth,
                is_satisfied=self._ac_status.get(ac_id, False),
                is_blocked=bool(self._graph.adjacency.get(ac_id)),
                blocker_count=len(self._graph.adjacency.get(ac_id, [])),
            )

            # Add children (ACs that depend on this one)
            dependents = self._graph.reverse_adjacency.get(ac_id, [])
            for dep_ac in dependents:
                child = build_subtree(dep_ac, depth + 1)
                node.children.append(child)

            return node

        for root_ac in roots:
            child = build_subtree(root_ac, 1)
            root.children.append(child)

        return root

    def get_summary(self) -> dict[str, Any]:
        """Get summary of dependency analysis.

        Returns:
            Summary statistics and key findings
        """
        if not self._dependencies:
            return {
                "total_dependencies": 0,
                "by_type": {},
                "by_strength": {},
                "critical_path_length": 0,
                "most_blocking_acs": [],
            }

        by_type: dict[str, int] = {}
        by_strength: dict[str, int] = {}
        blocker_counts: dict[str, int] = {}

        for dep in self._dependencies:
            by_type[dep.dependency_type.value] = by_type.get(dep.dependency_type.value, 0) + 1
            by_strength[dep.strength.value] = by_strength.get(dep.strength.value, 0) + 1
            blocker_counts[dep.target_ac] = blocker_counts.get(dep.target_ac, 0) + 1

        # Sort blockers by count
        most_blocking = sorted(blocker_counts.items(), key=lambda x: x[1], reverse=True)[:5]

        return {
            "total_dependencies": len(self._dependencies),
            "by_type": by_type,
            "by_strength": by_strength,
            "critical_path_length": len(self._critical_path),
            "most_blocking_acs": [{"ac_id": ac, "blocks": count} for ac, count in most_blocking],
            "execution_order": self._execution_order[:10],
        }

    # =========================================================================
    # Dependency Detection Methods
    # =========================================================================

    def _detect_blocked_dependencies(
        self,
        iterations: list[IterationData],
    ) -> list[ACDependency]:
        """Detect dependencies from explicitly blocked iterations."""
        dependencies: list[ACDependency] = []

        blocked = [i for i in iterations if i.outcome == IterationOutcome.BLOCKED]

        for iteration in blocked:
            # Extract blocking AC from error message
            blocking_ac = self._extract_blocking_ac(iteration.error_message)

            if blocking_ac and blocking_ac != iteration.ac_id:
                dep_id = hashlib.md5(
                    f"{iteration.ac_id}:{blocking_ac}".encode()
                ).hexdigest()[:12]

                dependencies.append(
                    ACDependency(
                        dependency_id=dep_id,
                        source_ac=iteration.ac_id,
                        target_ac=blocking_ac,
                        dependency_type=DependencyType.EXPLICIT,
                        strength=DependencyStrength.BLOCKING,
                        confidence=0.95,
                        evidence=(iteration.error_message[:200],),
                        detected_from="blocked_iteration",
                        first_detected=iteration.timestamp,
                    )
                )

        return dependencies

    def _detect_implicit_dependencies(
        self,
        iterations: list[IterationData],
        ac_ids: list[str],
    ) -> list[ACDependency]:
        """Detect implicit dependencies from error patterns."""
        dependencies: list[ACDependency] = []

        for iteration in iterations:
            if iteration.outcome not in (IterationOutcome.FAILURE, IterationOutcome.STAGNANT):
                continue

            if not iteration.ac_id:
                continue

            # Look for references to other ACs in error message
            error_lower = iteration.error_message.lower()

            for other_ac in ac_ids:
                if other_ac == iteration.ac_id:
                    continue

                # Check if other AC is mentioned
                if other_ac.lower() in error_lower:
                    dep_id = hashlib.md5(
                        f"implicit:{iteration.ac_id}:{other_ac}".encode()
                    ).hexdigest()[:12]

                    dependencies.append(
                        ACDependency(
                            dependency_id=dep_id,
                            source_ac=iteration.ac_id,
                            target_ac=other_ac,
                            dependency_type=DependencyType.IMPLICIT,
                            strength=DependencyStrength.INHIBITING,
                            confidence=0.6,
                            evidence=(f"Referenced in error: {iteration.error_message[:100]}",),
                            detected_from="error_analysis",
                            first_detected=iteration.timestamp,
                        )
                    )

            # Look for dependency keywords
            for keyword, pattern in DEPENDENCY_KEYWORDS.items():
                if re.search(pattern, error_lower):
                    # Try to extract the dependency target
                    match = re.search(pattern, error_lower)
                    if match:
                        # Check if any AC matches the extracted term
                        for other_ac in ac_ids:
                            if other_ac == iteration.ac_id:
                                continue

                            # Simple heuristic matching
                            if any(
                                part.lower() in error_lower
                                for part in other_ac.split("_")
                                if len(part) > 3
                            ):
                                dep_id = hashlib.md5(
                                    f"keyword:{iteration.ac_id}:{other_ac}:{keyword}".encode()
                                ).hexdigest()[:12]

                                dependencies.append(
                                    ACDependency(
                                        dependency_id=dep_id,
                                        source_ac=iteration.ac_id,
                                        target_ac=other_ac,
                                        dependency_type=DependencyType.IMPLICIT,
                                        strength=DependencyStrength.AFFECTING,
                                        confidence=0.4,
                                        evidence=(f"Keyword '{keyword}' detected",),
                                        detected_from="keyword_analysis",
                                        first_detected=iteration.timestamp,
                                    )
                                )

        return self._deduplicate_dependencies(dependencies)

    def _detect_temporal_dependencies(
        self,
        iterations: list[IterationData],
        ac_ids: list[str],
    ) -> list[ACDependency]:
        """Detect dependencies from temporal patterns."""
        dependencies: list[ACDependency] = []

        # Find ACs that consistently fail until another AC succeeds
        for blocked_ac in ac_ids:
            blocked_iters = self._ac_iterations.get(blocked_ac, [])
            failures = [i for i in blocked_iters if i.outcome == IterationOutcome.FAILURE]
            successes = [i for i in blocked_iters if i.outcome == IterationOutcome.SUCCESS]

            if not failures or not successes:
                continue

            first_success = min(s.timestamp for s in successes)

            # Check if another AC succeeded just before this one started succeeding
            for other_ac in ac_ids:
                if other_ac == blocked_ac:
                    continue

                other_iters = self._ac_iterations.get(other_ac, [])
                other_successes = [i for i in other_iters if i.outcome == IterationOutcome.SUCCESS]

                if not other_successes:
                    continue

                other_first_success = min(s.timestamp for s in other_successes)

                # Check if other succeeded before this one
                if other_first_success < first_success:
                    # Count failures before other's success
                    failures_before = sum(
                        1 for f in failures
                        if f.timestamp < other_first_success
                    )

                    if failures_before >= 2:
                        dep_id = hashlib.md5(
                            f"temporal:{blocked_ac}:{other_ac}".encode()
                        ).hexdigest()[:12]

                        confidence = min(0.7, 0.3 + (failures_before * 0.1))

                        dependencies.append(
                            ACDependency(
                                dependency_id=dep_id,
                                source_ac=blocked_ac,
                                target_ac=other_ac,
                                dependency_type=DependencyType.IMPLICIT,
                                strength=DependencyStrength.INHIBITING,
                                confidence=confidence,
                                evidence=(
                                    f"{failures_before} failures before {other_ac} succeeded",
                                ),
                                detected_from="temporal_analysis",
                                first_detected=failures[0].timestamp,
                            )
                        )

        return dependencies

    def _detect_shared_dependencies(
        self,
        iterations: list[IterationData],
        ac_ids: list[str],
    ) -> list[ACDependency]:
        """Detect dependencies from shared resources/imports."""
        dependencies: list[ACDependency] = []

        # Extract imports/resources from artifacts
        ac_imports: dict[str, set[str]] = {}

        for ac_id, ac_iters in self._ac_iterations.items():
            imports: set[str] = set()
            for iteration in ac_iters:
                # Extract import statements
                import_matches = re.findall(
                    r"(?:from|import)\s+([\w.]+)",
                    iteration.artifact,
                )
                imports.update(import_matches)

            ac_imports[ac_id] = imports

        # Find shared imports that might indicate dependencies
        for ac1 in ac_ids:
            for ac2 in ac_ids:
                if ac1 >= ac2:  # Avoid duplicates
                    continue

                shared = ac_imports.get(ac1, set()) & ac_imports.get(ac2, set())

                if len(shared) >= 3:  # Significant overlap
                    # Determine direction based on failure patterns
                    ac1_failures = sum(
                        1 for i in self._ac_iterations.get(ac1, [])
                        if i.outcome == IterationOutcome.FAILURE
                    )
                    ac2_failures = sum(
                        1 for i in self._ac_iterations.get(ac2, [])
                        if i.outcome == IterationOutcome.FAILURE
                    )

                    # More failures might indicate dependency
                    if ac1_failures > ac2_failures:
                        source, target = ac1, ac2
                    else:
                        source, target = ac2, ac1

                    dep_id = hashlib.md5(
                        f"shared:{source}:{target}".encode()
                    ).hexdigest()[:12]

                    dependencies.append(
                        ACDependency(
                            dependency_id=dep_id,
                            source_ac=source,
                            target_ac=target,
                            dependency_type=DependencyType.SOFT,
                            strength=DependencyStrength.AFFECTING,
                            confidence=0.4,
                            evidence=(
                                f"Shared imports: {', '.join(list(shared)[:5])}",
                            ),
                            detected_from="shared_resource_analysis",
                        )
                    )

        return dependencies

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _extract_blocking_ac(self, error_message: str) -> str | None:
        """Extract blocking AC ID from error message."""
        # Common patterns for blocking references
        patterns = [
            r"blocked by ['\"]?(\w+)['\"]?",
            r"waiting for ['\"]?(\w+)['\"]?",
            r"requires ['\"]?(\w+)['\"]?",
            r"depends on ['\"]?(\w+)['\"]?",
            r"AC[:\s]+['\"]?(\w+)['\"]?",
        ]

        for pattern in patterns:
            match = re.search(pattern, error_message, re.IGNORECASE)
            if match:
                return match.group(1)

        return None

    def _deduplicate_dependencies(
        self,
        dependencies: list[ACDependency],
    ) -> list[ACDependency]:
        """Remove duplicate dependencies, keeping highest confidence."""
        seen: dict[tuple[str, str], ACDependency] = {}

        for dep in dependencies:
            key = (dep.source_ac, dep.target_ac)
            if key not in seen or dep.confidence > seen[key].confidence:
                seen[key] = dep

        return list(seen.values())

    def _topological_sort(self) -> list[str]:
        """Topological sort of ACs based on dependencies.

        Returns:
            List of AC IDs in dependency order
        """
        # Kahn's algorithm
        in_degree: dict[str, int] = defaultdict(int)

        # Initialize in-degrees
        for ac_id in self._graph.nodes:
            in_degree[ac_id] = 0

        for dep in self._dependencies:
            in_degree[dep.source_ac] += 1

        # Queue of nodes with no dependencies
        queue = deque(
            ac_id for ac_id, degree in in_degree.items() if degree == 0
        )

        result: list[str] = []

        while queue:
            node = queue.popleft()
            result.append(node)

            # Reduce in-degree of dependents
            for dep in self._dependencies:
                if dep.target_ac == node:
                    in_degree[dep.source_ac] -= 1
                    if in_degree[dep.source_ac] == 0:
                        queue.append(dep.source_ac)

        # Add any remaining (cyclic) nodes
        for ac_id in self._graph.nodes:
            if ac_id not in result:
                result.append(ac_id)

        return result

    def _find_critical_path(self) -> list[str]:
        """Find the critical path (longest dependency chain).

        Returns:
            List of AC IDs on critical path
        """
        if not self._graph.nodes:
            return []

        # Find longest path using DFS
        memo: dict[str, list[str]] = {}

        def dfs(node: str, visited: set[str]) -> list[str]:
            if node in memo:
                return memo[node]
            if node in visited:
                return []

            visited.add(node)
            longest: list[str] = [node]

            # Check dependents
            dependents = self._graph.reverse_adjacency.get(node, [])
            for dep in dependents:
                path = dfs(dep, visited)
                if len(path) + 1 > len(longest):
                    longest = [node] + path

            visited.remove(node)
            memo[node] = longest
            return longest

        # Find longest path from any root
        longest_path: list[str] = []
        for ac_id in self._graph.nodes:
            path = dfs(ac_id, set())
            if len(path) > len(longest_path):
                longest_path = path

        return longest_path


__all__ = [
    "DependencyPredictor",
    "ACDependency",
    "BlockingPrediction",
    "DependencyType",
    "DependencyStrength",
    "DependencyTreeNode",
    "DependencyGraph",
]
