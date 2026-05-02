"""MCP handler for full-quality ``ooo auto`` sessions."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from typing import Any

from ouroboros.auto.adapters import (
    HandlerInterviewBackend,
    HandlerRunStarter,
    HandlerSeedGenerator,
    load_seed,
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
    mcp_manager: object | None = field(default=None, repr=False)
    mcp_tool_prefix: str = ""

    @property
    def definition(self) -> MCPToolDefinition:
        return MCPToolDefinition(
            name="ouroboros_auto",
            description=(
                "Run full-quality ooo auto: automatically interview, generate an A-grade Seed, "
                "and start execution only after the A-grade gate passes. All loops are bounded."
            ),
            parameters=(
                MCPToolParameter(
                    "goal", ToolInputType.STRING, "Goal/task for ooo auto", required=False
                ),
                MCPToolParameter("cwd", ToolInputType.STRING, "Working directory", required=False),
                MCPToolParameter(
                    "resume", ToolInputType.STRING, "Auto session id to resume", required=False
                ),
                MCPToolParameter(
                    "max_interview_rounds",
                    ToolInputType.INTEGER,
                    "Max interview rounds",
                    required=False,
                    default=12,
                ),
                MCPToolParameter(
                    "max_repair_rounds",
                    ToolInputType.INTEGER,
                    "Max repair rounds",
                    required=False,
                    default=5,
                ),
                MCPToolParameter(
                    "skip_run",
                    ToolInputType.BOOLEAN,
                    "Stop after A-grade Seed",
                    required=False,
                    default=False,
                ),
            ),
        )

    async def handle(self, arguments: dict[str, Any]) -> Result[MCPToolResult, MCPServerError]:
        try:
            result = await self._run(arguments)
        except Exception as exc:
            return Result.err(
                MCPToolError(f"Auto pipeline failed: {exc}", tool_name="ouroboros_auto")
            )
        meta: dict[str, Any] = {
            "status": result.status,
            "auto_session_id": result.auto_session_id,
            "phase": result.phase,
            "grade": result.grade,
            "seed_path": result.seed_path,
            "interview_session_id": result.interview_session_id,
            "execution_id": result.execution_id,
            "job_id": result.job_id,
            "run_session_id": result.run_session_id,
            "resume_command": f"ooo auto --resume {result.auto_session_id}",
            "blocker": result.blocker,
        }
        text = _format_result(result)
        if result.run_subagent is not None:
            meta["_subagent"] = result.run_subagent
            text = json.dumps({**meta, "message": text})
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text=text),),
                is_error=result.status in {"blocked", "failed"},
                meta=meta,
            )
        )

    async def _run(self, arguments: dict[str, Any]) -> AutoPipelineResult:
        store = self.store or AutoStore()
        resume = arguments.get("resume")
        requested_cwd = str(arguments.get("cwd") or _safe_default_cwd())
        requested_skip_run = bool(arguments.get("skip_run", False))
        if isinstance(resume, str) and resume:
            state = store.load(resume)
            cwd = state.cwd
            runtime_backend = state.runtime_backend or self.agent_runtime_backend
            opencode_mode = state.opencode_mode or self.opencode_mode
            skip_run = requested_skip_run or state.skip_run
        else:
            goal = arguments.get("goal")
            if not isinstance(goal, str) or not goal.strip():
                raise ValueError("goal is required when not resuming")
            cwd = requested_cwd
            runtime_backend = self.agent_runtime_backend
            opencode_mode = self.opencode_mode
            skip_run = requested_skip_run
            state = AutoPipelineState(goal=goal.strip(), cwd=cwd)
        state.runtime_backend = runtime_backend
        state.opencode_mode = opencode_mode
        state.skip_run = skip_run

        authoring_opencode_mode = "subprocess" if opencode_mode == "plugin" else opencode_mode
        interview_handler = _authoring_interview_handler(
            self.interview_handler,
            llm_backend=self.llm_backend,
            agent_runtime_backend=runtime_backend,
            opencode_mode=authoring_opencode_mode,
        )
        generate_seed_handler = _authoring_seed_handler(
            self.generate_seed_handler,
            llm_backend=self.llm_backend,
            agent_runtime_backend=runtime_backend,
            opencode_mode=authoring_opencode_mode,
        )
        start_execute = _execution_start_handler(
            self.start_execute_seed_handler,
            llm_backend=self.llm_backend,
            agent_runtime_backend=runtime_backend,
            opencode_mode=opencode_mode,
            mcp_manager=self.mcp_manager,
            mcp_tool_prefix=self.mcp_tool_prefix,
        )

        driver = AutoInterviewDriver(
            HandlerInterviewBackend(interview_handler, cwd=cwd),
            store=store,
            max_rounds=_positive_int_arg(arguments, "max_interview_rounds", 12),
        )
        pipeline = AutoPipeline(
            driver,
            HandlerSeedGenerator(generate_seed_handler),
            run_starter=HandlerRunStarter(start_execute, cwd=cwd),
            store=store,
            repairer=SeedRepairer(
                max_repair_rounds=_positive_int_arg(arguments, "max_repair_rounds", 5)
            ),
            seed_saver=save_seed,
            seed_loader=load_seed,
            skip_run=skip_run,
        )
        return await pipeline.run(state)


def _positive_int_arg(arguments: dict[str, Any], name: str, default: int) -> int:
    value = arguments.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"{name} must be a positive integer"
        raise ValueError(msg)
    if value <= 0:
        msg = f"{name} must be >= 1"
        raise ValueError(msg)
    return value


def _safe_default_cwd() -> Path:
    cwd = Path.cwd()
    if cwd == Path("/") or not os.access(cwd, os.W_OK):
        return Path.home()
    return cwd


def _authoring_interview_handler(
    handler: InterviewHandler | None,
    *,
    llm_backend: str | None,
    agent_runtime_backend: str | None,
    opencode_mode: str | None,
) -> InterviewHandler:
    if handler is None:
        return InterviewHandler(
            llm_backend=llm_backend,
            agent_runtime_backend=agent_runtime_backend,
            opencode_mode=opencode_mode,
        )
    if opencode_mode != "subprocess" or getattr(handler, "opencode_mode", None) != "plugin":
        return handler
    return InterviewHandler(
        interview_engine=handler.interview_engine,
        event_store=handler.event_store,
        llm_adapter=handler.llm_adapter,
        llm_backend=handler.llm_backend,
        agent_runtime_backend=handler.agent_runtime_backend,
        opencode_mode=opencode_mode,
        data_dir=handler.data_dir,
    )


def _authoring_seed_handler(
    handler: GenerateSeedHandler | None,
    *,
    llm_backend: str | None,
    agent_runtime_backend: str | None,
    opencode_mode: str | None,
) -> GenerateSeedHandler:
    if handler is None:
        return GenerateSeedHandler(
            llm_backend=llm_backend,
            agent_runtime_backend=agent_runtime_backend,
            opencode_mode=opencode_mode,
        )
    if opencode_mode != "subprocess" or getattr(handler, "opencode_mode", None) != "plugin":
        return handler
    return GenerateSeedHandler(
        interview_engine=handler.interview_engine,
        seed_generator=handler.seed_generator,
        llm_adapter=handler.llm_adapter,
        llm_backend=handler.llm_backend,
        event_store=handler.event_store,
        data_dir=handler.data_dir,
        agent_runtime_backend=handler.agent_runtime_backend,
        opencode_mode=opencode_mode,
    )


def _execution_start_handler(
    handler: StartExecuteSeedHandler | None,
    *,
    llm_backend: str | None,
    agent_runtime_backend: str | None,
    opencode_mode: str | None,
    mcp_manager: object | None,
    mcp_tool_prefix: str,
) -> StartExecuteSeedHandler:
    if handler is not None:
        return handler
    execute_seed = ExecuteSeedHandler(
        llm_backend=llm_backend,
        agent_runtime_backend=agent_runtime_backend,
        opencode_mode=opencode_mode,
        mcp_manager=mcp_manager,
        mcp_tool_prefix=mcp_tool_prefix,
    )
    return StartExecuteSeedHandler(
        execute_handler=execute_seed,
        agent_runtime_backend=agent_runtime_backend,
        opencode_mode=opencode_mode,
    )


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
    if result.job_id or result.execution_id or result.run_session_id:
        lines.extend(
            [
                "Execution started:",
                f"  job_id: {result.job_id}",
                f"  execution_id: {result.execution_id}",
                f"  session_id: {result.run_session_id}",
            ]
        )
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
