"""Orchestrator runner for executing seeds via Claude Agent SDK.

This module provides the main orchestration logic:
- OrchestratorRunner: Converts Seed → prompt, executes via adapter, tracks progress
- OrchestratorResult: Frozen dataclass with execution results

The runner integrates:
- ClaudeAgentAdapter for task execution
- SessionRepository for event-based session tracking
- Rich console for progress display
- Event emission for observability

Usage:
    runner = OrchestratorRunner(adapter, event_store)
    result = await runner.execute_seed(seed, execution_id)
    if result.is_ok:
        print(f"Success: {result.value.summary}")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from ouroboros.core.errors import OuroborosError
from ouroboros.core.types import Result
from ouroboros.observability.logging import get_logger
from ouroboros.orchestrator.adapter import DEFAULT_TOOLS, AgentMessage, ClaudeAgentAdapter
from ouroboros.orchestrator.events import (
    create_mcp_tools_loaded_event,
    create_progress_event,
    create_session_completed_event,
    create_session_failed_event,
    create_session_started_event,
    create_tool_called_event,
    create_workflow_progress_event,
)
from ouroboros.orchestrator.mcp_tools import MCPToolProvider
from ouroboros.orchestrator.session import SessionRepository, SessionStatus

if TYPE_CHECKING:
    from ouroboros.core.seed import Seed
    from ouroboros.mcp.client.manager import MCPClientManager
    from ouroboros.persistence.event_store import EventStore

log = get_logger(__name__)


# =============================================================================
# Result Types
# =============================================================================


@dataclass(frozen=True, slots=True)
class OrchestratorResult:
    """Result of orchestrator execution.

    Attributes:
        success: Whether execution completed successfully.
        session_id: Session identifier for resumption.
        execution_id: Workflow execution ID.
        summary: Execution summary dict.
        messages_processed: Total messages from agent.
        final_message: Final result message from agent.
        duration_seconds: Execution duration.
    """

    success: bool
    session_id: str
    execution_id: str
    summary: dict[str, Any] = field(default_factory=dict)
    messages_processed: int = 0
    final_message: str = ""
    duration_seconds: float = 0.0


# =============================================================================
# Errors
# =============================================================================


class OrchestratorError(OuroborosError):
    """Error during orchestrator execution."""

    pass


# =============================================================================
# Prompt Building
# =============================================================================


def build_system_prompt(seed: Seed) -> str:
    """Build system prompt from seed specification.

    Args:
        seed: Seed to extract system prompt from.

    Returns:
        System prompt string.
    """
    from ouroboros.orchestrator.workflow_state import get_ac_tracking_prompt

    constraints_text = "\n".join(f"- {c}" for c in seed.constraints) if seed.constraints else "None"

    principles_text = (
        "\n".join(f"- {p.name}: {p.description}" for p in seed.evaluation_principles)
        if seed.evaluation_principles
        else "None"
    )

    ac_tracking = get_ac_tracking_prompt()

    return f"""You are an autonomous coding agent executing a task for the Ouroboros workflow system.

## Goal
{seed.goal}

## Constraints
{constraints_text}

## Evaluation Principles
{principles_text}

## Guidelines
- Execute each acceptance criterion thoroughly
- Use the available tools (Read, Edit, Bash, Glob, Grep) to accomplish tasks
- Write clean, well-tested code following project conventions
- Report progress clearly as you work
- If you encounter blockers, explain them clearly
{ac_tracking}"""


def build_task_prompt(seed: Seed) -> str:
    """Build task prompt from seed acceptance criteria.

    Args:
        seed: Seed containing acceptance criteria.

    Returns:
        Task prompt string.
    """
    ac_list = "\n".join(f"{i + 1}. {ac}" for i, ac in enumerate(seed.acceptance_criteria))

    return f"""Execute the following task according to the acceptance criteria:

## Goal
{seed.goal}

## Acceptance Criteria
{ac_list}

