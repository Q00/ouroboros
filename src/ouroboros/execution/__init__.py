"""Execution module for Double Diamond cycle.

This module implements Phase 2 of the Ouroboros workflow:
- Double Diamond pattern: Discover → Define → Design → Deliver
- Recursive AC decomposition
- Phase transition management
- SubAgent isolation for child AC execution (Story 3.4)
"""

from ouroboros.execution.double_diamond import (
    CycleResult,
    DoubleDiamond,
    ExecutionError,
    Phase,
    PhaseContext,
    PhaseResult,
)
from ouroboros.execution.subagent import (
    SubAgentError,
    ValidationError,
    create_subagent_completed_event,
    create_subagent_failed_event,
    create_subagent_started_event,
    create_subagent_validated_event,
    validate_child_result,
)

__all__ = [
    # Double Diamond
    "CycleResult",
    "DoubleDiamond",
    "ExecutionError",
    "Phase",
    "PhaseContext",
    "PhaseResult",
    # SubAgent Isolation
    "SubAgentError",
    "ValidationError",
    "create_subagent_completed_event",
    "create_subagent_failed_event",
    "create_subagent_started_event",
    "create_subagent_validated_event",
    "validate_child_result",
]
