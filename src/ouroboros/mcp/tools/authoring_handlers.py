"""Authoring-phase tool handlers for Ouroboros MCP server.

Contains handlers for interview and seed generation tools:
- GenerateSeedHandler: Converts completed interview sessions into immutable Seeds.
- InterviewHandler: Manages interactive requirement-clarification interviews.
"""

import asyncio
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
from typing import Any

from pydantic import ValidationError as PydanticValidationError
import structlog
import yaml

from ouroboros.bigbang.ambiguity import (
    AMBIGUITY_THRESHOLD,
    AUTO_COMPLETE_STREAK_REQUIRED,
    AmbiguityScore,
    AmbiguityScorer,
    ComponentScore,
    ScoreBreakdown,
    get_completion_floor_failures,
    get_milestone,
    qualifies_for_seed_completion,
)
from ouroboros.bigbang.interview import (
    MIN_ROUNDS_BEFORE_EARLY_EXIT,
    InterviewEngine,
    InterviewState,
)
from ouroboros.bigbang.seed_generator import SeedGenerator
from ouroboros.config import get_clarification_model
from ouroboros.core.errors import ValidationError
from ouroboros.core.initial_context import resolve_initial_context_input
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

_LIVE_AMBIGUITY_MAX_RETRIES = 3

_INTERVIEW_COMPLETION_SIGNALS = {
    "done",
    "complete",
    "stop",
    "enough",
    "generate seed",
    "create seed",
    "seed",
}

_INTERVIEW_COMPLETION_PHRASES = (
    "close the interview",
    "close interview",
    "close now",
    "mark the interview complete",
    "mark interview complete",
    "generate the seed",
    "create the seed",
    "seed generation",
    "ready for seed generation",
    "hand off for seed generation",
    "no remaining ambiguity",
    "no ambiguity remains",
    "no ambiguity left",
)

_INTERVIEW_COMPLETION_NEGATIONS = (
    "not done",
    "not complete",
    "not enough",
    "not ready",
    "do not close",
    "dont close",
    "don't close",
)


def _normalize_interview_answer(answer: str) -> str:
    """Normalize interview answers for lightweight intent matching."""
    return " ".join(re.findall(r"[a-z0-9']+", answer.lower()))


def _is_interview_completion_signal(answer: str | None) -> bool:
    """Return True when the answer explicitly asks to end the interview."""
    if answer is None:
        return False

    normalized = _normalize_interview_answer(answer)
    if not normalized:
        return False

    if normalized in _INTERVIEW_COMPLETION_SIGNALS:
        return True

    if any(phrase in normalized for phrase in _INTERVIEW_COMPLETION_NEGATIONS):
        return False

    if any(phrase in normalized for phrase in _INTERVIEW_COMPLETION_PHRASES):
        return True

    tokens = set(normalized.split())
    if {"close", "interview"} <= tokens:
        return True
    if "seed" in tokens and tokens.intersection({"generate", "create", "ready"}):
        return True
    if "ambiguity" in tokens and "no" in tokens and tokens.intersection({"remaining", "left"}):
        return True
    return normalized.endswith(" done") or normalized == "done"


def _count_answered_rounds(state: InterviewState) -> int:
    """Return the number of completed interview rounds."""
    return sum(1 for round_data in state.rounds if round_data.user_response is not None)


def _update_completion_candidate_streak(
    state: InterviewState,
    score: AmbiguityScore,
) -> bool:
    """Update and return whether the current score qualifies for auto-completion."""
    qualifies = qualifies_for_seed_completion(score, is_brownfield=state.is_brownfield)
    if qualifies:
        state.completion_candidate_streak += 1
    else:
        state.completion_candidate_streak = 0
    return qualifies


def _completion_gate_reason(
    score: AmbiguityScore | None,
    *,
    is_brownfield: bool,
) -> str:
    """Describe the strongest reason interview completion is still blocked."""
    if score is None:
        return f"ambiguity score could not be confirmed against threshold {AMBIGUITY_THRESHOLD:.2f}"
    if score.overall_score > AMBIGUITY_THRESHOLD:
        return (
            f"ambiguity score {score.overall_score:.2f} exceeds threshold {AMBIGUITY_THRESHOLD:.2f}"
        )

    floor_failures = get_completion_floor_failures(score, is_brownfield=is_brownfield)
    if floor_failures:
        return f"completion floors are unmet ({'; '.join(floor_failures)})"

    return "requirements are not stable enough to close yet"


def _milestone_for_score(score: AmbiguityScore | None) -> str | None:
    """Return the milestone label for an ambiguity score, or None."""
    if score is None:
        return None
    milestone, _ = get_milestone(score.overall_score)
    return milestone.value


