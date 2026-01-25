"""Observability module for Ouroboros.

This module provides structured logging, drift measurement, and retrospective
analysis for observability across the application.

Main components:
- Logging: configure_logging, get_logger, bind_context, unbind_context
- Drift: DriftMeasurement, DriftMetrics for goal alignment tracking
- Retrospective: RetrospectiveAnalyzer for periodic self-assessment
"""

from ouroboros.observability.drift import (
    DRIFT_THRESHOLD,
    DriftMeasuredEvent,
    DriftMeasurement,
    DriftMetrics,
    DriftThresholdExceededEvent,
    calculate_constraint_drift,
    calculate_goal_drift,
    calculate_ontology_drift,
)
from ouroboros.observability.logging import (
    LoggingConfig,
    LogMode,
    bind_context,
    configure_logging,
    get_logger,
    unbind_context,
)
from ouroboros.observability.retrospective import (
    DEFAULT_RETROSPECTIVE_INTERVAL,
    HumanAttentionRequiredEvent,
    RetrospectiveAnalyzer,
    RetrospectiveCompletedEvent,
    RetrospectiveResult,
    should_trigger_retrospective,
)

__all__ = [
    # Logging
    "LogMode",
    "LoggingConfig",
    "bind_context",
    "configure_logging",
    "get_logger",
    "unbind_context",
    # Drift
    "DRIFT_THRESHOLD",
    "DriftMeasuredEvent",
    "DriftMeasurement",
    "DriftMetrics",
    "DriftThresholdExceededEvent",
    "calculate_constraint_drift",
    "calculate_goal_drift",
    "calculate_ontology_drift",
    # Retrospective
    "DEFAULT_RETROSPECTIVE_INTERVAL",
    "HumanAttentionRequiredEvent",
    "RetrospectiveAnalyzer",
    "RetrospectiveCompletedEvent",
    "RetrospectiveResult",
    "should_trigger_retrospective",
]
