"""Ouroboros tool definitions for MCP server.

This module defines the standard Ouroboros tools that are exposed
via the MCP server:
- execute_seed: Execute a seed (task specification)
- session_status: Get current session status
- query_events: Query event history
- evaluate: Evaluate an execution session using three-stage pipeline
- measure_drift: Measure goal deviation from seed specification
- lateral_think: Generate alternative thinking approaches using personas
- ouroboros_interview: Interactive interview for requirement clarification
- ouroboros_generate_seed: Convert interview to immutable seed
"""

from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import ValidationError as PydanticValidationError
from rich.console import Console
import structlog
import yaml

from ouroboros.bigbang.ambiguity import (
    AmbiguityScore,
    AmbiguityScorer,
    ComponentScore,
    ScoreBreakdown,
)
from ouroboros.bigbang.interview import InterviewEngine, InterviewState
from ouroboros.bigbang.seed_generator import SeedGenerator
from ouroboros.core.errors import ValidationError
from ouroboros.core.seed import Seed
from ouroboros.core.text import truncate_head_tail
from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPServerError, MCPToolError
from ouroboros.mcp.job_manager import JobLinks, JobManager, JobSnapshot, JobStatus
from ouroboros.mcp.types import (
    ContentType,
    MCPContentItem,
    MCPToolDefinition,
    MCPToolParameter,
    MCPToolResult,
    ToolInputType,
)
from ouroboros.observability.drift import (
    DRIFT_THRESHOLD,
    DriftMeasurement,
)
from ouroboros.orchestrator.adapter import ClaudeAgentAdapter
from ouroboros.orchestrator.runner import OrchestratorRunner
from ouroboros.orchestrator.session import SessionRepository, SessionStatus
from ouroboros.persistence.event_store import EventStore
from ouroboros.providers.claude_code_adapter import ClaudeCodeAdapter

log = structlog.get_logger(__name__)


@dataclass
class ExecuteSeedHandler:
    """Handler for the execute_seed tool.

    Executes a seed (task specification) in the Ouroboros system.
    This is the primary entry point for running tasks.
    """

    event_store: EventStore | None = field(default=None, repr=False)
    llm_adapter: ClaudeCodeAdapter | None = field(default=None, repr=False)

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the tool definition."""
        return MCPToolDefinition(
            name="ouroboros_execute_seed",
            description=(
                "Execute a seed (task specification) in Ouroboros. "
                "A seed defines a task to be executed with acceptance criteria."
            ),
            parameters=(
                MCPToolParameter(
                    name="seed_content",
                    type=ToolInputType.STRING,
                    description="The seed content describing the task to execute",
                    required=True,
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
            arguments: Tool arguments including seed_content.
            execution_id: Pre-allocated execution ID (used by StartExecuteSeedHandler).
            session_id_override: Pre-allocated session ID for new executions
                (used by StartExecuteSeedHandler).

        Returns:
            Result containing execution result or error.
        """
        seed_content = arguments.get("seed_content")
        if not seed_content:
            return Result.err(
                MCPToolError(
                    "seed_content is required",
                    tool_name="ouroboros_execute_seed",
                )
            )

        session_id = arguments.get("session_id")
        new_session_id = session_id_override
        model_tier = arguments.get("model_tier", "medium")
        max_iterations = arguments.get("max_iterations", 10)

        log.info(
            "mcp.tool.execute_seed",
            session_id=session_id,
            model_tier=model_tier,
            max_iterations=max_iterations,
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
            agent_adapter = ClaudeAgentAdapter(permission_mode="acceptEdits")
            event_store = self.event_store or EventStore()
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

            # Execute or resume session
            if session_id:
                # Resume existing session
                result = await runner.resume_session(session_id, seed)
                if result.is_err:
                    error = result.error
                    return Result.err(
                        MCPToolError(
                            f"Session resume failed: {error.message}",
                            tool_name="ouroboros_execute_seed",
                        )
                    )
                exec_result = result.value
            else:
                # Execute new seed
                result = await runner.execute_seed(
                    seed=seed,
                    execution_id=execution_id,
                    session_id=new_session_id,
                    parallel=True,
                )
                if result.is_err:
                    error = result.error
                    return Result.err(
                        MCPToolError(
                            f"Execution failed: {error.message}",
                            tool_name="ouroboros_execute_seed",
                        )
                    )
                exec_result = result.value

            # Format execution results
            result_text = self._format_execution_result(exec_result, seed)

            # Post-execution QA
            qa_verdict_text = ""
            qa_meta = None
            skip_qa = arguments.get("skip_qa", False)
            if exec_result.success and not skip_qa:
                from ouroboros.mcp.tools.qa import QAHandler

                qa_handler = QAHandler(llm_adapter=self.llm_adapter)
                quality_bar = self._derive_quality_bar(seed)
                qa_result = await qa_handler.handle(
                    {
                        "artifact": exec_result.final_message or "",
                        "artifact_type": "test_output",
                        "quality_bar": quality_bar,
                        "seed_content": seed_content,
                        "pass_threshold": 0.80,
                    }
                )
                if qa_result.is_ok:
                    qa_verdict_text = "\n\n" + qa_result.value.content[0].text
                    qa_meta = qa_result.value.meta

            meta = {
                "session_id": exec_result.session_id,
                "execution_id": exec_result.execution_id,
                "success": exec_result.success,
                "messages_processed": exec_result.messages_processed,
                "duration_seconds": exec_result.duration_seconds,
            }
            if qa_meta:
                meta["qa"] = qa_meta

            return Result.ok(
                MCPToolResult(
                    content=(
                        MCPContentItem(type=ContentType.TEXT, text=result_text + qa_verdict_text),
                    ),
                    is_error=not exec_result.success,
                    meta=meta,
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
class SessionStatusHandler:
    """Handler for the session_status tool.

    Returns the current status of an Ouroboros session.
    """

    event_store: EventStore | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """Initialize the session repository after dataclass creation."""
        self._event_store = self.event_store or EventStore()
        self._session_repo = SessionRepository(self._event_store)
        self._initialized = False

    async def _ensure_initialized(self) -> None:
        """Ensure the event store is initialized."""
        if not self._initialized:
            await self._event_store.initialize()
            self._initialized = True

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the tool definition."""
        return MCPToolDefinition(
            name="ouroboros_session_status",
            description=(
                "Get the status of an Ouroboros session. "
                "Returns information about the current phase, progress, and any errors."
            ),
            parameters=(
                MCPToolParameter(
                    name="session_id",
                    type=ToolInputType.STRING,
                    description="The session ID to query",
                    required=True,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Handle a session status request.

        Args:
            arguments: Tool arguments including session_id.

        Returns:
            Result containing session status or error.
        """
        session_id = arguments.get("session_id")
        if not session_id:
            return Result.err(
                MCPToolError(
                    "session_id is required",
                    tool_name="ouroboros_session_status",
                )
            )

        log.info("mcp.tool.session_status", session_id=session_id)

        try:
            # Ensure event store is initialized
            await self._ensure_initialized()

            # Query session state from repository
            result = await self._session_repo.reconstruct_session(session_id)

            if result.is_err:
                error = result.error
                return Result.err(
                    MCPToolError(
                        f"Session not found: {error.message}",
                        tool_name="ouroboros_session_status",
                    )
                )

            tracker = result.value

            # Build status response from SessionTracker
            status_text = (
                f"Session: {tracker.session_id}\n"
                f"Status: {tracker.status.value}\n"
                f"Execution ID: {tracker.execution_id}\n"
                f"Seed ID: {tracker.seed_id}\n"
                f"Messages Processed: {tracker.messages_processed}\n"
                f"Start Time: {tracker.start_time.isoformat()}\n"
            )

            if tracker.last_message_time:
                status_text += f"Last Message: {tracker.last_message_time.isoformat()}\n"

            if tracker.progress:
                status_text += "\nProgress:\n"
                for key, value in tracker.progress.items():
                    status_text += f"  {key}: {value}\n"

            return Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text=status_text),),
                    is_error=False,
                    meta={
                        "session_id": tracker.session_id,
                        "status": tracker.status.value,
                        "execution_id": tracker.execution_id,
                        "seed_id": tracker.seed_id,
                        "is_active": tracker.is_active,
                        "is_completed": tracker.is_completed,
                        "is_failed": tracker.is_failed,
                        "messages_processed": tracker.messages_processed,
                        "progress": tracker.progress,
                    },
                )
            )
        except Exception as e:
            log.error("mcp.tool.session_status.error", error=str(e))
            return Result.err(
                MCPToolError(
                    f"Failed to get session status: {e}",
                    tool_name="ouroboros_session_status",
                )
            )


@dataclass
class QueryEventsHandler:
    """Handler for the query_events tool.

    Queries the event history for a session or across sessions.
    """

    event_store: EventStore | None = field(default=None, repr=False)

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the tool definition."""
        return MCPToolDefinition(
            name="ouroboros_query_events",
            description=(
                "Query the event history for an Ouroboros session. "
                "Returns a list of events matching the specified criteria."
            ),
            parameters=(
                MCPToolParameter(
                    name="session_id",
                    type=ToolInputType.STRING,
                    description="Filter events by session ID. If not provided, returns events across all sessions.",
                    required=False,
                ),
                MCPToolParameter(
                    name="event_type",
                    type=ToolInputType.STRING,
                    description="Filter by event type (e.g., 'execution', 'evaluation', 'error')",
                    required=False,
                ),
                MCPToolParameter(
                    name="limit",
                    type=ToolInputType.INTEGER,
                    description="Maximum number of events to return. Default: 50",
                    required=False,
                    default=50,
                ),
                MCPToolParameter(
                    name="offset",
                    type=ToolInputType.INTEGER,
                    description="Number of events to skip for pagination. Default: 0",
                    required=False,
                    default=0,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Handle an event query request.

        Args:
            arguments: Tool arguments for filtering events.

        Returns:
            Result containing matching events or error.
        """
        session_id = arguments.get("session_id")
        event_type = arguments.get("event_type")
        limit = arguments.get("limit", 50)
        offset = arguments.get("offset", 0)

        log.info(
            "mcp.tool.query_events",
            session_id=session_id,
            event_type=event_type,
            limit=limit,
            offset=offset,
        )

        try:
            # Use injected or create event store
            store = self.event_store or EventStore()
            await store.initialize()

            # Query events from the store
            events = await store.query_events(
                aggregate_id=session_id,  # session_id maps to aggregate_id
                event_type=event_type,
                limit=limit,
                offset=offset,
            )

            # Only close if we created the store ourselves
            if self.event_store is None:
                await store.close()

            # Format events for response
            events_text = self._format_events(events, session_id, event_type, offset, limit)

            return Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text=events_text),),
                    is_error=False,
                    meta={
                        "total_events": len(events),
                        "offset": offset,
                        "limit": limit,
                    },
                )
            )
        except Exception as e:
            log.error("mcp.tool.query_events.error", error=str(e))
            return Result.err(
                MCPToolError(
                    f"Failed to query events: {e}",
                    tool_name="ouroboros_query_events",
                )
            )

    def _format_events(
        self,
        events: list,
        session_id: str | None,
        event_type: str | None,
        offset: int,
        limit: int,
    ) -> str:
        """Format events as human-readable text.

        Args:
            events: List of BaseEvent objects.
            session_id: Optional session ID filter.
            event_type: Optional event type filter.
            offset: Pagination offset.
            limit: Pagination limit.

        Returns:
            Formatted text representation.
        """
        lines = [
            "Event Query Results",
            "=" * 60,
            f"Session: {session_id or 'all'}",
            f"Type filter: {event_type or 'all'}",
            f"Showing {offset} to {offset + len(events)} (found {len(events)} events)",
            "",
        ]

        if not events:
            lines.append("No events found matching the criteria.")
        else:
            for i, event in enumerate(events, start=offset + 1):
                lines.extend(
                    [
                        f"{i}. [{event.type}]",
                        f"   ID: {event.id}",
                        f"   Timestamp: {event.timestamp.isoformat()}",
                        f"   Aggregate: {event.aggregate_type}/{event.aggregate_id}",
                        f"   Data: {str(event.data)[:100]}..."
                        if len(str(event.data)) > 100
                        else f"   Data: {event.data}",
                        "",
                    ]
                )

        return "\n".join(lines)


