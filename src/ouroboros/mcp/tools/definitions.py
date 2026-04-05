"""Ouroboros tool definitions for MCP server.

This module re-exports all handler classes from their dedicated modules
and provides the :func:`get_ouroboros_tools` factory that assembles
the default handler tuple for MCP registration.


Handler modules:
- execution_handlers: ExecuteSeedHandler, StartExecuteSeedHandler
- query_handlers: SessionStatusHandler, QueryEventsHandler, ACDashboardHandler
- authoring_handlers: GenerateSeedHandler, InterviewHandler
- evaluation_handlers: MeasureDriftHandler, EvaluateHandler, LateralThinkHandler
- evolution_handlers: EvolveStepHandler, StartEvolveStepHandler,
                      EvolveRewindHandler, LineageStatusHandler
- job_handlers: CancelExecutionHandler, JobStatusHandler, JobWaitHandler,
                JobResultHandler, CancelJobHandler
- qa: QAHandler
"""

from __future__ import annotations

from ouroboros.mcp.tools.ac_tree_hud_handler import ACTreeHUDHandler
from ouroboros.mcp.tools.authoring_handlers import (
    GenerateSeedHandler,
    InterviewHandler,
)
from ouroboros.mcp.tools.evaluation_handlers import (
    EvaluateHandler,
    LateralThinkHandler,
    MeasureDriftHandler,
)
from ouroboros.mcp.tools.evolution_handlers import (
    EvolveRewindHandler,
    EvolveStepHandler,
    LineageStatusHandler,
    StartEvolveStepHandler,
)
from ouroboros.mcp.tools.execution_handlers import (
    ExecuteSeedHandler,
    StartExecuteSeedHandler,
)
from ouroboros.mcp.tools.job_handlers import (
    CancelExecutionHandler,
    CancelJobHandler,
    JobResultHandler,
    JobStatusHandler,
    JobWaitHandler,
)
from ouroboros.mcp.tools.qa import QAHandler
from ouroboros.mcp.tools.query_handlers import (
    ACDashboardHandler,  # noqa: F401 — re-exported for adapter.py
    QueryEventsHandler,
    SessionStatusHandler,
)

# ---------------------------------------------------------------------------
# Convenience factory functions
# ---------------------------------------------------------------------------


def execute_seed_handler(
    *,
    runtime_backend: str | None = None,
    llm_backend: str | None = None,
    mcp_manager: object | None = None,
    mcp_tool_prefix: str = "",
) -> ExecuteSeedHandler:
    """Create an ExecuteSeedHandler instance."""
    return ExecuteSeedHandler(
        agent_runtime_backend=runtime_backend,
        llm_backend=llm_backend,
        mcp_manager=mcp_manager,
        mcp_tool_prefix=mcp_tool_prefix,
    )


def start_execute_seed_handler(
    *,
    runtime_backend: str | None = None,
    llm_backend: str | None = None,
    mcp_manager: object | None = None,
    mcp_tool_prefix: str = "",
) -> StartExecuteSeedHandler:
    """Create a StartExecuteSeedHandler instance."""
    execute_handler = ExecuteSeedHandler(
        agent_runtime_backend=runtime_backend,
        llm_backend=llm_backend,
        mcp_manager=mcp_manager,
        mcp_tool_prefix=mcp_tool_prefix,
    )
    return StartExecuteSeedHandler(execute_handler=execute_handler)


def session_status_handler() -> SessionStatusHandler:
    """Create a SessionStatusHandler instance."""
    return SessionStatusHandler()


def job_status_handler() -> JobStatusHandler:
    """Create a JobStatusHandler instance."""
    return JobStatusHandler()


def job_wait_handler() -> JobWaitHandler:
    """Create a JobWaitHandler instance."""
    return JobWaitHandler()


def job_result_handler() -> JobResultHandler:
    """Create a JobResultHandler instance."""
    return JobResultHandler()


def ac_tree_hud_handler() -> ACTreeHUDHandler:
    """Create an ACTreeHUDHandler instance."""
    return ACTreeHUDHandler()


def cancel_job_handler() -> CancelJobHandler:
    """Create a CancelJobHandler instance."""
    return CancelJobHandler()


