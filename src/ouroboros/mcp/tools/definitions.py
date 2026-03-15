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
) -> ExecuteSeedHandler:
    """Create an ExecuteSeedHandler instance."""
    return ExecuteSeedHandler(
        agent_runtime_backend=runtime_backend,
        llm_backend=llm_backend,
    )


def start_execute_seed_handler() -> StartExecuteSeedHandler:
    """Create a StartExecuteSeedHandler instance."""
    return StartExecuteSeedHandler()


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

OuroborosToolHandlers = tuple[
    ExecuteSeedHandler
    | StartExecuteSeedHandler
    | SessionStatusHandler
    | JobStatusHandler
    | JobWaitHandler
    | JobResultHandler
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
    | QAHandler,
    ...,
]


def get_ouroboros_tools(
    *,
    runtime_backend: str | None = None,
    llm_backend: str | None = None,
) -> OuroborosToolHandlers:
    """Create the default set of Ouroboros MCP tool handlers."""
    return (
        ExecuteSeedHandler(
            agent_runtime_backend=runtime_backend,
            llm_backend=llm_backend,
        ),
        StartExecuteSeedHandler(),
        SessionStatusHandler(),
        JobStatusHandler(),
        JobWaitHandler(),
        JobResultHandler(),
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
        QAHandler(llm_backend=llm_backend),
    )


# List of all Ouroboros tools for registration
OUROBOROS_TOOLS: OuroborosToolHandlers = get_ouroboros_tools()