@dataclass
class GenerateSeedHandler:
    """Handler for the ouroboros_generate_seed tool.

    Converts a completed interview session into an immutable Seed specification.
    The seed generation gates on ambiguity score (must be <= 0.2).
    """

    interview_engine: InterviewEngine | None = field(default=None, repr=False)
    seed_generator: SeedGenerator | None = field(default=None, repr=False)
    llm_adapter: ClaudeCodeAdapter | None = field(default=None, repr=False)

    def _build_ambiguity_score_from_value(self, ambiguity_score_value: float) -> AmbiguityScore:
        """Build an ambiguity score object from an explicit numeric override."""
        breakdown = ScoreBreakdown(
            goal_clarity=ComponentScore(
                name="goal_clarity",
                clarity_score=1.0 - ambiguity_score_value,
                weight=0.40,
                justification="Provided as input parameter",
            ),
            constraint_clarity=ComponentScore(
                name="constraint_clarity",
                clarity_score=1.0 - ambiguity_score_value,
                weight=0.30,
                justification="Provided as input parameter",
            ),
            success_criteria_clarity=ComponentScore(
                name="success_criteria_clarity",
                clarity_score=1.0 - ambiguity_score_value,
                weight=0.30,
                justification="Provided as input parameter",
            ),
        )
        return AmbiguityScore(
            overall_score=ambiguity_score_value,
            breakdown=breakdown,
        )

    def _load_stored_ambiguity_score(self, state: InterviewState) -> AmbiguityScore | None:
        """Load a persisted ambiguity score snapshot from interview state."""
        if state.ambiguity_score is None:
            return None

        if isinstance(state.ambiguity_breakdown, dict):
            try:
                breakdown = ScoreBreakdown.model_validate(state.ambiguity_breakdown)
            except PydanticValidationError:
                log.warning(
                    "mcp.tool.generate_seed.invalid_stored_ambiguity_breakdown",
                    session_id=state.interview_id,
                )
            else:
                return AmbiguityScore(
                    overall_score=state.ambiguity_score,
                    breakdown=breakdown,
                )

        return self._build_ambiguity_score_from_value(state.ambiguity_score)

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the tool definition."""
        return MCPToolDefinition(
            name="ouroboros_generate_seed",
            description=(
                "Generate an immutable Seed from a completed interview session. "
                "The seed contains structured requirements (goal, constraints, acceptance criteria) "
                "extracted from the interview conversation. Generation requires ambiguity_score <= 0.2."
            ),
            parameters=(
                MCPToolParameter(
                    name="session_id",
                    type=ToolInputType.STRING,
                    description="Interview session ID to convert to a seed",
                    required=True,
                ),
                MCPToolParameter(
                    name="ambiguity_score",
                    type=ToolInputType.NUMBER,
                    description=(
                        "Ambiguity score for the interview (0.0 = clear, 1.0 = ambiguous). "
                        "Required if interview didn't calculate it. Generation fails if > 0.2."
                    ),
                    required=False,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Handle a seed generation request.

        Args:
            arguments: Tool arguments including session_id and optional ambiguity_score.

        Returns:
            Result containing generated Seed YAML or error.
        """
        session_id = arguments.get("session_id")
        if not session_id:
            return Result.err(
                MCPToolError(
                    "session_id is required",
                    tool_name="ouroboros_generate_seed",
                )
            )

        ambiguity_score_value = arguments.get("ambiguity_score")

        log.info(
            "mcp.tool.generate_seed",
            session_id=session_id,
            ambiguity_score=ambiguity_score_value,
        )

        try:
            # Use injected or create services
            llm_adapter = self.llm_adapter or ClaudeCodeAdapter(max_turns=1)
            interview_engine = self.interview_engine or InterviewEngine(
                llm_adapter=llm_adapter,
            )

            # Load interview state
            state_result = await interview_engine.load_state(session_id)

            if state_result.is_err:
                return Result.err(
                    MCPToolError(
                        f"Failed to load interview state: {state_result.error}",
                        tool_name="ouroboros_generate_seed",
                    )
                )

            state: InterviewState = state_result.value

            # Use provided ambiguity score, a persisted snapshot, or compute on demand.
            if ambiguity_score_value is not None:
                ambiguity_score = self._build_ambiguity_score_from_value(ambiguity_score_value)
            else:
                ambiguity_score = self._load_stored_ambiguity_score(state)
                if ambiguity_score is None:
                    scorer = AmbiguityScorer(
                        llm_adapter=llm_adapter,
                    )
                    score_result = await scorer.score(state)
                    if score_result.is_err:
                        return Result.err(
                            MCPToolError(
                                f"Failed to calculate ambiguity: {score_result.error}",
                                tool_name="ouroboros_generate_seed",
                            )
                        )

                    ambiguity_score = score_result.value
                    state.store_ambiguity(
                        score=ambiguity_score.overall_score,
                        breakdown=ambiguity_score.breakdown.model_dump(mode="json"),
                    )
                    save_result = await interview_engine.save_state(state)
                    if save_result.is_err:
                        log.warning(
                            "mcp.tool.generate_seed.persist_ambiguity_failed",
                            session_id=session_id,
                            error=str(save_result.error),
                        )

            # Use injected or create seed generator
            generator = self.seed_generator or SeedGenerator(llm_adapter=llm_adapter)

            # Generate seed
            seed_result = await generator.generate(state, ambiguity_score)

            if seed_result.is_err:
                error = seed_result.error
                if isinstance(error, ValidationError):
                    return Result.err(
                        MCPToolError(
                            f"Validation error: {error}",
                            tool_name="ouroboros_generate_seed",
                        )
                    )
                return Result.err(
                    MCPToolError(
                        f"Failed to generate seed: {error}",
                        tool_name="ouroboros_generate_seed",
                    )
                )

            seed = seed_result.value

            # Convert seed to YAML
            seed_dict = seed.to_dict()
            seed_yaml = yaml.dump(
                seed_dict,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )

            result_text = (
                f"Seed Generated Successfully\n"
                f"=========================\n"
                f"Seed ID: {seed.metadata.seed_id}\n"
                f"Interview ID: {seed.metadata.interview_id}\n"
                f"Ambiguity Score: {seed.metadata.ambiguity_score:.2f}\n"
                f"Goal: {seed.goal}\n\n"
                f"--- Seed YAML ---\n"
                f"{seed_yaml}"
            )

            return Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text=result_text),),
                    is_error=False,
                    meta={
                        "seed_id": seed.metadata.seed_id,
                        "interview_id": seed.metadata.interview_id,
                        "ambiguity_score": seed.metadata.ambiguity_score,
                    },
                )
            )

        except Exception as e:
            log.error("mcp.tool.generate_seed.error", error=str(e))
            return Result.err(
                MCPToolError(
                    f"Seed generation failed: {e}",
                    tool_name="ouroboros_generate_seed",
                )
            )


@dataclass
class MeasureDriftHandler:
    """Handler for the measure_drift tool.

    Measures goal deviation from the original seed specification
    using DriftMeasurement with weighted components:
    goal (50%), constraint (30%), ontology (20%).
    """

    event_store: EventStore | None = field(default=None, repr=False)

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the tool definition."""
        return MCPToolDefinition(
            name="ouroboros_measure_drift",
            description=(
                "Measure drift from the original seed goal. "
                "Calculates goal deviation score using weighted components: "
                "goal drift (50%), constraint drift (30%), ontology drift (20%). "
                "Returns drift metrics, analysis, and suggestions if drift exceeds threshold."
            ),
            parameters=(
                MCPToolParameter(
                    name="session_id",
                    type=ToolInputType.STRING,
                    description="The execution session ID to measure drift for",
                    required=True,
                ),
                MCPToolParameter(
                    name="current_output",
                    type=ToolInputType.STRING,
                    description="Current execution output to measure drift against the seed goal",
                    required=True,
                ),
                MCPToolParameter(
                    name="seed_content",
                    type=ToolInputType.STRING,
                    description="Original seed YAML content for drift calculation",
                    required=True,
                ),
                MCPToolParameter(
                    name="constraint_violations",
                    type=ToolInputType.ARRAY,
                    description="Known constraint violations (e.g., ['Missing tests', 'Wrong language'])",
                    required=False,
                ),
                MCPToolParameter(
                    name="current_concepts",
                    type=ToolInputType.ARRAY,
                    description="Concepts present in the current output (for ontology drift)",
                    required=False,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Handle a drift measurement request.

        Args:
            arguments: Tool arguments including session_id, current_output, and seed_content.

        Returns:
            Result containing drift metrics or error.
        """
        session_id = arguments.get("session_id")
        if not session_id:
            return Result.err(
                MCPToolError(
                    "session_id is required",
                    tool_name="ouroboros_measure_drift",
                )
            )

        current_output = arguments.get("current_output")
        if not current_output:
            return Result.err(
                MCPToolError(
                    "current_output is required",
                    tool_name="ouroboros_measure_drift",
                )
            )

        seed_content = arguments.get("seed_content")
        if not seed_content:
            return Result.err(
                MCPToolError(
                    "seed_content is required",
                    tool_name="ouroboros_measure_drift",
                )
            )

        constraint_violations_raw = arguments.get("constraint_violations", [])
        current_concepts_raw = arguments.get("current_concepts", [])

        log.info(
            "mcp.tool.measure_drift",
            session_id=session_id,
            output_length=len(current_output),
            violations_count=len(constraint_violations_raw),
        )

        try:
            # Parse seed YAML
            seed_dict = yaml.safe_load(seed_content)
            seed = Seed.from_dict(seed_dict)
        except yaml.YAMLError as e:
            return Result.err(
                MCPToolError(
                    f"Failed to parse seed YAML: {e}",
                    tool_name="ouroboros_measure_drift",
                )
            )
        except (ValidationError, PydanticValidationError) as e:
            return Result.err(
                MCPToolError(
                    f"Seed validation failed: {e}",
                    tool_name="ouroboros_measure_drift",
                )
            )

        try:
            # Calculate drift using real DriftMeasurement
            measurement = DriftMeasurement()
            metrics = measurement.measure(
                current_output=current_output,
                constraint_violations=[str(v) for v in constraint_violations_raw],
                current_concepts=[str(c) for c in current_concepts_raw],
                seed=seed,
            )

            drift_text = (
                f"Drift Measurement Report\n"
                f"=======================\n"
                f"Session: {session_id}\n"
                f"Seed ID: {seed.metadata.seed_id}\n"
                f"Goal: {seed.goal}\n\n"
                f"Combined Drift: {metrics.combined_drift:.2f}\n"
                f"Acceptable Threshold: {DRIFT_THRESHOLD}\n"
                f"Status: {'ACCEPTABLE' if metrics.is_acceptable else 'EXCEEDED'}\n\n"
                f"Component Breakdown:\n"
                f"  Goal Drift: {metrics.goal_drift:.2f} (50% weight)\n"
                f"  Constraint Drift: {metrics.constraint_drift:.2f} (30% weight)\n"
                f"  Ontology Drift: {metrics.ontology_drift:.2f} (20% weight)\n"
            )

            suggestions: list[str] = []
            if not metrics.is_acceptable:
                suggestions.append("Drift exceeds threshold - consider consensus review")
                suggestions.append("Review execution path against original goal")
                if metrics.constraint_drift > 0:
                    suggestions.append(
                        f"Constraint violations detected: {constraint_violations_raw}"
                    )

            if suggestions:
                drift_text += "\nSuggestions:\n"
                for s in suggestions:
                    drift_text += f"  - {s}\n"

            return Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text=drift_text),),
                    is_error=False,
                    meta={
                        "session_id": session_id,
                        "seed_id": seed.metadata.seed_id,
                        "goal_drift": metrics.goal_drift,
                        "constraint_drift": metrics.constraint_drift,
                        "ontology_drift": metrics.ontology_drift,
                        "combined_drift": metrics.combined_drift,
                        "is_acceptable": metrics.is_acceptable,
                        "threshold": DRIFT_THRESHOLD,
                        "suggestions": suggestions,
                    },
                )
            )
        except Exception as e:
            log.error("mcp.tool.measure_drift.error", error=str(e))
            return Result.err(
                MCPToolError(
                    f"Failed to measure drift: {e}",
                    tool_name="ouroboros_measure_drift",
                )
            )


