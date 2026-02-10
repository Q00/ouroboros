"""Task Scheduler for parallel execution with dependency resolution.

This module provides:
- Parallel task execution with asyncio
- Dependency graph resolution
- Priority queuing
- Progress tracking

Architecture:
- Uses AgentPool for task execution
- Resolves dependencies before execution
- Executes independent tasks in parallel

Usage:
    scheduler = Scheduler(pool=agent_pool)
    await scheduler.start()

    # Schedule tasks
    task1 = ScheduledTask(
        id="task1",
        agent_type="executor",
        prompt="Do something",
    )
    await scheduler.schedule(task1)

    # Execute all
    results = await scheduler.execute_all()
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from ouroboros.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from ouroboros.plugin.agents.pool import AgentPool

log = get_logger(__name__)


# =============================================================================
# Task State Enum
# =============================================================================


class TaskState(Enum):
    """Task execution states."""

    PENDING = "pending"
    READY = "ready"  # Dependencies satisfied
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


# =============================================================================
# Scheduled Task
# =============================================================================


@dataclass(slots=True)
class ScheduledTask:
    """A task scheduled for execution.

    Attributes:
        id: Unique task identifier.
        agent_type: Agent name or role to execute.
        prompt: Task prompt/instructions.
        dependencies: List of task IDs this task depends on.
        priority: Higher values = higher priority.
        state: Current execution state.
        context: Additional context data.
        system_prompt: Optional override system prompt.
        tools: Optional override tool list.
        timeout_seconds: Maximum execution time.
        retry_count: Number of retries attempted.
        max_retries: Maximum number of retries.
        result: Task result if completed.
        error: Error message if failed.
        created_at: Task creation timestamp.
        started_at: Task start timestamp.
        completed_at: Task completion timestamp.
    """

    id: str
    agent_type: str
    prompt: str
    dependencies: list[str] = field(default_factory=list)
    priority: int = 0
    state: TaskState = TaskState.PENDING
    context: dict[str, Any] = field(default_factory=dict)
    system_prompt: str | None = None
    tools: list[str] | None = None
    timeout_seconds: float | None = None
    retry_count: int = 0
    max_retries: int = 3
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    created_at: float = field(default_factory=lambda: datetime.now(UTC).timestamp())
    started_at: float | None = None
    completed_at: float | None = None

    @property
    def is_ready(self) -> bool:
        """Check if task is ready to run (dependencies satisfied)."""
        return self.state == TaskState.READY

    @property
    def is_terminal(self) -> bool:
        """Check if task is in a terminal state."""
        return self.state in {
            TaskState.COMPLETED,
            TaskState.FAILED,
            TaskState.SKIPPED,
            TaskState.CANCELLED,
        }

    @property
    def duration_seconds(self) -> float | None:
        """Get execution duration if completed."""
        if self.started_at and self.completed_at:
            return self.completed_at - self.started_at
        return None


# =============================================================================
# Task Graph
# =============================================================================


@dataclass(slots=True)
class TaskGraph:
    """Dependency graph for tasks.

    Attributes:
        tasks: Dictionary of all tasks.
        execution_levels: List of task levels that can run in parallel.
        dependents_map: Map of task ID -> tasks that depend on it.
    """

    tasks: dict[str, ScheduledTask] = field(default_factory=dict)
    execution_levels: list[list[str]] = field(default_factory=list)
    dependents_map: dict[str, set[str]] = field(default_factory=dict)

    @property
    def total_tasks(self) -> int:
        """Total number of tasks."""
        return len(self.tasks)

    @property
    def total_levels(self) -> int:
        """Number of execution levels."""
        return len(self.execution_levels)

    def get_task(self, task_id: str) -> ScheduledTask | None:
        """Get task by ID."""
        return self.tasks.get(task_id)

    def get_dependencies(self, task_id: str) -> list[ScheduledTask]:
        """Get dependencies for a task."""
        task = self.tasks.get(task_id)
        if not task:
            return []
        result: list[ScheduledTask] = []
        for d in task.dependencies:
            t = self.tasks.get(d)
            if t:
                result.append(t)
        return result

    def get_dependents(self, task_id: str) -> list[ScheduledTask]:
        """Get tasks that depend on this task."""
        dependent_ids = self.dependents_map.get(task_id, set())
        result: list[ScheduledTask] = []
        for d in dependent_ids:
            t = self.tasks.get(d)
            if t:
                result.append(t)
        return result


# =============================================================================
# Scheduler Configuration
# =============================================================================


@dataclass(slots=True)
class SchedulerConfig:
    """Configuration for task scheduler.

    Attributes:
        max_parallel_tasks: Maximum tasks running simultaneously.
        enable_priority_scheduling: Use priority for task ordering.
        timeout_seconds: Default task timeout.
        retry_delay_seconds: Delay between retries.
        enable_progress_callbacks: Enable progress callback invocation.
    """

    max_parallel_tasks: int = 5
    enable_priority_scheduling: bool = True
    timeout_seconds: float = 1800.0
    retry_delay_seconds: float = 5.0
    enable_progress_callbacks: bool = True


# =============================================================================
# Task Scheduler
# =============================================================================


class Scheduler:
    """Task scheduler for parallel execution with dependency resolution.

    The scheduler manages task execution across an agent pool:
    - Builds dependency graphs from task dependencies
    - Executes independent tasks in parallel
    - Respects task priorities
    - Handles retries and timeouts
    - Tracks progress and results

    Example:
        from ouroboros.plugin.agents.pool import AgentPool
        from ouroboros.plugin.orchestration.scheduler import Scheduler, ScheduledTask

        pool = AgentPool(adapter=adapter)
        await pool.start()

        scheduler = Scheduler(pool=pool)
        await scheduler.start()

        # Create tasks
        task1 = ScheduledTask(
            id="task1",
            agent_type="executor",
            prompt="Fix the bug",
        )
        task2 = ScheduledTask(
            id="task2",
            agent_type="verifier",
            prompt="Verify the fix",
            dependencies=["task1"],
        )

        # Schedule tasks
        await scheduler.schedule(task1)
        await scheduler.schedule(task2)

        # Execute all
        results = await scheduler.execute_all()

        print(f"Task 1: {results['task1'].state}")
        print(f"Task 2: {results['task2'].state}")
    """

    def __init__(
        self,
        pool: AgentPool,
        config: SchedulerConfig | None = None,
    ) -> None:
        """Initialize the scheduler.

        Args:
            pool: Agent pool for task execution.
            config: Scheduler configuration.
        """
        self._pool = pool
        self._config = config or SchedulerConfig()

        # Task tracking
        self._tasks: dict[str, ScheduledTask] = {}
        self._graph = TaskGraph()
        self._results: dict[str, dict[str, Any]] = {}

        # Execution state
        self._running = False
        self._current_semaphore: asyncio.Semaphore | None = None

        log.info(
            "task_scheduler.initialized",
            max_parallel=self._config.max_parallel_tasks,
        )

    async def start(self) -> None:
        """Start the scheduler."""
        self._running = True
        self._current_semaphore = asyncio.Semaphore(self._config.max_parallel_tasks)
        log.info("task_scheduler.started")

    async def stop(self) -> None:
        """Stop the scheduler."""
        self._running = False
        log.info("task_scheduler.stopped")

    async def schedule(
        self,
        task: ScheduledTask,
    ) -> None:
        """Schedule a task for execution.

        Args:
            task: Task to schedule.
        """
        self._tasks[task.id] = task
        self._graph.tasks[task.id] = task

        # Build dependents map
        for dep_id in task.dependencies:
            if dep_id not in self._graph.dependents_map:
                self._graph.dependents_map[dep_id] = set()
            self._graph.dependents_map[dep_id].add(task.id)

        log.debug(
            "task_scheduler.task_scheduled",
            task_id=task.id,
            dependencies=len(task.dependencies),
        )

    async def schedule_batch(
        self,
        tasks: list[ScheduledTask],
    ) -> None:
        """Schedule multiple tasks at once.

        Args:
            tasks: List of tasks to schedule.
        """
        for task in tasks:
            await self.schedule(task)

        # Build execution levels
        await self._build_execution_levels()

    async def _build_execution_levels(self) -> None:
        """Build execution levels from dependency graph.

        Levels represent groups of tasks that can run in parallel.
        """
        levels: list[list[str]] = []
        remaining = set(self._tasks.keys())
        processed: set[str] = set()

        while remaining:
            # Find tasks with all dependencies processed
            ready = []
            for task_id in list(remaining):
                task = self._tasks[task_id]
                deps_satisfied = all(
                    dep in processed for dep in task.dependencies
                )
                if deps_satisfied:
                    ready.append(task_id)

            if not ready:
                # Circular dependency or missing dependency
                log.warning(
                    "task_scheduler.circular_dependency",
                    remaining=list(remaining),
                )
                # Add remaining as next level anyway
                ready = list(remaining)

            levels.append(ready)
            processed.update(ready)
            remaining -= set(ready)

        self._graph.execution_levels = levels

        log.debug(
            "task_scheduler.execution_levels_built",
            levels=len(levels),
        )

    async def execute_all(
        self,
        progress_callback: Callable[[str, TaskState, dict[str, Any]], Any] | None = None,
    ) -> dict[str, ScheduledTask]:
        """Execute all scheduled tasks.

        Args:
            progress_callback: Optional callback for progress updates.

        Returns:
            Dictionary mapping task ID to final task state.
        """
        if not self._running:
            msg = "Scheduler not started"
            raise RuntimeError(msg)

        # Build execution levels if not already built
        if not self._graph.execution_levels:
            await self._build_execution_levels()

        log.info(
            "task_scheduler.execution_started",
            total_tasks=len(self._tasks),
            levels=len(self._graph.execution_levels),
        )

        # Execute level by level
        for level_idx, level_tasks in enumerate(self._graph.execution_levels):
            log.debug(
                "task_scheduler.level_started",
                level=level_idx + 1,
                tasks=len(level_tasks),
            )

            # Execute tasks in this level in parallel
            level_results = await asyncio.gather(
                *[
                    self._execute_task(task_id, progress_callback)
                    for task_id in level_tasks
                ],
                return_exceptions=True,
            )

            # Check for failures
            for task_id, result in zip(level_tasks, level_results):
                if isinstance(result, Exception):
                    task = self._tasks[task_id]
                    task.state = TaskState.FAILED
                    task.error = str(result)

                    # Skip dependent tasks
                    await self._skip_dependents(task_id)

            log.debug(
                "task_scheduler.level_completed",
                level=level_idx + 1,
            )

        log.info("task_scheduler.execution_completed")
        return self._tasks

    async def _execute_task(
        self,
        task_id: str,
        progress_callback: Callable[[str, TaskState, dict[str, Any]], Any] | None = None,
    ) -> None:
        """Execute a single task.

        Args:
            task_id: Task to execute.
            progress_callback: Optional progress callback.
        """
        task = self._tasks[task_id]

        if task.state != TaskState.PENDING:
            return

        assert self._current_semaphore is not None
        async with self._current_semaphore:
            try:
                task.state = TaskState.RUNNING
                task.started_at = datetime.now(UTC).timestamp()

                if progress_callback and self._config.enable_progress_callbacks:
                    await progress_callback(task_id, task.state, {})

                log.debug(
                    "task_scheduler.task_started",
                    task_id=task_id,
                    agent_type=task.agent_type,
                )

                # Submit to agent pool
                pool_task_id = await self._pool.submit_task(
                    agent_type=task.agent_type,
                    prompt=task.prompt,
                    context=task.context,
                    priority=task.priority,
                    system_prompt=task.system_prompt,
                    tools=task.tools,
                )

                # Wait for result with timeout
                timeout = task.timeout_seconds or self._config.timeout_seconds
                result = await asyncio.wait_for(
                    self._pool.get_task_result(pool_task_id),
                    timeout=timeout,
                )

                if result.success:
                    task.state = TaskState.COMPLETED
                    task.result = result.result_data
                else:
                    # Check if we should retry
                    if task.retry_count < task.max_retries:
                        task.retry_count += 1
                        task.state = TaskState.PENDING

                        await asyncio.sleep(self._config.retry_delay_seconds)

                        # Retry
                        await self._execute_task(task_id, progress_callback)
                        return
                    else:
                        task.state = TaskState.FAILED
                        task.error = result.error_message

                        # Skip dependent tasks
                        await self._skip_dependents(task_id)

            except asyncio.TimeoutError:
                task.state = TaskState.FAILED
                task.error = "Task timeout"

                await self._skip_dependents(task_id)

                log.warning(
                    "task_scheduler.task_timeout",
                    task_id=task_id,
                )

            except Exception as e:
                task.state = TaskState.FAILED
                task.error = str(e)

                await self._skip_dependents(task_id)

                log.error(
                    "task_scheduler.task_failed",
                    task_id=task_id,
                    error=str(e),
                )

            finally:
                task.completed_at = datetime.now(UTC).timestamp()

                if progress_callback and self._config.enable_progress_callbacks:
                    await progress_callback(task_id, task.state, task.result)

    async def _skip_dependents(self, failed_task_id: str) -> None:
        """Skip tasks that depend on a failed task.

        Args:
            failed_task_id: Task that failed.
        """
        dependents = self._graph.get_dependents(failed_task_id)

        for dependent in dependents:
            if dependent.state == TaskState.PENDING:
                dependent.state = TaskState.SKIPPED
                dependent.error = f"Dependency failed: {failed_task_id}"

                log.debug(
                    "task_scheduler.task_skipped",
                    task_id=dependent.id,
                    reason=f"dependency {failed_task_id} failed",
                )

                # Recursively skip dependents
                await self._skip_dependents(dependent.id)

    def get_task(self, task_id: str) -> ScheduledTask | None:
        """Get a task by ID.

        Args:
            task_id: Task identifier.

        Returns:
            ScheduledTask or None if not found.
        """
        return self._tasks.get(task_id)

    def get_statistics(self) -> dict[str, Any]:
        """Get scheduler statistics.

        Returns:
            Dictionary with scheduler metrics.
        """
        total = len(self._tasks)
        by_state: dict[str, int] = {s.value: 0 for s in TaskState}

        for task in self._tasks.values():
            by_state[task.state.value] += 1

        return {
            "total_tasks": total,
            "by_state": by_state,
            "execution_levels": len(self._graph.execution_levels),
            "running": self._running,
        }


__all__ = [
    "Scheduler",
    "SchedulerConfig",
    "ScheduledTask",
    "TaskGraph",
    "TaskState",
]
