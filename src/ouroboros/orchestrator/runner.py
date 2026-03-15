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

import asyncio
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from ouroboros.core.errors import OuroborosError
from ouroboros.core.types import Result
from ouroboros.observability.drift import DriftMeasurement
from ouroboros.observability.logging import get_logger
from ouroboros.orchestrator.adapter import (
    DEFAULT_TOOLS,
    AgentMessage,
    AgentRuntime,
    RuntimeHandle,
)
from ouroboros.orchestrator.events import (
    create_drift_measured_event,
    create_mcp_tools_loaded_event,
    create_progress_event,
    create_session_completed_event,
    create_session_failed_event,
    create_tool_called_event,
    create_workflow_progress_event,
)
from ouroboros.orchestrator.execution_strategy import ExecutionStrategy, get_strategy
from ouroboros.orchestrator.mcp_tools import (
    MCPToolProvider,
    SessionToolCatalog,
    assemble_session_tool_catalog,
    serialize_tool_catalog,
)
from ouroboros.orchestrator.runtime_message_projection import (
    message_tool_input,
    message_tool_name,
    normalized_message_type,
    project_runtime_message,
)
from ouroboros.orchestrator.session import SessionRepository, SessionStatus, SessionTracker
from ouroboros.orchestrator.workflow_state import coerce_ac_marker_update

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


class ExecutionCancelledError(OuroborosError):
    """Raised when an execution is cancelled via the cancellation set."""

    def __init__(self, session_id: str, reason: str = "Cancelled by user") -> None:
        self.session_id = session_id
        self.reason = reason
        super().__init__(f"Execution cancelled for session {session_id}: {reason}")


# =============================================================================
# In-memory Cancellation Registry
# =============================================================================

# Module-level set of session IDs marked for cancellation.
# The MCP cancel tool adds IDs here; the runner's execution loop checks it.
# Guarded by _cancellation_lock to prevent races between MCP cancel calls
# and the runner's message loop reading the set concurrently.
_cancellation_registry: set[str] = set()
_cancellation_lock: asyncio.Lock = asyncio.Lock()


async def request_cancellation(session_id: str) -> None:
    """Mark a session for cancellation.

    Called by the MCP cancel tool to signal that the runner should
    stop processing the given session at its next checkpoint.

    Args:
        session_id: Session to cancel.
    """
    async with _cancellation_lock:
        _cancellation_registry.add(session_id)


async def is_cancellation_requested(session_id: str) -> bool:
    """Check whether cancellation has been requested for a session.

    Args:
        session_id: Session to check.

    Returns:
        True if cancellation was requested.
    """
    async with _cancellation_lock:
        return session_id in _cancellation_registry


async def clear_cancellation(session_id: str) -> None:
    """Remove a session from the cancellation registry.

    Called after the runner has acknowledged the cancellation and
    emitted the appropriate event, so the ID doesn't linger.

    Args:
        session_id: Session to clear.
    """
    async with _cancellation_lock:
        _cancellation_registry.discard(session_id)


async def get_pending_cancellations() -> frozenset[str]:
    """Return a snapshot of all pending cancellation session IDs.

    Returns:
        Frozen set of session IDs awaiting cancellation.
    """
    async with _cancellation_lock:
        return frozenset(_cancellation_registry)


# =============================================================================
# Prompt Building
# =============================================================================


def build_system_prompt(
    seed: Seed,
    strategy: ExecutionStrategy | None = None,
) -> str:
    """Build system prompt from seed specification.

    Args:
        seed: Seed to extract system prompt from.
        strategy: Execution strategy for prompt customization.
            If None, uses strategy from seed.task_type.

    Returns:
        System prompt string.
    """
    from ouroboros.orchestrator.workflow_state import get_ac_tracking_prompt

    if strategy is None:
        strategy = get_strategy(seed.task_type)

    constraints_text = "\n".join(f"- {c}" for c in seed.constraints) if seed.constraints else "None"

    principles_text = (
        "\n".join(f"- {p.name}: {p.description}" for p in seed.evaluation_principles)
        if seed.evaluation_principles
        else "None"
    )

    # Build brownfield context section
    brownfield_section = ""
    if seed.brownfield_context.project_type == "brownfield":
        refs = "\n".join(
            f"- [{r.role.upper()}] {r.path}: {r.summary}"
            for r in seed.brownfield_context.context_references
        )
        patterns = "\n".join(f"- {p}" for p in seed.brownfield_context.existing_patterns)
        deps = ", ".join(seed.brownfield_context.existing_dependencies)
        brownfield_section = f"""
## Existing Codebase Context (BROWNFIELD)
IMPORTANT: You are extending existing code, NOT creating a new project.

### Referenced Codebases
{refs or "None specified"}

### Existing Patterns to Follow
{patterns or "None specified"}

### Existing Dependencies to Reuse
{deps or "None specified"}
"""

    ac_tracking = get_ac_tracking_prompt()
    strategy_fragment = strategy.get_system_prompt_fragment()

    return f"""{strategy_fragment}

## Goal
{seed.goal}

## Constraints
{constraints_text}
{brownfield_section}
## Evaluation Principles
{principles_text}

{ac_tracking}"""


def build_task_prompt(
    seed: Seed,
    strategy: ExecutionStrategy | None = None,
) -> str:
    """Build task prompt from seed acceptance criteria.

    Args:
        seed: Seed containing acceptance criteria.
        strategy: Execution strategy for prompt customization.
            If None, uses strategy from seed.task_type.

    Returns:
        Task prompt string.
    """
    if strategy is None:
        strategy = get_strategy(seed.task_type)

    ac_list = "\n".join(f"{i + 1}. {ac}" for i, ac in enumerate(seed.acceptance_criteria))
    suffix = strategy.get_task_prompt_suffix()

    return f"""Execute the following task according to the acceptance criteria:

## Goal
{seed.goal}

## Acceptance Criteria
{ac_list}

{suffix}
"""


# =============================================================================
# Runner
# =============================================================================


# Progress event emission interval (every N messages)
PROGRESS_EMIT_INTERVAL = 10

# Session progress persistence interval (every N messages)
SESSION_PROGRESS_PERSIST_INTERVAL = 10

# Cancellation check interval (every N messages)
CANCELLATION_CHECK_INTERVAL = 5