@dataclass
class InterviewHandler:
    """Handler for the ouroboros_interview tool.

    Manages interactive interviews for requirement clarification.
    Supports starting new interviews, resuming existing sessions,
    and recording responses to questions.
    """

    interview_engine: InterviewEngine | None = field(default=None, repr=False)
    event_store: EventStore | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """Initialize event store."""
        self._event_store = self.event_store or EventStore()
        self._initialized = False

    async def _ensure_initialized(self) -> None:
        """Ensure the event store is initialized."""
        if not self._initialized:
            await self._event_store.initialize()
            self._initialized = True

    async def _emit_event(self, event: Any) -> None:
        """Emit event to store. Swallows errors to not break interview flow."""
        try:
            await self._ensure_initialized()
            await self._event_store.append(event)
        except Exception as e:
            log.warning("mcp.tool.interview.event_emission_failed", error=str(e))

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the tool definition."""
        return MCPToolDefinition(
            name="ouroboros_interview",
            description=(
                "Interactive interview for requirement clarification. "
                "Start a new interview with initial_context, resume with session_id, "
                "or record an answer to the current question."
            ),
            parameters=(
                MCPToolParameter(
                    name="initial_context",
                    type=ToolInputType.STRING,
                    description="Initial context to start a new interview session",
                    required=False,
                ),
                MCPToolParameter(
                    name="session_id",
                    type=ToolInputType.STRING,
                    description="Session ID to resume an existing interview",
                    required=False,
                ),
                MCPToolParameter(
                    name="answer",
                    type=ToolInputType.STRING,
                    description="Response to the current interview question",
                    required=False,
                ),
                MCPToolParameter(
                    name="cwd",
                    type=ToolInputType.STRING,
                    description=(
                        "Working directory for brownfield auto-detection. "
                        "Defaults to the current working directory if not provided."
                    ),
                    required=False,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Handle an interview request.

        Args:
            arguments: Tool arguments including initial_context, session_id, or answer.

        Returns:
            Result containing interview question and session_id or error.
        """
        initial_context = arguments.get("initial_context")
        session_id = arguments.get("session_id")
        answer = arguments.get("answer")

        # Use injected or create interview engine
        engine = self.interview_engine or InterviewEngine(
            llm_adapter=ClaudeAgentAdapter(permission_mode="bypassPermissions"),
            state_dir=Path.home() / ".ouroboros" / "data",
        )

        _interview_id: str | None = None  # Track for error event emission

        try:
            # Start new interview
            if initial_context:
                cwd = arguments.get("cwd") or os.getcwd()
                result = await engine.start_interview(initial_context, cwd=cwd)
                if result.is_err:
                    return Result.err(
                        MCPToolError(
                            str(result.error),
                            tool_name="ouroboros_interview",
                        )
                    )

                state = result.value
                _interview_id = state.interview_id
                question_result = await engine.ask_next_question(state)
                if question_result.is_err:
                    error_msg = str(question_result.error)
                    from ouroboros.events.interview import interview_failed

                    await self._emit_event(
                        interview_failed(
                            state.interview_id,
                            error_msg,
                            phase="question_generation",
                        )
                    )
                    # Return recoverable result with session ID for retry
                    if "empty response" in error_msg.lower():
                        return Result.ok(
                            MCPToolResult(
                                content=(
                                    MCPContentItem(
                                        type=ContentType.TEXT,
                                        text=(
                                            f"Interview started but question generation failed after retries. "
                                            f"Session ID: {state.interview_id}\n\n"
                                            f'Resume with: session_id="{state.interview_id}"'
                                        ),
                                    ),
                                ),
                                is_error=True,
                                meta={"session_id": state.interview_id, "recoverable": True},
                            )
                        )
                    return Result.err(MCPToolError(error_msg, tool_name="ouroboros_interview"))

                question = question_result.value

                # Record the question as an unanswered round so resume can find it
                from ouroboros.bigbang.interview import InterviewRound

                state.rounds.append(
                    InterviewRound(
                        round_number=1,
                        question=question,
                        user_response=None,
                    )
                )
                state.mark_updated()

                # Persist state to disk so subsequent calls can resume
                save_result = await engine.save_state(state)
                if save_result.is_err:
                    log.warning(
                        "mcp.tool.interview.save_failed_on_start",
                        error=str(save_result.error),
                    )

                # Emit interview started event
                from ouroboros.events.interview import interview_started

                await self._emit_event(
                    interview_started(
                        state.interview_id,
                        initial_context,
                    )
                )

                log.info(
                    "mcp.tool.interview.started",
                    session_id=state.interview_id,
                )

                return Result.ok(
                    MCPToolResult(
                        content=(
                            MCPContentItem(
                                type=ContentType.TEXT,
                                text=f"Interview started. Session ID: {state.interview_id}\n\n{question}",
                            ),
                        ),
                        is_error=False,
                        meta={"session_id": state.interview_id},
                    )
                )

            # Resume existing interview
            if session_id:
                load_result = await engine.load_state(session_id)
                if load_result.is_err:
                    return Result.err(
                        MCPToolError(
                            str(load_result.error),
                            tool_name="ouroboros_interview",
                        )
                    )

                state = load_result.value
                _interview_id = session_id

                # If answer provided, record it first
                if answer:
                    if not state.rounds:
                        return Result.err(
                            MCPToolError(
                                "Cannot record answer - no questions have been asked yet",
                                tool_name="ouroboros_interview",
                            )
                        )

                    last_question = state.rounds[-1].question

                    # Pop the unanswered round so record_response can re-create it
                    # with the correct round_number (len(rounds) + 1)
                    if state.rounds[-1].user_response is None:
                        state.rounds.pop()

                    record_result = await engine.record_response(state, answer, last_question)
                    if record_result.is_err:
                        return Result.err(
                            MCPToolError(
                                str(record_result.error),
                                tool_name="ouroboros_interview",
                            )
                        )
                    state = record_result.value
                    state.clear_stored_ambiguity()

                    # Emit response recorded event
                    from ouroboros.events.interview import interview_response_recorded

                    await self._emit_event(
                        interview_response_recorded(
                            interview_id=session_id,
                            round_number=len(state.rounds),
                            question_preview=last_question,
                            response_preview=answer,
                        )
                    )

                    log.info(
                        "mcp.tool.interview.response_recorded",
                        session_id=session_id,
                    )

                # Generate next question (whether resuming or after recording answer)
                question_result = await engine.ask_next_question(state)
                if question_result.is_err:
                    error_msg = str(question_result.error)
                    from ouroboros.events.interview import interview_failed

                    await self._emit_event(
                        interview_failed(
                            session_id,
                            error_msg,
                            phase="question_generation",
                        )
                    )
                    if "empty response" in error_msg.lower():
                        return Result.ok(
                            MCPToolResult(
                                content=(
                                    MCPContentItem(
                                        type=ContentType.TEXT,
                                        text=(
                                            f"Question generation failed after retries. "
                                            f"Session ID: {session_id}\n\n"
                                            f'Resume with: session_id="{session_id}"'
                                        ),
                                    ),
                                ),
                                is_error=True,
                                meta={"session_id": session_id, "recoverable": True},
                            )
                        )
                    return Result.err(MCPToolError(error_msg, tool_name="ouroboros_interview"))

                question = question_result.value

                # Save pending question as unanswered round for next resume
                from ouroboros.bigbang.interview import InterviewRound

                state.rounds.append(
                    InterviewRound(
                        round_number=state.current_round_number,
                        question=question,
                        user_response=None,
                    )
                )
                state.mark_updated()

                save_result = await engine.save_state(state)
                if save_result.is_err:
                    log.warning(
                        "mcp.tool.interview.save_failed",
                        error=str(save_result.error),
                    )

                log.info(
                    "mcp.tool.interview.question_asked",
                    session_id=session_id,
                )

                return Result.ok(
                    MCPToolResult(
                        content=(
                            MCPContentItem(
                                type=ContentType.TEXT,
                                text=f"Session {session_id}\n\n{question}",
                            ),
                        ),
                        is_error=False,
                        meta={"session_id": session_id},
                    )
                )

            # No valid parameters provided
            return Result.err(
                MCPToolError(
                    "Must provide initial_context to start or session_id to resume",
                    tool_name="ouroboros_interview",
                )
            )

        except Exception as e:
            log.error("mcp.tool.interview.error", error=str(e))
            if _interview_id:
                from ouroboros.events.interview import interview_failed

                await self._emit_event(
                    interview_failed(
                        _interview_id,
                        str(e),
                        phase="unexpected_error",
                    )
                )
            return Result.err(
                MCPToolError(
                    f"Interview failed: {e}",
                    tool_name="ouroboros_interview",
                )
            )


