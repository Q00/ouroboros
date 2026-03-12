"""Desktop-safe MCP tool registration.

This module exposes a lightweight tool surface for Desktop clients by
deferring imports of the heavy definitions module until a tool is called.
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib
from typing import Any

from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPServerError
from ouroboros.mcp.job_manager import JobManager
from ouroboros.mcp.types import (
    MCPToolDefinition,
    MCPToolParameter,
    MCPToolResult,
    ToolInputType,
)


@dataclass
class _LazyToolHandler:
    """Proxy handler that imports the real implementation on first use."""

    _definition: MCPToolDefinition
    _factory: Any
    _handler: Any = None

    @property
    def definition(self) -> MCPToolDefinition:
        return self._definition

    async def handle(self, arguments: dict[str, Any]) -> Result[MCPToolResult, MCPServerError]:
        if self._handler is None:
            self._handler = self._factory()
        return await self._handler.handle(arguments)


def _load_handler(class_name: str, **kwargs: Any) -> Any:
    module = importlib.import_module("ouroboros.mcp.tools.definitions")
    handler_class = getattr(module, class_name)
    return handler_class(**kwargs)


def build_desktop_safe_tool_handlers(event_store: Any) -> list[_LazyToolHandler]:
    """Create the reduced desktop-safe handler set."""
    job_manager = JobManager(event_store)

    execute_seed_proxy: _LazyToolHandler

    execute_seed_proxy = _LazyToolHandler(
        MCPToolDefinition(
            name="ouroboros_start_execute_seed",
            description=(
                "Start a seed execution in the background and return a job ID immediately. "
                "Use ouroboros_job_status, ouroboros_job_wait, and ouroboros_job_result to monitor progress."
            ),
            parameters=(
                MCPToolParameter("seed_content", ToolInputType.STRING, "The seed content describing the task to execute", required=True),
                MCPToolParameter("session_id", ToolInputType.STRING, "Optional session ID to resume. If not provided, a new session is created.", required=False),
                MCPToolParameter("model_tier", ToolInputType.STRING, "Model tier to use (small, medium, large). Default: medium", required=False, default="medium", enum=("small", "medium", "large")),
                MCPToolParameter("max_iterations", ToolInputType.INTEGER, "Maximum number of execution iterations. Default: 10", required=False, default=10),
                MCPToolParameter("skip_qa", ToolInputType.BOOLEAN, "Skip post-execution QA evaluation. Default: false", required=False, default=False),
            ),
        ),
        lambda: _load_handler(
            "StartExecuteSeedHandler",
            execute_handler=_load_handler("ExecuteSeedHandler", event_store=event_store),
            event_store=event_store,
            job_manager=job_manager,
        ),
    )

    return [
        execute_seed_proxy,
        _LazyToolHandler(
            MCPToolDefinition(
                name="ouroboros_session_status",
                description="Get the status of an Ouroboros session. Returns information about the current phase, progress, and any errors.",
                parameters=(
                    MCPToolParameter("session_id", ToolInputType.STRING, "The session ID to query", required=True),
                ),
            ),
            lambda: _load_handler("SessionStatusHandler", event_store=event_store),
        ),
        _LazyToolHandler(
            MCPToolDefinition(
                name="ouroboros_job_status",
                description="Get the latest summary for a background Ouroboros job.",
                parameters=(
                    MCPToolParameter("job_id", ToolInputType.STRING, "Job ID returned by a start tool", required=True),
                ),
            ),
            lambda: _load_handler("JobStatusHandler", event_store=event_store, job_manager=job_manager),
        ),
        _LazyToolHandler(
            MCPToolDefinition(
                name="ouroboros_job_wait",
                description="Wait briefly for a background job to change state. Useful for conversational polling after a start command.",
                parameters=(
                    MCPToolParameter("job_id", ToolInputType.STRING, "Job ID returned by a start tool", required=True),
                    MCPToolParameter("cursor", ToolInputType.INTEGER, "Previous cursor from job_status or job_wait", required=False, default=0),
                    MCPToolParameter("timeout_seconds", ToolInputType.INTEGER, "Maximum seconds to wait for a change (longer = fewer round-trips)", required=False, default=30),
                ),
            ),
            lambda: _load_handler("JobWaitHandler", event_store=event_store, job_manager=job_manager),
        ),
        _LazyToolHandler(
            MCPToolDefinition(
                name="ouroboros_job_result",
                description="Get the final output for a completed background job.",
                parameters=(
                    MCPToolParameter("job_id", ToolInputType.STRING, "Job ID returned by a start tool", required=True),
                ),
            ),
            lambda: _load_handler("JobResultHandler", event_store=event_store, job_manager=job_manager),
        ),
        _LazyToolHandler(
            MCPToolDefinition(
                name="ouroboros_cancel_job",
                description="Request cancellation for a background job.",
                parameters=(
                    MCPToolParameter("job_id", ToolInputType.STRING, "Job ID returned by a start tool", required=True),
                ),
            ),
            lambda: _load_handler("CancelJobHandler", event_store=event_store, job_manager=job_manager),
        ),
        _LazyToolHandler(
            MCPToolDefinition(
                name="ouroboros_query_events",
                description="Query the event history for an Ouroboros session. Returns a list of events matching the specified criteria.",
                parameters=(
                    MCPToolParameter("session_id", ToolInputType.STRING, "Filter events by session ID. If not provided, returns events across all sessions.", required=False),
                    MCPToolParameter("event_type", ToolInputType.STRING, "Filter by event type (e.g., 'execution', 'evaluation', 'error')", required=False),
                    MCPToolParameter("limit", ToolInputType.INTEGER, "Maximum number of events to return. Default: 50", required=False, default=50),
                    MCPToolParameter("offset", ToolInputType.INTEGER, "Number of events to skip for pagination. Default: 0", required=False, default=0),
                ),
            ),
            lambda: _load_handler("QueryEventsHandler", event_store=event_store),
        ),
        _LazyToolHandler(
            MCPToolDefinition(
                name="ouroboros_generate_seed",
                description="Generate an immutable Seed from a completed interview session. The seed contains structured requirements extracted from the interview conversation.",
                parameters=(
                    MCPToolParameter("session_id", ToolInputType.STRING, "Interview session ID to convert to a seed", required=True),
                    MCPToolParameter("ambiguity_score", ToolInputType.NUMBER, "Ambiguity score for the interview.", required=False),
                ),
            ),
            lambda: _load_handler("GenerateSeedHandler"),
        ),
        _LazyToolHandler(
            MCPToolDefinition(
                name="ouroboros_measure_drift",
                description="Measure drift from the original seed goal and constraints.",
                parameters=(
                    MCPToolParameter("session_id", ToolInputType.STRING, "The execution session ID to measure drift for", required=True),
                    MCPToolParameter("current_output", ToolInputType.STRING, "Current execution output to measure drift against the seed goal", required=True),
                    MCPToolParameter("seed_content", ToolInputType.STRING, "Original seed YAML content for drift calculation", required=True),
                    MCPToolParameter("constraint_violations", ToolInputType.ARRAY, "Known constraint violations", required=False),
                    MCPToolParameter("current_concepts", ToolInputType.ARRAY, "Concepts present in the current output", required=False),
                ),
            ),
            lambda: _load_handler("MeasureDriftHandler", event_store=event_store),
        ),
        _LazyToolHandler(
            MCPToolDefinition(
                name="ouroboros_interview",
                description="Interactive interview for requirement clarification. Start a new interview with initial_context, resume with session_id, or record an answer to the current question.",
                parameters=(
                    MCPToolParameter("initial_context", ToolInputType.STRING, "Initial context to start a new interview session", required=False),
                    MCPToolParameter("session_id", ToolInputType.STRING, "Session ID to resume an existing interview", required=False),
                    MCPToolParameter("answer", ToolInputType.STRING, "Response to the current interview question", required=False),
                    MCPToolParameter("cwd", ToolInputType.STRING, "Working directory for brownfield auto-detection", required=False),
                ),
            ),
            lambda: _load_handler("InterviewHandler", event_store=event_store),
        ),
        _LazyToolHandler(
            MCPToolDefinition(
                name="ouroboros_evaluate",
                description="Evaluate an Ouroboros execution session using the three-stage evaluation pipeline.",
                parameters=(
                    MCPToolParameter("session_id", ToolInputType.STRING, "The execution session ID to evaluate", required=True),
                    MCPToolParameter("artifact", ToolInputType.STRING, "The execution output/artifact to evaluate", required=True),
                    MCPToolParameter("seed_content", ToolInputType.STRING, "Original seed YAML for goal/constraints extraction", required=False),
                    MCPToolParameter("acceptance_criterion", ToolInputType.STRING, "Specific acceptance criterion to evaluate against", required=False),
                    MCPToolParameter("artifact_type", ToolInputType.STRING, "Type of artifact: code, docs, config. Default: code", required=False, default="code", enum=("code", "docs", "config")),
                    MCPToolParameter("trigger_consensus", ToolInputType.BOOLEAN, "Force Stage 3 consensus evaluation. Default: False", required=False, default=False),
                    MCPToolParameter("working_dir", ToolInputType.STRING, "Project working directory for language auto-detection of Stage 1 mechanical verification commands.", required=False),
                ),
            ),
            lambda: _load_handler("EvaluateHandler", event_store=event_store),
        ),
        _LazyToolHandler(
            MCPToolDefinition(
                name="ouroboros_lateral_think",
                description="Generate alternative thinking approaches using lateral thinking personas.",
                parameters=(
                    MCPToolParameter("problem_context", ToolInputType.STRING, "Description of the stuck situation or problem", required=True),
                    MCPToolParameter("current_approach", ToolInputType.STRING, "What has been tried so far that isn't working", required=True),
                    MCPToolParameter("persona", ToolInputType.STRING, "Specific persona to use", required=False, enum=("hacker", "researcher", "simplifier", "architect", "contrarian")),
                    MCPToolParameter("failed_attempts", ToolInputType.ARRAY, "Previous failed approaches to avoid repeating", required=False),
                ),
            ),
            lambda: _load_handler("LateralThinkHandler"),
        ),
        _LazyToolHandler(
            MCPToolDefinition(
                name="ouroboros_cancel_execution",
                description="Cancel a running or paused Ouroboros execution.",
                parameters=(
                    MCPToolParameter("execution_id", ToolInputType.STRING, "The execution/session ID to cancel", required=True),
                    MCPToolParameter("reason", ToolInputType.STRING, "Reason for cancellation", required=False, default="Cancelled by user"),
                ),
            ),
            lambda: _load_handler("CancelExecutionHandler", event_store=event_store),
        ),
    ]