def _format_question_with_ambiguity(question: str, score: AmbiguityScore | None) -> str:
    """Attach the current ambiguity score to a question for display."""
    if score is None:
        return question
    milestone_label = _milestone_for_score(score)
    if milestone_label:
        return f"(ambiguity: {score.overall_score:.2f} [{milestone_label}]) {question}"
    return f"(ambiguity: {score.overall_score:.2f}) {question}"


def _ambiguity_warning_for_failed_question(
    score: AmbiguityScore | None,
    *,
    is_brownfield: bool = False,
) -> str:
    """Build an explicit ambiguity warning for question-generation failures.

    When question generation fails mid-interview, the main session must NOT
    assume the interview is complete.
    See: https://github.com/Q00/ouroboros/issues/210
    """
    if score is None:
        return (
            "\n\nWARNING: Ambiguity score is unknown. "
            "The interview is NOT complete — do NOT generate a Seed. "
            "Resume the interview to continue clarifying requirements."
        )
    if not score.is_ready_for_seed:
        return (
            f"\n\nWARNING: Current ambiguity is {score.overall_score:.2f} "
            f"(threshold: {AMBIGUITY_THRESHOLD}). "
            f"The interview is NOT complete — do NOT generate a Seed. "
            f"Resume the interview to continue clarifying requirements."
        )
    floor_failures = get_completion_floor_failures(score, is_brownfield=is_brownfield)
    if floor_failures:
        return (
            "\n\nWARNING: Ambiguity is low, but completion floors are unmet "
            f"({'; '.join(floor_failures)}). "
            "The interview is NOT complete — do NOT generate a Seed. "
            "Resume the interview to continue clarifying requirements."
        )
    return ""


def _load_state_ambiguity_score(state: InterviewState) -> AmbiguityScore | None:
    """Rebuild a stored ambiguity snapshot from interview state."""
    if state.ambiguity_score is None:
        return None

    if isinstance(state.ambiguity_breakdown, dict):
        try:
            breakdown = ScoreBreakdown.model_validate(state.ambiguity_breakdown)
        except PydanticValidationError:
            log.warning(
                "mcp.tool.interview.invalid_stored_ambiguity_breakdown",
                session_id=state.interview_id,
            )
        else:
            return AmbiguityScore(
                overall_score=state.ambiguity_score,
                breakdown=breakdown,
            )

    breakdown = ScoreBreakdown(
        goal_clarity=ComponentScore(
            name="goal_clarity",
            clarity_score=1.0 - state.ambiguity_score,
            weight=0.40,
            justification="Loaded from stored interview ambiguity score",
        ),
        constraint_clarity=ComponentScore(
            name="constraint_clarity",
            clarity_score=1.0 - state.ambiguity_score,
            weight=0.30,
            justification="Loaded from stored interview ambiguity score",
        ),
        success_criteria_clarity=ComponentScore(
            name="success_criteria_clarity",
            clarity_score=1.0 - state.ambiguity_score,
            weight=0.30,
            justification="Loaded from stored interview ambiguity score",
        ),
    )
    return AmbiguityScore(
        overall_score=state.ambiguity_score,
        breakdown=breakdown,
    )


