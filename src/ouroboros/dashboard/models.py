"""Unified Data Models for Dashboard and HOTL Analysis.

This module provides unified data models that support both:
1. Dashboard visualization (maze demos, timeline views)
2. HOTL convergence tracking with pattern analysis

The models maintain backward compatibility with existing dashboard code
while adding optional fields for advanced analysis features from gemini3/.

Design Philosophy:
- All new fields are Optional for backward compatibility
- Existing dashboard code continues to work unchanged
- Advanced features can be enabled incrementally
"""

from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

# Python 3.11+ has StrEnum, for earlier versions use string mixin
if sys.version_info >= (3, 11):
    from enum import StrEnum
else:
    class StrEnum(str, Enum):
        """String enum for Python < 3.11 compatibility."""
        pass

# Python 3.10+ supports slots=True in dataclass
DATACLASS_SLOTS = {"slots": True} if sys.version_info >= (3, 10) else {}


# =============================================================================
# Enums
# =============================================================================


class IterationOutcome(StrEnum):
    """Outcome of a single HOTL iteration.

    Used for tracking convergence and detecting patterns.

    Attributes:
        SUCCESS: AC was satisfied in this iteration
        FAILURE: AC failed validation
        PARTIAL: Some progress made but not complete
        STAGNANT: No progress detected
        BLOCKED: Blocked by dependency on another AC
    """

    SUCCESS = "success"
    FAILURE = "failure"
    PARTIAL = "partial"
    STAGNANT = "stagnant"
    BLOCKED = "blocked"


class Phase(StrEnum):
    """Double Diamond phases for iteration tracking.

    Attributes:
        DISCOVER: Exploring the problem space
        DEFINE: Narrowing down to root cause
        DEVELOP: Implementing solutions
        DELIVER: Finalizing and validating
    """

    DISCOVER = "Discover"
    DEFINE = "Define"
    DEVELOP = "Develop"
    DELIVER = "Deliver"


# =============================================================================
# Core Data Models
# =============================================================================


