"""Ontological Analysis Events.

Events emitted during ontological analysis across phases.
These events support observability and audit trails for
the AOP-based ontological framework.

Event Types:
- ontology.analysis.passed: Analysis passed, execution continues
- ontology.analysis.violated: Analysis failed, execution may halt

Reference: docs/ontological-framework/aop-design.md
"""

from typing import Any
from uuid import uuid4

from pydantic import Field

from ouroboros.core.ontology_aspect import OntologicalJoinPoint
from ouroboros.events.base import BaseEvent


class OntologicalAnalysisEvent(BaseEvent):
    """Base event for ontological analysis.

    Common fields for all ontology-related events.
    """

    join_point: OntologicalJoinPoint
    """Which phase triggered this event."""

    confidence: float = 0.0
    """Analysis confidence (0.0-1.0)."""


class OntologicalPassedEvent(OntologicalAnalysisEvent):
    """Emitted when ontological analysis passes.

    The subject passed ontological scrutiny and execution continues.

    Event Type: ontology.analysis.passed
    """

    type: str = "ontology.analysis.passed"
    aggregate_type: str = "ontology"
    aggregate_id: str = Field(default_factory=lambda: str(uuid4()))

    def __init__(
        self,
        *,
        join_point: OntologicalJoinPoint,
        confidence: float = 1.0,
        **kwargs: Any,
    ) -> None:
        """Initialize passed event.

        Args:
            join_point: Which phase triggered this event.
            confidence: Analysis confidence.
            **kwargs: Additional BaseEvent fields.
        """
        super().__init__(
            join_point=join_point,
            confidence=confidence,
            data={
                "join_point": join_point.value,
                "confidence": confidence,
                "outcome": "passed",
            },
            **kwargs,
        )


class OntologicalViolationEvent(OntologicalAnalysisEvent):
    """Emitted when ontological analysis fails.

    The subject failed ontological scrutiny. Execution may halt
    depending on aspect configuration.

    Event Type: ontology.analysis.violated
    """

    type: str = "ontology.analysis.violated"
    aggregate_type: str = "ontology"
    aggregate_id: str = Field(default_factory=lambda: str(uuid4()))

    reasoning: tuple[str, ...] = ()
    """Why the analysis failed."""

    suggestions: tuple[str, ...] = ()
    """Refinement suggestions."""

    def __init__(
        self,
        *,
        join_point: OntologicalJoinPoint,
        confidence: float = 0.8,
        reasoning: tuple[str, ...] | list[str] = (),
        suggestions: tuple[str, ...] | list[str] = (),
        **kwargs: Any,
    ) -> None:
        """Initialize violation event.

        Args:
            join_point: Which phase triggered this event.
            confidence: Analysis confidence.
            reasoning: Why the analysis failed.
            suggestions: Refinement suggestions.
            **kwargs: Additional BaseEvent fields.
        """
        reasoning_tuple = tuple(reasoning) if isinstance(reasoning, list) else reasoning
        suggestions_tuple = tuple(suggestions) if isinstance(suggestions, list) else suggestions

        super().__init__(
            join_point=join_point,
            confidence=confidence,
            reasoning=reasoning_tuple,
            suggestions=suggestions_tuple,
            data={
                "join_point": join_point.value,
                "confidence": confidence,
                "outcome": "violated",
                "reasoning": reasoning_tuple,
                "suggestions": suggestions_tuple,
            },
            **kwargs,
        )


__all__ = [
    "OntologicalAnalysisEvent",
    "OntologicalPassedEvent",
    "OntologicalViolationEvent",
]
