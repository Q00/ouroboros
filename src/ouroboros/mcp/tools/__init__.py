"""MCP Tools package.

This package provides tool registration and management for the MCP server.

Public API:
    ToolRegistry: Registry for managing tool handlers
    Tool definitions for Ouroboros functionality
"""

from ouroboros.mcp.tools.definitions import (
    OUROBOROS_TOOLS,
    EvolveRewindHandler,
    EvolveStepHandler,
    LineageStatusHandler,
    evolve_rewind_handler,
    evolve_step_handler,
    execute_seed_handler,
    lineage_status_handler,
    query_events_handler,
    session_status_handler,
)
from ouroboros.mcp.tools.registry import ToolRegistry

__all__ = [
    "ToolRegistry",
    "OUROBOROS_TOOLS",
    "EvolveRewindHandler",
    "EvolveStepHandler",
    "LineageStatusHandler",
    "execute_seed_handler",
    "session_status_handler",
    "query_events_handler",
    "evolve_step_handler",
    "evolve_rewind_handler",
    "lineage_status_handler",
]