@dataclass(frozen=True, **DATACLASS_SLOTS)
class IterationData:
    """Unified iteration data supporting both dashboard and HOTL analysis.

    This model combines:
    - Dashboard fields (iteration_id, timestamp, phase, action, result, state, metrics, reasoning)
    - HOTL fields (ac_id, execution_id, outcome, artifact, error_message, drift_score, etc.)

    All HOTL-specific fields are optional with sensible defaults for backward compatibility.

    Attributes:
        # Core dashboard fields
        iteration_id: Unique identifier (int or str)
        timestamp: When this iteration occurred
        phase: Current phase (Discover/Define/Develop/Deliver)
        action: What action was taken
        result: Outcome description
        state: Current state representation (e.g., maze state)
        metrics: Quantitative metrics for this iteration
        reasoning: LLM reasoning if available

        # HOTL analysis fields (optional)
        ac_id: Acceptance criterion being worked on
        execution_id: Parent execution identifier
        outcome: Structured outcome enum (SUCCESS/FAILURE/etc)
        artifact: The generated artifact/code
        error_message: Error message if failed
        drift_score: Semantic drift from goal (0.0-1.0)
        confidence: Model confidence in outcome (0.0-1.0)
        model_used: Which model produced the artifact
        token_count: Tokens used in this iteration
        metadata: Additional iteration metadata
    """

    # Core dashboard fields
    iteration_id: int | str
    timestamp: datetime
    phase: str
    action: str
    result: str
    state: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, float] = field(default_factory=dict)
    reasoning: str = ""

    # HOTL analysis fields (optional)
    ac_id: str = ""
    execution_id: str = ""
    outcome: IterationOutcome | None = None
    artifact: str = ""
    error_message: str = ""
    drift_score: float = 0.0
    confidence: float = 0.5
    model_used: str = ""
    token_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def compute_hash(self) -> str:
        """Compute content hash for deduplication.

        Returns:
            SHA256 hash of artifact and error (first 16 chars)
        """
        content = f"{self.artifact}:{self.error_message}"
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def to_context_string(self) -> str:
        """Convert to context string for LLM prompts.

        Returns:
            Formatted string representation for context
        """
        outcome_emoji = {
            IterationOutcome.SUCCESS: "SUCCESS",
            IterationOutcome.FAILURE: "FAILURE",
            IterationOutcome.PARTIAL: "PARTIAL",
            IterationOutcome.STAGNANT: "STAGNANT",
            IterationOutcome.BLOCKED: "BLOCKED",
        }

        parts = [
            f"## Iteration {self.iteration_id}",
            f"Phase: {self.phase}",
            f"Action: {self.action}",
            f"Result: {self.result}",
            f"Timestamp: {self.timestamp.isoformat()}",
        ]

        if self.ac_id:
            parts.append(f"AC: {self.ac_id}")

        if self.outcome:
            parts.append(f"Outcome: {outcome_emoji.get(self.outcome, 'UNKNOWN')}")
            parts.append(f"Drift: {self.drift_score:.3f}")
            parts.append(f"Confidence: {self.confidence:.3f}")

        if self.error_message:
            parts.append(f"Error: {self.error_message[:500]}")

        if self.reasoning:
            parts.append(f"Reasoning: {self.reasoning[:500]}")

        if self.artifact:
            artifact_preview = self.artifact[:1000]
            if len(self.artifact) > 1000:
                artifact_preview += "... [truncated]"
            parts.append(f"Artifact Preview:\n```\n{artifact_preview}\n```")

        return "\n".join(parts)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization.

        Returns:
            Dictionary representation
        """
        return {
            "iteration_id": self.iteration_id,
            "timestamp": self.timestamp.isoformat(),
            "phase": self.phase,
            "action": self.action,
            "result": self.result,
            "state": self.state,
            "metrics": self.metrics,
            "reasoning": self.reasoning,
            "ac_id": self.ac_id,
            "execution_id": self.execution_id,
            "outcome": self.outcome.value if self.outcome else None,
            "artifact": self.artifact,
            "error_message": self.error_message,
            "drift_score": self.drift_score,
            "confidence": self.confidence,
            "model_used": self.model_used,
            "token_count": self.token_count,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> IterationData:
        """Create IterationData from dictionary.

        Args:
            data: Dictionary with iteration fields

        Returns:
            IterationData instance
        """
        timestamp = data.get("timestamp")
        if isinstance(timestamp, str):
            timestamp = datetime.fromisoformat(timestamp)

        outcome = data.get("outcome")
        if isinstance(outcome, str):
            outcome = IterationOutcome(outcome)

        return cls(
            iteration_id=data["iteration_id"],
            timestamp=timestamp or datetime.now(),
            phase=data.get("phase", "Discover"),
            action=data.get("action", ""),
            result=data.get("result", ""),
            state=data.get("state", {}),
            metrics=data.get("metrics", {}),
            reasoning=data.get("reasoning", ""),
            ac_id=data.get("ac_id", ""),
            execution_id=data.get("execution_id", ""),
            outcome=outcome,
            artifact=data.get("artifact", ""),
            error_message=data.get("error_message", ""),
            drift_score=float(data.get("drift_score", 0.0)),
            confidence=float(data.get("confidence", 0.5)),
            model_used=data.get("model_used", ""),
            token_count=int(data.get("token_count", 0)),
            metadata=data.get("metadata", {}),
        )


@dataclass
class ConvergenceState:
    """Current state of HOTL convergence.

    Tracks overall progress across all iterations and ACs.

    Attributes:
        total_iterations: Total iterations tracked
        successful_iterations: Count of successful iterations
        failed_iterations: Count of failed iterations
        satisfaction_percentage: % of ACs satisfied (0-100)
        convergence_rate: Rate of progress (iterations per success)
        is_converging: Whether system is making progress
        is_stagnant: Whether system is stuck
        estimated_remaining: Estimated iterations to convergence
        ac_satisfaction: Per-AC satisfaction status
        context_utilization: % of context window used
    """

    total_iterations: int = 0
    successful_iterations: int = 0
    failed_iterations: int = 0
    satisfaction_percentage: float = 0.0
    convergence_rate: float = 0.0
    is_converging: bool = True
    is_stagnant: bool = False
    estimated_remaining: int = -1  # -1 = unknown
    ac_satisfaction: dict[str, bool] = field(default_factory=dict)
    context_utilization: float = 0.0

    @property
    def partial_iterations(self) -> int:
        """Count of partial progress iterations."""
        return self.total_iterations - self.successful_iterations - self.failed_iterations


@dataclass
class ConvergenceCurvePoint:
    """Single point on the convergence curve.

    Used for visualization of progress over time.
    """

    iteration_number: int
    timestamp: datetime
    satisfaction_percentage: float
    ac_id: str
    outcome: IterationOutcome
    cumulative_successes: int
    cumulative_failures: int


# =============================================================================
# Analysis Result Models
# =============================================================================


@dataclass(frozen=True, **DATACLASS_SLOTS)
class AnalysisInsight:
    """Insight from Gemini 3 analysis.

    Attributes:
        insight_type: Category of insight (pattern/anomaly/recommendation/critical)
        title: Short title for the insight
        description: Detailed description
        confidence: Confidence score (0-1)
        affected_iterations: List of iteration IDs this insight relates to
        evidence: Supporting evidence from the history
    """

    insight_type: str  # "pattern", "anomaly", "recommendation", "critical"
    title: str
    description: str
    confidence: float
    affected_iterations: list[int] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)


@dataclass(frozen=True, **DATACLASS_SLOTS)
class ProgressTrajectory:
    """Multi-dimensional progress tracking.

    Attributes:
        dimension: What dimension this tracks (efficiency/correctness/coverage)
        values: List of (iteration_id, value) tuples
        trend: Overall trend direction
        inflection_points: Key turning points
    """

    dimension: str
    values: list[tuple[int, float]] = field(default_factory=list)
    trend: str = "stable"  # "improving", "degrading", "stable", "volatile"
    inflection_points: list[int] = field(default_factory=list)


@dataclass
class FullHistoryAnalysis:
    """Complete analysis result from Gemini 3.

    Attributes:
        total_iterations: Number of iterations analyzed
        token_count: Approximate token count of the input
        analysis_time_ms: Time taken for analysis in milliseconds
        insights: List of discovered insights
        trajectories: Multi-dimensional progress trajectories
        devil_advocate_critique: Critical analysis of the approach
        summary: Executive summary
        raw_response: Raw JSON response from Gemini
    """

    total_iterations: int = 0
    token_count: int = 0
    analysis_time_ms: float = 0.0
    insights: list[AnalysisInsight] = field(default_factory=list)
    trajectories: list[ProgressTrajectory] = field(default_factory=list)
    devil_advocate_critique: str = ""
    summary: str = ""
    raw_response: dict[str, Any] = field(default_factory=dict)


__all__ = [
    # Enums
    "IterationOutcome",
    "Phase",
    # Core models
    "IterationData",
    "ConvergenceState",
    "ConvergenceCurvePoint",
    # Analysis models
    "AnalysisInsight",
    "ProgressTrajectory",
    "FullHistoryAnalysis",
]
