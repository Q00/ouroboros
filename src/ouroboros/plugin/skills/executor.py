"""Skill Executor with composition support.

This module provides a skill executor that:
- Invokes skills via the Skill tool API
- Supports skill-to-skill composition
- Manages execution context with isolation
- Handles errors with Result types
- Tracks execution history
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

import structlog

from ouroboros.core.types import Result
from ouroboros.plugin.skills.registry import SkillRegistry, get_registry

log = structlog.get_logger()


class ExecutionStatus(Enum):
    """Status of a skill execution."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class ExecutionContext:
    """Execution context for a skill invocation.

    Provides isolated storage for skill execution data,
    including user input, accumulated results, and metadata.

    Attributes:
        skill_name: Name of the skill being executed.
        user_input: Original user input that triggered the skill.
        arguments: Parsed arguments for the skill.
        parent_id: ID of parent execution if composed.
        state: Isolated state dictionary for the skill.
        metadata: Additional execution metadata.
    """

    skill_name: str
    user_input: str
    arguments: dict[str, Any]
    parent_id: str | None = None
    state: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        """Get a value from the context state."""
        return self.state.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """Set a value in the context state."""
        self.state[key] = value

    def update(self, data: dict[str, Any]) -> None:
        """Update the context state with multiple values."""
        self.state.update(data)


@dataclass
class ExecutionResult:
    """Result of a skill execution.

    Attributes:
        status: The execution status.
        output: The skill's output text/content.
        error: Error message if execution failed.
        duration_ms: Execution duration in milliseconds.
        context: The execution context (may be modified by skill).
        metadata: Additional result metadata.
    """

    status: ExecutionStatus
    output: str = ""
    error: str = ""
    duration_ms: float = 0.0
    context: ExecutionContext | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_success(self) -> bool:
        """Check if execution completed successfully."""
        return self.status == ExecutionStatus.COMPLETED

    @property
    def is_failure(self) -> bool:
        """Check if execution failed."""
        return self.status == ExecutionStatus.FAILED


@dataclass
class ExecutionRecord:
    """Historical record of a skill execution.

    Attributes:
        id: Unique execution ID.
        skill_name: Name of the executed skill.
        status: Final execution status.
        started_at: When execution started.
        completed_at: When execution completed.
        result: The execution result.
    """

    id: str
    skill_name: str
    status: ExecutionStatus
    started_at: datetime
    completed_at: datetime | None
    result: ExecutionResult


