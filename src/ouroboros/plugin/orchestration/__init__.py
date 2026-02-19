"""Plugin orchestration module for task scheduling and routing.

This package provides:
- Router: Smart model tier selection (PAL routing)
- Scheduler: Parallel task execution with dependency resolution
- Hooks: Event system for agent coordination

Architecture:
- Router extends routing.complexity for complexity estimation
- Scheduler uses asyncio for parallel execution
- Hooks integrate with events.base
"""

from ouroboros.plugin.orchestration.router import (
    ModelRouter,
    RoutingContext,
)
from ouroboros.plugin.orchestration.scheduler import (
    ScheduledTask,
    Scheduler,
    SchedulerConfig,
    TaskGraph,
)

__all__ = [
    "ModelRouter",
    "RoutingContext",
    "Scheduler",
    "SchedulerConfig",
    "ScheduledTask",
    "TaskGraph",
]