def query_events_handler() -> QueryEventsHandler:
    """Create a QueryEventsHandler instance."""
    return QueryEventsHandler()


def generate_seed_handler(*, llm_backend: str | None = None) -> GenerateSeedHandler:
    """Create a GenerateSeedHandler instance."""
    return GenerateSeedHandler(llm_backend=llm_backend)


def measure_drift_handler() -> MeasureDriftHandler:
    """Create a MeasureDriftHandler instance."""
    return MeasureDriftHandler()


def interview_handler(*, llm_backend: str | None = None) -> InterviewHandler:
    """Create an InterviewHandler instance."""
    return InterviewHandler(llm_backend=llm_backend)


def lateral_think_handler() -> LateralThinkHandler:
    """Create a LateralThinkHandler instance."""
    return LateralThinkHandler()


def evaluate_handler(*, llm_backend: str | None = None) -> EvaluateHandler:
    """Create an EvaluateHandler instance."""
    return EvaluateHandler(llm_backend=llm_backend)


def evolve_step_handler() -> EvolveStepHandler:
    """Create an EvolveStepHandler instance."""
    return EvolveStepHandler()


def start_evolve_step_handler() -> StartEvolveStepHandler:
    """Create a StartEvolveStepHandler instance."""
    return StartEvolveStepHandler()


def lineage_status_handler() -> LineageStatusHandler:
    """Create a LineageStatusHandler instance."""
    return LineageStatusHandler()


def evolve_rewind_handler() -> EvolveRewindHandler:
    """Create an EvolveRewindHandler instance."""
    return EvolveRewindHandler()


# ---------------------------------------------------------------------------
# Tool handler tuple type and factory
# ---------------------------------------------------------------------------
from ouroboros.mcp.tools.brownfield_handler import BrownfieldHandler  # noqa: E402
from ouroboros.mcp.tools.pm_handler import PMInterviewHandler  # noqa: E402

OuroborosToolHandlers = tuple[
    ExecuteSeedHandler
    | StartExecuteSeedHandler
    | SessionStatusHandler
    | JobStatusHandler
    | JobWaitHandler
    | JobResultHandler
    | ACTreeHUDHandler
    | CancelJobHandler
    | QueryEventsHandler
    | GenerateSeedHandler
    | MeasureDriftHandler
    | InterviewHandler
    | EvaluateHandler
    | LateralThinkHandler
    | EvolveStepHandler
    | StartEvolveStepHandler
    | LineageStatusHandler
    | EvolveRewindHandler
    | CancelExecutionHandler
    | BrownfieldHandler
    | PMInterviewHandler
    | QAHandler,
    ...,
]


def get_ouroboros_tools(
    *,
    runtime_backend: str | None = None,
    llm_backend: str | None = None,
    mcp_manager: object | None = None,
    mcp_tool_prefix: str = "",
) -> OuroborosToolHandlers:
    """Create the default set of Ouroboros MCP tool handlers."""
    execute_seed = ExecuteSeedHandler(
        agent_runtime_backend=runtime_backend,
        llm_backend=llm_backend,
        mcp_manager=mcp_manager,
        mcp_tool_prefix=mcp_tool_prefix,
    )
    return (
        execute_seed,
        StartExecuteSeedHandler(execute_handler=execute_seed),
        SessionStatusHandler(),
        JobStatusHandler(),
        JobWaitHandler(),
        JobResultHandler(),
        ACTreeHUDHandler(),
        CancelJobHandler(),
        QueryEventsHandler(),
        GenerateSeedHandler(llm_backend=llm_backend),
        MeasureDriftHandler(),
        InterviewHandler(llm_backend=llm_backend),
        EvaluateHandler(llm_backend=llm_backend),
        LateralThinkHandler(),
        EvolveStepHandler(),
        StartEvolveStepHandler(),
        LineageStatusHandler(),
        EvolveRewindHandler(),
        CancelExecutionHandler(),
        BrownfieldHandler(),
        PMInterviewHandler(llm_backend=llm_backend),
        QAHandler(llm_backend=llm_backend),
    )


# List of all Ouroboros tools for registration
OUROBOROS_TOOLS: OuroborosToolHandlers = get_ouroboros_tools()
