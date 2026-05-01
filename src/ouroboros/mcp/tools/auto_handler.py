"""MCP handler for full-quality ``ooo auto`` sessions."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ouroboros.auto.adapters import (
    HandlerInterviewBackend,
    HandlerRunStarter,
    HandlerSeedGenerator,
    save_seed,
)
from ouroboros.auto.interview_driver import AutoInterviewDriver
from ouroboros.auto.pipeline import AutoPipeline, AutoPipelineResult
from ouroboros.auto.seed_repairer import SeedRepairer
from ouroboros.auto.state import AutoPipelineState, AutoStore
from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPServerError, MCPToolError
from ouroboros.mcp.tools.authoring_handlers import GenerateSeedHandler, InterviewHandler
from ouroboros.mcp.tools.execution_handlers import ExecuteSeedHandler, StartExecuteSeedHandler
from ouroboros.mcp.types import (
    ContentType,
    MCPContentItem,
    MCPToolDefinition,
    MCPToolParameter,
    MCPToolResult,
    ToolInputType,
)


@dataclass(slots=True)
class AutoHandler:
    """Run a bounded goal → A-grade Seed → execution handoff pipeline."""

    interview_handler: InterviewHandler | None = field(default=None, repr=False)
    generate_seed_handler: GenerateSeedHandler | None = field(default=None, repr=False)
    start_execute_seed_handler: StartExecuteSeedHandler | None = field(default=None, repr=False)
    store: AutoStore | None = field(default=None, repr=False)
    llm_backend: str | None = field(default=None, repr=False)
    agent_runtime_backend: str | None = field(default=None, repr=False)
    opencode_mode: str | None = field(default=None, repr=False)

    @property
    def definition(self) -> MCPToolDefinition:
        return MCPToolDefinition(
            name="ouroboros_auto",
            description=(
                "Run full-quality ooo auto: automatically interview, generate an A-grade Seed, "
                "and start execution only after the A-grade gate passes. All loops are bounded."
            ),
            parameters=(
                MCPToolParameter("goal", ToolInputType.STRING, "Goal/task for ooo auto", required=False),
                MCPToolParameter("cwd", ToolInputType.STRING, "Working directory", required=False),
                MCPToolParameter("resume", ToolInputType.STRING, "Auto session id to resume", required=False),
                MCPToolParameter("max_interview_rounds", ToolInputType.INTEGER, "Max interview rounds", required=False, default=12),
                MCPToolParameter("max_repair_rounds", ToolInputType.INTEGER, "Max repair rounds", required=False, default=5),
                MCPToolParameter("skip_run", ToolInputType.BOOLEAN, "Stop after A-grade Seed", required=False, default=False),
            ),
        )

    async def handle(self, arguments: dict[str, Any]) -> Result[MCPToolResult, MCPServerError]:
        try:
            result = await self._run(arguments)
        except Exception as exc:
            return Result.err(MCPToolError(f"Auto pipeline failed: {exc}", tool_name="ouroboros_auto"))
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text=_format_result(result)),),
                is_error=result.status in {"blocked", "failed"},
                meta={
                    "status": result.status,
                    "auto_session_id": result.auto_session_id,
                    "phase": result.phase,
                    "grade": result.grade,
                    "seed_path": result.seed_path,
                    "interview_session_id": result.interview_session_id,
                    "execution_id": result.execution_id,
                    "job_id": result.job_id,
                    "resume_command": f"ooo auto --resume {result.auto_session_id}",
                    "blocker": result.blocker,
                },
            )
        )

    async def _run(self, arguments: dict[str, Any]) -> AutoPipelineResult:
        store = self.store or AutoStore()
        resume = arguments.get("resume")
        cwd = str(arguments.get("cwd") or Path.cwd())
        if isinstance(resume, str) and resume:
            state = store.load(resume)
        else:
            goal = arguments.get("goal")
            if not isinstance(goal, str) or not goal.strip():
                raise ValueError("goal is required when not resuming")
            state = AutoPipelineState(goal=goal.strip(), cwd=cwd)

        interview_handler = self.interview_handler or InterviewHandler(
            llm_backend=self.llm_backend,
            agent_runtime_backend=self.agent_runtime_backend,
            opencode_mode=self.opencode_mode,
        )
        generate_seed_handler = self.generate_seed_handler or GenerateSeedHandler(
            llm_backend=self.llm_backend,
            agent_runtime_backend=self.agent_runtime_backend,
            opencode_mode=self.opencode_mode,
        )
        if self.start_execute_seed_handler is not None:
            start_execute = self.start_execute_seed_handler
        else:
            execute_seed = ExecuteSeedHandler(
                llm_backend=self.llm_backend,
                agent_runtime_backend=self.agent_runtime_backend,
                opencode_mode=self.opencode_mode,
            )
            start_execute = StartExecuteSeedHandler(
                execute_handler=execute_seed,
                agent_runtime_backend=self.agent_runtime_backend,
                opencode_mode=self.opencode_mode,
            )

        driver = AutoInterviewDriver(
            HandlerInterviewBackend(interview_handler, cwd=cwd),
            store=store,
            max_rounds=int(arguments.get("max_interview_rounds") or 12),
        )
        pipeline = AutoPipeline(
            driver,
            HandlerSeedGenerator(generate_seed_handler),
            run_starter=HandlerRunStarter(start_execute, cwd=cwd),
            store=store,
            repairer=SeedRepairer(max_repair_rounds=int(arguments.get("max_repair_rounds") or 5)),
            seed_saver=save_seed,
            skip_run=bool(arguments.get("skip_run", False)),
        )
        return await pipeline.run(state)


def _format_result(result: AutoPipelineResult) -> str:
    lines = [
        f"Auto session: {result.auto_session_id}",
        f"Status: {result.status}",
        f"Phase: {result.phase}",
    ]
    if result.grade:
        lines.append(f"Seed grade: {result.grade}")
    if result.interview_session_id:
        lines.append(f"Interview session: {result.interview_session_id}")
    if result.seed_path:
        lines.append(f"Seed: {result.seed_path}")
    if result.job_id or result.execution_id:
        lines.extend(["Execution started:", f"  job_id: {result.job_id}", f"  execution_id: {result.execution_id}"])
    if result.assumptions:
        lines.append("Assumptions:")
        lines.extend(f"- {item}" for item in result.assumptions)
    if result.non_goals:
        lines.append("Non-goals:")
        lines.extend(f"- {item}" for item in result.non_goals)
    if result.blocker:
        lines.append(f"Blocker: {result.blocker}")
    lines.append(f"Resume: ooo auto --resume {result.auto_session_id}")
    return "\n".join(lines)
