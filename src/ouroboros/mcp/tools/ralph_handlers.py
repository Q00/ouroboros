"""Ralph MCP tool handlers.

Provides ``ouroboros_ralph`` as a first-class background job so clients no
longer have to own the multi-generation loop in prompt/skill pseudo-code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPServerError, MCPToolError
from ouroboros.mcp.job_manager import JobLinks, JobManager
from ouroboros.mcp.tools.evolution_handlers import EvolveStepHandler
from ouroboros.mcp.types import (
    ContentType,
    MCPContentItem,
    MCPToolDefinition,
    MCPToolParameter,
    MCPToolResult,
    ToolInputType,
)
from ouroboros.persistence.event_store import EventStore
from ouroboros.ralph_loop import EvolveStepLike, RalphLoopConfig, RalphLoopRunner


@dataclass
class RalphHandler:
    """Start a runtime-owned Ralph loop as a background job."""

    evolve_handler: EvolveStepLike | None = field(default=None, repr=False)
    event_store: EventStore | None = field(default=None, repr=False)
    job_manager: JobManager | None = field(default=None, repr=False)
    agent_runtime_backend: str | None = field(default=None, repr=False)
    opencode_mode: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self._event_store = self.event_store or EventStore()
        self._job_manager = self.job_manager or JobManager(self._event_store)
        self._evolve_handler = self.evolve_handler or EvolveStepHandler(
            agent_runtime_backend=self.agent_runtime_backend,
            opencode_mode=self.opencode_mode,
        )

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the public MCP definition."""
        return MCPToolDefinition(
            name="ouroboros_ralph",
            description=(
                "Start a first-class Ralph loop in the background. The loop repeatedly "
                "runs evolve_step until QA passes, convergence is reached, a terminal "
                "evolution action occurs, cancellation is requested, or max_generations "
                "is reached. Returns a job_id immediately for ouroboros_job_status, "
                "ouroboros_job_wait, ouroboros_job_result, and ouroboros_job_cancel."
            ),
            parameters=(
                MCPToolParameter(
                    name="lineage_id",
                    type=ToolInputType.STRING,
                    description="Lineage ID to start or continue.",
                    required=True,
                ),
                MCPToolParameter(
                    name="seed_content",
                    type=ToolInputType.STRING,
                    description="Seed YAML content for generation 1. Omit for continuation.",
                    required=False,
                ),
                MCPToolParameter(
                    name="execute",
                    type=ToolInputType.BOOLEAN,
                    description="Whether each generation should execute and evaluate. Default: true.",
                    required=False,
                    default=True,
                ),
                MCPToolParameter(
                    name="parallel",
                    type=ToolInputType.BOOLEAN,
                    description="Whether each generation may execute ACs in parallel. Default: true.",
                    required=False,
                    default=True,
                ),
                MCPToolParameter(
                    name="skip_qa",
                    type=ToolInputType.BOOLEAN,
                    description="Skip post-execution QA. Default: false.",
                    required=False,
                    default=False,
                ),
                MCPToolParameter(
                    name="project_dir",
                    type=ToolInputType.STRING,
                    description="Project root forwarded to each evolve_step generation.",
                    required=False,
                ),
                MCPToolParameter(
                    name="max_generations",
                    type=ToolInputType.INTEGER,
                    description="Maximum generations to run before stopping. Default: 10.",
                    required=False,
                    default=10,
                ),
            ),
        )

    async def handle(self, arguments: dict[str, Any]) -> Result[MCPToolResult, MCPServerError]:
        """Start the Ralph loop job and return a job handle immediately."""
        lineage_id = arguments.get("lineage_id")
        if not lineage_id:
            return Result.err(MCPToolError("lineage_id is required", tool_name="ouroboros_ralph"))

        try:
            max_generations = int(arguments.get("max_generations", 10))
        except (TypeError, ValueError):
            return Result.err(
                MCPToolError("max_generations must be an integer", tool_name="ouroboros_ralph")
            )
        if max_generations < 1:
            return Result.err(
                MCPToolError("max_generations must be >= 1", tool_name="ouroboros_ralph")
            )

        config = RalphLoopConfig(
            lineage_id=str(lineage_id),
            seed_content=arguments.get("seed_content"),
            execute=bool(arguments.get("execute", True)),
            parallel=bool(arguments.get("parallel", True)),
            skip_qa=bool(arguments.get("skip_qa", False)),
            project_dir=arguments.get("project_dir"),
            max_generations=max_generations,
        )
        runner = RalphLoopRunner(self._evolve_handler)

        async def _run_loop() -> MCPToolResult:
            result = await runner.run(config)
            return result.to_tool_result()

        snapshot = await self._job_manager.start_job(
            job_type="ralph",
            initial_message=f"Queued Ralph loop for {config.lineage_id}",
            runner=_run_loop(),
            links=JobLinks(lineage_id=config.lineage_id),
        )

        text = (
            "Started background Ralph loop.\n\n"
            f"Job ID: {snapshot.job_id}\n"
            f"Lineage ID: {config.lineage_id}\n"
            f"Max generations: {config.max_generations}\n\n"
            "Use ouroboros_job_status, ouroboros_job_wait, ouroboros_job_result, "
            "or ouroboros_job_cancel to monitor it."
        )
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text=text),),
                is_error=False,
                meta={
                    "job_id": snapshot.job_id,
                    "lineage_id": config.lineage_id,
                    "status": snapshot.status.value,
                    "cursor": snapshot.cursor,
                    "max_generations": config.max_generations,
                },
            )
        )