@dataclass
class EvaluateHandler:
    """Handler for the ouroboros_evaluate tool.

    Evaluates an execution session using the three-stage evaluation pipeline:
    Stage 1: Mechanical Verification ($0)
    Stage 2: Semantic Evaluation (Standard tier)
    Stage 3: Multi-Model Consensus (Frontier tier, if triggered)
    """

    event_store: EventStore | None = field(default=None, repr=False)
    llm_adapter: ClaudeCodeAdapter | None = field(default=None, repr=False)

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the tool definition."""
        return MCPToolDefinition(
            name="ouroboros_evaluate",
            description=(
                "Evaluate an Ouroboros execution session using the three-stage evaluation pipeline. "
                "Stage 1 performs mechanical verification (lint, build, test). "
                "Stage 2 performs semantic evaluation of AC compliance and goal alignment. "
                "Stage 3 runs multi-model consensus if triggered by uncertainty or manual request."
            ),
            parameters=(
                MCPToolParameter(
                    name="session_id",
                    type=ToolInputType.STRING,
                    description="The execution session ID to evaluate",
                    required=True,
                ),
                MCPToolParameter(
                    name="artifact",
                    type=ToolInputType.STRING,
                    description="The execution output/artifact to evaluate",
                    required=True,
                ),
                MCPToolParameter(
                    name="seed_content",
                    type=ToolInputType.STRING,
                    description="Original seed YAML for goal/constraints extraction",
                    required=False,
                ),
                MCPToolParameter(
                    name="acceptance_criterion",
                    type=ToolInputType.STRING,
                    description="Specific acceptance criterion to evaluate against",
                    required=False,
                ),
                MCPToolParameter(
                    name="artifact_type",
                    type=ToolInputType.STRING,
                    description="Type of artifact: code, docs, config. Default: code",
                    required=False,
                    default="code",
                    enum=("code", "docs", "config"),
                ),
                MCPToolParameter(
                    name="trigger_consensus",
                    type=ToolInputType.BOOLEAN,
                    description="Force Stage 3 consensus evaluation. Default: False",
                    required=False,
                    default=False,
                ),
                MCPToolParameter(
                    name="working_dir",
                    type=ToolInputType.STRING,
                    description=(
                        "Project working directory for language auto-detection of Stage 1 "
                        "mechanical verification commands. Auto-detects language from marker "
                        "files (build.zig, Cargo.toml, go.mod, package.json, etc.). "
                        "Supports .ouroboros/mechanical.toml for custom overrides."
                    ),
                    required=False,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Handle an evaluation request.

        Args:
            arguments: Tool arguments including session_id, artifact, and optional seed_content.

        Returns:
            Result containing evaluation results or error.
        """
        from pathlib import Path

        from ouroboros.evaluation import (
            EvaluationContext,
            EvaluationPipeline,
            PipelineConfig,
            build_mechanical_config,
        )

        session_id = arguments.get("session_id")
        if not session_id:
            return Result.err(
                MCPToolError(
                    "session_id is required",
                    tool_name="ouroboros_evaluate",
                )
            )

        artifact = arguments.get("artifact")
        if not artifact:
            return Result.err(
                MCPToolError(
                    "artifact is required",
                    tool_name="ouroboros_evaluate",
                )
            )

        seed_content = arguments.get("seed_content")
        acceptance_criterion = arguments.get("acceptance_criterion")
        artifact_type = arguments.get("artifact_type", "code")
        trigger_consensus = arguments.get("trigger_consensus", False)

        log.info(
            "mcp.tool.evaluate",
            session_id=session_id,
            has_seed=seed_content is not None,
            trigger_consensus=trigger_consensus,
        )

        try:
            # Extract goal/constraints from seed if provided
            goal = ""
            constraints: tuple[str, ...] = ()
            seed_id = session_id  # fallback

            if seed_content:
                try:
                    seed_dict = yaml.safe_load(seed_content)
                    seed = Seed.from_dict(seed_dict)
                    goal = seed.goal
                    constraints = tuple(seed.constraints)
                    seed_id = seed.metadata.seed_id
                except (yaml.YAMLError, ValidationError, PydanticValidationError) as e:
                    log.warning("mcp.tool.evaluate.seed_parse_warning", error=str(e))
                    # Continue without seed data - not fatal

            # Try to enrich from session repository if event_store available
            if not goal:
                store = self.event_store or EventStore()
                try:
                    await store.initialize()
                    repo = SessionRepository(store)
                    session_result = await repo.reconstruct_session(session_id)
                    if session_result.is_ok:
                        tracker = session_result.value
                        seed_id = tracker.seed_id
                except Exception:
                    pass  # Best-effort enrichment

            # Use acceptance_criterion or derive from seed
            current_ac = acceptance_criterion or "Verify execution output meets requirements"

            context = EvaluationContext(
                execution_id=session_id,
                seed_id=seed_id,
                current_ac=current_ac,
                artifact=artifact,
                artifact_type=artifact_type,
                goal=goal,
                constraints=constraints,
            )

            # Use injected or create services
            llm_adapter = self.llm_adapter or ClaudeCodeAdapter(max_turns=1)
            working_dir_str = arguments.get("working_dir")
            working_dir = Path(working_dir_str).resolve() if working_dir_str else Path.cwd()
            mechanical_config = build_mechanical_config(working_dir)
            config = PipelineConfig(mechanical=mechanical_config)
            pipeline = EvaluationPipeline(llm_adapter, config)
            result = await pipeline.evaluate(context)

            if result.is_err:
                return Result.err(
                    MCPToolError(
                        f"Evaluation failed: {result.error}",
                        tool_name="ouroboros_evaluate",
                    )
                )

            eval_result = result.value

            # Detect code changes when Stage 1 fails (presentation concern)
            code_changes: bool | None = None
            if eval_result.stage1_result and not eval_result.stage1_result.passed:
                code_changes = await self._has_code_changes(working_dir)

            # Build result text
            result_text = self._format_evaluation_result(eval_result, code_changes=code_changes)

            # Build metadata
            meta = {
                "session_id": session_id,
                "final_approved": eval_result.final_approved,
                "highest_stage": eval_result.highest_stage_completed,
                "stage1_passed": eval_result.stage1_result.passed
                if eval_result.stage1_result
                else None,
                "stage2_ac_compliance": eval_result.stage2_result.ac_compliance
                if eval_result.stage2_result
                else None,
                "stage2_score": eval_result.stage2_result.score
                if eval_result.stage2_result
                else None,
                "stage3_approved": eval_result.stage3_result.approved
                if eval_result.stage3_result
                else None,
                "code_changes_detected": code_changes,
            }

            return Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text=result_text),),
                    is_error=False,
                    meta=meta,
                )
            )
        except Exception as e:
            log.error("mcp.tool.evaluate.error", error=str(e))
            return Result.err(
                MCPToolError(
                    f"Evaluation failed: {e}",
                    tool_name="ouroboros_evaluate",
                )
            )

    async def _has_code_changes(self, working_dir: Path) -> bool | None:
        """Detect whether the working tree has code changes.

        Runs ``git status --porcelain`` to check for modifications.

        Returns:
            True if changes detected, False if clean, None if not a git repo
            or git is unavailable.
        """
        from ouroboros.evaluation.mechanical import run_command

        try:
            cmd_result = await run_command(
                ("git", "status", "--porcelain"),
                timeout=10,
                working_dir=working_dir,
            )
            if cmd_result.return_code != 0:
                return None
            return bool(cmd_result.stdout.strip())
        except Exception:
            return None

    def _format_evaluation_result(self, result, *, code_changes: bool | None = None) -> str:
        """Format evaluation result as human-readable text.

        Args:
            result: EvaluationResult from pipeline.
            code_changes: Whether working tree has code changes (Stage 1 context).

        Returns:
            Formatted text representation.
        """
        lines = [
            "Evaluation Results",
            "=" * 60,
            f"Execution ID: {result.execution_id}",
            f"Final Approval: {'APPROVED' if result.final_approved else 'REJECTED'}",
            f"Highest Stage Completed: {result.highest_stage_completed}",
            "",
        ]

        # Stage 1 results
        if result.stage1_result:
            s1 = result.stage1_result
            lines.extend(
                [
                    "Stage 1: Mechanical Verification",
                    "-" * 40,
                    f"Status: {'PASSED' if s1.passed else 'FAILED'}",
                    f"Coverage: {s1.coverage_score:.1%}" if s1.coverage_score else "Coverage: N/A",
                ]
            )
            for check in s1.checks:
                status = "PASS" if check.passed else "FAIL"
                lines.append(f"  [{status}] {check.check_type}: {check.message}")
            lines.append("")

        # Stage 2 results
        if result.stage2_result:
            s2 = result.stage2_result
            lines.extend(
                [
                    "Stage 2: Semantic Evaluation",
                    "-" * 40,
                    f"Score: {s2.score:.2f}",
                    f"AC Compliance: {'YES' if s2.ac_compliance else 'NO'}",
                    f"Goal Alignment: {s2.goal_alignment:.2f}",
                    f"Drift Score: {s2.drift_score:.2f}",
                    f"Uncertainty: {s2.uncertainty:.2f}",
                    f"Reasoning: {s2.reasoning[:200]}..."
                    if len(s2.reasoning) > 200
                    else f"Reasoning: {s2.reasoning}",
                    "",
                ]
            )

        # Stage 3 results
        if result.stage3_result:
            s3 = result.stage3_result
            if s3.is_single_model:
                header = "Stage 3: Multi-Perspective Consensus (single-model)"
            else:
                header = "Stage 3: Multi-Model Consensus"
            lines.extend(
                [
                    header,
                    "-" * 40,
                    f"Status: {'APPROVED' if s3.approved else 'REJECTED'}",
                    f"Majority Ratio: {s3.majority_ratio:.1%}",
                    f"Total Votes: {s3.total_votes}",
                    f"Approving: {s3.approving_votes}",
                ]
            )
            for vote in s3.votes:
                decision = "APPROVE" if vote.approved else "REJECT"
                role_suffix = f" [{vote.role}]" if vote.role else ""
                lines.append(
                    f"  [{decision}] {vote.model}{role_suffix} (confidence: {vote.confidence:.2f})"
                )
            if s3.disagreements:
                lines.append("Disagreements:")
                for d in s3.disagreements:
                    lines.append(f"  - {d[:100]}...")
            if s3.is_single_model:
                lines.append(
                    "Note: Same model with different evaluation perspectives. "
                    "Configure OPENROUTER_API_KEY for true multi-model consensus."
                )
            lines.append("")

        # Failure reason
        if not result.final_approved:
            lines.extend(
                [
                    "Failure Reason",
                    "-" * 40,
                    result.failure_reason or "Unknown",
                ]
            )
            # Contextual annotation for Stage 1 failures
            stage1_failed = result.stage1_result and not result.stage1_result.passed
            if stage1_failed and code_changes is True:
                lines.extend(
                    [
                        "",
                        "⚠ Code changes detected — these are real build/test failures "
                        "that need to be fixed before re-evaluating.",
                    ]
                )
            elif stage1_failed and code_changes is False:
                lines.extend(
                    [
                        "",
                        "ℹ No code changes detected in the working tree. These failures "
                        "are expected if you haven't run `ooo run` yet to produce code.",
                    ]
                )

        return "\n".join(lines)


