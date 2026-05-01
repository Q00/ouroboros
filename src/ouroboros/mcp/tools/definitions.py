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
    opencode_mode: str | None = None,
) -> ExecuteSeedHandler:
    """Create an ExecuteSeedHandler instance."""
    return ExecuteSeedHandler(
        agent_runtime_backend=runtime_backend,
        llm_backend=llm_backend,
        mcp_manager=mcp_manager,
        mcp_tool_prefix=mcp_tool_prefix,
        opencode_mode=opencode_mode,
    )


def start_execute_seed_handler(
    *,
    runtime_backend: str | None = None,
    llm_backend: str | None = None,
    mcp_manager: object | None = None,
    mcp_tool_prefix: str = "",
    opencode_mode: str | None = None,
) -> StartExecuteSeedHandler:
    """Create a StartExecuteSeedHandler instance."""
    execute_handler = ExecuteSeedHandler(
        agent_runtime_backend=runtime_backend,
        llm_backend=llm_backend,
        mcp_manager=mcp_manager,
        mcp_tool_prefix=mcp_tool_prefix,
        opencode_mode=opencode_mode,
    )
    return StartExecuteSeedHandler(
        execute_handler=execute_handler,
        agent_runtime_backend=runtime_backend,
        opencode_mode=opencode_mode,
    )


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


def generate_seed_handler(
    *,
    llm_backend: str | None = None,
    runtime_backend: str | None = None,
    opencode_mode: str | None = None,
) -> GenerateSeedHandler:
    """Create a GenerateSeedHandler instance."""
    return GenerateSeedHandler(
        llm_backend=llm_backend,
        agent_runtime_backend=runtime_backend,
        opencode_mode=opencode_mode,
    )


def measure_drift_handler() -> MeasureDriftHandler:
    """Create a MeasureDriftHandler instance."""
    return MeasureDriftHandler()


def interview_handler(
    *,
    llm_backend: str | None = None,
    runtime_backend: str | None = None,
    opencode_mode: str | None = None,
) -> InterviewHandler:
    """Create an InterviewHandler instance."""
    return InterviewHandler(
        llm_backend=llm_backend,
        agent_runtime_backend=runtime_backend,
        opencode_mode=opencode_mode,
    )


def auto_handler(
    *,
    llm_backend: str | None = None,
    runtime_backend: str | None = None,
    mcp_manager: object | None = None,
    mcp_tool_prefix: str = "",
    opencode_mode: str | None = None,
) -> object:
    """Create an AutoHandler instance without adding it to legacy static tool tuples."""
    from ouroboros.mcp.tools.auto_handler import AutoHandler

    return AutoHandler(
        llm_backend=llm_backend,
        agent_runtime_backend=runtime_backend,
        opencode_mode=opencode_mode,
        mcp_manager=mcp_manager,
        mcp_tool_prefix=mcp_tool_prefix,
    )


def lateral_think_handler(
    *,
    runtime_backend: str | None = None,
    opencode_mode: str | None = None,
) -> LateralThinkHandler:
    """Create a LateralThinkHandler instance."""
    return LateralThinkHandler(
        agent_runtime_backend=runtime_backend,
        opencode_mode=opencode_mode,
    )


def evaluate_handler(
    *,
    llm_backend: str | None = None,
    runtime_backend: str | None = None,
    opencode_mode: str | None = None,
) -> EvaluateHandler:
    """Create an EvaluateHandler instance."""
    return EvaluateHandler(
        llm_backend=llm_backend,
        agent_runtime_backend=runtime_backend,
        opencode_mode=opencode_mode,
    )


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


def evolve_step_handler(
    *,
    runtime_backend: str | None = None,
    opencode_mode: str | None = None,
) -> EvolveStepHandler:
    """Create an EvolveStepHandler instance."""
    return EvolveStepHandler(
        agent_runtime_backend=runtime_backend,
        opencode_mode=opencode_mode,
    )


def start_evolve_step_handler(
    *,
    runtime_backend: str | None = None,
    opencode_mode: str | None = None,
) -> StartEvolveStepHandler:
    """Create a StartEvolveStepHandler instance."""
    return StartEvolveStepHandler(
        evolve_handler=EvolveStepHandler(
            agent_runtime_backend=runtime_backend,
            opencode_mode=opencode_mode,
        ),
        agent_runtime_backend=runtime_backend,
        opencode_mode=opencode_mode,
    )


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
    | QAHandler,
    ...,
]


