"""Interactive interview engine for requirement clarification.

This module implements the interview protocol that refines vague ideas into
clear requirements through iterative questioning. Users control when to stop.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
import functools
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
import structlog

from ouroboros.config import get_clarification_model
from ouroboros.core.errors import ProviderError, ValidationError
from ouroboros.core.file_lock import file_lock as _file_lock
from ouroboros.core.security import InputValidator
from ouroboros.core.types import Result
from ouroboros.providers.base import (
    CompletionConfig,
    LLMAdapter,
    Message,
    MessageRole,
)

log = structlog.get_logger()

# Interview round constants
# Start scoring after 3 answered rounds. Closing pressure and auto-completion
# are gated separately by ambiguity thresholds and sustained score quality.
MIN_ROUNDS_BEFORE_EARLY_EXIT = 3
DEFAULT_INTERVIEW_ROUNDS = 10  # Reference value for prompts (not enforced)

# Legacy alias for backward compatibility
MAX_INTERVIEW_ROUNDS = DEFAULT_INTERVIEW_ROUNDS


class InterviewPerspective(StrEnum):
    """Internal perspectives used to keep interviews broad and practical."""

    RESEARCHER = "researcher"
    SIMPLIFIER = "simplifier"
    ARCHITECT = "architect"
    BREADTH_KEEPER = "breadth-keeper"
    SEED_CLOSER = "seed-closer"


@dataclass(frozen=True, slots=True)
class InterviewPerspectiveStrategy:
    """Prompt data for one internal interview perspective."""

    perspective: InterviewPerspective
    system_prompt: str
    approach_instructions: tuple[str, ...]
    question_templates: tuple[str, ...]


@functools.lru_cache(maxsize=1)
def _load_interview_perspective_strategies() -> dict[
    InterviewPerspective,
    InterviewPerspectiveStrategy,
]:
    """Lazy-load perspective prompts from agent markdown files."""
    from ouroboros.agents.loader import load_persona_prompt_data

    mapping = {
        InterviewPerspective.RESEARCHER: "researcher",
        InterviewPerspective.SIMPLIFIER: "simplifier",
        InterviewPerspective.ARCHITECT: "architect",
        InterviewPerspective.BREADTH_KEEPER: "breadth-keeper",
        InterviewPerspective.SEED_CLOSER: "seed-closer",
    }

    return {
        perspective: InterviewPerspectiveStrategy(
            perspective=perspective,
            system_prompt=data.system_prompt,
            approach_instructions=data.approach_instructions,
            question_templates=data.question_templates,
        )
        for perspective, filename in mapping.items()
        for data in [load_persona_prompt_data(filename)]
    }


class InterviewStatus(StrEnum):
    """Status of the interview process."""

    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    ABORTED = "aborted"


class InterviewRound(BaseModel):
    """A single round of interview questions and responses.

    Attributes:
        round_number: 1-based round number (no upper limit - user controls).
        question: The question asked by the system.
        user_response: The user's response (None if not yet answered).
        timestamp: When this round was created.
    """

    round_number: int = Field(ge=1)  # No upper limit - user decides when to stop
    question: str
    user_response: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class InterviewState(BaseModel):
    """Persistent state of an interview session.

    Attributes:
        interview_id: Unique identifier for this interview.
        status: Current status of the interview.
        rounds: List of completed and current rounds.
        initial_context: The initial context provided by the user.
        created_at: When the interview was created.
        updated_at: When the interview was last updated.
        is_brownfield: Whether this is a brownfield project.
        codebase_paths: Directories to explore for brownfield context.
        codebase_context: Summary from auto-explore phase.
        explore_completed: Whether exploration has been completed.
    """

    interview_id: str
    status: InterviewStatus = InterviewStatus.IN_PROGRESS
    rounds: list[InterviewRound] = Field(default_factory=list)
    initial_context: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    is_brownfield: bool = False
    codebase_paths: list[dict[str, str]] = Field(default_factory=list)
    codebase_context: str = ""
    explore_completed: bool = False
    ambiguity_score: float | None = Field(default=None, ge=0.0, le=1.0)
    ambiguity_breakdown: dict[str, Any] | None = None
    completion_candidate_streak: int = Field(default=0, ge=0)

    @property
    def current_round_number(self) -> int:
        """Get the current round number (1-based)."""
        return len(self.rounds) + 1

    # Mirrors AMBIGUITY_THRESHOLD from ambiguity.py to avoid circular import.
    _SEED_READY_THRESHOLD: float = 0.2

    @property
    def is_complete(self) -> bool:
        """Check if interview is marked complete (user-controlled)."""
        return self.status == InterviewStatus.COMPLETED

    @property
    def can_reopen(self) -> bool:
        """True when a completed interview should be reopenable.

        A completed interview is reopenable only when its stored ambiguity
        score exceeds the seed-generation threshold — i.e. it was completed
        prematurely and is now in a deadlock (can't generate seed, can't
        resume).
        """
        return (
            self.is_complete
            and self.ambiguity_score is not None
            and self.ambiguity_score > self._SEED_READY_THRESHOLD
        )

    def mark_updated(self) -> None:
        """Update the updated_at timestamp."""
        self.updated_at = datetime.now(UTC)

    def store_ambiguity(
        self,
        *,
        score: float,
        breakdown: dict[str, Any],
    ) -> None:
        """Persist the latest ambiguity evaluation on the interview state."""
        self.ambiguity_score = score
        self.ambiguity_breakdown = breakdown
        self.mark_updated()

    def clear_stored_ambiguity(self) -> None:
        """Invalidate any persisted ambiguity snapshot after interview changes."""
        if self.ambiguity_score is None and self.ambiguity_breakdown is None:
            return

        self.ambiguity_score = None
        self.ambiguity_breakdown = None
        self.mark_updated()


@dataclass
class InterviewEngine:
    """Engine for conducting interactive requirement interviews.

    This engine orchestrates the interview process:
    1. Generates questions based on current context and ambiguity
    2. Collects user responses
    3. Persists state between sessions
    4. Tracks progress through rounds

    Example:
        engine = InterviewEngine(
            llm_adapter=LiteLLMAdapter(),
            state_dir=Path.home() / ".ouroboros" / "data",
        )

        # Start new interview
        result = await engine.start_interview(
            initial_context="I want to build a CLI tool for task management"
        )

        # Ask questions in rounds
        while not state.is_complete:
            question_result = await engine.ask_next_question(state)
            if question_result.is_ok:
                question = question_result.value
                user_response = input(question)
                await engine.record_response(state, user_response)

        # Generate final seed (not implemented in this story)

    Note:
        The model can be configured via OuroborosConfig.clarification.default_model
        or passed directly to the constructor.
    """

    llm_adapter: LLMAdapter
    state_dir: Path = field(default_factory=lambda: Path.home() / ".ouroboros" / "data")
    model: str = field(default_factory=get_clarification_model)
    temperature: float = 0.7
    max_tokens: int = 512
    _MAX_SYSTEM_PROMPT_CHARS = 3500
    _MAX_INITIAL_CONTEXT_SYSTEM_CHARS = 1800
    _MAX_INITIAL_CONTEXT_OVERFLOW_CHARS = 1200

    def __post_init__(self) -> None:
        """Ensure state directory exists."""
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def _state_file_path(self, interview_id: str) -> Path:
        """Get the path to the state file for an interview.

        Args:
            interview_id: The interview ID.

        Returns:
            Path to the state file.
        """
        return self.state_dir / f"interview_{interview_id}.json"

    async def start_interview(
        self, initial_context: str, interview_id: str | None = None, cwd: str | None = None
    ) -> Result[InterviewState, ValidationError]:
        """Start a new interview session.

        Args:
            initial_context: The initial context or idea provided by the user.
            interview_id: Optional interview ID (generated if not provided).
            cwd: Optional working directory. When provided, auto-detects
                brownfield projects and runs codebase exploration before the
                first question.

        Returns:
            Result containing the new InterviewState or ValidationError.
        """
        # Validate initial context with security limits
        is_valid, error_msg = InputValidator.validate_initial_context(initial_context)
        if not is_valid:
            return Result.err(ValidationError(error_msg, field="initial_context"))

        if interview_id is None:
            interview_id = f"interview_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}"

        state = InterviewState(
            interview_id=interview_id,
            initial_context=initial_context,
        )

        # Auto-detect brownfield projects from CWD.
        # codebase_paths is informational only — the main session (not MCP)
        # handles codebase exploration directly via Read/Glob/Grep.
        if cwd:
            from ouroboros.bigbang.explore import detect_brownfield

            if detect_brownfield(cwd):
                state.is_brownfield = True
                state.codebase_paths = [{"path": cwd, "role": "primary"}]

        log.info(
            "interview.started",
            interview_id=interview_id,
            initial_context_length=len(initial_context),
            is_brownfield=state.is_brownfield,
        )

        return Result.ok(state)

    async def ask_next_question(
        self, state: InterviewState
    ) -> Result[str, ProviderError | ValidationError]:
        """Generate the next question based on current state.

        Args:
            state: Current interview state.

        Returns:
            Result containing the next question or error.
        """
        if state.is_complete:
            return Result.err(
                ValidationError(
                    "Interview is already complete",
                    field="status",
                    value=state.status,
                )
            )

        # Build the context from previous rounds
        conversation_history = self._build_conversation_history(state)

        # Generate next question
        system_prompt = self._build_system_prompt(state)
        messages = [
            Message(role=MessageRole.SYSTEM, content=system_prompt),
            *conversation_history,
        ]

        config = CompletionConfig(
            model=self.model,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )

        log.debug(
            "interview.generating_question",
            interview_id=state.interview_id,
            round_number=state.current_round_number,
            message_count=len(messages),
        )

        result = await self.llm_adapter.complete(messages, config)

        if result.is_err:
            log.warning(
                "interview.question_generation_failed",
                interview_id=state.interview_id,
                round_number=state.current_round_number,
                error=str(result.error),
            )
            return Result.err(result.error)

        question = result.value.content.strip()

        log.info(
            "interview.question_generated",
            interview_id=state.interview_id,
            round_number=state.current_round_number,
            question_length=len(question),
        )

        return Result.ok(question)

    async def record_response(
        self, state: InterviewState, user_response: str, question: str
    ) -> Result[InterviewState, ValidationError]:
        """Record the user's response to the current question.

        Args:
            state: Current interview state.
            user_response: The user's response.
            question: The question that was asked.

        Returns:
            Result containing updated state or ValidationError.
        """
        # Validate user response with security limits
        is_valid, error_msg = InputValidator.validate_user_response(user_response)
        if not is_valid:
            return Result.err(ValidationError(error_msg, field="user_response"))

        if state.is_complete:
            if not state.can_reopen:
                return Result.err(
                    ValidationError(
                        "Cannot record response - interview is complete",
                        field="status",
                        value=state.status,
                    )
                )
            # Deadlock recovery: reopen when completed prematurely
            state.status = InterviewStatus.IN_PROGRESS
            log.info(
                "interview.reopened_for_ambiguity",
                interview_id=state.interview_id,
                ambiguity_score=state.ambiguity_score,
            )

        # Create new round
        round_data = InterviewRound(
            round_number=state.current_round_number,
            question=question,
            user_response=user_response,
        )

        state.rounds.append(round_data)
        state.mark_updated()

        log.info(
            "interview.response_recorded",
            interview_id=state.interview_id,
            round_number=round_data.round_number,
            response_length=len(user_response),
        )

        # Note: No auto-complete on round limit. User controls when to stop.
        # CLI handles prompting user to continue after each round.

        return Result.ok(state)

    async def save_state(self, state: InterviewState) -> Result[Path, ValidationError]:
        """Persist interview state to disk.

        Uses file locking to prevent race conditions during concurrent access.
        The blocking file I/O is offloaded to a thread to avoid stalling the
        asyncio event loop.

        Args:
            state: The interview state to save.

        Returns:
            Result containing path to saved file or ValidationError.
        """
        try:
            file_path = self._state_file_path(state.interview_id)
            state.mark_updated()
            # Serialize while still on the event-loop (CPU-bound, not I/O)
            content = state.model_dump_json(indent=2)

            def _sync_write() -> None:
                with _file_lock(file_path, exclusive=True):
                    file_path.write_text(content, encoding="utf-8")

            await asyncio.to_thread(_sync_write)

            log.info(
                "interview.state_saved",
                interview_id=state.interview_id,
                file_path=str(file_path),
            )

            return Result.ok(file_path)
        except (OSError, ValueError) as e:
            log.exception(
                "interview.state_save_failed",
                interview_id=state.interview_id,
                error=str(e),
            )
            return Result.err(
                ValidationError(
                    f"Failed to save interview state: {e}",
                    details={"interview_id": state.interview_id},
                )
            )

    async def load_state(self, interview_id: str) -> Result[InterviewState, ValidationError]:
        """Load interview state from disk.

        Uses file locking to prevent race conditions during concurrent access.
        The blocking file I/O is offloaded to a thread to avoid stalling the
        asyncio event loop.

        Args:
            interview_id: The interview ID to load.

        Returns:
            Result containing loaded state or ValidationError.
        """
        file_path = self._state_file_path(interview_id)

        if not file_path.exists():
            return Result.err(
                ValidationError(
                    f"Interview state not found: {interview_id}",
                    field="interview_id",
                    value=interview_id,
                )
            )

        try:

            def _sync_read() -> str:
                with _file_lock(file_path, exclusive=False):
                    return file_path.read_text(encoding="utf-8")

            content = await asyncio.to_thread(_sync_read)

            state = InterviewState.model_validate_json(content)

            log.info(
                "interview.state_loaded",
                interview_id=interview_id,
                rounds=len(state.rounds),
            )

            return Result.ok(state)
        except (OSError, ValueError) as e:
            log.exception(
                "interview.state_load_failed",
                interview_id=interview_id,
                error=str(e),
            )
            return Result.err(
                ValidationError(
                    f"Failed to load interview state: {e}",
                    field="interview_id",
                    value=interview_id,
                    details={"file_path": str(file_path)},
                )
            )

    def _build_system_prompt(self, state: InterviewState) -> str:
        """Build the system prompt for question generation.

        Args:
            state: Current interview state.

        Returns:
            The system prompt.
        """
        from ouroboros.agents.loader import load_agent_prompt

        round_info = f"Round {state.current_round_number}"

        base_prompt = load_agent_prompt("socratic-interviewer")

        prompt_initial_context = self._initial_context_for_system_prompt(state.initial_context)

        # For first round, add explicit instruction to start directly with a question
        if state.current_round_number == 1:
            dynamic_header = (
                f"You are an expert requirements engineer conducting a Socratic interview.\n\n"
                f"CRITICAL: Start your FIRST response with a DIRECT QUESTION about the project. "
                f'Do NOT introduce yourself. Do NOT say "I\'ll conduct" or "Let me ask". '
                f"Just ask a specific, clarifying question immediately.\n\n"
                f"This is {round_info}. Your ONLY job is to ask questions that reduce ambiguity.\n\n"
                f"Initial context: {prompt_initial_context}\n"
            )
        else:
            dynamic_header = (
                f"You are an expert requirements engineer conducting a Socratic interview.\n\n"
                f"This is {round_info}. Your ONLY job is to ask questions that reduce ambiguity.\n\n"
                f"Initial context: {prompt_initial_context}\n"
            )

        # Answer prefix hints — always present so the question generator
        # can interpret enriched answers regardless of brownfield status.
        dynamic_header += (
            "\n\nAnswer prefixes the caller may use:\n"
            "- [from-code]: Existing codebase state (factual, read from files).\n"
            "- [from-user]: Human decisions/judgments.\n"
            "- [from-research]: Externally researched information (API docs, pricing, compatibility)."
        )
        # Brownfield hint: main session handles code reading, MCP just asks questions
        if state.is_brownfield:
            dynamic_header += (
                "\n\nThis is a BROWNFIELD project. The caller (main session) has direct "
                "codebase access and will enrich answers with code context. Focus your "
                "questions on INTENT and DECISIONS, not on discovering what exists."
            )

        ambiguity_snapshot = self._build_ambiguity_snapshot_prompt(state)
        if ambiguity_snapshot:
            dynamic_header += f"\n\n{ambiguity_snapshot}"

        perspective_panel = self._build_perspective_panel_prompt(state)

        _OVERHEAD = 20  # newlines, ellipsis, separators

        # Preserve the dynamic header first; it contains the capped initial
        # context and first-turn instructions. Trim the optional panel/base
        # prompt before falling back to hard-truncating the header.
        available_after_header = self._MAX_SYSTEM_PROMPT_CHARS - len(dynamic_header) - _OVERHEAD
        if available_after_header <= 0:
            dynamic_header = dynamic_header[: self._MAX_SYSTEM_PROMPT_CHARS - _OVERHEAD]
            perspective_panel = ""
            base_budget = 0
        elif len(perspective_panel) > available_after_header:
            perspective_panel = perspective_panel[:available_after_header]
            base_budget = 0
        else:
            base_budget = available_after_header - len(perspective_panel)

        trimmed_base = base_prompt[:base_budget] if base_budget < len(base_prompt) else base_prompt
        full_prompt = f"{dynamic_header}\n{trimmed_base}\n\n{perspective_panel}"

        # Hard-truncate as final safety net
        if len(full_prompt) > self._MAX_SYSTEM_PROMPT_CHARS:
            full_prompt = full_prompt[: self._MAX_SYSTEM_PROMPT_CHARS]

        return full_prompt

    def _initial_context_for_system_prompt(self, initial_context: str) -> str:
        """Return the initial context portion safe to embed in system prompt."""
        if len(initial_context) <= self._MAX_INITIAL_CONTEXT_SYSTEM_CHARS:
            return initial_context
        return (
            initial_context[: self._MAX_INITIAL_CONTEXT_SYSTEM_CHARS]
            + "\n\n[Initial context continues in the first user message.]"
        )

    def _initial_context_overflow_message(self, initial_context: str) -> str:
        """Return overflow initial context as user-message content for round one."""
        if len(initial_context) <= self._MAX_INITIAL_CONTEXT_SYSTEM_CHARS:
            return ""
        overflow = initial_context[self._MAX_INITIAL_CONTEXT_SYSTEM_CHARS :]
        if len(overflow) > self._MAX_INITIAL_CONTEXT_OVERFLOW_CHARS:
            overflow = overflow[: self._MAX_INITIAL_CONTEXT_OVERFLOW_CHARS] + "..."
        return f"Additional initial context omitted from the system prompt:\n{overflow}"

    def _build_ambiguity_snapshot_prompt(self, state: InterviewState) -> str:
        """Build prompt context from the latest ambiguity snapshot."""
        if state.ambiguity_score is None:
            return ""

        from pydantic import ValidationError as PydanticValidationError

        from ouroboros.bigbang.ambiguity import (
            AMBIGUITY_THRESHOLD,
            AUTO_COMPLETE_STREAK_REQUIRED,
            SEED_CLOSER_ACTIVATION_THRESHOLD,
            AmbiguityScore,
            ScoreBreakdown,
            get_completion_floor_failures,
            get_milestone,
            get_next_milestone,
        )

        milestone, milestone_desc = get_milestone(state.ambiguity_score)
        next_ms = get_next_milestone(state.ambiguity_score)

        lines = [
            "## Current Ambiguity Snapshot",
            f"- Overall ambiguity: {state.ambiguity_score:.2f}",
            f"- Milestone: **{milestone.value.upper()}** — {milestone_desc}",
        ]
        if next_ms is not None:
            lines.append(
                f"- Next milestone: {next_ms[1].value} (<= {next_ms[0]:.1f}) — {next_ms[2]}"
            )
        lines.extend(
            [
                f"- Seed-ready threshold: {AMBIGUITY_THRESHOLD:.2f}",
                f"- Closure-mode threshold: {SEED_CLOSER_ACTIVATION_THRESHOLD:.2f}",
                (
                    "- Seed-ready now: yes"
                    if state.ambiguity_score <= AMBIGUITY_THRESHOLD
                    else "- Seed-ready now: no"
                ),
                (
                    "- Closure mode active: yes"
                    if state.ambiguity_score <= SEED_CLOSER_ACTIVATION_THRESHOLD
                    else "- Closure mode active: no"
                ),
                (
                    "- Completion candidate streak: "
                    f"{state.completion_candidate_streak}/{AUTO_COMPLETE_STREAK_REQUIRED}"
                ),
            ]
        )

        reconstructed_score: AmbiguityScore | None = None
        if isinstance(state.ambiguity_breakdown, dict):
            try:
                reconstructed_score = AmbiguityScore(
                    overall_score=state.ambiguity_score,
                    breakdown=ScoreBreakdown.model_validate(state.ambiguity_breakdown),
                )
            except PydanticValidationError:
                reconstructed_score = None

            weakest_components: list[tuple[float, str, str]] = []
            for payload in state.ambiguity_breakdown.values():
                if not isinstance(payload, dict):
                    continue
                clarity = payload.get("clarity_score")
                if clarity is None:
                    continue
                weakest_components.append(
                    (
                        float(clarity),
                        str(payload.get("name", "Unknown")),
                        str(payload.get("justification", "")),
                    )
                )

            weakest_components.sort(key=lambda item: item[0])
            for clarity, name, justification in weakest_components[:2]:
                lines.append(f"- Weakest area: {name} ({clarity:.2f} clarity)")
                if justification:
                    lines.append(f"  Reason: {justification}")

        if reconstructed_score is not None:
            floor_failures = get_completion_floor_failures(
                reconstructed_score,
                is_brownfield=state.is_brownfield,
            )
            if floor_failures:
                lines.append(f"- Completion floors unmet: {'; '.join(floor_failures)}")
            else:
                lines.append("- Completion floors: passed")

        lines.append(
            "- Use this snapshot to drill into the weakest area until closure mode is active."
        )
        lines.append(
            "- A single seed-ready score is not enough to end the interview; require sustained clarity before asking a closure question."
        )
        return "\n".join(lines)

    def _select_perspectives(self, state: InterviewState) -> tuple[InterviewPerspective, ...]:
        """Choose the active perspective panel for the current round."""
        from ouroboros.bigbang.ambiguity import SEED_CLOSER_ACTIVATION_THRESHOLD

        perspectives: list[InterviewPerspective] = [InterviewPerspective.BREADTH_KEEPER]

        if state.current_round_number <= 2:
            perspectives.extend(
                [
                    InterviewPerspective.RESEARCHER,
                    InterviewPerspective.SIMPLIFIER,
                ]
            )
        elif state.current_round_number <= 5:
            perspectives.extend(
                [
                    InterviewPerspective.RESEARCHER,
                    InterviewPerspective.SIMPLIFIER,
                    InterviewPerspective.ARCHITECT,
                ]
            )
        else:
            perspectives.extend(
                [
                    InterviewPerspective.RESEARCHER,
                    InterviewPerspective.SIMPLIFIER,
                    InterviewPerspective.ARCHITECT,
                ]
            )

        if (
            state.ambiguity_score is not None
            and state.ambiguity_score <= SEED_CLOSER_ACTIVATION_THRESHOLD
        ):
            perspectives.append(InterviewPerspective.SEED_CLOSER)

        if state.is_brownfield and InterviewPerspective.ARCHITECT not in perspectives:
            perspectives.append(InterviewPerspective.ARCHITECT)

        # Preserve declaration order while removing duplicates.
        return tuple(dict.fromkeys(perspectives))

    def _build_perspective_panel_prompt(self, state: InterviewState) -> str:
        """Build instructions for the internal perspective panel."""
        strategies = _load_interview_perspective_strategies()
        sections = [
            "## Perspective Panel",
            "Before asking the next question, silently consult these internal agents.",
            "They are planning aids only. Emit exactly one final question to the user.",
            "",
        ]

        for perspective in self._select_perspectives(state):
            strategy = strategies[perspective]
            sections.append(f"### {perspective.value}")
            sections.append(f"Focus: {strategy.system_prompt}")
            if strategy.approach_instructions:
                sections.append("Approach cues:")
                sections.extend(f"- {item}" for item in strategy.approach_instructions[:3])
            if strategy.question_templates:
                sections.append("Question patterns:")
                sections.extend(f"- {item}" for item in strategy.question_templates[:2])
            sections.append("")

        sections.extend(
            [
                "## Panel Synthesis Rules",
                "- Keep independent ambiguity tracks visible instead of collapsing onto one favorite subtopic.",
                "- If one file, abstraction, or bug has dominated several rounds, zoom back out before going deeper.",
                "- Preserve both implementation and written-output requirements when the user asked for both.",
                "- Prefer breadth recap questions when multiple unresolved tracks still exist.",
                "- Only ask a closure question when closure mode is active; otherwise keep drilling into the weakest area.",
                "- Even when the score is seed-ready, do not end the interview on the first low-ambiguity turn.",
            ]
        )

        return "\n".join(sections)

    # Agent SDK CLI can return empty responses when the combined prompt
    # (system_prompt + conversation history) exceeds an internal threshold.
    # Cap each user response to keep the total prompt within safe limits.
    _MAX_USER_RESPONSE_CHARS = 800

    def _build_conversation_history(self, state: InterviewState) -> list[Message]:
        """Build conversation history from completed rounds.

        Long user responses are truncated to prevent Agent SDK CLI from
        returning empty responses due to prompt size.

        Args:
            state: Current interview state.

        Returns:
            List of messages representing the conversation.
        """
        messages: list[Message] = []

        overflow = self._initial_context_overflow_message(state.initial_context)
        if overflow:
            messages.append(Message(role=MessageRole.USER, content=overflow))

        for round_data in state.rounds:
            messages.append(Message(role=MessageRole.ASSISTANT, content=round_data.question))
            if round_data.user_response:
                response = round_data.user_response
                if len(response) > self._MAX_USER_RESPONSE_CHARS:
                    response = response[: self._MAX_USER_RESPONSE_CHARS] + "..."
                messages.append(Message(role=MessageRole.USER, content=response))

        return messages

    async def complete_interview(
        self, state: InterviewState
    ) -> Result[InterviewState, ValidationError]:
        """Mark the interview as completed.

        Args:
            state: Current interview state.

        Returns:
            Result containing updated state or ValidationError.
        """
        if state.status == InterviewStatus.COMPLETED:
            return Result.ok(state)

        state.status = InterviewStatus.COMPLETED
        state.mark_updated()

        log.info(
            "interview.completed",
            interview_id=state.interview_id,
            total_rounds=len(state.rounds),
        )

        return Result.ok(state)

    async def list_interviews(self) -> list[dict[str, Any]]:
        """List all interview sessions in the state directory.

        Returns:
            List of interview metadata dictionaries.
        """
        interviews = []

        for file_path in self.state_dir.glob("interview_*.json"):
            try:
                content = file_path.read_text(encoding="utf-8")
                state = InterviewState.model_validate_json(content)
                interviews.append(
                    {
                        "interview_id": state.interview_id,
                        "status": state.status,
                        "rounds": len(state.rounds),
                        "created_at": state.created_at,
                        "updated_at": state.updated_at,
                    }
                )
            except (OSError, ValueError) as e:
                log.warning(
                    "interview.list_failed_for_file",
                    file_path=str(file_path),
                    error=str(e),
                )
                continue

        return sorted(interviews, key=lambda x: x["updated_at"], reverse=True)
