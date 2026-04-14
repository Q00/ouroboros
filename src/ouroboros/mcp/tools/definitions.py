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
from ouroboros.mcp.tools.channel_workflow_handler import ChannelWorkflowHandler
from ouroboros.mcp.tools.evaluation_handlers import (
    ChecklistVerifyHandler,
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


def channel_workflow_handler(
    *,
    runtime_backend: str | None = None,
    llm_backend: str | None = None,
    interview_handler: InterviewHandler | None = None,
    generate_seed_handler: GenerateSeedHandler | None = None,
    start_execute_seed_handler: StartExecuteSeedHandler | None = None,
    job_wait_handler: JobWaitHandler | None = None,
    job_status_handler: JobStatusHandler | None = None,
    job_result_handler: JobResultHandler | None = None,
    default_repo: str | None = None,
) -> ChannelWorkflowHandler:
    """Create a ChannelWorkflowHandler instance.

    When handler instances are provided they are reused, ensuring shared
    job state with the rest of the tool set.
    """
    if start_execute_seed_handler is None:
        execute_handler = ExecuteSeedHandler(
            agent_runtime_backend=runtime_backend,
            llm_backend=llm_backend,
        )
        start_execute_seed_handler = StartExecuteSeedHandler(execute_handler=execute_handler)
    return ChannelWorkflowHandler(
        interview_handler=interview_handler or InterviewHandler(llm_backend=llm_backend),
        generate_seed_handler=generate_seed_handler or GenerateSeedHandler(llm_backend=llm_backend),
        start_execute_seed_handler=start_execute_seed_handler,
        job_wait_handler=job_wait_handler or JobWaitHandler(),
        job_status_handler=job_status_handler or JobStatusHandler(),
        job_result_handler=job_result_handler or JobResultHandler(),
        default_repo=default_repo,
    )


def lateral_think_handler() -> LateralThinkHandler:
    """Create a LateralThinkHandler instance."""
    return LateralThinkHandler()


def evaluate_handler(*, llm_backend: str | None = None) -> EvaluateHandler:
    """Create an EvaluateHandler instance."""
    return EvaluateHandler(llm_backend=llm_backend)


def checklist_verify_handler(
    *,
    evaluate_handler: EvaluateHandler | None = None,
    llm_backend: str | None = None,
) -> ChecklistVerifyHandler:
    """Create a ChecklistVerifyHandler instance."""
    return ChecklistVerifyHandler(
        evaluate_handler=evaluate_handler,
        llm_backend=llm_backend,
    )


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
    | ChecklistVerifyHandler
    | LateralThinkHandler
    | EvolveStepHandler
    | StartEvolveStepHandler
    | LineageStatusHandler
    | EvolveRewindHandler
    | CancelExecutionHandler
    | BrownfieldHandler
    | PMInterviewHandler
    | ChannelWorkflowHandler
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
    """Create the default set of Ouroboros MCP tool handlers.

    Shared handler instances are passed to ``channel_workflow_handler``
    so the channel workflow surface uses the same job/event stores as
    the top-level tools.
    """
    execute_seed = ExecuteSeedHandler(
        agent_runtime_backend=runtime_backend,
        llm_backend=llm_backend,
        mcp_manager=mcp_manager,
        mcp_tool_prefix=mcp_tool_prefix,
    )
    start_execute = StartExecuteSeedHandler(execute_handler=execute_seed)
    job_status = JobStatusHandler()
    job_wait = JobWaitHandler()
    job_result = JobResultHandler()
    interview = InterviewHandler(llm_backend=llm_backend)
    generate_seed = GenerateSeedHandler(llm_backend=llm_backend)
    evaluate = EvaluateHandler(llm_backend=llm_backend)
    return (
        execute_seed,
        start_execute,
        SessionStatusHandler(),
        job_status,
        job_wait,
        job_result,
        ACTreeHUDHandler(),
        CancelJobHandler(),
        QueryEventsHandler(),
        generate_seed,
        MeasureDriftHandler(),
        interview,
        evaluate,
        ChecklistVerifyHandler(evaluate_handler=evaluate, llm_backend=llm_backend),
        LateralThinkHandler(),
        EvolveStepHandler(),
        StartEvolveStepHandler(),
        LineageStatusHandler(),
        EvolveRewindHandler(),
        CancelExecutionHandler(),
        BrownfieldHandler(),
        PMInterviewHandler(llm_backend=llm_backend),
        channel_workflow_handler(
            runtime_backend=runtime_backend,
            llm_backend=llm_backend,
            interview_handler=interview,
            generate_seed_handler=generate_seed,
            start_execute_seed_handler=start_execute,
            job_wait_handler=job_wait,
            job_status_handler=job_status,
            job_result_handler=job_result,
        ),
        QAHandler(llm_backend=llm_backend),
    )


# List of all Ouroboros tools for registration
OUROBOROS_TOOLS: OuroborosToolHandlers = get_ouroboros_tools()
