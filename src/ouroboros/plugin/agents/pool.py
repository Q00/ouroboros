"""Agent Pool for dynamic agent management and task execution.

This module provides:
- Dynamic pool of reusable agent instances
- Automatic scaling based on load
- Task queue with priority support
- Health monitoring and auto-recovery

Architecture:
- Pool manages AgentInstance lifecycle
- Tasks execute via ClaudeAgentAdapter
- Events emitted for state changes
- Health check for failed instances

Usage:
    pool = AgentPool(
        min_instances=2,
        max_instances=10,
        idle_timeout=300.0,
    )
    await pool.start()

    # Submit a task
    task_id = await pool.submit_task(
        agent_type="executor",
        prompt="Fix the bug in auth.py",
        priority=10,
    )

    # Wait for completion
    result = await pool.get_task_result(task_id)
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from ouroboros.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Awaitable

    from ouroboros.events.base import BaseEvent
    from ouroboros.plugin.agents.registry import AgentSpec
    from ouroboros.orchestrator.adapter import ClaudeAgentAdapter

log = get_logger(__name__)


# =============================================================================
# Agent State Enum
# =============================================================================


class AgentState(Enum):
    """Agent instance states."""

    IDLE = "idle"
    BUSY = "busy"
    FAILED = "failed"
    RECOVERING = "recovering"
    TERMINATED = "terminated"


# =============================================================================
# Agent Instance
# =============================================================================


@dataclass(slots=True)
class AgentInstance:
    """A single agent instance in the pool.

    Attributes:
        id: Unique instance identifier.
        spec: Agent specification for this instance.
        state: Current state of the agent.
        current_task: Task ID if busy, None otherwise.
        tasks_completed: Number of tasks successfully completed.
        tasks_failed: Number of tasks that failed.
        total_tokens_used: Estimated tokens consumed.
        last_activity: Timestamp of last activity.
        created_at: When this instance was created.
        error_message: Last error if in FAILED state.
    """

    id: str
    spec: AgentSpec
    state: AgentState = AgentState.IDLE
    current_task: str | None = None
    tasks_completed: int = 0
    tasks_failed: int = 0
    total_tokens_used: int = 0
    last_activity: float = field(default_factory=time.time)
    created_at: float = field(default_factory=time.time)
    error_message: str | None = None

    @property
    def idle_duration(self) -> float:
        """Seconds since last activity."""
        return time.time() - self.last_activity

    @property
    def age_seconds(self) -> float:
        """Seconds since creation."""
        return time.time() - self.created_at


# =============================================================================
# Task Request
# =============================================================================


@dataclass(slots=True, order=False)
class TaskRequest:
    """A task submitted to the agent pool.

    Attributes:
        id: Unique task identifier.
        agent_type: Agent name or role to execute the task.
        prompt: Task prompt/instructions.
        context: Additional context data.
        priority: Higher values = higher priority (default 0).
        dependencies: Task IDs that must complete first.
        callback: Optional async callback for progress updates.
        system_prompt: Optional override system prompt.
        tools: Optional override tool list.
        created_at: Task creation timestamp.
        started_at: Task execution start timestamp.
        completed_at: Task completion timestamp.
    """

    id: str
    agent_type: str
    prompt: str
    context: dict[str, Any]
    priority: int = 0
    dependencies: list[str] = field(default_factory=list)
    callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None
    system_prompt: str | None = None
    tools: list[str] | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None

    def __lt__(self, other: object) -> bool:
        """Compare tasks for priority queue ordering.

        Tasks are compared by (priority, created_at) tuple.
        Higher priority tasks come first. For equal priority,
        earlier tasks come first.
        """
        if not isinstance(other, TaskRequest):
            return NotImplemented
        # Higher priority first (reverse order), then earlier created first
        if self.priority != other.priority:
            return self.priority > other.priority
        return self.created_at < other.created_at

    @property
    def is_ready(self) -> bool:
        """Check if all dependencies are satisfied."""
        return True  # Dependencies managed by scheduler

    @property
    def duration_seconds(self) -> float | None:
        """Task execution duration if completed."""
        if self.started_at and self.completed_at:
            return self.completed_at - self.started_at
        return None


# =============================================================================
# Task Result
# =============================================================================


@dataclass(slots=True)
class TaskResult:
    """Result of a task execution.

    Attributes:
        task_id: Task identifier.
        success: Whether execution succeeded.
        result_data: Result data from execution.
        error_message: Error message if failed.
        messages: Agent messages from execution.
        duration_seconds: Execution duration.
    """

    task_id: str
    success: bool
    result_data: dict[str, Any] = field(default_factory=dict)
    error_message: str | None = None
    messages: tuple[str, ...] = field(default_factory=tuple)
    duration_seconds: float = 0.0


# =============================================================================
# Agent Pool Configuration
# =============================================================================


@dataclass(slots=True)
class AgentPoolConfig:
    """Configuration for agent pool.

    Attributes:
        min_instances: Minimum number of agents to maintain.
        max_instances: Maximum number of agents allowed.
        idle_timeout: Seconds before idle agent is terminated.
        health_check_interval: Seconds between health checks.
        task_timeout: Maximum seconds a task can run.
        enable_auto_scaling: Enable automatic scaling.
    """

    min_instances: int = 2
    max_instances: int = 10
    idle_timeout: float = 300.0
    health_check_interval: float = 60.0
    task_timeout: float = 1800.0
    enable_auto_scaling: bool = True


# =============================================================================
# Agent Pool
# =============================================================================


class AgentPool:
    """Dynamic agent pool with automatic scaling.

    The pool manages a set of agent instances that can execute tasks
    in parallel. Features include:

    - Automatic scaling based on load
    - Priority-based task queue
    - Health monitoring and recovery
    - Connection pooling

    Example:
        from ouroboros.plugin.agents.pool import AgentPool
        from ouroboros.orchestrator.adapter import ClaudeAgentAdapter

        adapter = ClaudeAgentAdapter()
        pool = AgentPool(adapter=adapter)
        await pool.start()

        # Submit task
        task_id = await pool.submit_task(
            agent_type="executor",
            prompt="Fix the bug",
            priority=10,
        )

        # Get result
        result = await pool.get_task_result(task_id)
        print(f"Success: {result.success}")

        await pool.stop()
    """

    def __init__(
        self,
        adapter: ClaudeAgentAdapter,
        registry: Any | None = None,
        config: AgentPoolConfig | None = None,
        event_store: Any | None = None,
    ) -> None:
        """Initialize the agent pool.

        Args:
            adapter: Claude Agent adapter for execution.
            registry: Optional AgentRegistry for agent lookup.
            config: Pool configuration.
            event_store: Optional event store for state tracking.
        """
        self._adapter = adapter
        self._registry = registry
        self._config = config or AgentPoolConfig()
        self._event_store = event_store

        # Pool state
        self._agents: dict[str, AgentInstance] = {}
        self._task_queue: asyncio.PriorityQueue[tuple[int, TaskRequest]] = (
            asyncio.PriorityQueue()
        )
        self._running_tasks: dict[str, asyncio.Task[None]] = {}
        self._task_results: dict[str, TaskResult] = {}
        self._task_waiters: dict[str, set[asyncio.Future[None]]] = {}

        # Background tasks
        self._dispatcher_task: asyncio.Task[None] | None = None
        self._scaler_task: asyncio.Task[None] | None = None
        self._health_task: asyncio.Task[None] | None = None

        # Shutdown flag
        self._shutdown = False

        log.info(
            "agents.pool.initialized",
            min_instances=self._config.min_instances,
            max_instances=self._config.max_instances,
            idle_timeout=self._config.idle_timeout,
        )

    async def start(self) -> None:
        """Start the agent pool with minimum instances.

        Spawns the minimum number of agents and starts background
        tasks for dispatching, scaling, and health checking.
        """
        log.info("agents.pool.starting")

        # Spawn minimum instances
        for i in range(self._config.min_instances):
            await self._spawn_agent(f"agent-{i}")

        # Start background tasks
        self._dispatcher_task = asyncio.create_task(self._task_dispatcher())
        self._scaler_task = asyncio.create_task(self._scale_monitor())
        self._health_task = asyncio.create_task(self._health_checker())

        log.info(
            "agents.pool.started",
            initial_agents=len(self._agents),
        )

    async def stop(self) -> None:
        """Stop the agent pool gracefully.

        Waits for running tasks to complete, then terminates all agents.
        """
        log.info("agents.pool.stopping")

        self._shutdown = True

        # Cancel background tasks
        if self._dispatcher_task:
            self._dispatcher_task.cancel()
        if self._scaler_task:
            self._scaler_task.cancel()
        if self._health_task:
            self._health_task.cancel()

        # Wait for running tasks
        if self._running_tasks:
            log.info(
                "agents.pool.waiting_tasks",
                count=len(self._running_tasks),
            )
            await asyncio.gather(*self._running_tasks.values(), return_exceptions=True)

        # Terminate all agents
        for agent_id in list(self._agents.keys()):
            await self._terminate_agent(agent_id)

        log.info(
            "agents.pool.stopped",
            final_agents=len(self._agents),
        )

    async def submit_task(
        self,
        agent_type: str,
        prompt: str,
        context: dict[str, Any] | None = None,
        priority: int = 0,
        callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        system_prompt: str | None = None,
        tools: list[str] | None = None,
    ) -> str:
        """Submit a task to the pool.

        Args:
            agent_type: Agent name or role to execute the task.
            prompt: Task prompt/instructions.
            context: Additional context data.
            priority: Higher values = higher priority.
            callback: Optional callback for progress updates.
            system_prompt: Optional override system prompt.
            tools: Optional override tool list.

        Returns:
            Task ID for tracking.
        """
        task_id = f"task-{uuid4().hex[:12]}"

        task = TaskRequest(
            id=task_id,
            agent_type=agent_type,
            prompt=prompt,
            context=context or {},
            priority=priority,
            callback=callback,
            system_prompt=system_prompt,
            tools=tools,
        )

        # Add to queue (priority queue uses negative for max-heap behavior)
        await self._task_queue.put((-priority, task))

        log.debug(
            "agents.pool.task_submitted",
            task_id=task_id,
            agent_type=agent_type,
            priority=priority,
            queue_size=self._task_queue.qsize(),
        )

        return task_id

    async def get_task_result(
        self,
        task_id: str,
        timeout: float | None = None,
    ) -> TaskResult:
        """Wait for and retrieve task result.

        Args:
            task_id: Task identifier.
            timeout: Maximum seconds to wait (None = wait forever).

        Returns:
            TaskResult when complete.

        Raises:
            asyncio.TimeoutError: If timeout expires before task completes.
        """
        # Check if result already available
        if task_id in self._task_results:
            return self._task_results[task_id]

        # Wait for result
        future: asyncio.Future[None] = asyncio.Future()

        if task_id not in self._task_waiters:
            self._task_waiters[task_id] = set()

        self._task_waiters[task_id].add(future)

        try:
            await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            # Remove from waiters
            self._task_waiters[task_id].discard(future)
            if not self._task_waiters[task_id]:
                del self._task_waiters[task_id]
            raise

        # Return result
        if task_id in self._task_results:
            return self._task_results[task_id]

        # Should not happen
        msg = f"Task {task_id} completed but result not found"
        raise RuntimeError(msg)

    async def _spawn_agent(self, agent_id: str) -> AgentInstance:
        """Spawn a new agent instance.

        Args:
            agent_id: Unique identifier for the agent.

        Returns:
            The created AgentInstance.
        """
        # Get default executor spec if no registry
        if self._registry:
            spec = self._registry.get_agent("executor")
        else:
            from ouroboros.plugin.agents.registry import (
                BUILTIN_AGENTS,
            )
            spec = BUILTIN_AGENTS.get("executor")

        if not spec:
            # Fallback to basic spec
            from ouroboros.plugin.agents.registry import AgentSpec, AgentRole
            spec = AgentSpec(
                name="executor",
                role=AgentRole.EXECUTION,
                model_preference="sonnet",
                system_prompt="You are an executor agent.",
                tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
                capabilities=("code", "implementation"),
                description="Default executor agent",
            )

        instance = AgentInstance(
            id=agent_id,
            spec=spec,
        )

        self._agents[agent_id] = instance

        await self._emit_event(
            "agents.pool.agent_spawned",
            {
                "agent_id": agent_id,
                "spec_name": spec.name,
                "spec_role": spec.role.value,
            },
        )

        log.info(
            "agents.pool.agent_spawned",
            agent_id=agent_id,
            spec=spec.name,
        )

        return instance

    async def _terminate_agent(self, agent_id: str) -> None:
        """Terminate an agent instance.

        Args:
            agent_id: Agent to terminate.
        """
        if agent_id not in self._agents:
            return

        agent = self._agents[agent_id]
        agent.state = AgentState.TERMINATED

        await self._emit_event(
            "agents.pool.agent_terminated",
            {
                "agent_id": agent_id,
                "tasks_completed": agent.tasks_completed,
                "tasks_failed": agent.tasks_failed,
                "uptime_seconds": agent.age_seconds,
            },
        )

        del self._agents[agent_id]

        log.debug(
            "agents.pool.agent_terminated",
            agent_id=agent_id,
        )

    async def _task_dispatcher(self) -> None:
        """Dispatch tasks from queue to available agents.

        Runs continuously as a background task.
        """
        log.info("agents.pool.dispatcher_started")

        while not self._shutdown:
            try:
                # Get next task with timeout
                priority, task = await asyncio.wait_for(
                    self._task_queue.get(),
                    timeout=1.0,
                )

                # Wait for available agent
                agent = await self._wait_for_agent(task.agent_type)
                if not agent:
                    log.warning(
                        "agents.pool.no_agent_available",
                        task=task.id,
                        agent_type=task.agent_type,
                    )
                    # Requeue
                    await self._task_queue.put((priority, task))
                    await asyncio.sleep(1.0)
                    continue

                # Execute task
                agent.state = AgentState.BUSY
                agent.current_task = task.id
                agent.last_activity = time.time()
                task.started_at = time.time()

                task_coro = self._execute_task(agent, task)
                self._running_tasks[task.id] = asyncio.create_task(task_coro)

            except asyncio.TimeoutError:
                continue
            except Exception as e:
                log.exception(
                    "agents.pool.dispatcher_error",
                    error=str(e),
                )

    async def _wait_for_agent(
        self,
        agent_type: str,
        timeout: float = 30.0,
    ) -> AgentInstance | None:
        """Wait for an available agent of the specified type.

        Args:
            agent_type: Agent name or role.
            timeout: Maximum seconds to wait.

        Returns:
            Available AgentInstance or None.
        """
        deadline = time.time() + timeout

        while time.time() < deadline:
            # Find idle agent
            for agent in self._agents.values():
                if (
                    agent.state == AgentState.IDLE
                    and agent.spec.name == agent_type
                ):
                    return agent

                # Check role match
                from ouroboros.plugin.agents.registry import AgentRole
                try:
                    role = AgentRole(agent_type)
                    if agent.state == AgentState.IDLE and agent.spec.role == role:
                        return agent
                except ValueError:
                    pass

            # No agent available, try to spawn if under limit
            if (
                self._config.enable_auto_scaling
                and len(self._agents) < self._config.max_instances
            ):
                new_id = f"agent-{uuid4().hex[:8]}"
                await self._spawn_agent(new_id)
                continue

            # Wait a bit
            await asyncio.sleep(0.1)

        return None

    async def _execute_task(
        self,
        agent: AgentInstance,
        task: TaskRequest,
    ) -> None:
        """Execute a task on an agent.

        Args:
            agent: Agent instance to use.
            task: Task to execute.
        """
        start_time = time.time()
        messages: list[str] = []

        try:
            log.debug(
                "agents.pool.task_started",
                task_id=task.id,
                agent_id=agent.id,
            )

            # Execute via adapter
            agent_msg_count = 0
            async for msg in self._adapter.execute_task(
                prompt=task.prompt,
                tools=task.tools or list(agent.spec.tools),
                system_prompt=task.system_prompt or agent.spec.system_prompt,
            ):
                messages.append(msg.content)
                agent_msg_count += 1

                # Callback for progress
                if task.callback:
                    try:
                        await task.callback({
                            "type": "progress",
                            "content": msg.content,
                            "agent_id": agent.id,
                            "task_id": task.id,
                        })
                    except Exception as e:
                        log.warning(
                            "agents.pool.callback_error",
                            task_id=task.id,
                            error=str(e),
                        )

            # Success
            agent.tasks_completed += 1
            agent.total_tokens_used += len(" ".join(messages)) // 4

            duration = time.time() - start_time

            result = TaskResult(
                task_id=task.id,
                success=True,
                result_data={"final_message": messages[-1] if messages else ""},
                messages=tuple(messages),
                duration_seconds=duration,
            )

            self._task_results[task.id] = result

            log.info(
                "agents.pool.task_completed",
                agent_id=agent.id,
                task_id=task.id,
                duration_seconds=duration,
                message_count=agent_msg_count,
            )

        except Exception as e:
            # Failure
            agent.tasks_failed += 1
            agent.error_message = str(e)

            duration = time.time() - start_time

            result = TaskResult(
                task_id=task.id,
                success=False,
                error_message=str(e),
                messages=tuple(messages),
                duration_seconds=duration,
            )

            self._task_results[task.id] = result

            log.error(
                "agents.pool.task_failed",
                agent_id=agent.id,
                task_id=task.id,
                error=str(e),
            )

        finally:
            # Cleanup
            agent.state = AgentState.IDLE
            agent.current_task = None
            agent.last_activity = time.time()
            task.completed_at = time.time()

            # Remove from running tasks
            self._running_tasks.pop(task.id, None)

            # Notify waiters
            if task.id in self._task_waiters:
                for waiter in self._task_waiters[task.id]:
                    if not waiter.done():
                        waiter.set_result(None)
                del self._task_waiters[task.id]

            # Emit completion event
            await self._emit_event(
                "agents.pool.task_completed",
                {
                    "task_id": task.id,
                    "agent_id": agent.id,
                    "success": result.success,
                    "duration_seconds": result.duration_seconds,
                },
            )

    async def _scale_monitor(self) -> None:
        """Monitor pool load and scale agents.

        Runs continuously as a background task.
        """
        log.info("agents.pool.scaler_started")

        while not self._shutdown:
            try:
                await asyncio.sleep(self._config.health_check_interval)

                idle_count = sum(
                    1 for a in self._agents.values() if a.state == AgentState.IDLE
                )
                busy_count = len(self._agents) - idle_count
                queue_size = self._task_queue.qsize()

                # Scale up if needed
                if (
                    idle_count == 0
                    and queue_size > 0
                    and len(self._agents) < self._config.max_instances
                ):
                    new_id = f"agent-{uuid4().hex[:8]}"
                    await self._spawn_agent(new_id)
                    log.info(
                        "agents.pool.scaled_up",
                        new_count=len(self._agents),
                    )

                # Scale down if too many idle
                elif (
                    idle_count > self._config.min_instances
                    and queue_size == 0
                ):
                    # Find oldest idle agent
                    idle_agents = [
                        a for a in self._agents.values() if a.state == AgentState.IDLE
                    ]
                    if idle_agents:
                        oldest = min(idle_agents, key=lambda a: a.last_activity)
                        if oldest.idle_duration > self._config.idle_timeout:
                            await self._terminate_agent(oldest.id)
                            log.info(
                                "agents.pool.scaled_down",
                                new_count=len(self._agents),
                            )

            except Exception as e:
                log.exception(
                    "agents.pool.scaler_error",
                    error=str(e),
                )

    async def _health_checker(self) -> None:
        """Monitor agent health and recover failed instances.

        Runs continuously as a background task.
        """
        log.info("agents.pool.health_checker_started")

        while not self._shutdown:
            try:
                await asyncio.sleep(self._config.health_check_interval)

                for agent in list(self._agents.values()):
                    # Check for failed agents
                    if agent.state == AgentState.FAILED:
                        log.warning(
                            "agents.pool.failed_agent_detected",
                            agent_id=agent.id,
                            error=agent.error_message,
                        )

                        # Mark for recovery
                        agent.state = AgentState.RECOVERING
                        # Reset to idle after recovery
                        await asyncio.sleep(5.0)
                        agent.state = AgentState.IDLE
                        agent.error_message = None
                        log.info(
                            "agents.pool.agent_recovered",
                            agent_id=agent.id,
                        )

                    # Check for stuck agents (task timeout)
                    if (
                        agent.state == AgentState.BUSY
                        and agent.current_task
                        and agent.last_activity > 0
                    ):
                        idle_time = time.time() - agent.last_activity
                        if idle_time > self._config.task_timeout:
                            log.warning(
                                "agents.pool.task_timeout",
                                agent_id=agent.id,
                                task_id=agent.current_task,
                                idle_seconds=idle_time,
                            )
                            # Reset agent state
                            agent.state = AgentState.IDLE
                            agent.current_task = None

            except Exception as e:
                log.exception(
                    "agents.pool.health_checker_error",
                    error=str(e),
                )

    async def _emit_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Emit an event to the event store if configured.

        Args:
            event_type: Type of event.
            data: Event data payload.
        """
        if not self._event_store:
            return

        from ouroboros.events.base import BaseEvent

        event = BaseEvent(
            type=event_type,
            aggregate_type="agent_pool",
            aggregate_id="pool",
            data=data,
        )
        await self._event_store.append(event)

    @property
    def stats(self) -> dict[str, Any]:
        """Get pool statistics.

        Returns:
            Dictionary with pool stats.
        """
        agents_list = list(self._agents.values())

        return {
            "total_agents": len(agents_list),
            "idle_agents": sum(1 for a in agents_list if a.state == AgentState.IDLE),
            "busy_agents": sum(1 for a in agents_list if a.state == AgentState.BUSY),
            "failed_agents": sum(1 for a in agents_list if a.state == AgentState.FAILED),
            "queue_size": self._task_queue.qsize(),
            "running_tasks": len(self._running_tasks),
            "completed_tasks": sum(a.tasks_completed for a in agents_list),
            "failed_tasks": sum(a.tasks_failed for a in agents_list),
            "total_tokens": sum(a.total_tokens_used for a in agents_list),
        }


__all__ = [
    "AgentInstance",
    "AgentPool",
    "AgentPoolConfig",
    "AgentState",
    "TaskRequest",
    "TaskResult",
]