@dataclass
class LateralThinkHandler:
    """Handler for the lateral_think tool.

    Generates alternative thinking approaches using lateral thinking personas
    to break through stagnation in problem-solving.
    """

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the tool definition."""
        return MCPToolDefinition(
            name="ouroboros_lateral_think",
            description=(
                "Generate alternative thinking approaches using lateral thinking personas. "
                "Use this tool when stuck on a problem to get fresh perspectives from "
                "different thinking modes: hacker (unconventional workarounds), "
                "researcher (seeks information), simplifier (reduces complexity), "
                "architect (restructures approach), or contrarian (challenges assumptions)."
            ),
            parameters=(
                MCPToolParameter(
                    name="problem_context",
                    type=ToolInputType.STRING,
                    description="Description of the stuck situation or problem",
                    required=True,
                ),
                MCPToolParameter(
                    name="current_approach",
                    type=ToolInputType.STRING,
                    description="What has been tried so far that isn't working",
                    required=True,
                ),
                MCPToolParameter(
                    name="persona",
                    type=ToolInputType.STRING,
                    description="Specific persona to use: hacker, researcher, simplifier, architect, or contrarian",
                    required=False,
                    enum=("hacker", "researcher", "simplifier", "architect", "contrarian"),
                ),
                MCPToolParameter(
                    name="failed_attempts",
                    type=ToolInputType.ARRAY,
                    description="Previous failed approaches to avoid repeating",
                    required=False,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Handle a lateral thinking request.

        Args:
            arguments: Tool arguments including problem_context and current_approach.

        Returns:
            Result containing lateral thinking prompt and questions or error.
        """
        from ouroboros.resilience.lateral import LateralThinker, ThinkingPersona

        problem_context = arguments.get("problem_context")
        if not problem_context:
            return Result.err(
                MCPToolError(
                    "problem_context is required",
                    tool_name="ouroboros_lateral_think",
                )
            )

        current_approach = arguments.get("current_approach")
        if not current_approach:
            return Result.err(
                MCPToolError(
                    "current_approach is required",
                    tool_name="ouroboros_lateral_think",
                )
            )

        persona_str = arguments.get("persona", "contrarian")
        failed_attempts_raw = arguments.get("failed_attempts", [])

        # Convert string to ThinkingPersona enum
        try:
            persona = ThinkingPersona(persona_str)
        except ValueError:
            return Result.err(
                MCPToolError(
                    f"Invalid persona: {persona_str}. Must be one of: "
                    f"hacker, researcher, simplifier, architect, contrarian",
                    tool_name="ouroboros_lateral_think",
                )
            )

        # Convert failed_attempts to tuple of strings
        failed_attempts = tuple(str(a) for a in failed_attempts_raw if a)

        log.info(
            "mcp.tool.lateral_think",
            persona=persona.value,
            context_length=len(problem_context),
            failed_count=len(failed_attempts),
        )

        try:
            thinker = LateralThinker()
            result = thinker.generate_alternative(
                persona=persona,
                problem_context=problem_context,
                current_approach=current_approach,
                failed_attempts=failed_attempts,
            )

            if result.is_err:
                return Result.err(
                    MCPToolError(
                        result.error,
                        tool_name="ouroboros_lateral_think",
                    )
                )

            lateral_result = result.unwrap()

            # Build the response
            response_text = (
                f"# Lateral Thinking: {lateral_result.approach_summary}\n\n"
                f"{lateral_result.prompt}\n\n"
                "## Questions to Consider\n"
            )
            for question in lateral_result.questions:
                response_text += f"- {question}\n"

            return Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text=response_text),),
                    is_error=False,
                    meta={
                        "persona": lateral_result.persona.value,
                        "approach_summary": lateral_result.approach_summary,
                        "questions_count": len(lateral_result.questions),
                    },
                )
            )
        except Exception as e:
            log.error("mcp.tool.lateral_think.error", error=str(e))
            return Result.err(
                MCPToolError(
                    f"Lateral thinking failed: {e}",
                    tool_name="ouroboros_lateral_think",
                )
            )


@dataclass
class EvolveStepHandler:
    """Handler for the ouroboros_evolve_step tool.

    Runs exactly ONE generation of the evolutionary loop.
    Designed for Ralph integration: stateless between calls,
    all state reconstructed from events.
    """

    evolutionary_loop: Any | None = field(default=None, repr=False)

    TIMEOUT_SECONDS: int = int(
        os.environ.get("OUROBOROS_GENERATION_TIMEOUT", "7200")
    )  # Override MCP adapter's default 30s

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the tool definition."""
        return MCPToolDefinition(
            name="ouroboros_evolve_step",
            description=(
                "Run exactly ONE generation of the evolutionary loop. "
                "For Gen 1: provide lineage_id and seed_content (YAML). "
                "For Gen 2+: provide lineage_id only (state reconstructed from events). "
                "Returns generation result, convergence signal, and next action "
                "(continue/converged/stagnated/exhausted/failed)."
            ),
            parameters=(
                MCPToolParameter(
                    name="lineage_id",
                    type=ToolInputType.STRING,
                    description="Lineage ID to continue or new ID for Gen 1",
                    required=True,
                ),
                MCPToolParameter(
                    name="seed_content",
                    type=ToolInputType.STRING,
                    description=(
                        "Seed YAML content for Gen 1. "
                        "Omit for Gen 2+ (seed reconstructed from events)."
                    ),
                    required=False,
                ),
                MCPToolParameter(
                    name="execute",
                    type=ToolInputType.BOOLEAN,
                    description=(
                        "Whether to run seed execution and evaluation. "
                        "True (default): full pipeline with Execute→Validate→Evaluate. "
                        "False: ontology-only evolution (fast, no execution)."
                    ),
                    required=False,
                    default=True,
                ),
                MCPToolParameter(
                    name="parallel",
                    type=ToolInputType.BOOLEAN,
                    description=(
                        "Whether to run ACs in parallel. "
                        "True (default): parallel execution (fast, may cause import conflicts). "
                        "False: sequential execution (slower, more stable code generation)."
                    ),
                    required=False,
                    default=True,
                ),
                MCPToolParameter(
                    name="skip_qa",
                    type=ToolInputType.BOOLEAN,
                    description="Skip post-execution QA evaluation. Default: false",
                    required=False,
                    default=False,
                ),
                MCPToolParameter(
                    name="project_dir",
                    type=ToolInputType.STRING,
                    description=(
                        "Project root directory for validation (pytest collection check). "
                        "If omitted, auto-detected from execution output or CWD."
                    ),
                    required=False,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Handle an evolve_step request."""
        lineage_id = arguments.get("lineage_id")
        if not lineage_id:
            return Result.err(
                MCPToolError(
                    "lineage_id is required",
                    tool_name="ouroboros_evolve_step",
                )
            )

        if self.evolutionary_loop is None:
            return Result.err(
                MCPToolError(
                    "EvolutionaryLoop not configured",
                    tool_name="ouroboros_evolve_step",
                )
            )

        # Parse seed if provided (Gen 1)
        initial_seed = None
        seed_content = arguments.get("seed_content")
        if seed_content:
            try:
                seed_dict = yaml.safe_load(seed_content)
                initial_seed = Seed.from_dict(seed_dict)
            except Exception as e:
                return Result.err(
                    MCPToolError(
                        f"Failed to parse seed_content: {e}",
                        tool_name="ouroboros_evolve_step",
                    )
                )

        execute = arguments.get("execute", True)
        parallel = arguments.get("parallel", True)
        project_dir = arguments.get("project_dir")
        normalized_project_dir = (
            project_dir if isinstance(project_dir, str) and project_dir else None
        )

        project_dir_token = self.evolutionary_loop.set_project_dir(normalized_project_dir)

        try:
            # Ensure event store is initialized before evolve_step accesses it
            # (evolve_step calls replay_lineage/append before executor/evaluator)
            await self.evolutionary_loop.event_store.initialize()
            result = await self.evolutionary_loop.evolve_step(
                lineage_id, initial_seed, execute=execute, parallel=parallel
            )
        except Exception as e:
            log.error("mcp.tool.evolve_step.error", error=str(e))
            return Result.err(
                MCPToolError(
                    f"evolve_step failed: {e}",
                    tool_name="ouroboros_evolve_step",
                )
            )
        finally:
            self.evolutionary_loop.reset_project_dir(project_dir_token)

        if result.is_err:
            return Result.err(
                MCPToolError(
                    str(result.error),
                    tool_name="ouroboros_evolve_step",
                )
            )

        step = result.value
        gen = step.generation_result
        sig = step.convergence_signal

        # Format output
        text_lines = [
            f"## Generation {gen.generation_number}",
            "",
            f"**Action**: {step.action.value}",
            f"**Phase**: {gen.phase.value}",
            f"**Convergence similarity**: {sig.ontology_similarity:.2%}",
            f"**Reason**: {sig.reason}",
            *(
                [f"**Failed ACs**: {', '.join(str(i + 1) for i in sig.failed_acs)}"]
                if sig.failed_acs
                else []
            ),
            f"**Lineage**: {step.lineage.lineage_id} ({step.lineage.current_generation} generations)",
            f"**Next generation**: {step.next_generation}",
        ]

        if gen.execution_output:
            text_lines.append("")
            text_lines.append("### Execution output")
            output_preview = truncate_head_tail(gen.execution_output)
            text_lines.append(output_preview)

        if gen.evaluation_summary:
            text_lines.append("")
            text_lines.append("### Evaluation")
            es = gen.evaluation_summary
            text_lines.append(f"- **Approved**: {es.final_approved}")
            text_lines.append(f"- **Score**: {es.score}")
            text_lines.append(f"- **Drift**: {es.drift_score}")
            if es.failure_reason:
                text_lines.append(f"- **Failure**: {es.failure_reason}")
            if es.ac_results:
                text_lines.append("")
                text_lines.append("#### Per-AC Results")
                for ac in es.ac_results:
                    status = "PASS" if ac.passed else "FAIL"
                    text_lines.append(f"- AC {ac.ac_index + 1}: [{status}] {ac.ac_content[:80]}")

        if gen.wonder_output:
            text_lines.append("")
            text_lines.append("### Wonder questions")
            for q in gen.wonder_output.questions:
                text_lines.append(f"- {q}")

        if gen.validation_output:
            text_lines.append("")
            text_lines.append("### Validation")
            text_lines.append(gen.validation_output)

        if gen.ontology_delta:
            text_lines.append("")
            text_lines.append(
                f"### Ontology delta (similarity: {gen.ontology_delta.similarity:.2%})"
            )
            for af in gen.ontology_delta.added_fields:
                text_lines.append(f"- **Added**: {af.name} ({af.field_type})")
            for rf in gen.ontology_delta.removed_fields:
                text_lines.append(f"- **Removed**: {rf}")
            for mf in gen.ontology_delta.modified_fields:
                text_lines.append(f"- **Modified**: {mf.field_name}: {mf.old_type} → {mf.new_type}")

        # Post-execution QA
        qa_meta = None
        skip_qa = arguments.get("skip_qa", False)
        if step.action.value in ("continue", "converged") and execute and not skip_qa:
            from ouroboros.mcp.tools.qa import QAHandler

            qa_handler = QAHandler()
            quality_bar = "Generation must improve upon previous generation."
            if initial_seed:
                ac_lines = [f"- {ac}" for ac in initial_seed.acceptance_criteria]
                quality_bar = "The execution must satisfy all acceptance criteria:\n" + "\n".join(
                    ac_lines
                )

            artifact = gen.execution_output or "\n".join(text_lines)
            qa_result = await qa_handler.handle(
                {
                    "artifact": artifact,
                    "artifact_type": "test_output",
                    "quality_bar": quality_bar,
                    "seed_content": seed_content or "",
                    "pass_threshold": 0.80,
                }
            )
            if qa_result.is_ok:
                text_lines.append("")
                text_lines.append("### QA Verdict")
                text_lines.append(qa_result.value.content[0].text)
                qa_meta = qa_result.value.meta

        meta = {
            "lineage_id": step.lineage.lineage_id,
            "generation": gen.generation_number,
            "action": step.action.value,
            "similarity": sig.ontology_similarity,
            "converged": sig.converged,
            "next_generation": step.next_generation,
            "executed": execute,
            "has_execution_output": gen.execution_output is not None,
        }
        if qa_meta:
            meta["qa"] = qa_meta

        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text="\n".join(text_lines)),),
                is_error=False,
                meta=meta,
            )
        )