def _stored_ambiguity_snapshot_is_degraded(state: InterviewState) -> bool:
    """Return True when stored ambiguity data lacks a parseable breakdown."""
    if state.ambiguity_score is None:
        return True
    if not isinstance(state.ambiguity_breakdown, dict):
        return True

    try:
        ScoreBreakdown.model_validate(state.ambiguity_breakdown)
    except PydanticValidationError:
        return True

    return False


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

            # Always use a trusted ambiguity score: persisted snapshot or
            # freshly computed.  The caller-supplied ``ambiguity_score``
            # parameter is intentionally ignored to prevent LLM callers
            # from overriding the gate with an arbitrary low value.
            # See: https://github.com/Q00/ouroboros/issues/210
            if ambiguity_score_value is not None:
                log.warning(
                    "mcp.tool.generate_seed.ignoring_caller_ambiguity_score",
                    session_id=session_id,
                    caller_value=ambiguity_score_value,
                )

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
        self._owns_event_store = self.event_store is None
        self._event_store = self.event_store or EventStore()
        self._initialized = False
        self._closed = False
        self._bg_tasks: set[asyncio.Task] = set()

    async def _ensure_initialized(self) -> None:
        """Ensure the event store is initialized."""
        if not self._initialized:
            await self._event_store.initialize()
            self._initialized = True

    async def _drain_bg_tasks(self, timeout: float = 5.0) -> None:
        """Await all pending background event tasks before shutdown."""
        if not self._bg_tasks:
            return
        tasks = list(self._bg_tasks)
        done, pending = await asyncio.wait(tasks, timeout=timeout)
        for t in pending:
            t.cancel()
        # Await cancelled tasks so CancelledError propagates cleanly
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._bg_tasks.clear()

    async def close(self) -> None:
        """Drain pending event tasks, then close the event store if owned."""
        self._closed = True
        await self._drain_bg_tasks()
        if self._owns_event_store:
            await self._event_store.close()
            self._initialized = False

    async def _emit_event(self, event: Any) -> None:
        """Emit event to store. Swallows errors to not break interview flow."""
        try:
            await self._ensure_initialized()
            await self._event_store.append(event)
        except Exception as e:
            log.warning("mcp.tool.interview.event_emission_failed", error=str(e))

    def _emit_event_bg(self, event: Any) -> None:
        """Fire-and-forget event emission — non-blocking on the hot path."""
        if self._closed:
            return
        task = asyncio.create_task(self._emit_event(event))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _score_interview_state(
        self,
        llm_adapter: LLMAdapter,
        state: InterviewState,
    ) -> AmbiguityScore | None:
        """Calculate and cache the latest ambiguity snapshot for interview routing."""
        scorer = AmbiguityScorer(
            llm_adapter=llm_adapter,
            model=get_clarification_model(self.llm_backend),
            max_retries=_LIVE_AMBIGUITY_MAX_RETRIES,
        )
        score_result = await scorer.score(state)
        if score_result.is_err:
            state.clear_stored_ambiguity()
            if state.completion_candidate_streak != 0:
                state.completion_candidate_streak = 0
                state.mark_updated()
            log.warning(
                "mcp.tool.interview.live_ambiguity_failed",
                interview_id=state.interview_id,
                error=str(score_result.error),
            )
            return None

        score = score_result.value
        _update_completion_candidate_streak(state, score)
        state.store_ambiguity(
            score=score.overall_score,
            breakdown=score.breakdown.model_dump(mode="json"),
        )
        return score

    @staticmethod
    def _ambiguity_gate_response(
        session_id: str,
        score: AmbiguityScore | None,
        *,
        is_brownfield: bool,
    ) -> Result[MCPToolResult, MCPServerError]:
        """Build an MCP response refusing premature interview completion."""
        score_display = f"{score.overall_score:.2f}" if score is not None else "unknown"
        gate_reason = _completion_gate_reason(score, is_brownfield=is_brownfield)
        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=(
                            f"Cannot complete yet — {gate_reason}. "
                            f"Current ambiguity: {score_display}. "
                            f"Please answer a few more questions to "
                            f"clarify remaining areas."
                        ),
                    ),
                ),
                is_error=False,
                meta={
                    "session_id": session_id,
                    "ambiguity_score": (score.overall_score if score is not None else None),
                    "milestone": _milestone_for_score(score),
                    "seed_ready": False,
                },
            )
        )

    async def _complete_interview_response(
        self,
        engine: InterviewEngine,
        state: InterviewState,
        session_id: str,
        score: AmbiguityScore | None = None,
    ) -> Result[MCPToolResult, MCPServerError]:
        """Complete the interview and return a Seed-ready MCP response."""
        complete_result = await engine.complete_interview(state)
        if complete_result.is_err:
            return Result.err(
                MCPToolError(
                    str(complete_result.error),
                    tool_name="ouroboros_interview",
                )
            )

        state = complete_result.value
        save_result = await engine.save_state(state)
        if save_result.is_err:
            log.warning(
                "mcp.tool.interview.save_failed_on_complete",
                error=str(save_result.error),
            )

        from ouroboros.events.interview import interview_completed

        self._emit_event_bg(
            interview_completed(
                interview_id=session_id,
                total_rounds=len(state.rounds),
            )
        )

        score_line = ""
        if score is not None:
            ms_label = _milestone_for_score(score)
            score_line = (
                f"(ambiguity: {score.overall_score:.2f} [{ms_label}]) Ready for Seed generation.\n"
            )

        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=(
                            f"Interview completed. Session ID: {session_id}\n\n"
                            f"{score_line}"
                            f'Generate a Seed with: session_id="{session_id}"'
                        ),
                    ),
                ),
                is_error=False,
                meta={
                    "session_id": session_id,
                    "completed": True,
                    "ambiguity_score": score.overall_score if score is not None else None,
                    "milestone": _milestone_for_score(score),
                    "seed_ready": score.is_ready_for_seed if score is not None else None,
                },
            )
        )

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
        # max_turns=1: MCP is a pure question generator. No tool use needed.
        # Main session handles codebase exploration and answering.
        llm_adapter = self.llm_adapter or create_llm_adapter(
            backend=self.llm_backend,
            max_turns=1,
            use_case="interview",
            allowed_tools=[],
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
                resolved_context = resolve_initial_context_input(initial_context, cwd=cwd)
                if resolved_context.is_err:
                    return Result.err(
                        MCPToolError(
                            str(resolved_context.error),
                            tool_name="ouroboros_interview",
                        )
                    )

                result = await engine.start_interview(resolved_context.value, cwd=cwd)
                if result.is_err:
                    return Result.err(
                        MCPToolError(
                            str(result.error),
                            tool_name="ouroboros_interview",
                        )
                    )

                state = result.value
                _interview_id = state.interview_id
                # No answers exist yet — scoring cannot trigger completion
                # and would waste an LLM call (~3-8s). The PM handler
                # already skips scoring before MIN_ROUNDS_BEFORE_EARLY_EXIT
                # (pm_interview.py:889); apply the same optimisation here.
                live_score = None
                question_result = await engine.ask_next_question(state)
                if question_result.is_err:
                    error_msg = str(question_result.error)
                    from ouroboros.events.interview import interview_failed

                    self._emit_event_bg(
                        interview_failed(
                            state.interview_id,
                            error_msg,
                            phase="question_generation",
                        )
                    )
                    # Return recoverable result with session ID for retry
                    if "empty response" in error_msg.lower():
                        # Persist state so the session can actually be resumed
                        await engine.save_state(state)
                        amb_warning = _ambiguity_warning_for_failed_question(
                            live_score,
                            is_brownfield=state.is_brownfield,
                        )
                        stderr_info = ""
                        err = question_result.error
                        if hasattr(err, "details") and isinstance(err.details, dict):
                            stderr = err.details.get("stderr", "")
                            if stderr:
                                stderr_info = f"\n\nDiagnostics (stderr):\n{stderr}"
                        return Result.ok(
                            MCPToolResult(
                                content=(
                                    MCPContentItem(
                                        type=ContentType.TEXT,
                                        text=(
                                            f"Question generation failed (empty response from Agent SDK). "
                                            f"Session ID: {state.interview_id}\n\n"
                                            f'Resume with: session_id="{state.interview_id}"'
                                            f"{amb_warning}"
                                            f"{stderr_info}"
                                        ),
                                    ),
                                ),
                                is_error=True,
                                meta={"session_id": state.interview_id, "recoverable": True},
                            )
                        )
                    return Result.err(MCPToolError(error_msg, tool_name="ouroboros_interview"))

                question = question_result.value
                display_question = _format_question_with_ambiguity(question, live_score)

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

                self._emit_event_bg(
                    interview_started(
                        state.interview_id,
                        resolved_context.value,
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
                                text=(
                                    f"Interview started. Session ID: {state.interview_id}\n\n"
                                    f"{display_question}"
                                ),
                            ),
                        ),
                        is_error=False,
                        meta={
                            "session_id": state.interview_id,
                            "ambiguity_score": (
                                live_score.overall_score if live_score is not None else None
                            ),
                            "milestone": _milestone_for_score(live_score),
                            "seed_ready": (
                                live_score.is_ready_for_seed if live_score is not None else None
                            ),
                        },
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

                if not answer and state.rounds and state.rounds[-1].user_response is None:
                    display_question = _format_question_with_ambiguity(
                        state.rounds[-1].question,
                        _load_state_ambiguity_score(state),
                    )
                    return Result.ok(
                        MCPToolResult(
                            content=(
                                MCPContentItem(
                                    type=ContentType.TEXT,
                                    text=f"Session {session_id}\n\n{display_question}",
                                ),
                            ),
                            is_error=False,
                            meta={
                                "session_id": session_id,
                                "ambiguity_score": state.ambiguity_score,
                                "milestone": (
                                    get_milestone(state.ambiguity_score)[0].value
                                    if state.ambiguity_score is not None
                                    else None
                                ),
                                "seed_ready": (
                                    state.ambiguity_score is not None
                                    and state.ambiguity_score <= AMBIGUITY_THRESHOLD
                                ),
                            },
                        )
                    )

                # If answer provided, record it first
                if answer:
                    if _is_interview_completion_signal(answer):
                        if state.rounds and state.rounds[-1].user_response is None:
                            state.rounds.pop()
                        # Gate: check ambiguity before completing.
                        # Stored score first; live scoring as fallback.
                        exit_score = _load_state_ambiguity_score(state)
                        if (
                            exit_score is None
                            or _stored_ambiguity_snapshot_is_degraded(state)
                            or not qualifies_for_seed_completion(
                                exit_score,
                                is_brownfield=state.is_brownfield,
                            )
                        ):
                            exit_score = await self._score_interview_state(llm_adapter, state)
                        if exit_score is not None and qualifies_for_seed_completion(
                            exit_score,
                            is_brownfield=state.is_brownfield,
                        ):
                            return await self._complete_interview_response(
                                engine,
                                state,
                                session_id,
                                exit_score,
                            )
                        # Ambiguity too high — refuse completion
                        await engine.save_state(state)
                        return self._ambiguity_gate_response(
                            session_id,
                            exit_score,
                            is_brownfield=state.is_brownfield,
                        )

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

                    self._emit_event_bg(
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

                    # Persist recorded answer immediately so it survives
                    # question generation failures downstream
                    await engine.save_state(state)

                    # Only score ambiguity when completion is actually
                    # possible. Before MIN_ROUNDS_BEFORE_EARLY_EXIT the
                    # result cannot trigger early exit, so the LLM call
                    # (~3-8 s) is pure waste. Once scoring starts, run it
                    # before question generation so the next prompt sees
                    # the latest ambiguity snapshot, closure threshold,
                    # and completion-candidate streak.
                    answered = _count_answered_rounds(state)
                    if answered >= MIN_ROUNDS_BEFORE_EARLY_EXIT:
                        # Scoring must complete before question generation:
                        # _score_interview_state mutates state.ambiguity_score,
                        # completion_candidate_streak, and ambiguity_breakdown.
                        # ask_next_question reads those fields to build the
                        # system prompt (closure mode, seed-ready, streak).
                        # Running them in parallel would give the question
                        # generator stale routing context.
                        live_score = await self._score_interview_state(llm_adapter, state)
                        if (
                            live_score is not None
                            and qualifies_for_seed_completion(
                                live_score,
                                is_brownfield=state.is_brownfield,
                            )
                            and state.completion_candidate_streak >= AUTO_COMPLETE_STREAK_REQUIRED
                        ):
                            return await self._complete_interview_response(
                                engine,
                                state,
                                session_id,
                                live_score,
                            )
                        question_result = await engine.ask_next_question(state)
                    else:
                        live_score = None
                        question_result = await engine.ask_next_question(state)
                else:
                    live_score = _load_state_ambiguity_score(state)
                    question_result = await engine.ask_next_question(state)
                if question_result.is_err:
                    error_msg = str(question_result.error)
                    from ouroboros.events.interview import interview_failed

                    self._emit_event_bg(
                        interview_failed(
                            session_id,
                            error_msg,
                            phase="question_generation",
                        )
                    )
                    if "empty response" in error_msg.lower():
                        amb_warning = _ambiguity_warning_for_failed_question(
                            live_score,
                            is_brownfield=state.is_brownfield,
                        )
                        # Extract stderr from ProviderError details for diagnostics
                        stderr_info = ""
                        err = question_result.error
                        if hasattr(err, "details") and isinstance(err.details, dict):
                            stderr = err.details.get("stderr", "")
                            if stderr:
                                stderr_info = f"\n\nDiagnostics (stderr):\n{stderr}"
                        return Result.ok(
                            MCPToolResult(
                                content=(
                                    MCPContentItem(
                                        type=ContentType.TEXT,
                                        text=(
                                            f"Question generation failed (empty response from Agent SDK). "
                                            f"Session ID: {session_id}\n\n"
                                            f'Resume with: session_id="{session_id}"'
                                            f"{amb_warning}"
                                            f"{stderr_info}"
                                        ),
                                    ),
                                ),
                                is_error=True,
                                meta={"session_id": session_id, "recoverable": True},
                            )
                        )
                    return Result.err(MCPToolError(error_msg, tool_name="ouroboros_interview"))

                question = question_result.value
                display_question = _format_question_with_ambiguity(question, live_score)

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
                                text=f"Session {session_id}\n\n{display_question}",
                            ),
                        ),
                        is_error=False,
                        meta={
                            "session_id": session_id,
                            "ambiguity_score": (
                                live_score.overall_score if live_score is not None else None
                            ),
                            "milestone": _milestone_for_score(live_score),
                            "seed_ready": (
                                live_score.is_ready_for_seed if live_score is not None else None
                            ),
                        },
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

                self._emit_event_bg(
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
        finally:
            if self._owns_event_store:
                await self.close()
