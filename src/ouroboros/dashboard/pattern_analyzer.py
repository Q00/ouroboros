"""Pattern Analyzer for HOTL Iteration Failures.

This module implements ML-powered pattern detection across 50+ HOTL iterations,
identifying recurring failure patterns, root causes, and correlations.

Key Features:
1. Identifies failure patterns across iterations
2. Clusters similar failures using semantic similarity
3. Detects oscillation, spinning, and stagnation patterns
4. Builds pattern network graphs for visualization
5. Generates Socratic questions to probe root causes

Design Philosophy:
- Socratic method: Ask "Why?" repeatedly to find root causes
- Ontological analysis: Classify failures by their essential nature
- Pattern recognition: Learn from iteration history

Usage:
    from ouroboros.dashboard.pattern_analyzer import PatternAnalyzer

    analyzer = PatternAnalyzer(llm_adapter)

    # Analyze patterns from iterations
    patterns = await analyzer.analyze_patterns(iterations)

    # Get pattern clusters
    clusters = await analyzer.cluster_patterns(patterns)

    # Generate pattern network for visualization
    network = analyzer.build_pattern_network(patterns)
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import sys
from collections import Counter
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

# Minimum iterations for pattern detection
MIN_ITERATIONS_FOR_ANALYSIS = 5

# Maximum patterns to return
MAX_PATTERNS = 50

# Similarity threshold for clustering
SIMILARITY_THRESHOLD = 0.7

# Common error pattern signatures (pre-compiled for performance)
ERROR_SIGNATURES: dict[str, re.Pattern[str]] = {
    key: re.compile(pattern, re.IGNORECASE)
    for key, pattern in {
        "import_error": r"(ImportError|ModuleNotFoundError)",
        "type_error": r"TypeError:",
        "attribute_error": r"AttributeError:",
        "value_error": r"ValueError:",
        "syntax_error": r"SyntaxError:",
        "key_error": r"KeyError:",
        "index_error": r"IndexError:",
        "assertion_error": r"AssertionError:",
        "name_error": r"NameError:",
        "timeout_error": r"(TimeoutError|asyncio\.TimeoutError)",
        "connection_error": r"(ConnectionError|ConnectionRefused)",
        "permission_error": r"PermissionError:",
        "file_not_found": r"FileNotFoundError:",
        "validation_failed": r"(validation|invalid|constraint)",
        "test_failed": r"(AssertionError|assert|test.*fail)",
        "dependency_missing": r"(missing|not found|could not)",
    }.items()
}


# =============================================================================
# Enums and Data Models
# =============================================================================


class PatternCategory(StrEnum):
    """Categories of failure patterns.

    Attributes:
        SPINNING: Same error repeated (stuck in loop)
        OSCILLATION: Alternating between errors A and B
        DEPENDENCY: Blocked by missing dependency
        ROOT_CAUSE: Fundamental issue (ontological)
        SYMPTOM: Surface-level issue
        STAGNATION: No progress being made
        REGRESSION: Previously working, now broken
        COMPLEXITY: Task too complex
        AMBIGUITY: Requirements unclear
    """

    SPINNING = "spinning"
    OSCILLATION = "oscillation"
    DEPENDENCY = "dependency"
    ROOT_CAUSE = "root_cause"
    SYMPTOM = "symptom"
    STAGNATION = "stagnation"
    REGRESSION = "regression"
    COMPLEXITY = "complexity"
    AMBIGUITY = "ambiguity"


class PatternSeverity(StrEnum):
    """Severity of failure pattern.

    Attributes:
        CRITICAL: Blocks all progress
        HIGH: Major impact on convergence
        MEDIUM: Moderate impact
        LOW: Minor impact
    """

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True, **DATACLASS_SLOTS)
class FailurePattern:
    """A detected failure pattern across iterations.

    Attributes:
        pattern_id: Unique identifier
        category: Type of pattern
        severity: Impact severity
        description: Human-readable description
        error_signature: Common error pattern
        affected_acs: List of affected AC IDs
        iteration_ids: IDs of iterations exhibiting pattern
        occurrence_count: How many times pattern occurred
        first_seen: When pattern first appeared
        last_seen: When pattern last appeared
        confidence: Confidence in pattern detection (0-1)
        root_cause_hypothesis: Hypothesized root cause
        socratic_questions: Questions to probe root cause
        metadata: Additional pattern data
    """

    pattern_id: str
    category: PatternCategory
    severity: PatternSeverity
    description: str
    error_signature: str = ""
    affected_acs: tuple[str, ...] = ()
    iteration_ids: tuple[str, ...] = ()
    occurrence_count: int = 1
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    confidence: float = 0.5
    root_cause_hypothesis: str = ""
    socratic_questions: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "pattern_id": self.pattern_id,
            "category": self.category.value,
            "severity": self.severity.value,
            "description": self.description,
            "error_signature": self.error_signature,
            "affected_acs": list(self.affected_acs),
            "iteration_ids": list(self.iteration_ids),
            "occurrence_count": self.occurrence_count,
            "first_seen": self.first_seen.isoformat() if self.first_seen else None,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "confidence": self.confidence,
            "root_cause_hypothesis": self.root_cause_hypothesis,
            "socratic_questions": list(self.socratic_questions),
        }


@dataclass
class PatternCluster:
    """A cluster of similar failure patterns.

    Attributes:
        cluster_id: Unique identifier
        patterns: Patterns in this cluster
        centroid_pattern: Most representative pattern
        common_error: Most common error in cluster
        affected_acs: All ACs affected by cluster
        total_occurrences: Total occurrences in cluster
        cluster_label: Descriptive label
    """

    cluster_id: str
    patterns: list[FailurePattern] = field(default_factory=list)
    centroid_pattern: FailurePattern | None = None
    common_error: str = ""
    affected_acs: set[str] = field(default_factory=set)
    total_occurrences: int = 0
    cluster_label: str = ""


@dataclass
class PatternNetworkNode:
    """Node in the pattern network graph.

    Attributes:
        node_id: Unique identifier (pattern_id or ac_id)
        node_type: "pattern" or "ac"
        label: Display label
        weight: Node importance weight
        metadata: Additional node data
    """

    node_id: str
    node_type: str  # "pattern" or "ac"
    label: str
    weight: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class PatternNetworkEdge:
    """Edge in the pattern network graph.

    Attributes:
        source_id: Source node ID
        target_id: Target node ID
        edge_type: Type of relationship
        weight: Edge strength
        label: Edge label
    """

    source_id: str
    target_id: str
    edge_type: str  # "causes", "related", "blocks"
    weight: float = 1.0
    label: str = ""


@dataclass
class PatternNetwork:
    """Network graph of failure patterns.

    Used for visualization of pattern relationships.
    """

    nodes: list[PatternNetworkNode] = field(default_factory=list)
    edges: list[PatternNetworkEdge] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for visualization."""
        return {
            "nodes": [
                {
                    "id": n.node_id,
                    "type": n.node_type,
                    "label": n.label,
                    "weight": n.weight,
                    **n.metadata,
                }
                for n in self.nodes
            ],
            "edges": [
                {
                    "source": e.source_id,
                    "target": e.target_id,
                    "type": e.edge_type,
                    "weight": e.weight,
                    "label": e.label,
                }
                for e in self.edges
            ],
        }


