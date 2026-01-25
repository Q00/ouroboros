"""Secondary Loop module for TODO management and batch processing.

This module implements Phase 6 of the Ouroboros execution model:
- TODO Registry: Captures improvements discovered during execution
- Secondary Loop Scheduler: Processes TODOs after primary goal achievement

Components:
    Todo: Immutable TODO item model
    Priority: TODO priority enum (HIGH, MEDIUM, LOW)
    TodoStatus: TODO lifecycle status enum
    TodoRegistry: Non-blocking TODO registration and persistence
    SecondaryLoopScheduler: Batch processing of TODOs after primary completion
    BatchStatus: Status of batch processing run
    BatchSummary: Summary of batch processing results
    TodoResult: Result of processing a single TODO
"""

from ouroboros.secondary.scheduler import (
    BatchStatus,
    BatchSummary,
    SecondaryLoopScheduler,
    TodoResult,
)
from ouroboros.secondary.todo_registry import (
    Priority,
    Todo,
    TodoRegistry,
    TodoStatus,
)

__all__ = [
    "BatchStatus",
    "BatchSummary",
    "Priority",
    "SecondaryLoopScheduler",
    "Todo",
    "TodoRegistry",
    "TodoResult",
    "TodoStatus",
]