class OrchestratorRunner:
    """Main orchestration runner for executing seeds via Claude Agent.

    Converts Seed specifications to agent prompts, executes via adapter,
    tracks progress through event emission, and displays status via Rich.

    Optionally integrates with external MCP servers via MCPClientManager
    to provide additional tools to the Claude Agent during execution.
    """

    def __init__(
        self,
        adapter: AgentRuntime,
        event_store: EventStore,
        console: Console | None = None,
        mcp_manager: MCPClientManager | None = None,
        mcp_tool_prefix: str = "",
        debug: bool = False,
        enable_decomposition: bool = True,
        inherited_runtime_handle: RuntimeHandle | None = None,
        inherited_tools: list[str] | None = None,
    ) -> None:
        """Initialize orchestrator runner.

        Args:
            adapter: Agent runtime for task execution.
            event_store: Event store for persistence.
            console: Rich console for output. Uses default if not provided.
            mcp_manager: Optional MCP client manager for external tool integration.
                        When provided, tools from connected MCP servers will be
                        made available to the Claude Agent during execution.
            mcp_tool_prefix: Optional prefix to add to MCP tool names to avoid
                           conflicts (e.g., "mcp_" makes "read" become "mcp_read").
            debug: Enable verbose logging output. When False, only Live display shown.
            enable_decomposition: Enable AC decomposition into Sub-ACs.
            inherited_runtime_handle: Optional parent Claude runtime handle for
                        delegated child executions that should fork a session.
            inherited_tools: Optional effective tool set inherited from a
                        delegating parent session.
        """
        self._adapter = adapter
        self._event_store = event_store
        self._console = console or Console()
        self._session_repo = SessionRepository(event_store)
        self._mcp_manager: MCPClientManager | None = mcp_manager
        self._mcp_tool_prefix = mcp_tool_prefix
        self._debug = debug
        self._enable_decomposition = enable_decomposition
        self._inherited_runtime_handle = inherited_runtime_handle
        self._inherited_tools = list(inherited_tools) if inherited_tools else None
        # Track active session for external cancellation by execution_id
        self._active_sessions: dict[str, str] = {}  # execution_id -> session_id

    @property
    def mcp_manager(self) -> MCPClientManager | None:
        """Return the MCP client manager if configured.

        Returns:
            The MCPClientManager instance or None if not configured.
        """
        return self._mcp_manager

    @property
    def session_repo(self) -> SessionRepository:
        """Return the session repository.

        Returns:
            The SessionRepository instance for session management.
        """
        return self._session_repo

    @property
    def active_sessions(self) -> dict[str, str]:
        """Return a copy of currently active execution_id -> session_id mappings.

        Returns:
            Dict mapping execution IDs to session IDs for in-flight executions.
        """
        return dict(self._active_sessions)

    def _register_session(self, execution_id: str, session_id: str) -> None:
        """Register an active session for cancellation tracking.

        Called at the start of execution to enable in-flight cancellation.

        Args:
            execution_id: Execution ID for external lookup.
            session_id: Session ID for internal tracking.
        """
        self._active_sessions[execution_id] = session_id

    def _unregister_session(self, execution_id: str, session_id: str) -> None:
        """Unregister a session after execution completes.

        Called at the end of execution (success, failure, or cancellation)
        to clean up tracking state.

        Args:
            execution_id: Execution ID to remove.
            session_id: Session ID to remove.
        """
        self._active_sessions.pop(execution_id, None)

    def _deserialize_runtime_handle(self, progress: dict[str, Any]) -> RuntimeHandle | None:
        """Deserialize runtime resume state from session progress."""
        runtime_payload = progress.get("runtime")
        try:
            runtime_handle = RuntimeHandle.from_dict(runtime_payload)
        except ValueError as exc:
            log.warning(
                "orchestrator.runner.runtime_handle_deserialize_failed",
                error=str(exc),
                runtime_keys=sorted(runtime_payload) if isinstance(runtime_payload, dict) else None,
            )
            runtime_handle = None
        if runtime_handle is not None:
            return runtime_handle

        legacy_session_id = progress.get("agent_session_id")
        if isinstance(legacy_session_id, str) and legacy_session_id:
            # Legacy sessions predate multi-runtime; infer backend from context
            legacy_backend = progress.get("runtime_backend", "claude")
            if not isinstance(legacy_backend, str):
                legacy_backend = "claude"
            return RuntimeHandle(backend=legacy_backend, native_session_id=legacy_session_id)

        return None

    def _seed_runtime_handle(
        self,
        runtime_handle: RuntimeHandle | None,
        *,
        tool_catalog: SessionToolCatalog | None = None,
    ) -> RuntimeHandle | None:
        """Seed a runtime handle with startup metadata before execution begins."""
        backend = (
            runtime_handle.backend if runtime_handle is not None else None
        ) or self._adapter.runtime_backend
        if not backend:
            return runtime_handle

        metadata = dict(runtime_handle.metadata) if runtime_handle is not None else {}
        if tool_catalog is not None:
            metadata["tool_catalog"] = serialize_tool_catalog(tool_catalog)

        cwd = self._adapter.working_directory
        approval_mode = self._adapter.permission_mode

        if runtime_handle is not None:
            return replace(
                runtime_handle,
                backend=backend,
                kind=runtime_handle.kind or "agent_runtime",
                cwd=(
                    runtime_handle.cwd
                    if runtime_handle.cwd
                    else cwd
                    if isinstance(cwd, str) and cwd
                    else None
                ),
                approval_mode=(
                    runtime_handle.approval_mode
                    if runtime_handle.approval_mode
                    else approval_mode
                    if isinstance(approval_mode, str) and approval_mode
                    else None
                ),
                updated_at=datetime.now(UTC).isoformat(),
                metadata=metadata,
            )

        return RuntimeHandle(
            backend=backend,
            kind="agent_runtime",
            cwd=cwd if isinstance(cwd, str) and cwd else None,
            approval_mode=approval_mode
            if isinstance(approval_mode, str) and approval_mode
            else None,
            updated_at=datetime.now(UTC).isoformat(),
            metadata=metadata,
        )

    def _normalized_message_type(self, message: AgentMessage) -> str:
        """Collapse runtime-specific message details into shared progress categories."""
        return normalized_message_type(message)

    def _message_tool_name(self, message: AgentMessage) -> str | None:
        """Resolve the tool name from either the message envelope or message data."""
        return message_tool_name(message)

    def _message_tool_input(self, message: AgentMessage) -> dict[str, Any]:
        """Return structured tool input when present."""
        return message_tool_input(message)

    def _message_tool_input_preview(self, message: AgentMessage) -> str | None:
        """Build a compact preview string for persisted tool-call events."""
        tool_input = self._message_tool_input(message)
        if not tool_input:
            return None

        parts: list[str] = []
        for key, value in tool_input.items():
            rendered = str(value).strip()
            if rendered:
                parts.append(f"{key}: {rendered}")
        preview = ", ".join(parts)
        return preview[:100] if preview else None

    def _serialize_runtime_message_metadata(self, message: AgentMessage) -> dict[str, Any]:
        """Serialize shared runtime metadata for persisted progress/audit events."""
        projected = project_runtime_message(message)
        return dict(projected.runtime_metadata)

    def _build_progress_update(
        self,
        message: AgentMessage,
        messages_processed: int,
    ) -> dict[str, Any]:
        """Build a normalized progress payload for session persistence."""
        projected = project_runtime_message(message)
        message_type = projected.message_type
        progress: dict[str, Any] = {
            "last_message_type": message_type,
            "messages_processed": messages_processed,
            "content_preview": projected.content[:200],
        }

        runtime_handle = message.resume_handle
        progress.update(projected.runtime_metadata)

        if runtime_handle is not None:
            progress["runtime"] = runtime_handle.to_session_state_dict()
            progress["runtime_backend"] = runtime_handle.backend
            runtime_event_type = runtime_handle.metadata.get("runtime_event_type")
            if isinstance(runtime_event_type, str) and runtime_event_type:
                progress["runtime_event_type"] = runtime_event_type
            if runtime_handle.backend == "claude" and runtime_handle.native_session_id:
                progress["agent_session_id"] = runtime_handle.native_session_id

        return progress

    def _build_progress_event(
        self,
        session_id: str,
        message: AgentMessage,
        *,
        step: int | None = None,
    ):
        """Create an enriched progress event from a normalized runtime message."""
        projected = project_runtime_message(message)
        message_type = projected.message_type
        tool_name = projected.tool_name
        event = create_progress_event(
            session_id=session_id,
            message_type=message_type,
            content_preview=projected.content,
            step=step,
            tool_name=tool_name if message_type in {"tool", "tool_result"} else None,
        )
        event_data = {
            **event.data,
            **projected.runtime_metadata,
            "progress": {
                "last_message_type": message_type,
                "last_content_preview": projected.content[:200],
            },
        }
        runtime = event_data.get("runtime")
        if isinstance(runtime, dict):
            event_data["progress"]["runtime"] = runtime
        runtime_event_type = event_data.get("runtime_event_type")
        if isinstance(runtime_event_type, str) and runtime_event_type:
            event_data["progress"]["runtime_event_type"] = runtime_event_type
        thinking = event_data.get("thinking")
        if isinstance(thinking, str) and thinking:
            event_data["progress"]["thinking"] = thinking
        ac_tracking = coerce_ac_marker_update(event_data.get("ac_tracking"))
        if not ac_tracking.is_empty:
            event_data["progress"]["ac_tracking"] = ac_tracking.to_dict()
        return event.model_copy(update={"data": event_data})

    def _build_tool_called_event(
        self,
        session_id: str,
        message: AgentMessage,
    ):
        """Create an enriched tool-called event from a normalized runtime message."""
        projected = project_runtime_message(message)
        tool_name = projected.tool_name
        if tool_name is None:
            return None
        event = create_tool_called_event(
            session_id=session_id,
            tool_name=tool_name,
            tool_input_preview=self._message_tool_input_preview(message),
        )
        event_data = {
            **event.data,
            **projected.runtime_metadata,
        }
        return event.model_copy(update={"data": event_data})

    def _should_emit_progress_event(
        self,
        message: AgentMessage,
        messages_processed: int,
    ) -> bool:
        """Determine whether a message should emit a persisted progress event."""
        projected = project_runtime_message(message)
        runtime_backend = message.resume_handle.backend if message.resume_handle else None
        return (
            message.is_final
            or messages_processed % PROGRESS_EMIT_INTERVAL == 0
            or projected.is_tool_call
            or projected.thinking is not None
            or message.type == "system"
            or runtime_backend == "opencode"
            or projected.is_tool_result
        )

    async def _update_and_persist_progress(
        self,
        tracker: SessionTracker,
        message: AgentMessage,
        messages_processed: int,
        session_id: str,
    ) -> SessionTracker:
        """Update tracker progress and persist when needed.

        Persists on: final message, every N messages, or runtime handle change.
        Returns updated tracker.
        """
        previous_runtime = tracker.progress.get("runtime")
        progress_update = self._build_progress_update(message, messages_processed)
        tracker = tracker.with_progress(progress_update)

        # Compare runtime dicts ignoring the volatile updated_at field
        def _stable_runtime(rt: Any) -> Any:
            if isinstance(rt, dict):
                return {k: v for k, v in rt.items() if k != "updated_at"}
            return rt

        should_persist = (
            message.is_final
            or messages_processed % SESSION_PROGRESS_PERSIST_INTERVAL == 0
            or _stable_runtime(progress_update.get("runtime")) != _stable_runtime(previous_runtime)
        )
        if should_persist:
            await self._persist_session_progress(session_id, progress_update)
        return tracker

    async def _persist_session_progress(
        self,
        session_id: str,
        progress: dict[str, Any],
    ) -> None:
        """Persist session progress without interrupting execution on failure."""
        result = await self._session_repo.track_progress(session_id, progress)
        if result.is_err:
            log.warning(
                "orchestrator.runner.progress_persist_failed",
                session_id=session_id,
                error=str(result.error),
            )

    async def _replay_workflow_state(
        self,
        session_id: str,
        state_tracker: Any,
    ) -> None:
        """Replay persisted session progress events into workflow state."""
        try:
            events = await self._event_store.replay("session", session_id)
        except Exception as e:
            log.warning(
                "orchestrator.runner.workflow_state_replay_failed",
                session_id=session_id,
                error=str(e),
            )
            return

        state_tracker.replay_progress_events(events)

    async def cancel_execution(
        self,
        execution_id: str,
        reason: str = "Cancelled by user",
        cancelled_by: str = "user",
    ) -> Result[dict[str, Any], OrchestratorError]:
        """Cancel a running execution gracefully.

        This is the shared cancellation entry point used by both the MCP tool
        and CLI command. It signals the in-flight execution to stop at the
        next message boundary and updates the session status to CANCELLED.

        If the execution is actively running in this runner instance, adds
        the session to the cancellation registry so the message loop exits
        gracefully. If the execution is not found in-flight (e.g., orphaned
        or stuck), marks the session as cancelled directly via the repository.

        Args:
            execution_id: Execution ID to cancel.
            reason: Human-readable cancellation reason.
            cancelled_by: Who/what initiated cancellation ("user", "auto_cleanup").

        Returns:
            Result with cancellation details on success, or error.
        """
        session_id = self._active_sessions.get(execution_id)

        if session_id is not None:
            # In-flight cancellation: signal via the cancellation registry
            await request_cancellation(session_id)
            log.info(
                "orchestrator.runner.cancellation_requested",
                execution_id=execution_id,
                session_id=session_id,
                reason=reason,
                cancelled_by=cancelled_by,
                in_flight=True,
            )
            # The message loop will detect this and call _handle_cancellation
            return Result.ok(
                {
                    "execution_id": execution_id,
                    "session_id": session_id,
                    "status": "cancellation_requested",
                    "in_flight": True,
                    "reason": reason,
                }
            )

        # Not in-flight: cancel directly via session repository
        return await self._cancel_session_directly(
            execution_id=execution_id,
            reason=reason,
            cancelled_by=cancelled_by,
        )

    async def _cancel_session_directly(
        self,
        execution_id: str,
        reason: str,
        cancelled_by: str,
    ) -> Result[dict[str, Any], OrchestratorError]:
        """Cancel a session directly via the repository (not in-flight).

        Used for orphaned/stuck executions that are no longer actively
        running in this process. Looks up the session_id from the event
        store and marks it as cancelled.

        Args:
            execution_id: Execution ID being cancelled.
            reason: Human-readable cancellation reason.
            cancelled_by: Who/what initiated cancellation.

        Returns:
            Result with cancellation details on success, or error.
        """
        session_id: str | None = None
        # Try to find session_id from event store
        try:
            events = await self._event_store.get_all_sessions()
            for event in events:
                if (
                    event.type == "orchestrator.session.started"
                    and event.data.get("execution_id") == execution_id
                ):
                    session_id = event.aggregate_id
                    break
        except Exception as e:
            log.warning(
                "orchestrator.runner.session_lookup_failed",
                execution_id=execution_id,
                error=str(e),
            )

        if session_id is None:
            return Result.err(
                OrchestratorError(
                    message=f"No session found for execution {execution_id}",
                    details={"execution_id": execution_id},
                )
            )

        # Guard: do not overwrite a terminal state (completed/failed/cancelled)
        _terminal_event_types = frozenset(
            {
                "orchestrator.session.completed",
                "orchestrator.session.failed",
                "orchestrator.session.cancelled",
            }
        )
        try:
            session_events = await self._event_store.query_events(
                aggregate_id=session_id,
                limit=100,
            )
            for ev in session_events:
                if ev.type in _terminal_event_types:
                    log.info(
                        "orchestrator.runner.cancel_skipped_terminal",
                        execution_id=execution_id,
                        session_id=session_id,
                        terminal_event=ev.type,
                    )
                    return Result.ok(
                        {
                            "execution_id": execution_id,
                            "session_id": session_id,
                            "status": "already_terminal",
                            "terminal_event": ev.type,
                            "reason": reason,
                        }
                    )
        except Exception as e:
            log.warning(
                "orchestrator.runner.terminal_check_failed",
                execution_id=execution_id,
                session_id=session_id,
                error=str(e),
            )

        # Mark as cancelled via repository
        cancel_result = await self._session_repo.mark_cancelled(
            session_id=session_id,
            reason=reason,
            cancelled_by=cancelled_by,
        )

        if cancel_result.is_err:
            return Result.err(
                OrchestratorError(
                    message=f"Failed to cancel session: {cancel_result.error}",
                    details={
                        "execution_id": execution_id,
                        "session_id": session_id,
                    },
                )
            )

        log.info(
            "orchestrator.runner.session_cancelled_directly",
            execution_id=execution_id,
            session_id=session_id,
            reason=reason,
            cancelled_by=cancelled_by,
        )

        return Result.ok(
            {
                "execution_id": execution_id,
                "session_id": session_id,
                "status": "cancelled",
                "in_flight": False,
                "reason": reason,
            }
        )

    async def _get_merged_tools(
        self,
        session_id: str,
        tool_prefix: str = "",
        strategy: ExecutionStrategy | None = None,
    ) -> tuple[list[str], MCPToolProvider | None, SessionToolCatalog]:
        """Get merged tool list from strategy tools and MCP tools.

        Uses strategy.get_tools() as the base tool set (falls back to
        DEFAULT_TOOLS when no strategy is provided). If MCP manager is
        configured, discovers tools from connected servers and merges them.

        Args:
            session_id: Current session ID for event emission.
            tool_prefix: Optional prefix for MCP tool names.
            strategy: Execution strategy providing base tool set.

        Returns:
            Tuple of (merged tool names list, MCPToolProvider or None, session catalog).
        """
        # Start with strategy tools (or DEFAULT_TOOLS as fallback)
        base_tools = strategy.get_tools() if strategy else list(DEFAULT_TOOLS)
        if self._inherited_tools:
            for tool_name in self._inherited_tools:
                if tool_name not in base_tools:
                    base_tools.append(tool_name)
        session_catalog = assemble_session_tool_catalog(base_tools)
        merged_tools = [tool.name for tool in session_catalog.tools]

        if self._mcp_manager is None:
            return merged_tools, None, session_catalog

        # Create provider and get MCP tools
        provider = MCPToolProvider(
            self._mcp_manager,
            tool_prefix=tool_prefix,
        )

        try:
            mcp_tools = await provider.get_tools(builtin_tools=base_tools)
        except Exception as e:
            log.warning(
                "orchestrator.runner.mcp_tools_load_failed",
                session_id=session_id,
                error=str(e),
            )
            return merged_tools, None, session_catalog

        if not mcp_tools:
            log.info(
                "orchestrator.runner.no_mcp_tools_available",
                session_id=session_id,
            )
            return merged_tools, provider, session_catalog

        session_catalog = provider.session_catalog
        merged_tools = [tool.name for tool in session_catalog.tools]
        mcp_tool_names = [t.name for t in mcp_tools]

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
        server_names = tuple({t.server_name for t in mcp_tools})
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

        return merged_tools, provider, session_catalog

    async def _check_cancellation(self, session_id: str) -> bool:
        """Check for cancellation via in-memory registry and event store.

        First checks the in-memory cancellation registry (fast path) which is
        populated by the MCP cancel tool. Falls back to querying the event store
        for ``orchestrator.session.cancelled`` events so that cancellations
        persisted by the CLI or other processes are also detected.

        Args:
            session_id: Session ID to check for cancellation.

        Returns:
            True if cancellation was requested, False otherwise.
        """
        # Fast path: check the in-memory cancellation set first.
        # This is O(1) and requires no I/O.
        if await is_cancellation_requested(session_id):
            return True

        # Slow path: check event store for externally-persisted cancellation
        try:
            events = await self._event_store.query_events(
                aggregate_id=session_id,
                event_type="orchestrator.session.cancelled",
                limit=1,
            )
            return len(events) > 0
        except Exception:
            # Graceful degradation: if event store query fails,
            # don't interrupt execution — just log and continue
            log.warning(
                "orchestrator.runner.cancellation_check_failed",
                session_id=session_id,
            )
            return False

    async def _handle_cancellation(
        self,
        session_id: str,
        execution_id: str,
        messages_processed: int,
        start_time: datetime,
    ) -> Result[OrchestratorResult, OrchestratorError]:
        """Handle a detected cancellation by marking the session and returning a result.

        Args:
            session_id: Session that was cancelled.
            execution_id: Execution ID for the result.
            messages_processed: Number of messages processed before cancellation.
            start_time: When execution started.

        Returns:
            Result containing OrchestratorResult with success=False and cancellation info.
        """
        duration = (datetime.now(UTC) - start_time).total_seconds()

        log.info(
            "orchestrator.runner.execution_cancelled",
            session_id=session_id,
            execution_id=execution_id,
            messages_processed=messages_processed,
            duration_seconds=duration,
        )

        # Clear the in-memory cancellation flag so it doesn't linger
        await clear_cancellation(session_id)

        # Clean up session tracking
        self._unregister_session(execution_id, session_id)

        # Only mark cancelled if not already cancelled by another path
        session_result = await self._session_repo.reconstruct_session(session_id)
        if session_result.is_ok and session_result.value.status != SessionStatus.CANCELLED:
            cancel_result = await self._session_repo.mark_cancelled(
                session_id,
                reason="Cancellation detected during execution",
                cancelled_by="runner",
            )
            if cancel_result.is_err:
                log.warning(
                    "orchestrator.runner.mark_cancelled_failed",
                    session_id=session_id,
                    error=str(cancel_result.error),
                )

        # Display cancellation notice
        self._console.print(
            Panel(
                Text("Execution cancelled by external request", style="yellow"),
                title="[yellow]Execution Cancelled[/yellow]",
                border_style="yellow",
            )
        )

        return Result.ok(
            OrchestratorResult(
                success=False,
                session_id=session_id,
                execution_id=execution_id,
                summary={"cancelled": True},
                messages_processed=messages_processed,
                final_message="Execution cancelled by external request",
                duration_seconds=duration,
            )
        )

    async def execute_seed(
        self,
        seed: Seed,
        execution_id: str | None = None,
        session_id: str | None = None,
        parallel: bool = True,
    ) -> Result[OrchestratorResult, OrchestratorError]:
        """Execute seed via Claude Agent.

        This is the main entry point for orchestrator execution.
        It converts the seed to prompts, executes via the adapter,
        and tracks progress through events.

        Args:
            seed: Seed specification to execute.
            execution_id: Optional execution ID. Generated if not provided.
            session_id: Optional session ID to preallocate for external tracking.
            parallel: Enable parallel AC execution. When True, independent ACs
                     run concurrently. Default: True (parallel execution).

        Returns:
            Result containing OrchestratorResult on success.
        """
        session_result = await self.prepare_session(seed, execution_id=execution_id)
        if session_result.is_err:
            return Result.err(session_result.error)

        return await self.execute_precreated_session(
            seed=seed,
            tracker=session_result.value,
            parallel=parallel,
        )

    async def prepare_session(
        self,
        seed: Seed,
        execution_id: str | None = None,
        session_id: str | None = None,
    ) -> Result[SessionTracker, OrchestratorError]:
        """Create and persist the orchestration session before execution begins.

        This allows callers such as MCP handlers to return stable tracking IDs
        immediately and then start the actual runtime work asynchronously.
        """
        exec_id = execution_id or f"exec_{uuid4().hex[:12]}"
        session_result = await self._session_repo.create_session(
            execution_id=exec_id,
            seed_id=seed.metadata.seed_id,
            session_id=session_id,
            seed_goal=seed.goal,
        )

        if session_result.is_err:
            return Result.err(
                OrchestratorError(
                    message=f"Failed to create session: {session_result.error}",
                    details={"execution_id": exec_id, "session_id": session_id},
                )
            )

        return Result.ok(session_result.value)

    async def execute_precreated_session(
        self,
        seed: Seed,
        tracker: SessionTracker,
        parallel: bool = True,
    ) -> Result[OrchestratorResult, OrchestratorError]:
        """Execute a seed using an already-persisted orchestrator session."""
        exec_id = tracker.execution_id
        start_time = datetime.now(UTC)

        # Control console logging based on debug mode
        from ouroboros.observability.logging import set_console_logging

        set_console_logging(self._debug)

        log.info(
            "orchestrator.runner.execute_started",
            execution_id=exec_id,
            session_id=tracker.session_id,
            seed_id=seed.metadata.seed_id,
            goal=seed.goal[:100],
        )

        # Register session for cancellation tracking
        self._register_session(exec_id, tracker.session_id)

        # Build prompts with strategy
        strategy = get_strategy(seed.task_type)
        system_prompt = build_system_prompt(seed, strategy=strategy)
        task_prompt = build_task_prompt(seed, strategy=strategy)

        # Get merged tools (strategy tools + MCP tools if configured)
        merged_tools, mcp_provider, tool_catalog = await self._get_merged_tools(
            session_id=tracker.session_id,
            tool_prefix=self._mcp_tool_prefix,
            strategy=strategy,
        )

        # Execute with progress display
        messages_processed = 0
        final_message = ""
        success = False

        # Create workflow state tracker for progress display
        from ouroboros.orchestrator.workflow_state import WorkflowStateTracker

        state_tracker = WorkflowStateTracker(
            acceptance_criteria=seed.acceptance_criteria,
            goal=seed.goal,
            session_id=tracker.session_id,
            activity_map=strategy.get_activity_map(),
        )

        # Check for parallel execution mode
        if parallel and len(seed.acceptance_criteria) > 1:
            return await self._execute_parallel(
                seed=seed,
                exec_id=exec_id,
                tracker=tracker,
                merged_tools=merged_tools,
                tool_catalog=tool_catalog,
                system_prompt=system_prompt,
                start_time=start_time,
            )

        try:
            # Use simple status spinner with log-style output for changes
            from rich.status import Status

            last_tool: str | None = None
            last_completed_count = 0

            with Status(
                f"[bold cyan]Executing: {seed.goal[:50]}...[/]",
                console=self._console,
                spinner="dots",
            ) as status:
                runtime_handle = self._seed_runtime_handle(
                    self._inherited_runtime_handle, tool_catalog=tool_catalog
                )
                async for message in self._adapter.execute_task(
                    prompt=task_prompt,
                    tools=merged_tools,
                    system_prompt=system_prompt,
                    resume_handle=runtime_handle,
                ):
                    messages_processed += 1
                    projected = project_runtime_message(message)

                    # Check for cancellation periodically
                    if messages_processed % CANCELLATION_CHECK_INTERVAL == 0:
                        if await self._check_cancellation(tracker.session_id):
                            return await self._handle_cancellation(
                                session_id=tracker.session_id,
                                execution_id=exec_id,
                                messages_processed=messages_processed,
                                start_time=start_time,
                            )

                    tracker = await self._update_and_persist_progress(
                        tracker,
                        message,
                        messages_processed,
                        tracker.session_id,
                    )

                    # Update workflow state tracker
                    state_tracker.process_runtime_message(message)

                    # Print log-style output for tool calls and agent messages
                    if projected.tool_name and projected.tool_name != last_tool:
                        status.stop()
                        self._console.print(f"  [yellow]🔧 {projected.tool_name}[/yellow]")
                        status.start()
                        last_tool = projected.tool_name
                    elif (
                        projected.message_type == "assistant"
                        and projected.content
                        and not projected.tool_name
                    ):
                        # Show agent thinking/reasoning
                        content = projected.content.strip()
                        status.stop()
                        self._console.print(f"  [dim]💭 {content}[/dim]")
                        status.start()

                    # Print when AC is completed
                    current_completed = state_tracker.state.completed_count
                    if current_completed > last_completed_count:
                        status.stop()
                        self._console.print(f"  [green]✓ AC {current_completed} completed[/green]")
                        status.start()
                        last_completed_count = current_completed

                    # Update status with current activity
                    ac_progress = (
                        f"{state_tracker.state.completed_count}/{state_tracker.state.total_count}"
                    )
                    tool_info = f" | {projected.tool_name}" if projected.tool_name else ""
                    status.update(
                        f"[bold cyan]AC {ac_progress}{tool_info} | {messages_processed} msgs[/]"
                    )

                    # Emit workflow progress event for TUI
                    # Use exec_id defined at start of function (not execution_id param)
                    progress_data = state_tracker.state.to_tui_message_data(execution_id=exec_id)
                    workflow_event = create_workflow_progress_event(
                        execution_id=exec_id,
                        session_id=tracker.session_id,
                        acceptance_criteria=progress_data["acceptance_criteria"],
                        completed_count=progress_data["completed_count"],
                        total_count=progress_data["total_count"],
                        current_ac_index=progress_data["current_ac_index"],
                        current_phase=progress_data["current_phase"],
                        activity=progress_data["activity"],
                        activity_detail=progress_data["activity_detail"],
                        elapsed_display=progress_data["elapsed_display"],
                        estimated_remaining=progress_data["estimated_remaining"],
                        messages_count=progress_data["messages_count"],
                        tool_calls_count=progress_data["tool_calls_count"],
                        estimated_tokens=progress_data["estimated_tokens"],
                        estimated_cost_usd=progress_data["estimated_cost_usd"],
                        last_update=progress_data.get("last_update"),
                    )
                    await self._event_store.append(workflow_event)

                    tool_event = self._build_tool_called_event(tracker.session_id, message)
                    if tool_event is not None:
                        await self._event_store.append(tool_event)

                    if self._should_emit_progress_event(message, messages_processed):
                        progress_event = self._build_progress_event(
                            tracker.session_id,
                            message,
                            step=messages_processed,
                        )
                        await self._event_store.append(progress_event)

                    # Measure and emit drift periodically
                    if messages_processed % PROGRESS_EMIT_INTERVAL == 0:
                        # Measure and emit drift
                        drift_measurement = DriftMeasurement()
                        drift_metrics = drift_measurement.measure(
                            current_output=message.content,
                            constraint_violations=[],  # TODO: track violations
                            current_concepts=[],  # TODO: extract concepts
                            seed=seed,
                        )
                        drift_event = create_drift_measured_event(
                            execution_id=exec_id,
                            goal_drift=drift_metrics.goal_drift,
                            constraint_drift=drift_metrics.constraint_drift,
                            ontology_drift=drift_metrics.ontology_drift,
                            combined_drift=drift_metrics.combined_drift,
                            is_acceptable=drift_metrics.is_acceptable,
                        )
                        await self._event_store.append(drift_event)

                    # Handle final message
                    if message.is_final:
                        final_message = message.content
                        success = not message.is_error

            # Calculate duration
            duration = (datetime.now(UTC) - start_time).total_seconds()

            # Emit completion event
            if success:
                completion_summary = {
                    "final_message": final_message[:500],
                    "messages_processed": messages_processed,
                }
                completed_event = create_session_completed_event(
                    session_id=tracker.session_id,
                    summary=completion_summary,
                    messages_processed=messages_processed,
                )
                await self._event_store.append(completed_event)
                await self._session_repo.mark_completed(
                    tracker.session_id,
                    completion_summary,
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

            # Clean up session tracking
            self._unregister_session(exec_id, tracker.session_id)

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

            # Clean up session tracking
            self._unregister_session(exec_id, tracker.session_id)

            # Emit failure event
            failed_event = create_session_failed_event(
                session_id=tracker.session_id,
                error_message=str(e),
                error_type=type(e).__name__,
                messages_processed=messages_processed,
            )
            await self._event_store.append(failed_event)
            await self._session_repo.mark_failed(
                tracker.session_id,
                str(e),
            )

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

    async def _execute_parallel(
        self,
        seed: Seed,
        exec_id: str,
        tracker: Any,
        merged_tools: list[str],
        tool_catalog: SessionToolCatalog,
        system_prompt: str,
        start_time: datetime,
    ) -> Result[OrchestratorResult, OrchestratorError]:
        """Execute seed with parallel AC execution.

        Analyzes AC dependencies using LLM, then executes independent ACs
        in parallel. ACs with dependencies execute after their dependencies complete.

        Args:
            seed: Seed specification to execute.
            exec_id: Execution ID.
            tracker: Session tracker.
            merged_tools: Available tools.
            system_prompt: System prompt for agents.
            start_time: Execution start time.

        Returns:
            Result containing OrchestratorResult on success.
        """
        from ouroboros.orchestrator.dependency_analyzer import ACNode, DependencyAnalyzer
        from ouroboros.orchestrator.parallel_executor import (
            ACExecutionOutcome,
            ParallelACExecutor,
        )

        log.info(
            "orchestrator.runner.parallel_mode_enabled",
            execution_id=exec_id,
            session_id=tracker.session_id,
            ac_count=len(seed.acceptance_criteria),
        )

        # Analyze dependencies
        self._console.print("\n[cyan]Analyzing AC dependencies...[/cyan]")

        analyzer = DependencyAnalyzer()
        dep_result = await analyzer.analyze(seed.acceptance_criteria)

        if dep_result.is_err:
            log.warning(
                "orchestrator.runner.dependency_analysis_failed",
                execution_id=exec_id,
                error=str(dep_result.error),
            )
            # Fallback: run all ACs in a single parallel level
            from ouroboros.orchestrator.dependency_analyzer import DependencyGraph

            all_indices = tuple(range(len(seed.acceptance_criteria)))
            dependency_graph = DependencyGraph(
                nodes=tuple(
                    ACNode(index=i, content=ac, depends_on=())
                    for i, ac in enumerate(seed.acceptance_criteria)
                ),
                execution_levels=(all_indices,) if all_indices else (),
            )
        else:
            dependency_graph = dep_result.value

        execution_plan = dependency_graph.to_execution_plan()

        # Log execution plan
        log.info(
            "orchestrator.runner.execution_plan",
            execution_id=exec_id,
            total_levels=execution_plan.total_stages,
            levels=execution_plan.execution_levels,
            parallelizable=execution_plan.is_parallelizable,
        )

        self._console.print(
            f"[green]Execution plan: {execution_plan.total_stages} stages, "
            f"parallelizable: {execution_plan.is_parallelizable}[/green]"
        )
        for stage in execution_plan.stages:
            self._console.print(
                f"  Stage {stage.stage_number}: ACs {[idx + 1 for idx in stage.ac_indices]}"
            )

        # Execute in parallel
        parallel_executor = ParallelACExecutor(
            adapter=self._adapter,
            event_store=self._event_store,
            console=self._console,
            enable_decomposition=self._enable_decomposition,
            inherited_runtime_handle=self._inherited_runtime_handle,
        )

        # Check for cancellation before starting parallel execution
        if await self._check_cancellation(tracker.session_id):
            return await self._handle_cancellation(
                session_id=tracker.session_id,
                execution_id=exec_id,
                messages_processed=0,
                start_time=start_time,
            )

        parallel_result = await parallel_executor.execute_parallel(
            seed=seed,
            execution_plan=execution_plan,
            session_id=tracker.session_id,
            execution_id=exec_id,
            tools=merged_tools,
            tool_catalog=tool_catalog.tools,
            system_prompt=system_prompt,
        )

        # Check for cancellation after parallel execution
        if await self._check_cancellation(tracker.session_id):
            return await self._handle_cancellation(
                session_id=tracker.session_id,
                execution_id=exec_id,
                messages_processed=parallel_result.total_messages,
                start_time=start_time,
            )

        # Calculate duration
        duration = (datetime.now(UTC) - start_time).total_seconds()

        # Determine overall success
        success = parallel_result.all_succeeded

        # Build summary message with per-AC results for downstream evaluation.
        # The evaluator uses final_message as the artifact to judge compliance,
        # so a bare "Success: 6/6" gives it no evidence to work with.
        summary_parts = [
            "Parallel Execution Complete",
            f"Success: {parallel_result.success_count}/{len(seed.acceptance_criteria)}",
        ]
        if parallel_result.failure_count > 0:
            summary_parts.append(f"Failed: {parallel_result.failure_count}")
        if parallel_result.blocked_count > 0:
            summary_parts.append(f"Blocked: {parallel_result.blocked_count}")
        if parallel_result.invalid_count > 0:
            summary_parts.append(f"Invalid: {parallel_result.invalid_count}")

        summary_parts.append("\n## Stage Results")
        for stage_result in parallel_result.stages:
            stage_bits = [
                f"success={stage_result.success_count}",
                f"failed={stage_result.failure_count}",
            ]
            if stage_result.blocked_count:
                stage_bits.append(f"blocked={stage_result.blocked_count}")
            if not stage_result.started:
                stage_bits.append("not_started")
            summary_parts.append(
                f"- Stage {stage_result.level_number}: {stage_result.outcome.value} "
                f"({', '.join(stage_bits)})"
            )

        summary_parts.append("\n## AC Results")
        for r in parallel_result.results:
            if r.outcome == ACExecutionOutcome.SUCCEEDED:
                status = "PASS"
            elif r.outcome == ACExecutionOutcome.BLOCKED:
                status = "BLOCKED"
            elif r.outcome == ACExecutionOutcome.INVALID:
                status = "INVALID"
            else:
                status = "FAIL"
            ac_label = f"AC {r.ac_index + 1}"
            summary_parts.append(f"\n### {ac_label}: [{status}] {r.ac_content}")
            if r.final_message:
                # Include last 500 chars of agent output as evidence
                evidence = r.final_message[-500:] if len(r.final_message) > 500 else r.final_message
                summary_parts.append(evidence)
            elif r.error:
                summary_parts.append(f"Error: {r.error}")

        final_message = "\n".join(summary_parts)

        # Emit completion event
        if success:
            completion_summary = {
                "parallel_execution": True,
                "success_count": parallel_result.success_count,
                "failure_count": parallel_result.failure_count,
                "blocked_count": parallel_result.blocked_count,
                "invalid_count": parallel_result.invalid_count,
                "skipped_count": parallel_result.skipped_count,
                "total_levels": execution_plan.total_stages,
                "messages_processed": parallel_result.total_messages,
            }
            completed_event = create_session_completed_event(
                session_id=tracker.session_id,
                summary=completion_summary,
                messages_processed=parallel_result.total_messages,
            )
            await self._event_store.append(completed_event)
            await self._session_repo.mark_completed(
                tracker.session_id,
                completion_summary,
            )

            self._console.print(
                Panel(
                    Text(final_message, style="green"),
                    title="[green]Parallel Execution Completed[/green]",
                    border_style="green",
                )
            )
        else:
            failed_event = create_session_failed_event(
                session_id=tracker.session_id,
                error_message=(
                    "Partial failure: "
                    f"{parallel_result.failure_count} failed, "
                    f"{parallel_result.blocked_count} blocked, "
                    f"{parallel_result.invalid_count} invalid"
                ),
                messages_processed=parallel_result.total_messages,
            )
            await self._event_store.append(failed_event)
            await self._session_repo.mark_failed(
                tracker.session_id,
                final_message,
            )

            self._console.print(
                Panel(
                    Text(final_message, style="yellow"),
                    title="[yellow]Partial Success[/yellow]",
                    border_style="yellow",
                )
            )

        log.info(
            "orchestrator.runner.parallel_completed",
            execution_id=exec_id,
            session_id=tracker.session_id,
            success=success,
            success_count=parallel_result.success_count,
            failure_count=parallel_result.failure_count,
            blocked_count=parallel_result.blocked_count,
            invalid_count=parallel_result.invalid_count,
            skipped_count=parallel_result.skipped_count,
            total_messages=parallel_result.total_messages,
            duration_seconds=duration,
        )

        # Clean up session tracking
        self._unregister_session(exec_id, tracker.session_id)
        await clear_cancellation(tracker.session_id)

        return Result.ok(
            OrchestratorResult(
                success=success,
                session_id=tracker.session_id,
                execution_id=exec_id,
                summary={
                    "goal": seed.goal,
                    "acceptance_criteria_count": len(seed.acceptance_criteria),
                    "parallel_execution": True,
                    "success_count": parallel_result.success_count,
                    "failure_count": parallel_result.failure_count,
                    "blocked_count": parallel_result.blocked_count,
                    "invalid_count": parallel_result.invalid_count,
                    "skipped_count": parallel_result.skipped_count,
                },
                messages_processed=parallel_result.total_messages,
                final_message=final_message,
                duration_seconds=duration,
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
        # Control console logging based on debug mode
        from ouroboros.observability.logging import set_console_logging

        set_console_logging(self._debug)

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
        if tracker.status in (
            SessionStatus.COMPLETED,
            SessionStatus.CANCELLED,
            SessionStatus.FAILED,
        ):
            return Result.err(
                OrchestratorError(
                    message=f"Session is in terminal state {tracker.status.value}, cannot resume",
                    details={"session_id": session_id, "status": tracker.status.value},
                )
            )

        # Register session for cancellation tracking
        self._register_session(tracker.execution_id, session_id)

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

        # Get runtime resume state if stored
        runtime_handle = self._deserialize_runtime_handle(tracker.progress)

        # Get merged tools (DEFAULT_TOOLS + MCP tools if configured)
        merged_tools, mcp_provider, tool_catalog = await self._get_merged_tools(
            session_id=session_id,
            tool_prefix=self._mcp_tool_prefix,
        )
        runtime_handle = self._seed_runtime_handle(runtime_handle, tool_catalog=tool_catalog)

        start_time = datetime.now(UTC)
        messages_processed = tracker.messages_processed
        final_message = ""
        success = False

        # Create workflow state tracker for progress display
        from ouroboros.orchestrator.workflow_state import WorkflowStateTracker

        resume_strategy = get_strategy(seed.task_type)
        state_tracker = WorkflowStateTracker(
            acceptance_criteria=seed.acceptance_criteria,
            goal=seed.goal,
            session_id=session_id,
            activity_map=resume_strategy.get_activity_map(),
        )
        await self._replay_workflow_state(session_id, state_tracker)

        try:
            # Use simple status spinner with log-style output for changes
            from rich.status import Status

            last_tool: str | None = None
            last_completed_count = state_tracker.state.completed_count

            with Status(
                f"[bold cyan]Resuming: {seed.goal[:50]}...[/]",
                console=self._console,
                spinner="dots",
            ) as status:
                async for message in self._adapter.execute_task(
                    prompt=resume_prompt,
                    tools=merged_tools,
                    system_prompt=system_prompt,
                    resume_handle=runtime_handle,
                ):
                    messages_processed += 1
                    projected = project_runtime_message(message)

                    # Check for cancellation periodically
                    if messages_processed % CANCELLATION_CHECK_INTERVAL == 0:
                        if await self._check_cancellation(session_id):
                            return await self._handle_cancellation(
                                session_id=session_id,
                                execution_id=tracker.execution_id,
                                messages_processed=messages_processed,
                                start_time=start_time,
                            )

                    tracker = await self._update_and_persist_progress(
                        tracker,
                        message,
                        messages_processed,
                        session_id,
                    )

                    # Update workflow state tracker
                    state_tracker.process_runtime_message(message)

                    # Print log-style output for tool calls and agent messages
                    if projected.tool_name and projected.tool_name != last_tool:
                        status.stop()
                        self._console.print(f"  [yellow]🔧 {projected.tool_name}[/yellow]")
                        status.start()
                        last_tool = projected.tool_name
                    elif (
                        projected.message_type == "assistant"
                        and projected.content
                        and not projected.tool_name
                    ):
                        # Show agent thinking/reasoning
                        content = projected.content.strip()
                        status.stop()
                        self._console.print(f"  [dim]💭 {content}[/dim]")
                        status.start()

                    # Print when AC is completed
                    current_completed = state_tracker.state.completed_count
                    if current_completed > last_completed_count:
                        status.stop()
                        self._console.print(f"  [green]✓ AC {current_completed} completed[/green]")
                        status.start()
                        last_completed_count = current_completed

                    # Update status with current activity
                    ac_progress = (
                        f"{state_tracker.state.completed_count}/{state_tracker.state.total_count}"
                    )
                    tool_info = f" | {projected.tool_name}" if projected.tool_name else ""
                    status.update(
                        f"[bold cyan]AC {ac_progress}{tool_info} | {messages_processed} msgs[/]"
                    )

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
                        current_phase=progress_data["current_phase"],
                        activity=progress_data["activity"],
                        activity_detail=progress_data["activity_detail"],
                        elapsed_display=progress_data["elapsed_display"],
                        estimated_remaining=progress_data["estimated_remaining"],
                        messages_count=progress_data["messages_count"],
                        tool_calls_count=progress_data["tool_calls_count"],
                        estimated_tokens=progress_data["estimated_tokens"],
                        estimated_cost_usd=progress_data["estimated_cost_usd"],
                        last_update=progress_data.get("last_update"),
                    )
                    await self._event_store.append(workflow_event)

                    tool_event = self._build_tool_called_event(session_id, message)
                    if tool_event is not None:
                        await self._event_store.append(tool_event)

                    if self._should_emit_progress_event(message, messages_processed):
                        progress_event = self._build_progress_event(
                            session_id,
                            message,
                            step=messages_processed,
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

            # Clear the in-memory cancellation flag so it doesn't linger
            await clear_cancellation(session_id)

            # Clean up session tracking
            self._unregister_session(tracker.execution_id, session_id)

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

            # Clean up session tracking
            self._unregister_session(tracker.execution_id, session_id)

            return Result.err(
                OrchestratorError(
                    message=f"Session resume failed: {e}",
                    details={"session_id": session_id},
                )
            )


__all__ = [
    "ExecutionCancelledError",
    "OrchestratorError",
    "OrchestratorResult",
    "OrchestratorRunner",
    "build_system_prompt",
    "build_task_prompt",
    "clear_cancellation",
    "get_pending_cancellations",
    "is_cancellation_requested",
    "request_cancellation",
]