@dataclass
class EvolveRewindHandler:
    """Handler for the ouroboros_evolve_rewind tool.

    Rewinds an evolutionary lineage to a specific generation.
    Delegates to EvolutionaryLoop.rewind_to().
    """

    evolutionary_loop: Any | None = field(default=None, repr=False)

    TIMEOUT_SECONDS: int = 60

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the tool definition."""
        return MCPToolDefinition(
            name="ouroboros_evolve_rewind",
            description=(
                "Rewind an evolutionary lineage to a specific generation. "
                "Truncates all generations after the target and emits a "
                "lineage.rewound event. The lineage can then continue evolving "
                "from the rewind point."
            ),
            parameters=(
                MCPToolParameter(
                    name="lineage_id",
                    type=ToolInputType.STRING,
                    description="ID of the lineage to rewind",
                    required=True,
                ),
                MCPToolParameter(
                    name="to_generation",
                    type=ToolInputType.INTEGER,
                    description="Generation number to rewind to (inclusive)",
                    required=True,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Handle a rewind request."""
        lineage_id = arguments.get("lineage_id")
        if not lineage_id:
            return Result.err(
                MCPToolError(
                    "lineage_id is required",
                    tool_name="ouroboros_evolve_rewind",
                )
            )

        to_generation = arguments.get("to_generation")
        if to_generation is None:
            return Result.err(
                MCPToolError(
                    "to_generation is required",
                    tool_name="ouroboros_evolve_rewind",
                )
            )

        if self.evolutionary_loop is None:
            return Result.err(
                MCPToolError(
                    "EvolutionaryLoop not configured",
                    tool_name="ouroboros_evolve_rewind",
                )
            )

        try:
            await self.evolutionary_loop.event_store.initialize()
            events = await self.evolutionary_loop.event_store.replay_lineage(lineage_id)
        except Exception as e:
            return Result.err(
                MCPToolError(
                    f"Failed to replay lineage: {e}",
                    tool_name="ouroboros_evolve_rewind",
                )
            )

        if not events:
            return Result.err(
                MCPToolError(
                    f"No lineage found with ID: {lineage_id}",
                    tool_name="ouroboros_evolve_rewind",
                )
            )

        from ouroboros.evolution.projector import LineageProjector

        projector = LineageProjector()
        lineage = projector.project(events)

        if lineage is None:
            return Result.err(
                MCPToolError(
                    f"Failed to project lineage: {lineage_id}",
                    tool_name="ouroboros_evolve_rewind",
                )
            )

        # Validate generation is in range
        if to_generation < 1 or to_generation > lineage.current_generation:
            return Result.err(
                MCPToolError(
                    f"Generation {to_generation} out of range [1, {lineage.current_generation}]",
                    tool_name="ouroboros_evolve_rewind",
                )
            )

        if to_generation == lineage.current_generation:
            return Result.err(
                MCPToolError(
                    f"Already at generation {to_generation}, nothing to rewind",
                    tool_name="ouroboros_evolve_rewind",
                )
            )

        from_gen = lineage.current_generation
        result = await self.evolutionary_loop.rewind_to(lineage, to_generation)

        if result.is_err:
            return Result.err(
                MCPToolError(
                    str(result.error),
                    tool_name="ouroboros_evolve_rewind",
                )
            )

        rewound_lineage = result.value

        # Get seed_json from the target generation if available
        target_gen = None
        for g in rewound_lineage.generations:
            if g.generation_number == to_generation:
                target_gen = g
                break

        seed_info = ""
        if target_gen and target_gen.seed_json:
            seed_info = f"\n\n### Target generation seed\n```yaml\n{target_gen.seed_json}\n```"

        text = (
            f"## Rewind Complete\n\n"
            f"**Lineage**: {lineage_id}\n"
            f"**From generation**: {from_gen}\n"
            f"**To generation**: {to_generation}\n"
            f"**Status**: {rewound_lineage.status.value}\n"
            f"**Git tag**: `ooo/{lineage_id}/gen_{to_generation}`\n\n"
            f"Generations {to_generation + 1}–{from_gen} have been truncated.\n"
            f"Run `ralph.sh --lineage-id {lineage_id}` to resume evolution."
            f"{seed_info}"
        )

        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text=text),),
                is_error=False,
                meta={
                    "lineage_id": lineage_id,
                    "from_generation": from_gen,
                    "to_generation": to_generation,
                },
            )
        )


@dataclass
class LineageStatusHandler:
    """Handler for the ouroboros_lineage_status tool.

    Queries the current state of an evolutionary lineage
    without running a generation.
    """

    event_store: EventStore | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """Initialize event store."""
        self._event_store = self.event_store or EventStore()
        self._initialized = False

    async def _ensure_initialized(self) -> None:
        """Ensure the event store is initialized."""
        if not self._initialized:
            await self._event_store.initialize()
            self._initialized = True

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the tool definition."""
        return MCPToolDefinition(
            name="ouroboros_lineage_status",
            description=(
                "Query the current state of an evolutionary lineage. "
                "Returns generation count, status, ontology evolution, "
                "and convergence progress."
            ),
            parameters=(
                MCPToolParameter(
                    name="lineage_id",
                    type=ToolInputType.STRING,
                    description="ID of the lineage to query",
                    required=True,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Handle a lineage status request."""
        lineage_id = arguments.get("lineage_id")
        if not lineage_id:
            return Result.err(
                MCPToolError(
                    "lineage_id is required",
                    tool_name="ouroboros_lineage_status",
                )
            )

        await self._ensure_initialized()

        try:
            events = await self._event_store.replay_lineage(lineage_id)
        except Exception as e:
            return Result.err(
                MCPToolError(
                    f"Failed to query events: {e}",
                    tool_name="ouroboros_lineage_status",
                )
            )

        if not events:
            return Result.err(
                MCPToolError(
                    f"No lineage found with ID: {lineage_id}",
                    tool_name="ouroboros_lineage_status",
                )
            )

        from ouroboros.evolution.projector import LineageProjector

        projector = LineageProjector()
        lineage = projector.project(events)

        if lineage is None:
            return Result.err(
                MCPToolError(
                    f"Failed to project lineage from events: {lineage_id}",
                    tool_name="ouroboros_lineage_status",
                )
            )

        text_lines = [
            f"## Lineage: {lineage.lineage_id}",
            "",
            f"**Status**: {lineage.status.value}",
            f"**Goal**: {lineage.goal}",
            f"**Generations**: {lineage.current_generation}",
            f"**Created**: {lineage.created_at.isoformat()}",
        ]

        # Ontology summary
        if lineage.current_ontology:
            text_lines.append("")
            text_lines.append(f"### Current Ontology: {lineage.current_ontology.name}")
            for f in lineage.current_ontology.fields:
                required = " (required)" if f.required else ""
                text_lines.append(f"- **{f.name}**: {f.field_type}{required}")

        # Generation history
        if lineage.generations:
            text_lines.append("")
            text_lines.append("### Generation History")
            for gen in lineage.generations:
                status = (
                    "passed"
                    if gen.evaluation_summary and gen.evaluation_summary.final_approved
                    else "pending"
                )
                error_part = ""
                if gen.failure_error:
                    error_part = f" | {gen.failure_error[:60]}"
                text_lines.append(
                    f"- Gen {gen.generation_number}: {gen.phase.value} | {status}{error_part}"
                )

        # Rewind history
        if lineage.rewind_history:
            text_lines.append("")
            text_lines.append("### Rewind History")
            for rr in lineage.rewind_history:
                ts = rr.rewound_at
                time_str = (
                    ts.strftime("%Y-%m-%d %H:%M") if hasattr(ts, "strftime") else str(ts)[:16]
                )
                text_lines.append(
                    f"- \u21a9 Rewound Gen {rr.from_generation} \u2192 "
                    f"Gen {rr.to_generation} ({time_str})"
                )
                for dg in rr.discarded_generations:
                    score_part = ""
                    if dg.evaluation_summary and dg.evaluation_summary.score is not None:
                        score_part = f" | score={dg.evaluation_summary.score:.2f}"
                    error_part = ""
                    if dg.failure_error:
                        error_part = f" | {dg.failure_error[:60]}"
                    text_lines.append(
                        f"  - Gen {dg.generation_number}: {dg.phase.value}{score_part}{error_part}"
                    )

        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text="\n".join(text_lines)),),
                is_error=False,
                meta={
                    "lineage_id": lineage.lineage_id,
                    "status": lineage.status.value,
                    "generations": lineage.current_generation,
                    "goal": lineage.goal,
                },
            )
        )