class SkillExecutor:
    """Executor for skill invocations with composition support.

    The executor manages skill execution lifecycle, provides isolated
    context for each execution, and tracks execution history.

    Features:
        - Context isolation between executions
        - Skill-to-skill composition
        - Execution history tracking
        - Error handling with Result types
    """

    def __init__(self, registry: SkillRegistry | None = None) -> None:
        """Initialize the skill executor.

        Args:
            registry: Optional skill registry. Uses global singleton if not provided.
        """
        self._registry = registry or get_registry()
        self._history: list[ExecutionRecord] = []
        self._active_executions: dict[str, ExecutionContext] = {}

    @property
    def registry(self) -> SkillRegistry:
        """Get the associated skill registry."""
        return self._registry

    def get_history(
        self,
        skill_name: str | None = None,
        limit: int = 100,
    ) -> list[ExecutionRecord]:
        """Get execution history.

        Args:
            skill_name: Filter by skill name if provided.
            limit: Maximum number of records to return.

        Returns:
            List of execution records, most recent first.
        """
        history = self._history
        if skill_name:
            history = [r for r in history if r.skill_name == skill_name]
        return history[:limit]

    def get_active_execution(self, execution_id: str) -> ExecutionContext | None:
        """Get an active execution context by ID.

        Args:
            execution_id: The execution ID.

        Returns:
            The execution context if active, None otherwise.
        """
        return self._active_executions.get(execution_id)

    async def execute(
        self,
        skill_name: str,
        user_input: str,
        arguments: dict[str, Any] | None = None,
        parent_id: str | None = None,
    ) -> Result[ExecutionResult, str]:
        """Execute a skill by name.

        Args:
            skill_name: Name of the skill to execute.
            user_input: Original user input that triggered execution.
            arguments: Optional arguments for the skill.
            parent_id: Optional parent execution ID for composition.

        Returns:
            Result containing the execution result or an error message.
        """
        import time
        import uuid

        execution_id = str(uuid.uuid4())
        started_at = datetime.now(UTC)
        arguments = arguments or {}

        # Get skill from registry
        skill = self._registry.get_skill(skill_name)
        if not skill:
            return Result.err(f"Skill not found: {skill_name}")

        # Create execution context
        context = ExecutionContext(
            skill_name=skill_name,
            user_input=user_input,
            arguments=arguments,
            parent_id=parent_id,
        )
        self._active_executions[execution_id] = context

        try:
            log.info(
                "plugin.skill.execution_started",
                skill=skill_name,
                execution_id=execution_id,
            )

            start_time = time.perf_counter()

            # Execute the skill (read instructions and execute)
            result = await self._execute_skill_impl(skill, context)

            duration_ms = (time.perf_counter() - start_time) * 1000

            # Create final result
            execution_result = ExecutionResult(
                status=result.status,
                output=result.output,
                error=result.error,
                duration_ms=duration_ms,
                context=context,
                metadata=result.metadata,
            )

            # Record history
            completed_at = datetime.now(UTC)
            record = ExecutionRecord(
                id=execution_id,
                skill_name=skill_name,
                status=execution_result.status,
                started_at=started_at,
                completed_at=completed_at,
                result=execution_result,
            )
            self._history.append(record)

            # Cleanup active execution
            self._active_executions.pop(execution_id, None)

            log.info(
                "plugin.skill.execution_completed",
                skill=skill_name,
                execution_id=execution_id,
                status=execution_result.status.value,
                duration_ms=duration_ms,
            )

            return Result.ok(execution_result)

        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            error_msg = f"Execution error: {e}"
            log.error(
                "plugin.skill.execution_failed",
                skill=skill_name,
                execution_id=execution_id,
                error=str(e),
            )

            # Create failure result
            execution_result = ExecutionResult(
                status=ExecutionStatus.FAILED,
                error=error_msg,
                duration_ms=duration_ms,
                context=context,
            )

            # Record failure
            record = ExecutionRecord(
                id=execution_id,
                skill_name=skill_name,
                status=ExecutionStatus.FAILED,
                started_at=started_at,
                completed_at=datetime.now(UTC),
                result=execution_result,
            )
            self._history.append(record)

            self._active_executions.pop(execution_id, None)

            return Result.err(error_msg)

    async def invoke_skill(
        self,
        skill_name: str,
        user_input: str,
        **arguments: Any,
    ) -> Result[ExecutionResult, str]:
        """Invoke a skill from within another skill (composition).

        This method allows skills to invoke other skills, enabling
        composition and workflow building.

        Args:
            skill_name: Name of the skill to invoke.
            user_input: User input for the invoked skill.
            **arguments: Additional arguments for the skill.

        Returns:
            Result containing the invoked skill's result or an error.
        """
        # Get current execution ID from context if available
        parent_id = None
        if self._active_executions:
            parent_id = next(iter(self._active_executions.keys()), None)

        return await self.execute(skill_name, user_input, arguments, parent_id)

    async def _execute_skill_impl(
        self,
        skill,
        context: ExecutionContext,
    ) -> ExecutionResult:
        """Execute a skill implementation.

        This method reads the SKILL.md file and executes the
        instructions defined within it.

        Args:
            skill: The skill instance to execute.
            context: The execution context.

        Returns:
            The execution result.
        """
        spec = skill.spec

        # Check if there's an 'instructions' section
        instructions = spec.get("sections", {}).get("response", "")
        if not instructions:
            instructions = spec.get("sections", {}).get("instructions", "")

        if not instructions:
            # No executable instructions - return the spec content
            return ExecutionResult(
                status=ExecutionStatus.COMPLETED,
                output=spec.get("raw", ""),
                metadata={"mode": "spec_only"},
            )

        # For now, return the instructions as output
        # In a full implementation, this would:
        # 1. Parse the instructions
        # 2. Execute any tools/commands specified
        # 3. Handle composition (invoking other skills)
        # 4. Return structured output

        return ExecutionResult(
            status=ExecutionStatus.COMPLETED,
            output=instructions,
            metadata={
                "mode": "instructions",
                "skill_version": skill.metadata.version,
            },
        )

    def clear_history(self) -> None:
        """Clear execution history."""
        self._history.clear()

    def get_stats(self) -> dict[str, Any]:
        """Get execution statistics.

        Returns:
            Dictionary with execution statistics.
        """
        total = len(self._history)
        if total == 0:
            return {
                "total_executions": 0,
                "successful": 0,
                "failed": 0,
                "success_rate": 0.0,
            }

        successful = sum(1 for r in self._history if r.status == ExecutionStatus.COMPLETED)
        failed = sum(1 for r in self._history if r.status == ExecutionStatus.FAILED)

        return {
            "total_executions": total,
            "successful": successful,
            "failed": failed,
            "success_rate": successful / total if total > 0 else 0.0,
            "active_executions": len(self._active_executions),
        }


# Global singleton instance
_global_executor: SkillExecutor | None = None


def get_executor(registry: SkillRegistry | None = None) -> SkillExecutor:
    """Get or create the global skill executor singleton.

    Args:
        registry: Optional skill registry.

    Returns:
        The global SkillExecutor instance.
    """
    global _global_executor

    if _global_executor is None:
        _global_executor = SkillExecutor(registry)
    return _global_executor
