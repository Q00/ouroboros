"""Durable background entry point for the in-process QA tool."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPServerError, MCPToolError
from ouroboros.mcp.job_manager import JobLinks, JobManager
from ouroboros.mcp.tools.background import (
    _runtime_requires_in_process_job,
    job_work_error,
    start_background_tool_job,
)
from ouroboros.mcp.tools.qa import QAHandler
from ouroboros.mcp.tools.subagent import should_dispatch_via_plugin
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolDefinition, MCPToolResult
from ouroboros.persistence.event_store import EventStore
from ouroboros.providers.base import LLMAdapter


@dataclass
class StartQAHandler:
    """Start pollable QA in a detached worker using the existing job protocol."""

    qa_handler: QAHandler = field(repr=False)
    event_store: EventStore = field(repr=False)
    job_manager: JobManager = field(repr=False)
    llm_backend: str | None = field(default=None, repr=False)
    agent_runtime_backend: str | None = field(default=None, repr=False)
    opencode_mode: str | None = field(default=None, repr=False)
    project_dir: str | None = field(default=None, repr=False)
    reconstructible_backend: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        if self.project_dir:
            self.project_dir = str(Path(self.project_dir).expanduser().resolve())

    @property
    def definition(self) -> MCPToolDefinition:
        return replace(
            self.qa_handler.definition,
            name="ouroboros_start_qa",
            description=(
                "Start QA in a detached background job and return a job ID. "
                "Poll with ouroboros_job_wait and read the verdict with "
                "ouroboros_job_result. The request is written to a private local file; "
                "the full result is stored in the local event store and may repeat input. "
                "The worker reconstructs the configured backend rather than injected adapters. "
                "Requires a durable local event store and a subprocess-capable backend; "
                "host or plugin dispatch should use ouroboros_qa instead."
            ),
        )

    async def handle(self, arguments: dict[str, Any]) -> Result[MCPToolResult, MCPServerError]:
        if not isinstance(arguments.get("artifact"), str):
            return Result.err(MCPToolError("artifact is required", tool_name="ouroboros_start_qa"))
        if not isinstance(arguments.get("quality_bar"), str) or not arguments["quality_bar"]:
            return Result.err(
                MCPToolError("quality_bar is required", tool_name="ouroboros_start_qa")
            )
        if (
            not self.project_dir
            or not self.reconstructible_backend
            or not self.job_manager.durable_jobs_enabled
            or not self.event_store.supports_cross_process_workers
            or _runtime_requires_in_process_job(self.agent_runtime_backend)
            or should_dispatch_via_plugin(self.agent_runtime_backend, self.opencode_mode)
        ):
            return Result.err(
                MCPToolError(
                    "Background QA requires a reconstructible backend, project directory, "
                    "durable event store, and subprocess backend; "
                    "use ouroboros_qa for host or plugin dispatch.",
                    tool_name="ouroboros_start_qa",
                )
            )

        async def run_qa(_handle: Any) -> MCPToolResult:
            result = await self.qa_handler.handle(arguments)
            if result.is_err:
                raise job_work_error(result.error)
            return result.value

        snapshot = await start_background_tool_job(
            job_manager=self.job_manager,
            event_store=self.event_store,
            job_type="qa",
            intent="qa",
            process_scope="qa",
            initial_message="Queued QA evaluation",
            links=JobLinks(),
            work_fn=run_qa,
            cancelled_text="QA cancelled before work began.",
            detached_tool_name="ouroboros_start_qa",
            detached_arguments=arguments,
            runtime_backend=self.agent_runtime_backend,
            llm_backend=self.llm_backend,
            opencode_mode=self.opencode_mode,
            project_dir=self.project_dir,
        )
        meta = {
            "job_id": snapshot.job_id,
            "status": snapshot.status.value,
            "cursor": snapshot.cursor,
        }
        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=(
                            f"Started background QA. Job ID: {snapshot.job_id}. "
                            "Use ouroboros_job_wait and ouroboros_job_result."
                        ),
                    ),
                ),
                meta=meta,
                structured_content=dict(meta),
            )
        )


def create_qa_handlers(
    *,
    llm_adapter: LLMAdapter | None,
    event_store: EventStore,
    job_manager: JobManager,
    llm_backend: str | None,
    agent_runtime_backend: str | None,
    opencode_mode: str | None,
    project_dir: str,
) -> tuple[QAHandler, StartQAHandler]:
    """Compose synchronous and durable QA with the same factory-owned backend."""
    qa = QAHandler(
        llm_adapter=llm_adapter,
        event_store=event_store,
        llm_backend=llm_backend,
        agent_runtime_backend=agent_runtime_backend,
        opencode_mode=opencode_mode,
    )
    return qa, StartQAHandler(
        qa_handler=qa,
        event_store=event_store,
        job_manager=job_manager,
        llm_backend=llm_backend,
        agent_runtime_backend=agent_runtime_backend,
        opencode_mode=opencode_mode,
        project_dir=project_dir,
        reconstructible_backend=True,
    )
