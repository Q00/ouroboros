"""Execution-related tool handlers for MCP server.

This module contains handlers for seed execution:
- ExecuteSeedHandler: Synchronous seed execution
- StartExecuteSeedHandler: Asynchronous (background) seed execution with job tracking
"""

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import ValidationError as PydanticValidationError
from rich.console import Console
import structlog
import yaml

from ouroboros.core.errors import ValidationError
from ouroboros.core.security import InputValidator
from ouroboros.core.seed import Seed
from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPServerError, MCPToolError
from ouroboros.mcp.job_manager import JobLinks, JobManager
from ouroboros.mcp.types import (
    ContentType,
    MCPContentItem,
    MCPToolDefinition,
    MCPToolParameter,
    MCPToolResult,
    ToolInputType,
)
from ouroboros.orchestrator import create_agent_runtime
from ouroboros.orchestrator.runner import OrchestratorRunner
from ouroboros.orchestrator.session import SessionRepository, SessionStatus
from ouroboros.persistence.event_store import EventStore
from ouroboros.providers.base import LLMAdapter

log = structlog.get_logger(__name__)


@dataclass
class ExecuteSeedHandler:
    """Handler for the execute_seed tool.

    Executes a seed (task specification) in the Ouroboros system.
    This is the primary entry point for running tasks.
    """

    event_store: EventStore | None = field(default=None, repr=False)
    llm_adapter: LLMAdapter | None = field(default=None, repr=False)
    llm_backend: str | None = field(default=None, repr=False)
    agent_runtime_backend: str | None = field(default=None, repr=False)
    _background_tasks: set[asyncio.Task[None]] = field(default_factory=set, init=False, repr=False)

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the tool definition."""
        return MCPToolDefinition(
            name="ouroboros_execute_seed",
            description=(
                "Execute a seed (task specification) in Ouroboros. "
                "A seed defines a task to be executed with acceptance criteria. "
                "This is the handler for 'ooo run' commands — "
                "do NOT run 'ooo' in the shell; call this MCP tool instead."
            ),
            parameters=(
                MCPToolParameter(
                    name="seed_content",
                    type=ToolInputType.STRING,
                    description="Inline seed YAML content to execute.",
                    required=False,
                ),
                MCPToolParameter(
                    name="seed_path",
                    type=ToolInputType.STRING,
                    description=(
                        "Path to a seed YAML file. If the path does not exist, the value is "
                        "treated as inline seed YAML."
                    ),
                    required=False,
                ),
                MCPToolParameter(
                    name="cwd",
                    type=ToolInputType.STRING,
                    description="Working directory used to resolve relative seed paths.",
                    required=False,
                ),
                MCPToolParameter(
                    name="session_id",
                    type=ToolInputType.STRING,
                    description="Optional session ID to resume. If not provided, a new session is created.",
                    required=False,
                ),
                MCPToolParameter(
                    name="model_tier",
                    type=ToolInputType.STRING,
                    description="Model tier to use (small, medium, large). Default: medium",
                    required=False,
                    default="medium",
                    enum=("small", "medium", "large"),
                ),
                MCPToolParameter(
                    name="max_iterations",
                    type=ToolInputType.INTEGER,
                    description="Maximum number of execution iterations. Default: 10",
                    required=False,
                    default=10,
                ),
                MCPToolParameter(
                    name="skip_qa",
                    type=ToolInputType.BOOLEAN,
                    description="Skip post-execution QA evaluation. Default: false",
                    required=False,
                    default=False,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
        *,
        execution_id: str | None = None,
        session_id_override: str | None = None,
    ) -> Result[MCPToolResult, MCPServerError]:
        """Handle a seed execution request.

        Args:
            arguments: Tool arguments including seed_content or seed_path.
            execution_id: Pre-allocated execution ID (used by StartExecuteSeedHandler).
            session_id_override: Pre-allocated session ID for new executions
                (used by StartExecuteSeedHandler).

        Returns:
            Result containing execution result or error.
        """
        resolved_cwd = self._resolve_dispatch_cwd(arguments.get("cwd"))
        seed_content = arguments.get("seed_content")
        seed_path = arguments.get("seed_path")
        if not seed_content and seed_path:
            seed_candidate = Path(str(seed_path)).expanduser()
            if not seed_candidate.is_absolute():
                seed_candidate = resolved_cwd / seed_candidate

            # Allow seeds from both cwd and the global ~/.ouroboros/ directory
            ouroboros_home = Path.home() / ".ouroboros"
            valid_cwd, _ = InputValidator.validate_path_containment(
                seed_candidate,
                resolved_cwd,
            )
            valid_home, _ = InputValidator.validate_path_containment(
                seed_candidate,
                ouroboros_home,
            )
            if not valid_cwd and not valid_home:
                return Result.err(
                    MCPToolError(
                        f"Seed path escapes allowed directories: "
                        f"{seed_candidate} is not under {resolved_cwd} or {ouroboros_home}",
                        tool_name="ouroboros_execute_seed",
                    )
                )

            try:
                seed_content = await asyncio.to_thread(
                    seed_candidate.read_text,
                    encoding="utf-8",
                )
            except FileNotFoundError:
                return Result.err(
                    MCPToolError(
                        f"Seed file not found: {seed_candidate}",
                        tool_name="ouroboros_execute_seed",
                    )
                )
            except OSError as e:
                return Result.err(
                    MCPToolError(
                        f"Failed to read seed file: {e}",
                        tool_name="ouroboros_execute_seed",
                    )
                )

        if not seed_content:
            return Result.err(
                MCPToolError(
                    "seed_content or seed_path is required",
                    tool_name="ouroboros_execute_seed",
                )
            )

        session_id = arguments.get("session_id") or session_id_override
        model_tier = arguments.get("model_tier", "medium")
        max_iterations = arguments.get("max_iterations", 10)

        log.info(
            "mcp.tool.execute_seed",
            session_id=session_id,
            model_tier=model_tier,
            max_iterations=max_iterations,
            runtime_backend=self.agent_runtime_backend,
            llm_backend=self.llm_backend,
            cwd=str(resolved_cwd),
        )

        # Parse seed_content YAML into Seed object
        try:
            seed_dict = yaml.safe_load(seed_content)
            seed = Seed.from_dict(seed_dict)
        except yaml.YAMLError as e:
            log.error("mcp.tool.execute_seed.yaml_error", error=str(e))
            return Result.err(
                MCPToolError(
                    f"Failed to parse seed YAML: {e}",
                    tool_name="ouroboros_execute_seed",
                )
            )
        except (ValidationError, PydanticValidationError) as e:
            log.error("mcp.tool.execute_seed.validation_error", error=str(e))
            return Result.err(
                MCPToolError(
                    f"Seed validation failed: {e}",
                    tool_name="ouroboros_execute_seed",
                )
            )

        # Use injected or create orchestrator dependencies
        try:
            from ouroboros.orchestrator.runtime_factory import resolve_agent_runtime_backend
            from ouroboros.providers.factory import resolve_llm_backend

            agent_adapter = create_agent_runtime(
                backend=self.agent_runtime_backend,
                cwd=resolved_cwd,
                llm_backend=self.llm_backend,
            )
            runtime_backend = resolve_agent_runtime_backend(self.agent_runtime_backend)
            resolved_llm_backend = resolve_llm_backend(self.llm_backend)
            event_store = self.event_store or EventStore()
            owns_event_store = self.event_store is None
            await event_store.initialize()
            # Use stderr: in MCP stdio mode, stdout is the JSON-RPC channel.
            console = Console(stderr=True)

            # Create orchestrator runner
            runner = OrchestratorRunner(
                adapter=agent_adapter,
                event_store=event_store,
                console=console,
                debug=False,
                enable_decomposition=True,
            )
            session_repo = SessionRepository(event_store)

            skip_qa = arguments.get("skip_qa", False)
            if session_id:
                tracker_result = await session_repo.reconstruct_session(session_id)
                if tracker_result.is_err:
                    return Result.err(
                        MCPToolError(
                            f"Session resume failed: {tracker_result.error.message}",
                            tool_name="ouroboros_execute_seed",
                        )
                    )
                tracker = tracker_result.value
                if tracker.status in (
                    SessionStatus.COMPLETED,
                    SessionStatus.CANCELLED,
                    SessionStatus.FAILED,
                ):
                    return Result.err(
                        MCPToolError(
                            (
                                f"Session {tracker.session_id} is already "
                                f"{tracker.status.value} and cannot be resumed"
                            ),
                            tool_name="ouroboros_execute_seed",
                        )
                    )
            else:
                prepared = await runner.prepare_session(seed)
                if prepared.is_err:
                    return Result.err(
                        MCPToolError(
                            f"Execution failed: {prepared.error.message}",
                            tool_name="ouroboros_execute_seed",
                        )
                    )
                tracker = prepared.value

            # Fire-and-forget: launch execution in a background task and
            # return the session/execution IDs immediately so the MCP
            # client is not blocked by Codex's tool-call timeout.
            async def _run_in_background(
                _runner: OrchestratorRunner,
                _seed: Seed,
                _tracker,
                _seed_content: str,
                _resume_existing: bool,
                _skip_qa: bool,
                _session_repo: SessionRepository = session_repo,
                _event_store: EventStore = event_store,
                _owns_event_store: bool = owns_event_store,
            ) -> None:
                try:
                    if _resume_existing:
                        result = await _runner.resume_session(_tracker.session_id, _seed)
                    else:
                        result = await _runner.execute_precreated_session(
                            seed=_seed,
                            tracker=_tracker,
                            parallel=True,
                        )
                    if result.is_err:
                        log.error(
                            "mcp.tool.execute_seed.background_failed",
                            session_id=_tracker.session_id,
                            error=str(result.error),
                        )
                        await _session_repo.mark_failed(
                            _tracker.session_id,
                            error_message=str(result.error),
                        )
                        return
                    if not result.value.success:
                        log.warning(
                            "mcp.tool.execute_seed.background_unsuccessful",
                            session_id=_tracker.session_id,
                            message=result.value.final_message,
                        )
                        return
                    if not _skip_qa:
                        from ouroboros.mcp.tools.qa import QAHandler

                        qa_handler = QAHandler(
                            llm_adapter=self.llm_adapter,
                            llm_backend=self.llm_backend,
                        )
                        quality_bar = self._derive_quality_bar(_seed)
                        await qa_handler.handle(
                            {
                                "artifact": result.value.final_message or "",
                                "artifact_type": "test_output",
                                "quality_bar": quality_bar,
                                "seed_content": _seed_content,
                                "pass_threshold": 0.80,
                            }
                        )
                except Exception:
                    log.exception(
                        "mcp.tool.execute_seed.background_error",
                        session_id=_tracker.session_id,
                    )
                    try:
                        await _session_repo.mark_failed(
                            _tracker.session_id,
                            error_message="Unexpected error in background execution",
                        )
                    except Exception:
                        log.exception("mcp.tool.execute_seed.mark_failed_error")
                finally:
                    if _owns_event_store:
                        try:
                            await _event_store.close()
                        except Exception:
                            log.exception("mcp.tool.execute_seed.event_store_close_error")

            task = asyncio.create_task(
                _run_in_background(runner, seed, tracker, seed_content, bool(session_id), skip_qa)
            )
            # Prevent the task from being garbage-collected.
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

            # Return immediately with the seed ID.  The execution runs
            # in the background and progress can be tracked via
            # ouroboros_session_status / ouroboros_query_events.
            return Result.ok(
                MCPToolResult(
                    content=(
                        MCPContentItem(
                            type=ContentType.TEXT,
                            text=(
                                f"Seed Execution LAUNCHED\n"
                                f"{'=' * 60}\n"
                                f"Seed ID: {seed.metadata.seed_id}\n"
                                f"Session ID: {tracker.session_id}\n"
                                f"Execution ID: {tracker.execution_id}\n"
                                f"Goal: {seed.goal}\n\n"
                                f"Runtime Backend: {runtime_backend}\n"
                                f"LLM Backend: {resolved_llm_backend}\n\n"
                                f"Execution is running in the background.\n"
                                f"Use ouroboros_session_status to track progress.\n"
                                f"Use ouroboros_query_events for detailed event history.\n"
                            ),
                        ),
                    ),
                    is_error=False,
                    meta={
                        "seed_id": seed.metadata.seed_id,
                        "session_id": tracker.session_id,
                        "execution_id": tracker.execution_id,
                        "launched": True,
                        "status": "running",
                        "runtime_backend": runtime_backend,
                        "llm_backend": self.llm_backend,
                        "resume_requested": bool(session_id),
                    },
                )
            )
        except Exception as e:
            log.error("mcp.tool.execute_seed.error", error=str(e))
            return Result.err(
                MCPToolError(
                    f"Seed execution failed: {e}",
                    tool_name="ouroboros_execute_seed",
                )
            )

    @staticmethod
    def _resolve_dispatch_cwd(raw_cwd: Any) -> Path:
        """Resolve the working directory for intercepted seed execution."""
        if isinstance(raw_cwd, str) and raw_cwd.strip():
            return Path(raw_cwd).expanduser().resolve()
        return Path.cwd()

    @staticmethod
    def _derive_quality_bar(seed: Seed) -> str:
        """Derive a quality bar string from seed acceptance criteria."""
        ac_lines = [f"- {ac}" for ac in seed.acceptance_criteria]
        return "The execution must satisfy all acceptance criteria:\n" + "\n".join(ac_lines)

    @staticmethod
    def _format_execution_result(exec_result, seed: Seed) -> str:
        """Format execution result as human-readable text.

        Args:
            exec_result: OrchestratorResult from execution.
            seed: Original seed specification.

        Returns:
            Formatted text representation.
        """
        status = "SUCCESS" if exec_result.success else "FAILED"
        lines = [
            f"Seed Execution {status}",
            "=" * 60,
            f"Seed ID: {seed.metadata.seed_id}",
            f"Session ID: {exec_result.session_id}",
            f"Execution ID: {exec_result.execution_id}",
            f"Goal: {seed.goal}",
            f"Messages Processed: {exec_result.messages_processed}",
            f"Duration: {exec_result.duration_seconds:.2f}s",
            "",
        ]

        if exec_result.summary:
            lines.append("Summary:")
            for key, value in exec_result.summary.items():
                lines.append(f"  {key}: {value}")
            lines.append("")

        if exec_result.final_message:
            lines.extend(
                [
                    "Final Message:",
                    "-" * 40,
                    exec_result.final_message[:1000],
                ]
            )
            if len(exec_result.final_message) > 1000:
                lines.append("...(truncated)")

        return "\n".join(lines)


@dataclass
class StartExecuteSeedHandler:
    """Start a seed execution asynchronously and return a job ID immediately."""

    execute_handler: ExecuteSeedHandler | None = field(default=None, repr=False)
    event_store: EventStore | None = field(default=None, repr=False)
    job_manager: JobManager | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self._event_store = self.event_store or EventStore()
        self._job_manager = self.job_manager or JobManager(self._event_store)
        self._execute_handler = self.execute_handler or ExecuteSeedHandler(
            event_store=self._event_store
        )

    @property
    def definition(self) -> MCPToolDefinition:
        return MCPToolDefinition(
            name="ouroboros_start_execute_seed",
            description=(
                "Start a seed execution in the background and return a job ID immediately. "
                "Use ouroboros_job_status, ouroboros_job_wait, and ouroboros_job_result "
                "to monitor progress. "
                "This is the handler for 'ooo run' commands — "
                "do NOT run 'ooo' in the shell; call this MCP tool instead."
            ),
            parameters=ExecuteSeedHandler().definition.parameters,
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        seed_content = arguments.get("seed_content")
        seed_path = arguments.get("seed_path")
        if not seed_content and seed_path:
            resolved_cwd = ExecuteSeedHandler._resolve_dispatch_cwd(
                arguments.get("cwd"),
            )
            seed_candidate = Path(str(seed_path)).expanduser()
            if not seed_candidate.is_absolute():
                seed_candidate = resolved_cwd / seed_candidate

            # Allow seeds from both cwd and the global ~/.ouroboros/ directory
            ouroboros_home = Path.home() / ".ouroboros"
            valid_cwd, _ = InputValidator.validate_path_containment(
                seed_candidate,
                resolved_cwd,
            )
            valid_home, _ = InputValidator.validate_path_containment(
                seed_candidate,
                ouroboros_home,
            )
            if not valid_cwd and not valid_home:
                return Result.err(
                    MCPToolError(
                        f"Seed path escapes allowed directories: "
                        f"{seed_candidate} is not under {resolved_cwd} or {ouroboros_home}",
                        tool_name="ouroboros_start_execute_seed",
                    )
                )

            try:
                seed_content = await asyncio.to_thread(seed_candidate.read_text, encoding="utf-8")
                arguments = {**arguments, "seed_content": seed_content}
            except FileNotFoundError:
                return Result.err(
                    MCPToolError(
                        f"Seed file not found: {seed_candidate}",
                        tool_name="ouroboros_start_execute_seed",
                    )
                )
            except OSError as e:
                return Result.err(
                    MCPToolError(
                        f"Failed to read seed file: {e}",
                        tool_name="ouroboros_start_execute_seed",
                    )
                )

        if not seed_content:
            return Result.err(
                MCPToolError(
                    "seed_content or seed_path is required",
                    tool_name="ouroboros_start_execute_seed",
                )
            )

        await self._event_store.initialize()

        session_id = arguments.get("session_id")
        execution_id: str | None = None
        new_session_id: str | None = None
        if session_id:
            repo = SessionRepository(self._event_store)
            session_result = await repo.reconstruct_session(session_id)
            if session_result.is_err:
                return Result.err(
                    MCPToolError(
                        f"Session resume failed: {session_result.error.message}",
                        tool_name="ouroboros_start_execute_seed",
                    )
                )
            execution_id = session_result.value.execution_id
        else:
            execution_id = f"exec_{uuid4().hex[:12]}"
            new_session_id = f"orch_{uuid4().hex[:12]}"

        async def _runner() -> MCPToolResult:
            result = await self._execute_handler.handle(
                arguments,
                execution_id=execution_id,
                session_id_override=new_session_id,
            )
            if result.is_err:
                raise RuntimeError(str(result.error))
            return result.value

        snapshot = await self._job_manager.start_job(
            job_type="execute_seed",
            initial_message="Queued seed execution",
            runner=_runner(),
            links=JobLinks(
                session_id=session_id or new_session_id,
                execution_id=execution_id,
            ),
        )

        from ouroboros.orchestrator.runtime_factory import resolve_agent_runtime_backend
        from ouroboros.providers.factory import resolve_llm_backend

        try:
            runtime_backend = resolve_agent_runtime_backend()
        except (ValueError, Exception):
            runtime_backend = "unknown"
        try:
            llm_backend = resolve_llm_backend()
        except (ValueError, Exception):
            llm_backend = "unknown"

        text = (
            f"Started background execution.\n\n"
            f"Job ID: {snapshot.job_id}\n"
            f"Session ID: {snapshot.links.session_id or 'pending'}\n"
            f"Execution ID: {snapshot.links.execution_id or 'pending'}\n\n"
            f"Runtime Backend: {runtime_backend}\n"
            f"LLM Backend: {llm_backend}\n\n"
            "Use ouroboros_job_status, ouroboros_job_wait, or ouroboros_job_result to monitor it."
        )
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text=text),),
                is_error=False,
                meta={
                    "job_id": snapshot.job_id,
                    "session_id": snapshot.links.session_id,
                    "execution_id": snapshot.links.execution_id,
                    "status": snapshot.status.value,
                    "cursor": snapshot.cursor,
                },
            )
        )
