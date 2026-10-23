"""Assemble the public execution entry points' runner in one place."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ouroboros.config.loader import get_max_parallel_workers
from ouroboros.orchestrator.decomposition_limits import DEFAULT_MAX_DECOMPOSITION_DEPTH
from ouroboros.orchestrator.runner import OrchestratorRunner
from ouroboros.persistence.checkpoint import CheckpointStore

if TYPE_CHECKING:
    from rich.console import Console

    from ouroboros.core.worktree import TaskWorkspace
    from ouroboros.mcp.client.manager import MCPClientManager
    from ouroboros.orchestrator.adapter import AgentRuntime, RuntimeHandle
    from ouroboros.orchestrator.synapse import SessionSignalHub
    from ouroboros.persistence.event_store import EventStore


def build_orchestrator_runner(
    *,
    adapter: AgentRuntime,
    event_store: EventStore,
    console: Console,
    mcp_manager: MCPClientManager | None,
    mcp_tool_prefix: str,
    debug: bool,
    task_workspace: TaskWorkspace | None,
    enable_decomposition: bool,
    max_decomposition_depth: int | None,
    max_parallel_workers: int | None,
    fat_harness_mode: bool,
    base_model_tier: str | None,
    efficiency_mode: str | None,
    frugality_assurance: str | None,
    inherited_runtime_handle: RuntimeHandle | None,
    inherited_tools: list[str] | None,
    session_signal_hub: SessionSignalHub | None,
) -> OrchestratorRunner:
    """Use common store and defaults while retaining named entry-specific inputs.

    The caller supplies already-resolved request and execution-mode values.
    A missing depth or worker limit always has the same meaning, independent
    of which entry point starts the run.
    """
    checkpoint_store = CheckpointStore()
    checkpoint_store.initialize()
    return OrchestratorRunner(
        adapter=adapter,
        event_store=event_store,
        console=console,
        mcp_manager=mcp_manager,
        mcp_tool_prefix=mcp_tool_prefix,
        debug=debug,
        task_workspace=task_workspace,
        enable_decomposition=enable_decomposition,
        max_decomposition_depth=(
            DEFAULT_MAX_DECOMPOSITION_DEPTH
            if max_decomposition_depth is None
            else max_decomposition_depth
        ),
        max_parallel_workers=(
            get_max_parallel_workers() if max_parallel_workers is None else max_parallel_workers
        ),
        fat_harness_mode=fat_harness_mode,
        base_model_tier=base_model_tier,
        efficiency_mode=efficiency_mode,
        frugality_assurance=frugality_assurance,
        inherited_runtime_handle=inherited_runtime_handle,
        inherited_tools=inherited_tools,
        session_signal_hub=session_signal_hub,
        checkpoint_store=checkpoint_store,
    )
