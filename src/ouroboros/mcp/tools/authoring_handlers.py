"""Authoring-phase tool handlers for Ouroboros MCP server.

Contains handlers for interview and seed generation tools:
- GenerateSeedHandler: Converts completed interview sessions into immutable Seeds.
- InterviewHandler: Manages interactive requirement-clarification interviews.
"""

from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Any

from pydantic import ValidationError as PydanticValidationError
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
from ouroboros.config import get_clarification_model
from ouroboros.core.errors import ValidationError
from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPServerError, MCPToolError
from ouroboros.mcp.types import (
    ContentType,
    MCPContentItem,
    MCPToolDefinition,
    MCPToolParameter,
    MCPToolResult,
    ToolInputType,
)
from ouroboros.persistence.event_store import EventStore
from ouroboros.providers import create_llm_adapter
from ouroboros.providers.base import LLMAdapter

log = structlog.get_logger(__name__)


@dataclass
class GenerateSeedHandler:
    """Handler for the ouroboros_generate_seed tool.

    Converts a completed interview session into an immutable Seed specification.
    The seed generation gates on ambiguity score (must be <= 0.2).
    """

    interview_engine: InterviewEngine | None = field(default=None, repr=False)
    seed_generator: SeedGenerator | None = field(default=None, repr=False)
    llm_adapter: LLMAdapter | None = field(default=None, repr=False)
    llm_backend: str | None = field(default=None, repr=False)

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
            llm_adapter = self.llm_adapter or create_llm_adapter(
                backend=self.llm_backend,
                max_turns=1,
            )
            interview_engine = self.interview_engine or InterviewEngine(
                llm_adapter=llm_adapter,
                model=get_clarification_model(self.llm_backend),
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
            generator = self.seed_generator or SeedGenerator(
                llm_adapter=llm_adapter,
                model=get_clarification_model(self.llm_backend),
            )

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
class InterviewHandler:
    """Handler for the ouroboros_interview tool.

    Manages interactive interviews for requirement clarification.
    Supports starting new interviews, resuming existing sessions,
    and recording responses to questions.
    """

    interview_engine: InterviewEngine | None = field(default=None, repr=False)
    event_store: EventStore | None = field(default=None, repr=False)
    llm_adapter: LLMAdapter | None = field(default=None, repr=False)
    llm_backend: str | None = field(default=None, repr=False)

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
        llm_adapter = self.llm_adapter or create_llm_adapter(
            backend=self.llm_backend,
            max_turns=3,
            use_case="interview",
            allowed_tools=None,
        )
        engine = self.interview_engine or InterviewEngine(
            llm_adapter=llm_adapter,
            state_dir=Path.home() / ".ouroboros" / "data",
            model=get_clarification_model(self.llm_backend),
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
