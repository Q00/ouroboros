"""Orchestrator module for Claude Agent SDK integration.

This module provides Epic 8 functionality - executing Ouroboros workflows
via Claude Agent SDK as an alternative execution mode.

Key Components:
    - ClaudeAgentAdapter: Wrapper for Claude Agent SDK with streaming support
    - SessionTracker: Immutable session state tracking
    - SessionRepository: Event-based session persistence
    - OrchestratorRunner: Main orchestration logic

Usage:
    from ouroboros.orchestrator import ClaudeAgentAdapter, OrchestratorRunner

    adapter = ClaudeAgentAdapter()
    runner = OrchestratorRunner(adapter, event_store)
    result = await runner.execute_seed(seed, execution_id)

CLI Usage:
    ouroboros run --orchestrator seed.yaml
    ouroboros run --orchestrator seed.yaml --resume <session_id>
"""

from ouroboros.orchestrator.adapter import (
    DEFAULT_TOOLS,
    AgentMessage,
    ClaudeAgentAdapter,
    TaskResult,
)
from ouroboros.orchestrator.events import (
    create_progress_event,
    create_session_completed_event,
    create_session_failed_event,
    create_session_paused_event,
    create_session_started_event,
    create_task_completed_event,
    create_task_started_event,
    create_tool_called_event,
)
from ouroboros.orchestrator.runner import (
    OrchestratorError,
    OrchestratorResult,
    OrchestratorRunner,
    build_system_prompt,
    build_task_prompt,
)
from ouroboros.orchestrator.session import (
    SessionRepository,
    SessionStatus,
    SessionTracker,
)

__all__ = [
    # Adapter
    "AgentMessage",
    "ClaudeAgentAdapter",
    "DEFAULT_TOOLS",
    "TaskResult",
    # Session
    "SessionRepository",
    "SessionStatus",
    "SessionTracker",
    # Runner
    "OrchestratorError",
    "OrchestratorResult",
    "OrchestratorRunner",
    "build_system_prompt",
    "build_task_prompt",
    # Events
    "create_progress_event",
    "create_session_completed_event",
    "create_session_failed_event",
    "create_session_paused_event",
    "create_session_started_event",
    "create_task_completed_event",
    "create_task_started_event",
    "create_tool_called_event",
]