def get_ouroboros_tools(
    *,
    runtime_backend: str | None = None,
    llm_backend: str | None = None,
    mcp_manager: object | None = None,
    mcp_tool_prefix: str = "",
    opencode_mode: str | None = None,
    include_auto: bool = True,
) -> OuroborosToolHandlers:
    """Create the default set of Ouroboros MCP tool handlers.

    ``opencode_mode`` is threaded into every handler that dispatches a
    ``_subagent`` envelope. When ``runtime_backend`` is an OpenCode variant
    AND ``opencode_mode`` is ``"plugin"`` the handler returns the envelope.
    In every other combination (including ``opencode_mode=None``) the handler
    falls through to its real in-process path. See
    ``ouroboros.mcp.tools.subagent.should_dispatch_via_plugin``.
    """
    execute_seed = ExecuteSeedHandler(
        agent_runtime_backend=runtime_backend,
        llm_backend=llm_backend,
        mcp_manager=mcp_manager,
        mcp_tool_prefix=mcp_tool_prefix,
        opencode_mode=opencode_mode,
    )
    start_execute = StartExecuteSeedHandler(
        execute_handler=execute_seed,
        agent_runtime_backend=runtime_backend,
        opencode_mode=opencode_mode,
    )
    job_status = JobStatusHandler()
    job_wait = JobWaitHandler()
    job_result = JobResultHandler()
    interview = InterviewHandler(
        llm_backend=llm_backend,
        agent_runtime_backend=runtime_backend,
        opencode_mode=opencode_mode,
    )
    generate_seed = GenerateSeedHandler(
        llm_backend=llm_backend,
        agent_runtime_backend=runtime_backend,
        opencode_mode=opencode_mode,
    )
    evaluate = EvaluateHandler(
        llm_backend=llm_backend,
        agent_runtime_backend=runtime_backend,
        opencode_mode=opencode_mode,
    )
    auto = (
        (
            auto_handler(
                llm_backend=llm_backend,
                runtime_backend=runtime_backend,
                mcp_manager=mcp_manager,
                mcp_tool_prefix=mcp_tool_prefix,
                opencode_mode=opencode_mode,
            ),
        )
        if include_auto
        else ()
    )
    return (
        execute_seed,
        start_execute,
        *auto,
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
        LateralThinkHandler(
            agent_runtime_backend=runtime_backend,
            opencode_mode=opencode_mode,
        ),
        EvolveStepHandler(
            agent_runtime_backend=runtime_backend,
            opencode_mode=opencode_mode,
        ),
        StartEvolveStepHandler(
            evolve_handler=EvolveStepHandler(
                agent_runtime_backend=runtime_backend,
                opencode_mode=opencode_mode,
            ),
            agent_runtime_backend=runtime_backend,
            opencode_mode=opencode_mode,
        ),
        LineageStatusHandler(),
        EvolveRewindHandler(),
        CancelExecutionHandler(),
        BrownfieldHandler(),
        PMInterviewHandler(
            llm_backend=llm_backend,
            agent_runtime_backend=runtime_backend,
            opencode_mode=opencode_mode,
        ),
        QAHandler(
            llm_backend=llm_backend,
            agent_runtime_backend=runtime_backend,
            opencode_mode=opencode_mode,
        ),
    )


class _LazyAutoHandler:
    """Lazy static auto handler to avoid import cycles in OUROBOROS_TOOLS."""

    @property
    def definition(self):
        from ouroboros.mcp.tools.auto_handler import AutoHandler

        return AutoHandler().definition

    async def handle(self, arguments):
        from ouroboros.mcp.tools.auto_handler import AutoHandler

        return await AutoHandler().handle(arguments)


def __getattr__(name: str) -> object:
    """Lazily re-export handlers that would otherwise create import cycles."""
    if name == "AutoHandler":
        from ouroboros.mcp.tools.auto_handler import AutoHandler

        return AutoHandler
    raise AttributeError(name)


# List of all Ouroboros tools for registration
OUROBOROS_TOOLS = (*get_ouroboros_tools(include_auto=False), _LazyAutoHandler())