@dataclass
class ACDashboardHandler:
    """Handler for the ouroboros_ac_dashboard tool.

    Displays per-AC pass/fail visibility across generations
    with three display modes: summary, full, ac.
    """

    event_store: EventStore | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """Initialize event store."""
        self._event_store = self.event_store or EventStore()
        self._initialized = False

    async def _ensure_initialized(self) -> None:
        """Ensure the event store is initialized."""
        if not self._initialized:
            await self._event_store.initialize()
            self._initialized = True

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the tool definition."""
        return MCPToolDefinition(
            name="ouroboros_ac_dashboard",
            description=(
                "Display per-AC pass/fail compliance dashboard across generations. "
                "Shows which acceptance criteria passed, failed, or are flaky. "
                "Modes: 'summary' (default), 'full' (AC x Gen matrix), 'ac' (single AC history)."
            ),
            parameters=(
                MCPToolParameter(
                    name="lineage_id",
                    type=ToolInputType.STRING,
                    description="ID of the lineage to display",
                    required=True,
                ),
                MCPToolParameter(
                    name="mode",
                    type=ToolInputType.STRING,
                    description="Display mode: 'summary' (default), 'full', or 'ac'",
                    required=False,
                ),
                MCPToolParameter(
                    name="ac_index",
                    type=ToolInputType.INTEGER,
                    description="AC index (1-based) for 'ac' mode. Required when mode='ac'.",
                    required=False,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Handle a dashboard request."""
        lineage_id = arguments.get("lineage_id")
        if not lineage_id:
            return Result.err(
                MCPToolError(
                    "lineage_id is required",
                    tool_name="ouroboros_ac_dashboard",
                )
            )

        mode = arguments.get("mode", "summary")
        ac_index = arguments.get("ac_index")

        await self._ensure_initialized()

        try:
            events = await self._event_store.replay_lineage(lineage_id)
        except Exception as e:
            return Result.err(
                MCPToolError(
                    f"Failed to query events: {e}",
                    tool_name="ouroboros_ac_dashboard",
                )
            )

        if not events:
            return Result.err(
                MCPToolError(
                    f"No lineage found with ID: {lineage_id}",
                    tool_name="ouroboros_ac_dashboard",
                )
            )

        from ouroboros.evolution.projector import LineageProjector
        from ouroboros.mcp.tools.dashboard import (
            format_full,
            format_single_ac,
            format_summary,
        )

        projector = LineageProjector()
        lineage = projector.project(events)

        if lineage is None:
            return Result.err(
                MCPToolError(
                    f"Failed to project lineage: {lineage_id}",
                    tool_name="ouroboros_ac_dashboard",
                )
            )

        if mode == "full":
            text = format_full(lineage)
        elif mode == "ac":
            if ac_index is None:
                return Result.err(
                    MCPToolError(
                        "ac_index is required for mode='ac'",
                        tool_name="ouroboros_ac_dashboard",
                    )
                )
            text = format_single_ac(lineage, int(ac_index) - 1)  # Convert to 0-based
        else:
            text = format_summary(lineage)

        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text=text),),
                is_error=False,
                meta={
                    "lineage_id": lineage.lineage_id,
                    "mode": mode,
                    "generations": lineage.current_generation,
                },
            )
        )


@dataclass
class CancelExecutionHandler:
    """Handler for the cancel_execution tool.

    Cancels a running or paused Ouroboros execution session.
    Validates that the execution exists and is not already in a terminal state
    (completed, failed, or cancelled) before performing cancellation.
    """

    event_store: EventStore | None = field(default=None, repr=False)

    # Terminal statuses that cannot be cancelled
    TERMINAL_STATUSES: tuple[SessionStatus, ...] = (
        SessionStatus.COMPLETED,
        SessionStatus.FAILED,
        SessionStatus.CANCELLED,
    )

    def __post_init__(self) -> None:
        """Initialize the session repository after dataclass creation."""
        self._event_store = self.event_store or EventStore()
        self._session_repo = SessionRepository(self._event_store)
        self._initialized = False

    async def _ensure_initialized(self) -> None:
        """Ensure the event store is initialized."""
        if not self._initialized:
            await self._event_store.initialize()
            self._initialized = True

    async def _resolve_session_id(self, execution_id: str) -> str | None:
        """Resolve an execution_id to its session_id via event store lookup."""
        events = await self._event_store.get_all_sessions()
        for event in events:
            if event.data.get("execution_id") == execution_id:
                return event.aggregate_id
        return None

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the tool definition."""
        return MCPToolDefinition(
            name="ouroboros_cancel_execution",
            description=(
                "Cancel a running or paused Ouroboros execution. "
                "Validates that the execution exists and is not already in a "
                "terminal state (completed, failed, cancelled) before cancelling."
            ),
            parameters=(
                MCPToolParameter(
                    name="execution_id",
                    type=ToolInputType.STRING,
                    description="The execution/session ID to cancel",
                    required=True,
                ),
                MCPToolParameter(
                    name="reason",
                    type=ToolInputType.STRING,
                    description="Reason for cancellation",
                    required=False,
                    default="Cancelled by user",
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Handle a cancel execution request.

        Validates the execution exists and is not in a terminal state,
        then marks it as cancelled.

        Args:
            arguments: Tool arguments including execution_id and optional reason.

        Returns:
            Result containing cancellation confirmation or error.
        """
        execution_id = arguments.get("execution_id")
        if not execution_id:
            return Result.err(
                MCPToolError(
                    "execution_id is required",
                    tool_name="ouroboros_cancel_execution",
                )
            )

        reason = arguments.get("reason", "Cancelled by user")

        log.info(
            "mcp.tool.cancel_execution",
            execution_id=execution_id,
            reason=reason,
        )

        try:
            await self._ensure_initialized()

            # Try direct lookup first (user may have passed session_id)
            result = await self._session_repo.reconstruct_session(execution_id)

            if result.is_err:
                # Try resolving as execution_id
                session_id = await self._resolve_session_id(execution_id)
                if session_id is None:
                    return Result.err(
                        MCPToolError(
                            f"Execution not found: {execution_id}",
                            tool_name="ouroboros_cancel_execution",
                        )
                    )
                result = await self._session_repo.reconstruct_session(session_id)
                if result.is_err:
                    return Result.err(
                        MCPToolError(
                            f"Execution not found: {result.error.message}",
                            tool_name="ouroboros_cancel_execution",
                        )
                    )

            tracker = result.value

            # Check if already in a terminal state
            if tracker.status in self.TERMINAL_STATUSES:
                return Result.err(
                    MCPToolError(
                        f"Execution {execution_id} is already in terminal state: "
                        f"{tracker.status.value}. Cannot cancel.",
                        tool_name="ouroboros_cancel_execution",
                    )
                )

            # Perform cancellation
            cancel_result = await self._session_repo.mark_cancelled(
                session_id=tracker.session_id,
                reason=reason,
                cancelled_by="mcp_tool",
            )

            if cancel_result.is_err:
                cancel_error = cancel_result.error
                return Result.err(
                    MCPToolError(
                        f"Failed to cancel execution: {cancel_error.message}",
                        tool_name="ouroboros_cancel_execution",
                    )
                )

            status_text = (
                f"Execution {execution_id} has been cancelled.\n"
                f"Previous status: {tracker.status.value}\n"
                f"Reason: {reason}\n"
            )

            return Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text=status_text),),
                    is_error=False,
                    meta={
                        "execution_id": execution_id,
                        "previous_status": tracker.status.value,
                        "new_status": SessionStatus.CANCELLED.value,
                        "reason": reason,
                        "cancelled_by": "mcp_tool",
                    },
                )
            )
        except Exception as e:
            log.error(
                "mcp.tool.cancel_execution.error",
                execution_id=execution_id,
                error=str(e),
            )
            return Result.err(
                MCPToolError(
                    f"Failed to cancel execution: {e}",
                    tool_name="ouroboros_cancel_execution",
                )
            )


_render_cache: dict[tuple[str, int], str] = {}
_RENDER_CACHE_MAX = 64


async def _render_job_snapshot(snapshot: JobSnapshot, event_store: EventStore) -> str:
    """Format a user-facing job summary with linked execution context.

    Results are cached by (job_id, cursor) to avoid redundant EventStore queries
    when the same snapshot is rendered repeatedly (e.g. poll loops).
    Terminal snapshots are never cached since they won't change.
    """
    cache_key = (snapshot.job_id, snapshot.cursor)
    if not snapshot.is_terminal and cache_key in _render_cache:
        return _render_cache[cache_key]

    text = await _render_job_snapshot_inner(snapshot, event_store)

    if not snapshot.is_terminal:
        if len(_render_cache) >= _RENDER_CACHE_MAX:
            # Evict oldest entries
            to_remove = list(_render_cache.keys())[: _RENDER_CACHE_MAX // 2]
            for key in to_remove:
                _render_cache.pop(key, None)
        _render_cache[cache_key] = text

    return text


async def _render_job_snapshot_inner(snapshot: JobSnapshot, event_store: EventStore) -> str:
    """Inner render without caching."""
    lines = [
        f"## Job: {snapshot.job_id}",
        "",
        f"**Type**: {snapshot.job_type}",
        f"**Status**: {snapshot.status.value}",
        f"**Message**: {snapshot.message}",
        f"**Created**: {snapshot.created_at.isoformat()}",
        f"**Updated**: {snapshot.updated_at.isoformat()}",
        f"**Cursor**: {snapshot.cursor}",
    ]

    if snapshot.links.execution_id:
        events = await event_store.query_events(
            aggregate_id=snapshot.links.execution_id,
            limit=25,
        )
        workflow_event = next((e for e in events if e.type == "workflow.progress.updated"), None)
        if workflow_event is not None:
            data = workflow_event.data
            lines.extend(
                [
                    "",
                    "### Execution",
                    f"**Execution ID**: {snapshot.links.execution_id}",
                    f"**Phase**: {data.get('current_phase') or 'Working'}",
                    f"**Activity**: {data.get('activity_detail') or data.get('activity') or 'running'}",
                    f"**AC Progress**: {data.get('completed_count', 0)}/{data.get('total_count', '?')}",
                ]
            )

        subtasks: dict[str, tuple[str, str]] = {}
        for event in events:
            if event.type != "execution.subtask.updated":
                continue
            sub_task_id = event.data.get("sub_task_id")
            if sub_task_id and sub_task_id not in subtasks:
                subtasks[sub_task_id] = (
                    event.data.get("content", ""),
                    event.data.get("status", "unknown"),
                )

        if subtasks:
            lines.append("")
            lines.append("### Recent Subtasks")
            for sub_task_id, (content, status) in list(subtasks.items())[:3]:
                lines.append(f"- `{sub_task_id}`: {status} -- {content}")

    elif snapshot.links.session_id:
        repo = SessionRepository(event_store)
        session_result = await repo.reconstruct_session(snapshot.links.session_id)
        if session_result.is_ok:
            tracker = session_result.value
            lines.extend(
                [
                    "",
                    "### Session",
                    f"**Session ID**: {tracker.session_id}",
                    f"**Session Status**: {tracker.status.value}",
                    f"**Messages Processed**: {tracker.messages_processed}",
                ]
            )

    if snapshot.links.lineage_id:
        events = await event_store.query_events(
            aggregate_id=snapshot.links.lineage_id,
            limit=10,
        )
        latest = next((e for e in events if e.type.startswith("lineage.")), None)
        if latest is not None:
            lines.extend(
                [
                    "",
                    "### Lineage",
                    f"**Lineage ID**: {snapshot.links.lineage_id}",
                ]
            )
            if latest.type == "lineage.generation.started":
                lines.append(
                    f"**Current Step**: Gen {latest.data.get('generation_number')} {latest.data.get('phase')}"
                )
            elif latest.type == "lineage.generation.completed":
                lines.append(
                    f"**Current Step**: Gen {latest.data.get('generation_number')} completed"
                )
            elif latest.type == "lineage.generation.failed":
                lines.append(
                    f"**Current Step**: Gen {latest.data.get('generation_number')} failed at {latest.data.get('phase')}"
                )
            elif latest.type in {"lineage.converged", "lineage.stagnated", "lineage.exhausted"}:
                lines.append(f"**Current Step**: {latest.type.split('.', 1)[1]}")
                if latest.data.get("reason"):
                    lines.append(f"**Reason**: {latest.data.get('reason')}")

    if snapshot.result_text and snapshot.is_terminal:
        lines.extend(
            [
                "",
                "### Result",
                "Use `ouroboros_job_result` to fetch the full terminal output.",
            ]
        )

    if snapshot.error:
        lines.extend(["", f"**Error**: {snapshot.error}"])

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
                "to monitor progress."
            ),
            parameters=ExecuteSeedHandler().definition.parameters,
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        seed_content = arguments.get("seed_content")
        if not seed_content:
            return Result.err(
                MCPToolError(
                    "seed_content is required",
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
            if session_result.is_ok:
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

        text = (
            f"Started background execution.\n\n"
            f"Job ID: {snapshot.job_id}\n"
            f"Session ID: {snapshot.links.session_id or 'pending'}\n"
            f"Execution ID: {snapshot.links.execution_id or 'pending'}\n\n"
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


@dataclass
class StartEvolveStepHandler:
    """Start one evolve_step generation asynchronously."""

    evolve_handler: EvolveStepHandler | None = field(default=None, repr=False)
    event_store: EventStore | None = field(default=None, repr=False)
    job_manager: JobManager | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self._event_store = self.event_store or EventStore()
        self._job_manager = self.job_manager or JobManager(self._event_store)
        self._evolve_handler = self.evolve_handler or EvolveStepHandler()

    @property
    def definition(self) -> MCPToolDefinition:
        return MCPToolDefinition(
            name="ouroboros_start_evolve_step",
            description=(
                "Start one evolve_step generation in the background and return a job ID "
                "immediately for later status checks."
            ),
            parameters=EvolveStepHandler().definition.parameters,
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        lineage_id = arguments.get("lineage_id")
        if not lineage_id:
            return Result.err(
                MCPToolError(
                    "lineage_id is required",
                    tool_name="ouroboros_start_evolve_step",
                )
            )

        async def _runner() -> MCPToolResult:
            result = await self._evolve_handler.handle(arguments)
            if result.is_err:
                raise RuntimeError(str(result.error))
            return result.value

        snapshot = await self._job_manager.start_job(
            job_type="evolve_step",
            initial_message=f"Queued evolve_step for {lineage_id}",
            runner=_runner(),
            links=JobLinks(lineage_id=lineage_id),
        )

        text = (
            f"Started background evolve_step.\n\n"
            f"Job ID: {snapshot.job_id}\n"
            f"Lineage ID: {lineage_id}\n\n"
            "Use ouroboros_job_status, ouroboros_job_wait, or ouroboros_job_result to monitor it."
        )
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text=text),),
                is_error=False,
                meta={
                    "job_id": snapshot.job_id,
                    "lineage_id": lineage_id,
                    "status": snapshot.status.value,
                    "cursor": snapshot.cursor,
                },
            )
        )


