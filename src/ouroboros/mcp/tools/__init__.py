"""MCP Tools package.

This package provides tool registration and management for the MCP server.

Public API:
    ToolRegistry: Registry for managing tool handlers
    Tool definitions for Ouroboros functionality
"""

from ouroboros.mcp.tools.definitions import (
    OUROBOROS_TOOLS,
    CancelJobHandler,
    EvolveRewindHandler,
    EvolveStepHandler,
    JobResultHandler,
    JobStatusHandler,
    JobWaitHandler,
    LineageStatusHandler,
    StartEvolveStepHandler,
    StartExecuteSeedHandler,
    cancel_job_handler,
    evolve_rewind_handler,
    evolve_step_handler,
    execute_seed_handler,
    job_result_handler,
    job_status_handler,
    job_wait_handler,
    lineage_status_handler,
    query_events_handler,
    session_status_handler,
    start_evolve_step_handler,
    start_execute_seed_handler,
)
from ouroboros.mcp.tools.registry import ToolRegistry

__all__ = [
    "ToolRegistry",
    "OUROBOROS_TOOLS",
    "CancelJobHandler",
    "EvolveRewindHandler",
    "EvolveStepHandler",
    "JobResultHandler",
    "JobStatusHandler",
    "JobWaitHandler",
    "LineageStatusHandler",
    "StartEvolveStepHandler",
    "StartExecuteSeedHandler",
    "start_execute_seed_handler",
    "execute_seed_handler",
    "session_status_handler",
    "job_status_handler",
    "job_wait_handler",
    "job_result_handler",
    "cancel_job_handler",
    "query_events_handler",
    "evolve_step_handler",
    "start_evolve_step_handler",
    "evolve_rewind_handler",
    "lineage_status_handler",
]
