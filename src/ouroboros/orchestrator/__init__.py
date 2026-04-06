"""Orchestrator module for backend-neutral agent runtime integration.

This module provides Epic 8 functionality - executing Ouroboros workflows
via pluggable agent runtimes as an alternative execution mode.

Key Components:
    - AgentRuntime: Common runtime protocol
    - ClaudeAgentAdapter: Claude runtime implementation
    - CodexCliRuntime: Codex runtime implementation
    - SessionTracker: Immutable session state tracking
    - SessionRepository: Event-based session persistence
    - OrchestratorRunner: Main orchestration logic
    - MCPToolProvider: Integration with external MCP tools

Usage:
    from ouroboros.orchestrator import OrchestratorRunner, create_agent_runtime

    adapter = create_agent_runtime(backend="claude")
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
    ouroboros run --orchestrator seed.yaml --runtime codex
    ouroboros run --orchestrator seed.yaml --mcp-config mcp.yaml
"""

from ouroboros.orchestrator.adapter import (
    DEFAULT_TOOLS,
    AgentMessage,
    AgentRuntime,
    ClaudeAgentAdapter,
    ClaudeCodeRuntime,
    RuntimeHandle,
    TaskResult,
)
from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime
from ouroboros.orchestrator.coordinator import (
    CoordinatorReview,
    FileConflict,
    LevelCoordinator,
)
from ouroboros.orchestrator.opencode_runtime import (
    OpenCodeRuntime,
)

try:
    from ouroboros.orchestrator.dependency_analyzer import (
        ACDependencySpec,
        ACNode,
        ACSharedRuntimeResource,
        DependencyAnalysisError,
        DependencyAnalyzer,
        DependencyGraph,
        ExecutionPlanningError,
        ExecutionStage,
        HybridExecutionPlanner,
        StagedExecutionPlan,
    )
except ModuleNotFoundError:  # pragma: no cover - compatibility for partial installs
    ACDependencySpec = None
    ACNode = None
    ACSharedRuntimeResource = None
    DependencyAnalysisError = None
    DependencyAnalyzer = None
    DependencyGraph = None
    ExecutionPlanningError = None
    ExecutionStage = None
    HybridExecutionPlanner = None
    StagedExecutionPlan = None
from ouroboros.orchestrator.events import (
    create_mcp_tools_loaded_event,
    create_progress_event,
    create_session_cancelled_event,
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
from ouroboros.orchestrator.opencode_event_normalizer import (
    OpenCodeEventContext,
    OpenCodeEventNormalizer,
    normalize_opencode_event,
)
from ouroboros.orchestrator.parallel_executor import (
    ACExecutionOutcome,
    ACExecutionResult,
    ParallelACExecutor,
    ParallelExecutionResult,
    ParallelExecutionStageResult,
    StageExecutionOutcome,
)
from ouroboros.orchestrator.runner import (
    OrchestratorError,
    OrchestratorResult,
    OrchestratorRunner,
    build_system_prompt,
    build_task_prompt,
)
from ouroboros.orchestrator.runtime_factory import (
    create_agent_runtime,
    resolve_agent_runtime_backend,
)
from ouroboros.orchestrator.session import (
    SessionRepository,
    SessionStatus,
    SessionTracker,
)

__all__ = [
    # Adapter
    "AgentRuntime",
    "AgentMessage",
    "ClaudeAgentAdapter",
    "ClaudeCodeRuntime",
    "CodexCliRuntime",
    "OpenCodeRuntime",
    "DEFAULT_TOOLS",
    "RuntimeHandle",
    "TaskResult",
    "create_agent_runtime",
    "resolve_agent_runtime_backend",
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
    "create_session_cancelled_event",
    "create_session_completed_event",
    "create_session_failed_event",
    "create_session_paused_event",
    "create_session_started_event",
    "create_task_completed_event",
    "create_task_started_event",
    "create_tool_called_event",
    # Parallel Execution
    "ACDependencySpec",
    "ACNode",
    "ACSharedRuntimeResource",
    "DependencyAnalyzer",
    "DependencyAnalysisError",
    "DependencyGraph",
    "ExecutionPlanningError",
    "ExecutionStage",
    "HybridExecutionPlanner",
    "StagedExecutionPlan",
    "ACExecutionOutcome",
    "ACExecutionResult",
    "ParallelACExecutor",
    "ParallelExecutionStageResult",
    "ParallelExecutionResult",
    "StageExecutionOutcome",
    "OpenCodeEventContext",
    "OpenCodeEventNormalizer",
    "normalize_opencode_event",
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