Please execute each criterion in order, using the available tools to read, write, and modify code as needed.
Report your progress and results for each criterion.
"""


# =============================================================================
# Runner
# =============================================================================


# Progress event emission interval (every N messages)
PROGRESS_EMIT_INTERVAL = 10


class OrchestratorRunner:
    """Main orchestration runner for executing seeds via Claude Agent.

    Converts Seed specifications to agent prompts, executes via adapter,
    tracks progress through event emission, and displays status via Rich.

    Optionally integrates with external MCP servers via MCPClientManager
    to provide additional tools to the Claude Agent during execution.
    """

    def __init__(
        self,
        adapter: ClaudeAgentAdapter,
        event_store: EventStore,
        console: Console | None = None,
        mcp_manager: MCPClientManager | None = None,
        mcp_tool_prefix: str = "",
    ) -> None:
        """Initialize orchestrator runner.

        Args:
            adapter: Claude Agent adapter for task execution.
            event_store: Event store for persistence.
            console: Rich console for output. Uses default if not provided.
            mcp_manager: Optional MCP client manager for external tool integration.
                        When provided, tools from connected MCP servers will be
                        made available to the Claude Agent during execution.
            mcp_tool_prefix: Optional prefix to add to MCP tool names to avoid
                           conflicts (e.g., "mcp_" makes "read" become "mcp_read").
        """
        self._adapter = adapter
        self._event_store = event_store
        self._console = console or Console()
        self._session_repo = SessionRepository(event_store)
        self._mcp_manager: MCPClientManager | None = mcp_manager
        self._mcp_tool_prefix = mcp_tool_prefix

    @property
    def mcp_manager(self) -> MCPClientManager | None:
        """Return the MCP client manager if configured.

        Returns:
            The MCPClientManager instance or None if not configured.
        """
        return self._mcp_manager

    async def _get_merged_tools(
        self,
        session_id: str,
        tool_prefix: str = "",
    ) -> tuple[list[str], MCPToolProvider | None]:
        """Get merged tool list from DEFAULT_TOOLS and MCP tools.

        If MCP manager is configured, discovers tools from connected servers
        and merges them with DEFAULT_TOOLS. DEFAULT_TOOLS always take priority.

        Args:
            session_id: Current session ID for event emission.
            tool_prefix: Optional prefix for MCP tool names.

        Returns:
            Tuple of (merged tool names list, MCPToolProvider or None).
        """
        # Start with default tools
        merged_tools = list(DEFAULT_TOOLS)

        if self._mcp_manager is None:
            return merged_tools, None

        # Create provider and get MCP tools
        provider = MCPToolProvider(
            self._mcp_manager,
            tool_prefix=tool_prefix,
        )

        try:
            mcp_tools = await provider.get_tools(builtin_tools=DEFAULT_TOOLS)
        except Exception as e:
            log.warning(
                "orchestrator.runner.mcp_tools_load_failed",
                session_id=session_id,
                error=str(e),
            )
            return merged_tools, None

        if not mcp_tools:
            log.info(
                "orchestrator.runner.no_mcp_tools_available",
                session_id=session_id,
            )
            return merged_tools, provider

        # Add MCP tool names to merged list
        mcp_tool_names = [t.name for t in mcp_tools]
        merged_tools.extend(mcp_tool_names)

        # Log conflicts
        for conflict in provider.conflicts:
            log.warning(
                "orchestrator.runner.tool_conflict",
                tool_name=conflict.tool_name,
                source=conflict.source,
                shadowed_by=conflict.shadowed_by,
                resolution=conflict.resolution,
            )

        # Emit MCP tools loaded event
        server_names = tuple(set(t.server_name for t in mcp_tools))
        mcp_event = create_mcp_tools_loaded_event(
            session_id=session_id,
            tool_count=len(mcp_tools),
            server_names=server_names,
            conflict_count=len(provider.conflicts),
            tool_names=mcp_tool_names,
        )
        await self._event_store.append(mcp_event)

        log.info(
            "orchestrator.runner.mcp_tools_loaded",
            session_id=session_id,
            mcp_tool_count=len(mcp_tools),
            total_tools=len(merged_tools),
            servers=server_names,
        )

        return merged_tools, provider

    async def execute_seed(
        self,
        seed: Seed,
        execution_id: str | None = None,
    ) -> Result[OrchestratorResult, OrchestratorError]:
        """Execute seed via Claude Agent.

        This is the main entry point for orchestrator execution.
        It converts the seed to prompts, executes via the adapter,
        and tracks progress through events.

        Args:
            seed: Seed specification to execute.
            execution_id: Optional execution ID. Generated if not provided.

        Returns:
            Result containing OrchestratorResult on success.
        """
        exec_id = execution_id or f"exec_{uuid4().hex[:12]}"
        start_time = datetime.now(UTC)

        log.info(
            "orchestrator.runner.execute_started",
            execution_id=exec_id,
            seed_id=seed.metadata.seed_id,
            goal=seed.goal[:100],
        )

        # Create session
        session_result = await self._session_repo.create_session(
            execution_id=exec_id,
            seed_id=seed.metadata.seed_id,
        )

        if session_result.is_err:
            return Result.err(
                OrchestratorError(
                    message=f"Failed to create session: {session_result.error}",
                    details={"execution_id": exec_id},
                )
            )

        tracker = session_result.value

        # Emit session started event
        start_event = create_session_started_event(
            session_id=tracker.session_id,
            execution_id=exec_id,
            seed_id=seed.metadata.seed_id,
            seed_goal=seed.goal,
        )
        await self._event_store.append(start_event)

        # Build prompts
        system_prompt = build_system_prompt(seed)
        task_prompt = build_task_prompt(seed)

        # Get merged tools (DEFAULT_TOOLS + MCP tools if configured)
        merged_tools, mcp_provider = await self._get_merged_tools(
            session_id=tracker.session_id,
            tool_prefix=self._mcp_tool_prefix,
        )

        # Execute with progress display
        messages_processed = 0
        final_message = ""
        success = False

        # Create workflow state tracker for progress display
        from ouroboros.cli.formatters.workflow_display import WorkflowDisplay
        from ouroboros.orchestrator.workflow_state import WorkflowStateTracker

        state_tracker = WorkflowStateTracker(
            acceptance_criteria=seed.acceptance_criteria,
            goal=seed.goal,
            session_id=tracker.session_id,
        )

        try:
            with WorkflowDisplay(state_tracker) as display:
                async for message in self._adapter.execute_task(
                    prompt=task_prompt,
                    tools=merged_tools,
                    system_prompt=system_prompt,
                ):
                    messages_processed += 1
                    tracker = tracker.with_progress(
                        {
                            "last_message_type": message.type,
                            "messages_processed": messages_processed,
                        }
                    )

                    # Update workflow state tracker
                    state_tracker.process_message(
                        content=message.content,
                        message_type=message.type,
                        tool_name=message.tool_name,
                        is_input=message.type == "user",
                    )

                    # Refresh the display
                    display.refresh()

                    # Emit workflow progress event for TUI
                    exec_id = execution_id or tracker.session_id
                    progress_data = state_tracker.state.to_tui_message_data(
                        execution_id=exec_id
                    )
                    workflow_event = create_workflow_progress_event(
                        execution_id=exec_id,
                        session_id=tracker.session_id,
                        acceptance_criteria=progress_data["acceptance_criteria"],
                        completed_count=progress_data["completed_count"],
                        total_count=progress_data["total_count"],
                        current_ac_index=progress_data["current_ac_index"],
                        activity=progress_data["activity"],
                        activity_detail=progress_data["activity_detail"],
                        elapsed_display=progress_data["elapsed_display"],
                        estimated_remaining=progress_data["estimated_remaining"],
                    )
                    await self._event_store.append(workflow_event)

                    # Emit tool called event
                    if message.tool_name:
                        tool_event = create_tool_called_event(
                            session_id=tracker.session_id,
                            tool_name=message.tool_name,
                        )
                        await self._event_store.append(tool_event)

                    # Emit progress event periodically
                    if messages_processed % PROGRESS_EMIT_INTERVAL == 0:
                        progress_event = create_progress_event(
                            session_id=tracker.session_id,
                            message_type=message.type,
                            content_preview=message.content,
                            step=messages_processed,
                            tool_name=message.tool_name,
                        )
                        await self._event_store.append(progress_event)

                    # Handle final message
                    if message.is_final:
                        final_message = message.content
                        success = not message.is_error

            # Calculate duration
            duration = (datetime.now(UTC) - start_time).total_seconds()

            # Emit completion event
            if success:
                completed_event = create_session_completed_event(
                    session_id=tracker.session_id,
                    summary={"final_message": final_message[:500]},
                    messages_processed=messages_processed,
                )
                await self._event_store.append(completed_event)
                await self._session_repo.mark_completed(
                    tracker.session_id,
                    {"messages_processed": messages_processed},
                )

                # Display success
                self._console.print(
                    Panel(
                        Text(final_message[:1000], style="green"),
                        title="[green]Execution Completed[/green]",
                        border_style="green",
                    )
                )
            else:
                failed_event = create_session_failed_event(
                    session_id=tracker.session_id,
                    error_message=final_message,
                    messages_processed=messages_processed,
                )
                await self._event_store.append(failed_event)
                await self._session_repo.mark_failed(
                    tracker.session_id,
                    final_message,
                )

                # Display failure
                self._console.print(
                    Panel(
                        Text(final_message[:1000], style="red"),
                        title="[red]Execution Failed[/red]",
                        border_style="red",
                    )
                )

            log.info(
                "orchestrator.runner.execute_completed",
                execution_id=exec_id,
                session_id=tracker.session_id,
                success=success,
                messages_processed=messages_processed,
                duration_seconds=duration,
            )

            return Result.ok(
                OrchestratorResult(
                    success=success,
                    session_id=tracker.session_id,
                    execution_id=exec_id,
                    summary={
                        "goal": seed.goal,
                        "acceptance_criteria_count": len(seed.acceptance_criteria),
                    },
                    messages_processed=messages_processed,
                    final_message=final_message,
                    duration_seconds=duration,
                )
            )

        except Exception as e:
            log.exception(
                "orchestrator.runner.execute_failed",
                execution_id=exec_id,
                error=str(e),
            )

            # Emit failure event
            failed_event = create_session_failed_event(
                session_id=tracker.session_id,
                error_message=str(e),
                error_type=type(e).__name__,
                messages_processed=messages_processed,
            )
            await self._event_store.append(failed_event)

            return Result.err(
                OrchestratorError(
                    message=f"Orchestrator execution failed: {e}",
                    details={
                        "execution_id": exec_id,
                        "session_id": tracker.session_id,
                        "messages_processed": messages_processed,
                    },
                )
            )

    async def resume_session(
        self,
        session_id: str,
        seed: Seed,
    ) -> Result[OrchestratorResult, OrchestratorError]:
        """Resume a paused or failed session.

        Reconstructs session state from events and continues execution.

        Args:
            session_id: Session to resume.
            seed: Original seed (needed for prompt building).

        Returns:
            Result containing OrchestratorResult on success.
        """
        log.info(
            "orchestrator.runner.resume_started",
            session_id=session_id,
        )

        # Reconstruct session
        session_result = await self._session_repo.reconstruct_session(session_id)

        if session_result.is_err:
            return Result.err(
                OrchestratorError(
                    message=f"Failed to reconstruct session: {session_result.error}",
                    details={"session_id": session_id},
                )
            )

        tracker = session_result.value

        # Check if session can be resumed
        if tracker.status == SessionStatus.COMPLETED:
            return Result.err(
                OrchestratorError(
                    message="Session already completed, cannot resume",
                    details={"session_id": session_id, "status": tracker.status.value},
                )
            )

        self._console.print(
            f"[cyan]Resuming session {session_id}[/cyan]\n"
            f"[dim]Previously processed: {tracker.messages_processed} messages[/dim]"
        )

        # Build resume prompt
        system_prompt = build_system_prompt(seed)
        resume_prompt = f"""Continue executing the task from where you left off.

