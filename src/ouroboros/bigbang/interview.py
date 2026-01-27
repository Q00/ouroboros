"""Interactive interview engine for requirement clarification.

This module implements the interview protocol that refines vague ideas into
clear requirements through iterative questioning (max 10 rounds).
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
import fcntl
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
import structlog

from ouroboros.core.errors import ProviderError, ValidationError
from ouroboros.core.types import Result
from ouroboros.providers.base import (
    CompletionConfig,
    LLMAdapter,
    Message,
    MessageRole,
)


@contextmanager
def _file_lock(file_path: Path, exclusive: bool = True) -> Iterator[None]:
    """Context manager for file locking to prevent race conditions.

    Args:
        file_path: Path to the file to lock.
        exclusive: If True, use exclusive lock (for writes).
                   If False, use shared lock (for reads).

    Yields:
        None when lock is acquired.
    """
    lock_path = file_path.with_suffix(file_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with open(lock_path, "w") as lock_file:
        lock_type = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        try:
            fcntl.flock(lock_file.fileno(), lock_type)
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

log = structlog.get_logger()

MAX_INTERVIEW_ROUNDS = 10
# Default model moved to config.models.ClarificationConfig.default_model
_FALLBACK_MODEL = "openrouter/google/gemini-2.0-flash-001"


class InterviewStatus(StrEnum):
    """Status of the interview process."""

    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    ABORTED = "aborted"


class InterviewRound(BaseModel):
    """A single round of interview questions and responses.

    Attributes:
        round_number: 1-based round number (1 to MAX_INTERVIEW_ROUNDS).
        question: The question asked by the system.
        user_response: The user's response (None if not yet answered).
        timestamp: When this round was created.
    """

    round_number: int = Field(ge=1, le=MAX_INTERVIEW_ROUNDS)
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
    """

    interview_id: str
    status: InterviewStatus = InterviewStatus.IN_PROGRESS
    rounds: list[InterviewRound] = Field(default_factory=list)
    initial_context: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def current_round_number(self) -> int:
        """Get the current round number (1-based)."""
        return len(self.rounds) + 1

    @property
    def is_complete(self) -> bool:
        """Check if interview has reached max rounds or is marked complete."""
        return (
            self.status == InterviewStatus.COMPLETED
            or len(self.rounds) >= MAX_INTERVIEW_ROUNDS
        )

    def mark_updated(self) -> None:
        """Update the updated_at timestamp."""
        self.updated_at = datetime.now(UTC)


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
    model: str = _FALLBACK_MODEL
    temperature: float = 0.7
    max_tokens: int = 2048

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
        self, initial_context: str, interview_id: str | None = None
    ) -> Result[InterviewState, ValidationError]:
        """Start a new interview session.

        Args:
            initial_context: The initial context or idea provided by the user.
            interview_id: Optional interview ID (generated if not provided).

        Returns:
            Result containing the new InterviewState or ValidationError.
        """
        if not initial_context.strip():
            return Result.err(
                ValidationError("Initial context cannot be empty", field="initial_context")
            )

        if interview_id is None:
            interview_id = f"interview_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}"

        state = InterviewState(
            interview_id=interview_id,
            initial_context=initial_context,
        )

        log.info(
            "interview.started",
            interview_id=interview_id,
            initial_context_length=len(initial_context),
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
        if not user_response.strip():
            return Result.err(
                ValidationError("User response cannot be empty", field="user_response")
            )

        if state.is_complete:
            return Result.err(
                ValidationError(
                    "Cannot record response - interview is complete",
                    field="status",
                    value=state.status,
                )
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

        # Check if we've reached max rounds
        if len(state.rounds) >= MAX_INTERVIEW_ROUNDS:
            state.status = InterviewStatus.COMPLETED
            log.info(
                "interview.max_rounds_reached",
                interview_id=state.interview_id,
                total_rounds=len(state.rounds),
            )

        return Result.ok(state)

    async def save_state(
        self, state: InterviewState
    ) -> Result[Path, ValidationError]:
        """Persist interview state to disk.

        Uses file locking to prevent race conditions during concurrent access.

        Args:
            state: The interview state to save.

        Returns:
            Result containing path to saved file or ValidationError.
        """
        try:
            file_path = self._state_file_path(state.interview_id)
            state.mark_updated()

            # Use file locking to prevent race conditions
            with _file_lock(file_path, exclusive=True):
                # Write state as JSON
                content = state.model_dump_json(indent=2)
                file_path.write_text(content, encoding="utf-8")

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

    async def load_state(
        self, interview_id: str
    ) -> Result[InterviewState, ValidationError]:
        """Load interview state from disk.

        Uses file locking to prevent race conditions during concurrent access.

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
            # Use shared lock for reading
            with _file_lock(file_path, exclusive=False):
                content = file_path.read_text(encoding="utf-8")

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
        round_info = f"Round {state.current_round_number} of {MAX_INTERVIEW_ROUNDS}"

        return f"""You are an expert requirements engineer conducting an interview to refine vague ideas into clear, executable requirements.

This is {round_info}. Your goal is to reduce ambiguity and gather concrete details.

Initial context: {state.initial_context}

Guidelines:
- Ask ONE focused question per round
- Target the biggest source of ambiguity
- Build on previous responses
- Be specific and actionable
- Keep questions concise and clear

Generate the next question to reduce ambiguity."""

    def _build_conversation_history(
        self, state: InterviewState
    ) -> list[Message]:
        """Build conversation history from completed rounds.

        Args:
            state: Current interview state.

        Returns:
            List of messages representing the conversation.
        """
        messages: list[Message] = []

        for round_data in state.rounds:
            messages.append(
                Message(role=MessageRole.ASSISTANT, content=round_data.question)
            )
            if round_data.user_response:
                messages.append(
                    Message(role=MessageRole.USER, content=round_data.user_response)
                )

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
