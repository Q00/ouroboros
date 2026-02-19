"""Orchestrator module for Claude Agent SDK integration.

This module provides Epic 8 functionality - executing Ouroboros workflows
via Claude Agent SDK as an alternative execution mode.

Key Components:
    - ClaudeAgentAdapter: Wrapper for Claude Agent SDK with streaming support
    - SessionTracker: Immutable session state tracking
    - SessionRepository: Event-based session persistence
    - OrchestratorRunner: Main orchestration logic
    - MCPToolProvider: Integration with external MCP tools

Usage:
    from ouroboros.orchestrator import ClaudeAgentAdapter, OrchestratorRunner

    adapter = ClaudeAgentAdapter()
    runner = OrchestratorRunner(adapter, event_store)
    result = await runner.execute_seed(seed, execution_id)

    # With MCP tools:
    from ouroboros.mcp.client.manager import MCPClientManager
    mcp_manager = MCPClientManager()
    runner = OrchestratorRunner(adapter, event_store, mcp_manager=mcp_manager)

CLI Usage:
    ouroboros run --orchestrator seed.yaml
    ouroboros run --orchestrator seed.yaml --parallel  # Parallel AC execution
    ouroboros run --orchestrator seed.yaml --resume <session_id>
    ouroboros run --orchestrator seed.yaml --mcp-config mcp.yaml
"""

from ouroboros.orchestrator.adapter import (
    DEFAULT_TOOLS,
    AgentMessage,
    ClaudeAgentAdapter,
    TaskResult,
)
from ouroboros.orchestrator.coordinator import (
    CoordinatorReview,
    FileConflict,
    LevelCoordinator,
)
from ouroboros.orchestrator.dependency_analyzer import (
    ACNode,
    DependencyAnalysisError,
    DependencyAnalyzer,
    DependencyGraph,
)
from ouroboros.orchestrator.events import (
    create_mcp_tools_loaded_event,
    create_progress_event,
    create_session_completed_event,
    create_session_failed_event,
    create_session_paused_event,
    create_session_started_event,
    create_task_completed_event,
    create_task_started_event,
    create_tool_called_event,
)
from ouroboros.orchestrator.execution_strategy import (
    AnalysisStrategy,
    CodeStrategy,
    ExecutionStrategy,
    ResearchStrategy,
    get_strategy,
    register_strategy,
)
from ouroboros.orchestrator.level_context import (
    ACContextSummary,
    LevelContext,
    build_context_prompt,
    extract_level_context,
)
from ouroboros.orchestrator.mcp_config import (
    ConfigError,
    MCPClientConfig,
    MCPConnectionConfig,
    load_mcp_config,
)
from ouroboros.orchestrator.mcp_tools import (
    MCPToolInfo,
    MCPToolProvider,
    MCPToolsLoadedEvent,
    ToolConflict,
)
from ouroboros.orchestrator.parallel_executor import (
    ACExecutionResult,
    ParallelACExecutor,
    ParallelExecutionResult,
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
    # MCP Config
    "ConfigError",
    "MCPClientConfig",
    "MCPConnectionConfig",
    "load_mcp_config",
    # MCP Tools
    "MCPToolInfo",
    "MCPToolProvider",
    "MCPToolsLoadedEvent",
    "ToolConflict",
    # Events
    "create_mcp_tools_loaded_event",
    "create_progress_event",
    "create_session_completed_event",
    "create_session_failed_event",
    "create_session_paused_event",
    "create_session_started_event",
    "create_task_completed_event",
    "create_task_started_event",
    "create_tool_called_event",
    # Parallel Execution
    "ACNode",
    "DependencyAnalyzer",
    "DependencyAnalysisError",
    "DependencyGraph",
    "ACExecutionResult",
    "ParallelACExecutor",
    "ParallelExecutionResult",
    # Level Context
    "ACContextSummary",
    "LevelContext",
    "build_context_prompt",
    "extract_level_context",
    # Coordinator
    "CoordinatorReview",
    "FileConflict",
    "LevelCoordinator",
    # Execution Strategy
    "AnalysisStrategy",
    "CodeStrategy",
    "ExecutionStrategy",
    "ResearchStrategy",
    "get_strategy",
    "register_strategy",
]