{build_task_prompt(seed)}

Note: This is a resumed session. Please continue from where execution was interrupted.
"""

        # Get Claude Agent session ID if stored
        agent_session_id = tracker.progress.get("agent_session_id")

        # Get merged tools (DEFAULT_TOOLS + MCP tools if configured)
        merged_tools, mcp_provider = await self._get_merged_tools(
            session_id=session_id,
            tool_prefix=self._mcp_tool_prefix,
        )

        start_time = datetime.now(UTC)
        messages_processed = tracker.messages_processed
        final_message = ""
        success = False

        # Create workflow state tracker for progress display
        from ouroboros.cli.formatters.workflow_display import WorkflowDisplay
        from ouroboros.orchestrator.workflow_state import WorkflowStateTracker

        state_tracker = WorkflowStateTracker(
            acceptance_criteria=seed.acceptance_criteria,
            goal=seed.goal,
            session_id=session_id,
        )

        try:
            with WorkflowDisplay(state_tracker) as display:
                async for message in self._adapter.execute_task(
                    prompt=resume_prompt,
                    tools=merged_tools,
                    system_prompt=system_prompt,
                    resume_session_id=agent_session_id,
                ):
                    messages_processed += 1

                    # Update workflow state tracker
                    state_tracker.process_message(
                        content=message.content,
                        message_type=message.type,
                        tool_name=message.tool_name,
                        is_input=message.type == "user",
                    )

                    # Refresh the display
                    display.refresh()

                    # Emit workflow progress event for TUI
                    progress_data = state_tracker.state.to_tui_message_data(
                        execution_id=session_id  # Use session_id as execution_id for resume
                    )
                    workflow_event = create_workflow_progress_event(
                        execution_id=session_id,
                        session_id=session_id,
                        acceptance_criteria=progress_data["acceptance_criteria"],
                        completed_count=progress_data["completed_count"],
                        total_count=progress_data["total_count"],
                        current_ac_index=progress_data["current_ac_index"],
                        activity=progress_data["activity"],
                        activity_detail=progress_data["activity_detail"],
                        elapsed_display=progress_data["elapsed_display"],
                        estimated_remaining=progress_data["estimated_remaining"],
                    )
                    await self._event_store.append(workflow_event)

                    if message.tool_name:
                        tool_event = create_tool_called_event(
                            session_id=session_id,
                            tool_name=message.tool_name,
                        )
                        await self._event_store.append(tool_event)

                    if messages_processed % PROGRESS_EMIT_INTERVAL == 0:
                        progress_event = create_progress_event(
                            session_id=session_id,
                            message_type=message.type,
                            content_preview=message.content,
                            step=messages_processed,
                            tool_name=message.tool_name,
                        )
                        await self._event_store.append(progress_event)

                    if message.is_final:
                        final_message = message.content
                        success = not message.is_error

            duration = (datetime.now(UTC) - start_time).total_seconds()

            if success:
                await self._session_repo.mark_completed(
                    session_id,
                    {"messages_processed": messages_processed},
                )
                self._console.print(
                    Panel(
                        Text(final_message[:1000], style="green"),
                        title="[green]Resumed Execution Completed[/green]",
                        border_style="green",
                    )
                )
            else:
                await self._session_repo.mark_failed(session_id, final_message)
                self._console.print(
                    Panel(
                        Text(final_message[:1000], style="red"),
                        title="[red]Resumed Execution Failed[/red]",
                        border_style="red",
                    )
                )

            log.info(
                "orchestrator.runner.resume_completed",
                session_id=session_id,
                success=success,
                messages_processed=messages_processed,
                duration_seconds=duration,
            )

            return Result.ok(
                OrchestratorResult(
                    success=success,
                    session_id=session_id,
                    execution_id=tracker.execution_id,
                    summary={"resumed": True},
                    messages_processed=messages_processed,
                    final_message=final_message,
                    duration_seconds=duration,
                )
            )

        except Exception as e:
            log.exception(
                "orchestrator.runner.resume_failed",
                session_id=session_id,
                error=str(e),
            )
            return Result.err(
                OrchestratorError(
                    message=f"Session resume failed: {e}",
                    details={"session_id": session_id},
                )
            )

__all__ = [
    "OrchestratorError",
    "OrchestratorResult",
    "OrchestratorRunner",
    "build_system_prompt",
    "build_task_prompt",
]