# =============================================================================
# Pattern Analyzer
# =============================================================================


class PatternAnalyzer:
    """ML-powered pattern analyzer for HOTL iteration failures.

    Identifies recurring patterns, clusters similar failures,
    and builds pattern networks for visualization.

    Uses Gemini 3's 1M context to analyze comprehensive history.

    Attributes:
        llm_adapter: LLM adapter for semantic analysis
        min_pattern_occurrences: Minimum occurrences for pattern
    """

    def __init__(
        self,
        llm_adapter: LLMAdapter | None = None,
        *,
        min_pattern_occurrences: int = 2,
    ) -> None:
        """Initialize PatternAnalyzer.

        Args:
            llm_adapter: LLM adapter for semantic analysis
            min_pattern_occurrences: Min occurrences for pattern (default 2)
        """
        self._llm_adapter = llm_adapter
        self._min_occurrences = min_pattern_occurrences

        # Pattern cache
        self._detected_patterns: list[FailurePattern] = []
        self._pattern_clusters: list[PatternCluster] = []

    async def analyze_patterns(
        self,
        iterations: list[IterationData],
    ) -> list[FailurePattern]:
        """Analyze iterations to detect failure patterns.

        Examines 50+ iterations to find recurring patterns,
        categorize failures, and generate Socratic questions.

        This method offloads CPU-bound pattern detection to a thread
        to avoid blocking the event loop.

        Args:
            iterations: List of iteration data to analyze

        Returns:
            List of detected failure patterns
        """
        return await asyncio.to_thread(self._analyze_patterns_sync, iterations)

    def _analyze_patterns_sync(
        self,
        iterations: list[IterationData],
    ) -> list[FailurePattern]:
        """Synchronous pattern analysis implementation.

        Args:
            iterations: List of iteration data to analyze

        Returns:
            List of detected failure patterns
        """
        if len(iterations) < MIN_ITERATIONS_FOR_ANALYSIS:
            log.warning(
                "pattern.insufficient_iterations",
                extra={"count": len(iterations), "required": MIN_ITERATIONS_FOR_ANALYSIS},
            )
            return []

        patterns: list[FailurePattern] = []

        # Filter to failures only
        failures = [
            i for i in iterations
            if i.outcome in (
                IterationOutcome.FAILURE,
                IterationOutcome.STAGNANT,
                IterationOutcome.BLOCKED,
            )
        ]

        if not failures:
            log.info("pattern.no_failures_found")
            return []

        # 1. Detect spinning patterns
        spinning_patterns = self._detect_spinning_patterns(failures)
        patterns.extend(spinning_patterns)

        # 2. Detect oscillation patterns
        oscillation_patterns = self._detect_oscillation_patterns(failures)
        patterns.extend(oscillation_patterns)

        # 3. Detect error signature patterns
        error_patterns = self._detect_error_patterns(failures)
        patterns.extend(error_patterns)

        # 4. Detect dependency patterns
        dependency_patterns = self._detect_dependency_patterns(failures)
        patterns.extend(dependency_patterns)

        # 5. Detect stagnation patterns
        stagnation_patterns = self._detect_stagnation_patterns(failures)
        patterns.extend(stagnation_patterns)

        # 6. Generate Socratic questions for each pattern
        patterns = self._add_socratic_questions(patterns)

        # Cache results
        self._detected_patterns = patterns

        log.info(
            "pattern.analysis_complete",
            extra={
                "total_iterations": len(iterations),
                "failures": len(failures),
                "patterns_found": len(patterns),
            },
        )

        return patterns[:MAX_PATTERNS]

    async def cluster_patterns(
        self,
        patterns: list[FailurePattern] | None = None,
    ) -> list[PatternCluster]:
        """Cluster similar failure patterns.

        Groups patterns by semantic similarity and error signatures.

        Args:
            patterns: Patterns to cluster (uses cached if None)

        Returns:
            List of pattern clusters
        """
        if patterns is None:
            patterns = self._detected_patterns

        if not patterns:
            return []

        clusters: list[PatternCluster] = []

        # Group by category first
        by_category: dict[PatternCategory, list[FailurePattern]] = {}
        for p in patterns:
            if p.category not in by_category:
                by_category[p.category] = []
            by_category[p.category].append(p)

        # Create cluster for each category
        for category, category_patterns in by_category.items():
            # Further cluster by error signature within category
            by_signature: dict[str, list[FailurePattern]] = {}
            for p in category_patterns:
                sig = p.error_signature or "unknown"
                if sig not in by_signature:
                    by_signature[sig] = []
                by_signature[sig].append(p)

            for sig, sig_patterns in by_signature.items():
                cluster_id = hashlib.md5(
                    f"{category.value}:{sig}".encode()
                ).hexdigest()[:8]

                # Find centroid (most occurrences)
                centroid = max(sig_patterns, key=lambda p: p.occurrence_count)

                # Collect affected ACs
                affected_acs = set()
                for p in sig_patterns:
                    affected_acs.update(p.affected_acs)

                clusters.append(
                    PatternCluster(
                        cluster_id=cluster_id,
                        patterns=sig_patterns,
                        centroid_pattern=centroid,
                        common_error=sig,
                        affected_acs=affected_acs,
                        total_occurrences=sum(p.occurrence_count for p in sig_patterns),
                        cluster_label=f"{category.value}: {sig}",
                    )
                )

        # Sort by total occurrences
        clusters.sort(key=lambda c: c.total_occurrences, reverse=True)

        self._pattern_clusters = clusters
        return clusters

    def build_pattern_network(
        self,
        patterns: list[FailurePattern] | None = None,
    ) -> PatternNetwork:
        """Build pattern network graph for visualization.

        Creates a graph showing relationships between patterns and ACs.

        Args:
            patterns: Patterns to include (uses cached if None)

        Returns:
            PatternNetwork for visualization
        """
        if patterns is None:
            patterns = self._detected_patterns

        if not patterns:
            return PatternNetwork()

        nodes: list[PatternNetworkNode] = []
        edges: list[PatternNetworkEdge] = []
        seen_nodes: set[str] = set()

        # Add pattern nodes
        for p in patterns:
            if p.pattern_id not in seen_nodes:
                nodes.append(
                    PatternNetworkNode(
                        node_id=p.pattern_id,
                        node_type="pattern",
                        label=p.description[:50],
                        weight=p.occurrence_count,
                        metadata={
                            "category": p.category.value,
                            "severity": p.severity.value,
                            "confidence": p.confidence,
                        },
                    )
                )
                seen_nodes.add(p.pattern_id)

            # Add AC nodes and edges
            for ac_id in p.affected_acs:
                if ac_id not in seen_nodes:
                    nodes.append(
                        PatternNetworkNode(
                            node_id=ac_id,
                            node_type="ac",
                            label=ac_id,
                            weight=1.0,
                        )
                    )
                    seen_nodes.add(ac_id)

                # Add edge: pattern -> AC
                edges.append(
                    PatternNetworkEdge(
                        source_id=p.pattern_id,
                        target_id=ac_id,
                        edge_type="affects",
                        weight=p.occurrence_count,
                        label=f"affects ({p.occurrence_count}x)",
                    )
                )

        # Add inter-pattern relationships
        pattern_list = list(patterns)
        for i, p1 in enumerate(pattern_list):
            for p2 in pattern_list[i + 1:]:
                # Check for shared ACs
                shared_acs = set(p1.affected_acs) & set(p2.affected_acs)
                if shared_acs:
                    edges.append(
                        PatternNetworkEdge(
                            source_id=p1.pattern_id,
                            target_id=p2.pattern_id,
                            edge_type="related",
                            weight=len(shared_acs),
                            label=f"shared: {', '.join(list(shared_acs)[:3])}",
                        )
                    )

        return PatternNetwork(nodes=nodes, edges=edges)

    def get_summary(self) -> dict[str, Any]:
        """Get summary of detected patterns.

        Returns:
            Summary statistics and top patterns
        """
        if not self._detected_patterns:
            return {
                "total_patterns": 0,
                "patterns_by_category": {},
                "patterns_by_severity": {},
                "top_patterns": [],
            }

        by_category = Counter(p.category.value for p in self._detected_patterns)
        by_severity = Counter(p.severity.value for p in self._detected_patterns)

        return {
            "total_patterns": len(self._detected_patterns),
            "patterns_by_category": dict(by_category),
            "patterns_by_severity": dict(by_severity),
            "top_patterns": [
                p.to_dict()
                for p in sorted(
                    self._detected_patterns,
                    key=lambda p: p.occurrence_count,
                    reverse=True,
                )[:10]
            ],
        }

    # =========================================================================
    # Pattern Detection Methods
    # =========================================================================

    def _detect_spinning_patterns(
        self,
        failures: list[IterationData],
    ) -> list[FailurePattern]:
        """Detect spinning patterns (same error repeated)."""
        patterns: list[FailurePattern] = []

        # Group by error hash
        error_groups: dict[str, list[IterationData]] = {}
        for f in failures:
            error_hash = hashlib.md5(
                f.error_message[:200].encode()
            ).hexdigest()[:8]
            if error_hash not in error_groups:
                error_groups[error_hash] = []
            error_groups[error_hash].append(f)

        for error_hash, group in error_groups.items():
            if len(group) >= self._min_occurrences:
                # Detect if same error appears consecutively
                consecutive = self._count_consecutive(failures, group)
                if consecutive >= 3:
                    patterns.append(
                        FailurePattern(
                            pattern_id=f"spinning_{error_hash}",
                            category=PatternCategory.SPINNING,
                            severity=PatternSeverity.HIGH,
                            description=f"Same error repeated {consecutive} times consecutively",
                            error_signature=self._extract_error_signature(group[0].error_message),
                            affected_acs=tuple(set(f.ac_id for f in group if f.ac_id)),
                            iteration_ids=tuple(str(f.iteration_id) for f in group),
                            occurrence_count=len(group),
                            first_seen=min(f.timestamp for f in group),
                            last_seen=max(f.timestamp for f in group),
                            confidence=min(1.0, consecutive / 5),
                            root_cause_hypothesis="System is stuck in a loop, likely missing context or capability",
                        )
                    )

        return patterns

    def _detect_oscillation_patterns(
        self,
        failures: list[IterationData],
    ) -> list[FailurePattern]:
        """Detect oscillation patterns (A->B->A->B)."""
        patterns: list[FailurePattern] = []

        # Group by AC
        by_ac: dict[str, list[IterationData]] = {}
        for f in failures:
            ac_key = f.ac_id or "default"
            if ac_key not in by_ac:
                by_ac[ac_key] = []
            by_ac[ac_key].append(f)

        for ac_id, ac_failures in by_ac.items():
            if len(ac_failures) < 4:
                continue

            # Check for A-B-A-B pattern
            error_hashes = [
                hashlib.md5(f.error_message[:100].encode()).hexdigest()[:8]
                for f in ac_failures
            ]

            # Check if alternating
            for i in range(len(error_hashes) - 3):
                window = error_hashes[i:i + 4]
                if (
                    window[0] == window[2] and
                    window[1] == window[3] and
                    window[0] != window[1]
                ):
                    patterns.append(
                        FailurePattern(
                            pattern_id=f"oscillation_{ac_id}_{window[0]}_{window[1]}",
                            category=PatternCategory.OSCILLATION,
                            severity=PatternSeverity.HIGH,
                            description=f"Oscillating between two error states on {ac_id}",
                            error_signature=f"{window[0]} <-> {window[1]}",
                            affected_acs=(ac_id,) if ac_id != "default" else (),
                            iteration_ids=tuple(str(f.iteration_id) for f in ac_failures[i:i + 4]),
                            occurrence_count=4,
                            confidence=0.85,
                            root_cause_hypothesis="Fix attempts are contradictory, need different approach",
                        )
                    )
                    break

        return patterns

    def _detect_error_patterns(
        self,
        failures: list[IterationData],
    ) -> list[FailurePattern]:
        """Detect patterns based on error signatures."""
        patterns: list[FailurePattern] = []

        for sig_name, sig_regex in ERROR_SIGNATURES.items():
            matching = [
                f for f in failures
                if sig_regex.search(f.error_message)
            ]

            if len(matching) >= self._min_occurrences:
                severity = self._determine_severity(sig_name, len(matching))
                patterns.append(
                    FailurePattern(
                        pattern_id=f"error_{sig_name}",
                        category=PatternCategory.SYMPTOM,
                        severity=severity,
                        description=f"{sig_name.replace('_', ' ').title()} occurring frequently",
                        error_signature=sig_name,
                        affected_acs=tuple(set(f.ac_id for f in matching if f.ac_id)),
                        iteration_ids=tuple(str(f.iteration_id) for f in matching),
                        occurrence_count=len(matching),
                        first_seen=min(f.timestamp for f in matching),
                        last_seen=max(f.timestamp for f in matching),
                        confidence=0.7,
                        root_cause_hypothesis=self._get_error_hypothesis(sig_name),
                    )
                )

        return patterns

    def _detect_dependency_patterns(
        self,
        failures: list[IterationData],
    ) -> list[FailurePattern]:
        """Detect dependency-related patterns."""
        patterns: list[FailurePattern] = []

        blocked = [f for f in failures if f.outcome == IterationOutcome.BLOCKED]

        if len(blocked) >= self._min_occurrences:
            patterns.append(
                FailurePattern(
                    pattern_id="dependency_blocked",
                    category=PatternCategory.DEPENDENCY,
                    severity=PatternSeverity.CRITICAL,
                    description=f"{len(blocked)} iterations blocked by dependencies",
                    affected_acs=tuple(set(f.ac_id for f in blocked if f.ac_id)),
                    iteration_ids=tuple(str(f.iteration_id) for f in blocked),
                    occurrence_count=len(blocked),
                    confidence=0.9,
                    root_cause_hypothesis="Task ordering issue - some ACs must complete first",
                )
            )

        return patterns

    def _detect_stagnation_patterns(
        self,
        failures: list[IterationData],
    ) -> list[FailurePattern]:
        """Detect stagnation patterns (no progress)."""
        patterns: list[FailurePattern] = []

        stagnant = [f for f in failures if f.outcome == IterationOutcome.STAGNANT]

        if len(stagnant) >= self._min_occurrences:
            # Group by AC
            by_ac: dict[str, list[IterationData]] = {}
            for f in stagnant:
                ac_key = f.ac_id or "default"
                if ac_key not in by_ac:
                    by_ac[ac_key] = []
                by_ac[ac_key].append(f)

            for ac_id, ac_stagnant in by_ac.items():
                if len(ac_stagnant) >= 3:
                    patterns.append(
                        FailurePattern(
                            pattern_id=f"stagnation_{ac_id}",
                            category=PatternCategory.STAGNATION,
                            severity=PatternSeverity.HIGH,
                            description=f"No progress on {ac_id} for {len(ac_stagnant)} iterations",
                            affected_acs=(ac_id,) if ac_id != "default" else (),
                            iteration_ids=tuple(str(f.iteration_id) for f in ac_stagnant),
                            occurrence_count=len(ac_stagnant),
                            confidence=0.8,
                            root_cause_hypothesis="Task may require different approach or human guidance",
                        )
                    )

        return patterns

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _count_consecutive(
        self,
        all_failures: list[IterationData],
        target_group: list[IterationData],
    ) -> int:
        """Count max consecutive occurrences of target group in failures."""
        target_ids = set(f.iteration_id for f in target_group)
        max_consecutive = 0
        current_consecutive = 0

        for f in all_failures:
            if f.iteration_id in target_ids:
                current_consecutive += 1
                max_consecutive = max(max_consecutive, current_consecutive)
            else:
                current_consecutive = 0

        return max_consecutive

    def _extract_error_signature(self, error_message: str) -> str:
        """Extract signature from error message."""
        for sig_name, sig_regex in ERROR_SIGNATURES.items():
            if sig_regex.search(error_message):
                return sig_name
        return "unknown"

    def _determine_severity(self, sig_name: str, count: int) -> PatternSeverity:
        """Determine severity based on error type and count."""
        critical_errors = {"syntax_error", "import_error", "dependency_missing"}
        high_errors = {"assertion_error", "test_failed", "validation_failed"}

        if sig_name in critical_errors:
            return PatternSeverity.CRITICAL
        if sig_name in high_errors or count >= 10:
            return PatternSeverity.HIGH
        if count >= 5:
            return PatternSeverity.MEDIUM
        return PatternSeverity.LOW

    def _get_error_hypothesis(self, sig_name: str) -> str:
        """Get root cause hypothesis for error type."""
        hypotheses = {
            "import_error": "Missing or incorrectly named module/package",
            "type_error": "Type mismatch - check function signatures and data types",
            "attribute_error": "Object missing expected attribute - check class/object structure",
            "value_error": "Invalid value passed to function - check input validation",
            "syntax_error": "Python syntax issue - check for typos or formatting",
            "key_error": "Dictionary key not found - check data structure",
            "index_error": "List index out of range - check array bounds",
            "assertion_error": "Test assertion failed - check test logic or implementation",
            "name_error": "Variable not defined - check variable scope",
            "timeout_error": "Operation timed out - check for infinite loops or slow operations",
            "connection_error": "Network connection failed - check connectivity",
            "permission_error": "Insufficient permissions - check file/directory access",
            "file_not_found": "File does not exist - check file paths",
            "validation_failed": "Input validation failed - check constraints",
            "test_failed": "Test failed - implementation may not match requirements",
            "dependency_missing": "Required dependency not available",
        }
        return hypotheses.get(sig_name, "Unknown root cause - requires investigation")

    def _add_socratic_questions(
        self,
        patterns: list[FailurePattern],
    ) -> list[FailurePattern]:
        """Add Socratic questions to probe root causes."""
        enhanced: list[FailurePattern] = []

        base_questions = {
            PatternCategory.SPINNING: (
                "Why is the same error occurring repeatedly?",
                "What context is the model missing?",
                "Is there a fundamental capability gap?",
                "Would a different approach work better?",
            ),
            PatternCategory.OSCILLATION: (
                "Why are fix attempts contradicting each other?",
                "What is the essential conflict between approaches?",
                "Is there an underlying requirement mismatch?",
                "What would break this oscillation cycle?",
            ),
            PatternCategory.DEPENDENCY: (
                "What must be completed first?",
                "Is there an implicit ordering in the requirements?",
                "What are the prerequisite capabilities?",
                "Can the dependency be decoupled?",
            ),
            PatternCategory.STAGNATION: (
                "Why is no progress being made?",
                "Is the task properly scoped?",
                "What additional information is needed?",
                "Would human guidance help here?",
            ),
            PatternCategory.ROOT_CAUSE: (
                "What is the essential nature of this problem?",
                "Is this treating symptoms or causes?",
                "What assumption needs to be challenged?",
                "What would a first-principles solution look like?",
            ),
        }

        for p in patterns:
            questions = base_questions.get(p.category, (
                "What is the root cause?",
                "What would fix this permanently?",
            ))

            enhanced.append(
                FailurePattern(
                    pattern_id=p.pattern_id,
                    category=p.category,
                    severity=p.severity,
                    description=p.description,
                    error_signature=p.error_signature,
                    affected_acs=p.affected_acs,
                    iteration_ids=p.iteration_ids,
                    occurrence_count=p.occurrence_count,
                    first_seen=p.first_seen,
                    last_seen=p.last_seen,
                    confidence=p.confidence,
                    root_cause_hypothesis=p.root_cause_hypothesis,
                    socratic_questions=questions,
                    metadata=p.metadata,
                )
            )

        return enhanced


__all__ = [
    "PatternAnalyzer",
    "FailurePattern",
    "PatternCategory",
    "PatternSeverity",
    "PatternCluster",
    "PatternNetwork",
    "PatternNetworkNode",
    "PatternNetworkEdge",
]