@dataclass
class JobStatusHandler:
    """Return a human-readable status summary for a background job."""

    event_store: EventStore | None = field(default=None, repr=False)
    job_manager: JobManager | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self._event_store = self.event_store or EventStore()
        self._job_manager = self.job_manager or JobManager(self._event_store)

    @property
    def definition(self) -> MCPToolDefinition:
        return MCPToolDefinition(
            name="ouroboros_job_status",
            description="Get the latest summary for a background Ouroboros job.",
            parameters=(
                MCPToolParameter(
                    name="job_id",
                    type=ToolInputType.STRING,
                    description="Job ID returned by a start tool",
                    required=True,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        job_id = arguments.get("job_id")
        if not job_id:
            return Result.err(
                MCPToolError(
                    "job_id is required",
                    tool_name="ouroboros_job_status",
                )
            )

        try:
            snapshot = await self._job_manager.get_snapshot(job_id)
        except ValueError as exc:
            return Result.err(MCPToolError(str(exc), tool_name="ouroboros_job_status"))

        text = await _render_job_snapshot(snapshot, self._event_store)
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text=text),),
                is_error=snapshot.status in {JobStatus.FAILED, JobStatus.CANCELLED},
                meta={
                    "job_id": snapshot.job_id,
                    "status": snapshot.status.value,
                    "cursor": snapshot.cursor,
                    "session_id": snapshot.links.session_id,
                    "execution_id": snapshot.links.execution_id,
                    "lineage_id": snapshot.links.lineage_id,
                },
            )
        )


@dataclass
class JobWaitHandler:
    """Long-poll for the next background job update."""

    event_store: EventStore | None = field(default=None, repr=False)
    job_manager: JobManager | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self._event_store = self.event_store or EventStore()
        self._job_manager = self.job_manager or JobManager(self._event_store)

    @property
    def definition(self) -> MCPToolDefinition:
        return MCPToolDefinition(
            name="ouroboros_job_wait",
            description=(
                "Wait briefly for a background job to change state. "
                "Useful for conversational polling after a start command."
            ),
            parameters=(
                MCPToolParameter(
                    name="job_id",
                    type=ToolInputType.STRING,
                    description="Job ID returned by a start tool",
                    required=True,
                ),
                MCPToolParameter(
                    name="cursor",
                    type=ToolInputType.INTEGER,
                    description="Previous cursor from job_status or job_wait",
                    required=False,
                    default=0,
                ),
                MCPToolParameter(
                    name="timeout_seconds",
                    type=ToolInputType.INTEGER,
                    description="Maximum seconds to wait for a change (longer = fewer round-trips)",
                    required=False,
                    default=30,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        job_id = arguments.get("job_id")
        if not job_id:
            return Result.err(
                MCPToolError(
                    "job_id is required",
                    tool_name="ouroboros_job_wait",
                )
            )

        cursor = int(arguments.get("cursor", 0))
        timeout_seconds = int(arguments.get("timeout_seconds", 30))

        try:
            snapshot, changed = await self._job_manager.wait_for_change(
                job_id,
                cursor=cursor,
                timeout_seconds=timeout_seconds,
            )
        except ValueError as exc:
            return Result.err(MCPToolError(str(exc), tool_name="ouroboros_job_wait"))

        text = await _render_job_snapshot(snapshot, self._event_store)
        if not changed:
            text += "\n\nNo new job-level events during this wait window."
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text=text),),
                is_error=snapshot.status in {JobStatus.FAILED, JobStatus.CANCELLED},
                meta={
                    "job_id": snapshot.job_id,
                    "status": snapshot.status.value,
                    "cursor": snapshot.cursor,
                    "changed": changed,
                },
            )
        )


@dataclass
class JobResultHandler:
    """Fetch the terminal output for a background job."""

    event_store: EventStore | None = field(default=None, repr=False)
    job_manager: JobManager | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self._event_store = self.event_store or EventStore()
        self._job_manager = self.job_manager or JobManager(self._event_store)

    @property
    def definition(self) -> MCPToolDefinition:
        return MCPToolDefinition(
            name="ouroboros_job_result",
            description="Get the final output for a completed background job.",
            parameters=(
                MCPToolParameter(
                    name="job_id",
                    type=ToolInputType.STRING,
                    description="Job ID returned by a start tool",
                    required=True,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        job_id = arguments.get("job_id")
        if not job_id:
            return Result.err(
                MCPToolError(
                    "job_id is required",
                    tool_name="ouroboros_job_result",
                )
            )

        try:
            snapshot = await self._job_manager.get_snapshot(job_id)
        except ValueError as exc:
            return Result.err(MCPToolError(str(exc), tool_name="ouroboros_job_result"))

        if not snapshot.is_terminal:
            return Result.err(
                MCPToolError(
                    f"Job still running: {snapshot.status.value}",
                    tool_name="ouroboros_job_result",
                )
            )

        result_text = snapshot.result_text or snapshot.error or snapshot.message
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text=result_text),),
                is_error=snapshot.status in {JobStatus.FAILED, JobStatus.CANCELLED},
                meta={
                    "job_id": snapshot.job_id,
                    "status": snapshot.status.value,
                    "session_id": snapshot.links.session_id,
                    "execution_id": snapshot.links.execution_id,
                    "lineage_id": snapshot.links.lineage_id,
                    **snapshot.result_meta,
                },
            )
        )


@dataclass
class CancelJobHandler:
    """Cancel a background job."""

    event_store: EventStore | None = field(default=None, repr=False)
    job_manager: JobManager | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self._event_store = self.event_store or EventStore()
        self._job_manager = self.job_manager or JobManager(self._event_store)

    @property
    def definition(self) -> MCPToolDefinition:
        return MCPToolDefinition(
            name="ouroboros_cancel_job",
            description="Request cancellation for a background job.",
            parameters=(
                MCPToolParameter(
                    name="job_id",
                    type=ToolInputType.STRING,
                    description="Job ID returned by a start tool",
                    required=True,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        job_id = arguments.get("job_id")
        if not job_id:
            return Result.err(
                MCPToolError(
                    "job_id is required",
                    tool_name="ouroboros_cancel_job",
                )
            )

        try:
            snapshot = await self._job_manager.cancel_job(job_id)
        except ValueError as exc:
            return Result.err(MCPToolError(str(exc), tool_name="ouroboros_cancel_job"))

        text = await _render_job_snapshot(snapshot, self._event_store)
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text=text),),
                is_error=False,
                meta={
                    "job_id": snapshot.job_id,
                    "status": snapshot.status.value,
                    "cursor": snapshot.cursor,
                },
            )
        )


# Convenience functions for handler access
def execute_seed_handler() -> ExecuteSeedHandler:
    """Create an ExecuteSeedHandler instance."""
    return ExecuteSeedHandler()


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


def generate_seed_handler() -> GenerateSeedHandler:
    """Create a GenerateSeedHandler instance."""
    return GenerateSeedHandler()


def measure_drift_handler() -> MeasureDriftHandler:
    """Create a MeasureDriftHandler instance."""
    return MeasureDriftHandler()


def interview_handler() -> InterviewHandler:
    """Create an InterviewHandler instance."""
    return InterviewHandler()


def lateral_think_handler() -> LateralThinkHandler:
    """Create a LateralThinkHandler instance."""
    return LateralThinkHandler()


def evaluate_handler() -> EvaluateHandler:
    """Create an EvaluateHandler instance."""
    return EvaluateHandler()


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


# List of all Ouroboros tools for registration
from ouroboros.mcp.tools.qa import QAHandler  # noqa: E402

OUROBOROS_TOOLS: tuple[
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
] = (
    ExecuteSeedHandler(),
    StartExecuteSeedHandler(),
    SessionStatusHandler(),
    JobStatusHandler(),
    JobWaitHandler(),
    JobResultHandler(),
    CancelJobHandler(),
    QueryEventsHandler(),
    GenerateSeedHandler(),
    MeasureDriftHandler(),
    InterviewHandler(),
    EvaluateHandler(),
    LateralThinkHandler(),
    EvolveStepHandler(),
    StartEvolveStepHandler(),
    LineageStatusHandler(),
    EvolveRewindHandler(),
    CancelExecutionHandler(),
    QAHandler(),
)
