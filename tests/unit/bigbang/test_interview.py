"""Unit tests for ouroboros.bigbang.interview module."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.bigbang.interview import (
    INITIAL_CONTEXT_SUMMARY_QUESTION,
    MAX_PROMPT_SAFE_INITIAL_CONTEXT_CHARS,
    InterviewEngine,
    InterviewRound,
    InterviewState,
    InterviewStatus,
    prompt_safe_initial_context,
)
from ouroboros.core.errors import ProviderError, ValidationError
from ouroboros.core.types import Result
from ouroboros.providers.base import (
    CompletionResponse,
    MessageRole,
    UsageInfo,
)


def create_mock_completion_response(
    content: str = "What is your target audience?",
    model: str = "claude-opus-4-6",
) -> CompletionResponse:
    """Create a mock completion response."""
    return CompletionResponse(
        content=content,
        model=model,
        usage=UsageInfo(prompt_tokens=100, completion_tokens=50, total_tokens=150),
        finish_reason="stop",
    )


class TestInterviewState:
    """Test InterviewState model."""

    def test_initial_state(self) -> None:
        """InterviewState initializes with correct defaults."""
        state = InterviewState(interview_id="test_001")

        assert state.interview_id == "test_001"
        assert state.status == InterviewStatus.IN_PROGRESS
        assert state.rounds == []
        assert state.initial_context == ""
        assert state.current_round_number == 1
        assert not state.is_complete

    def test_current_round_number_increments(self) -> None:
        """current_round_number increments with each round."""
        state = InterviewState(interview_id="test_001")

        assert state.current_round_number == 1

        state.rounds.append(
            InterviewRound(
                round_number=1,
                question="Q1",
                user_response="A1",
            )
        )
        assert state.current_round_number == 2

        state.rounds.append(
            InterviewRound(
                round_number=2,
                question="Q2",
                user_response="A2",
            )
        )
        assert state.current_round_number == 3

    def test_is_complete_when_status_completed(self) -> None:
        """is_complete returns True when status is COMPLETED."""
        state = InterviewState(
            interview_id="test_001",
            status=InterviewStatus.COMPLETED,
        )

        assert state.is_complete

    def test_is_complete_only_checks_status(self) -> None:
        """is_complete only returns True when status is COMPLETED (user-controlled)."""
        state = InterviewState(interview_id="test_001")

        # Add many rounds - should NOT auto-complete
        for i in range(20):
            state.rounds.append(
                InterviewRound(
                    round_number=i + 1,
                    question=f"Q{i + 1}",
                    user_response=f"A{i + 1}",
                )
            )

        # Still not complete - user must explicitly complete
        assert not state.is_complete
        assert len(state.rounds) == 20

        # Only complete when status is set
        state.status = InterviewStatus.COMPLETED
        assert state.is_complete

    def test_mark_updated(self) -> None:
        """mark_updated updates the updated_at timestamp."""
        state = InterviewState(interview_id="test_001")
        original_updated_at = state.updated_at

        # Ensure time difference
        import time

        time.sleep(0.01)

        state.mark_updated()

        assert state.updated_at > original_updated_at

    def test_serialization(self) -> None:
        """InterviewState can be serialized and deserialized."""
        state = InterviewState(
            interview_id="test_001",
            initial_context="Build a CLI tool",
            status=InterviewStatus.IN_PROGRESS,
            ambiguity_score=0.18,
            ambiguity_breakdown={
                "goal_clarity": {
                    "name": "goal_clarity",
                    "clarity_score": 0.9,
                    "weight": 0.4,
                    "justification": "Clear goal",
                },
                "constraint_clarity": {
                    "name": "constraint_clarity",
                    "clarity_score": 0.8,
                    "weight": 0.3,
                    "justification": "Mostly clear constraints",
                },
                "success_criteria_clarity": {
                    "name": "success_criteria_clarity",
                    "clarity_score": 0.75,
                    "weight": 0.3,
                    "justification": "Success criteria are measurable",
                },
            },
        )
        state.rounds.append(
            InterviewRound(
                round_number=1,
                question="What problem does it solve?",
                user_response="Task management",
            )
        )

        # Serialize
        json_data = state.model_dump_json()

        # Deserialize
        restored = InterviewState.model_validate_json(json_data)

        assert restored.interview_id == state.interview_id
        assert restored.initial_context == state.initial_context
        assert restored.status == state.status
        assert len(restored.rounds) == 1
        assert restored.rounds[0].question == "What problem does it solve?"
        assert restored.rounds[0].user_response == "Task management"
        assert restored.ambiguity_score == 0.18
        assert restored.ambiguity_breakdown == state.ambiguity_breakdown

    def test_clear_stored_ambiguity(self) -> None:
        """Stored ambiguity snapshots can be invalidated after interview changes."""
        state = InterviewState(
            interview_id="test_001",
            ambiguity_score=0.12,
            ambiguity_breakdown={"goal_clarity": {"name": "goal_clarity"}},
        )

        state.clear_stored_ambiguity()

        assert state.ambiguity_score is None
        assert state.ambiguity_breakdown is None


class TestInterviewRound:
    """Test InterviewRound model."""

    def test_round_validation_min(self) -> None:
        """InterviewRound validates minimum round number."""
        with pytest.raises(ValueError):
            InterviewRound(
                round_number=0,
                question="Invalid round",
            )

    def test_round_accepts_high_numbers(self) -> None:
        """InterviewRound accepts high round numbers (no max limit)."""
        # No upper limit - user controls when to stop
        round_data = InterviewRound(
            round_number=100,
            question="Round 100 question",
        )
        assert round_data.round_number == 100

    def test_valid_round_numbers(self) -> None:
        """InterviewRound accepts valid round numbers (1 and above)."""
        for i in range(1, 25):  # Test up to 25 rounds
            round_data = InterviewRound(round_number=i, question=f"Q{i}")
            assert round_data.round_number == i


class TestInterviewEngineInit:
    """Test InterviewEngine initialization."""

    def test_init_creates_state_dir(self, tmp_path: Path) -> None:
        """InterviewEngine creates state directory on initialization."""
        state_dir = tmp_path / "interviews"
        assert not state_dir.exists()

        mock_adapter = MagicMock()
        InterviewEngine(llm_adapter=mock_adapter, state_dir=state_dir)

        assert state_dir.exists()
        assert state_dir.is_dir()

    def test_default_state_dir(self) -> None:
        """InterviewEngine uses default state directory."""
        mock_adapter = MagicMock()
        engine = InterviewEngine(llm_adapter=mock_adapter)

        expected_dir = Path.home() / ".ouroboros" / "data"
        assert engine.state_dir == expected_dir


class TestInterviewEngineStartInterview:
    """Test InterviewEngine.start_interview method."""

    @pytest.mark.asyncio
    async def test_start_with_context(self) -> None:
        """start_interview creates new state with provided context."""
        mock_adapter = MagicMock()
        engine = InterviewEngine(llm_adapter=mock_adapter)

        result = await engine.start_interview("Build a task manager")

        assert result.is_ok
        state = result.value
        assert state.interview_id.startswith("interview_")
        assert state.initial_context == "Build a task manager"
        assert state.status == InterviewStatus.IN_PROGRESS
        assert len(state.rounds) == 0

    @pytest.mark.asyncio
    async def test_start_with_custom_id(self) -> None:
        """start_interview accepts custom interview ID."""
        mock_adapter = MagicMock()
        engine = InterviewEngine(llm_adapter=mock_adapter)

        result = await engine.start_interview(
            "Build a task manager",
            interview_id="custom_id_123",
        )

        assert result.is_ok
        state = result.value
        assert state.interview_id == "custom_id_123"

    @pytest.mark.asyncio
    async def test_start_with_empty_context(self) -> None:
        """start_interview rejects empty context."""
        mock_adapter = MagicMock()
        engine = InterviewEngine(llm_adapter=mock_adapter)

        result = await engine.start_interview("")

        assert result.is_err
        error = result.error
        assert isinstance(error, ValidationError)
        assert error.field == "initial_context"

    @pytest.mark.asyncio
    async def test_start_with_whitespace_context(self) -> None:
        """start_interview rejects whitespace-only context."""
        mock_adapter = MagicMock()
        engine = InterviewEngine(llm_adapter=mock_adapter)

        result = await engine.start_interview("   \n\t  ")

        assert result.is_err
        assert isinstance(result.error, ValidationError)


class TestInterviewEngineAskNextQuestion:
    """Test InterviewEngine.ask_next_question method."""

    @pytest.mark.asyncio
    async def test_ask_first_question(self) -> None:
        """ask_next_question generates first question."""
        mock_adapter = MagicMock()
        mock_adapter.complete = AsyncMock(return_value=Result.ok(create_mock_completion_response()))

        engine = InterviewEngine(llm_adapter=mock_adapter)
        state = InterviewState(
            interview_id="test_001",
            initial_context="Build a CLI tool",
        )

        result = await engine.ask_next_question(state)

        assert result.is_ok
        question = result.value
        assert isinstance(question, str)
        assert len(question) > 0
        mock_adapter.complete.assert_called_once()

    @pytest.mark.asyncio
    async def test_ask_question_includes_context(self) -> None:
        """ask_next_question includes initial context in prompt."""
        mock_adapter = MagicMock()
        mock_adapter.complete = AsyncMock(return_value=Result.ok(create_mock_completion_response()))

        engine = InterviewEngine(llm_adapter=mock_adapter)
        state = InterviewState(
            interview_id="test_001",
            initial_context="Build a task manager",
        )

        await engine.ask_next_question(state)

        # Check that complete was called with messages containing the context
        call_args = mock_adapter.complete.call_args
        messages = call_args[0][0]
        system_message = messages[0]

        assert system_message.role == MessageRole.SYSTEM
        assert "Build a task manager" in system_message.content

    @pytest.mark.asyncio
    @pytest.mark.parametrize("context_length", [2500, 3500])
    async def test_long_initial_context_stays_below_cli_failure_cap(
        self, context_length: int
    ) -> None:
        """Long initial_context is split so the system prompt stays below 3500 chars."""
        mock_adapter = MagicMock()
        engine = InterviewEngine(llm_adapter=mock_adapter)

        async def _complete(messages, _config):
            total_prompt_chars = sum(len(message.content) for message in messages)
            if total_prompt_chars > engine._MAX_TOTAL_PROMPT_CHARS:
                return Result.err(
                    ProviderError(
                        "Command failed with exit code 1. Check stderr output for details"
                    )
                )
            return Result.ok(create_mock_completion_response())

        mock_adapter.complete = AsyncMock(side_effect=_complete)
        state = InterviewState(
            interview_id=f"test_long_context_{context_length}",
            initial_context="X" * context_length,
        )

        result = await engine.ask_next_question(state)

        assert result.is_ok
        messages = mock_adapter.complete.call_args[0][0]
        assert len(messages[0].content) <= engine._MAX_SYSTEM_PROMPT_CHARS
        assert sum(len(message.content) for message in messages) <= engine._MAX_TOTAL_PROMPT_CHARS
        assert "Initial context continues in the first user message" in messages[0].content
        assert messages[1].role == MessageRole.USER
        assert "Additional initial context omitted" in messages[1].content

    @pytest.mark.asyncio
    async def test_long_initial_context_overflow_remains_after_first_round(self) -> None:
        """Overflow initial_context remains present in later stateless requests."""
        mock_adapter = MagicMock()
        mock_adapter.complete = AsyncMock(return_value=Result.ok(create_mock_completion_response()))

        engine = InterviewEngine(llm_adapter=mock_adapter)
        state = InterviewState(
            interview_id="test_long_context_round_2",
            initial_context="X" * 3500,
        )
        state.rounds.append(
            InterviewRound(
                round_number=1,
                question="What is the main goal?",
                user_response="Ship the feature",
            )
        )

        result = await engine.ask_next_question(state)

        assert result.is_ok
        messages = mock_adapter.complete.call_args[0][0]
        assert len(messages[0].content) <= engine._MAX_SYSTEM_PROMPT_CHARS
        assert sum(len(message.content) for message in messages) <= engine._MAX_TOTAL_PROMPT_CHARS
        assert messages[1].role == MessageRole.USER
        assert "Additional initial context omitted" in messages[1].content

    @pytest.mark.asyncio
    async def test_very_long_initial_context_is_rejected_before_prompting(self) -> None:
        """Persisted long initial_context asks for a summary instead of failing."""
        mock_adapter = MagicMock()
        mock_adapter.complete = AsyncMock(return_value=Result.ok(create_mock_completion_response()))

        engine = InterviewEngine(llm_adapter=mock_adapter)
        initial_context = ("A" * 49_990) + "TAIL_MARKER"
        state = InterviewState(
            interview_id="test_very_long_context",
            initial_context=initial_context,
        )

        result = await engine.ask_next_question(state)

        assert result.is_ok
        assert result.value == INITIAL_CONTEXT_SUMMARY_QUESTION
        mock_adapter.complete.assert_not_called()

    @pytest.mark.asyncio
    async def test_start_accepts_very_long_initial_context_for_summary_recovery(self) -> None:
        """start_interview remains backward-compatible with security limits."""
        mock_adapter = MagicMock()
        engine = InterviewEngine(llm_adapter=mock_adapter)

        result = await engine.start_interview(("A" * 4_000) + "TAIL_MARKER")

        assert result.is_ok
        assert result.value.initial_context.endswith("TAIL_MARKER")

    def test_prompt_safe_initial_context_uses_summary_round(self) -> None:
        """Shared prompt-safe context helper uses the recorded user summary."""
        state = InterviewState(
            interview_id="test_summary_context",
            initial_context=("A" * 4_000) + "TAIL_MARKER",
        )
        state.rounds.append(
            InterviewRound(
                round_number=1,
                question=INITIAL_CONTEXT_SUMMARY_QUESTION,
                user_response="Short project summary",
            )
        )

        assert prompt_safe_initial_context(state) == "Short project summary"

    def test_prompt_safe_initial_context_caps_long_summary_round(self) -> None:
        """Shared prompt-safe context helper caps oversized recorded summaries."""
        state = InterviewState(
            interview_id="test_long_summary_context",
            initial_context=("A" * 4_000) + "ORIGINAL_TAIL",
        )
        state.rounds.append(
            InterviewRound(
                round_number=1,
                question=INITIAL_CONTEXT_SUMMARY_QUESTION,
                user_response=("B" * 4_000) + "SUMMARY_TAIL",
            )
        )

        prompt_context = prompt_safe_initial_context(state)

        assert len(prompt_context) <= MAX_PROMPT_SAFE_INITIAL_CONTEXT_CHARS
        assert "Context truncated for prompt safety" in prompt_context
        assert "ORIGINAL_TAIL" not in prompt_context
        assert "SUMMARY_TAIL" not in prompt_context

    @pytest.mark.asyncio
    async def test_completed_long_context_requests_summary_recovery(self) -> None:
        """Completed long-context interviews can still ask for the required summary."""
        mock_adapter = MagicMock()
        mock_adapter.complete = AsyncMock()
        engine = InterviewEngine(llm_adapter=mock_adapter)
        state = InterviewState(
            interview_id="test_completed_legacy_context",
            initial_context=("A" * 4_000) + "ORIGINAL_TAIL",
            status=InterviewStatus.COMPLETED,
        )

        result = await engine.ask_next_question(state)

        assert result.is_ok
        assert result.value == INITIAL_CONTEXT_SUMMARY_QUESTION
        mock_adapter.complete.assert_not_called()

    @pytest.mark.asyncio
    async def test_long_history_stays_under_total_prompt_cap(self) -> None:
        """Later rounds trim retained history so the full request stays safe."""
        mock_adapter = MagicMock()
        mock_adapter.complete = AsyncMock(return_value=Result.ok(create_mock_completion_response()))

        engine = InterviewEngine(llm_adapter=mock_adapter)
        state = InterviewState(
            interview_id="test_long_history",
            initial_context=("X" * 3489) + "TAIL_MARKER",
        )
        for i in range(8):
            state.rounds.append(
                InterviewRound(
                    round_number=i + 1,
                    question=f"What detail matters next? {i}",
                    user_response="Y" * 800,
                )
            )

        result = await engine.ask_next_question(state)

        assert result.is_ok
        messages = mock_adapter.complete.call_args[0][0]
        prompt_content = "\n".join(message.content for message in messages)
        assert sum(len(message.content) for message in messages) <= engine._MAX_TOTAL_PROMPT_CHARS
        assert "Additional initial context omitted" in prompt_content
        assert "TAIL_MARKER" in prompt_content

    @pytest.mark.asyncio
    async def test_ask_question_with_history(self) -> None:
        """ask_next_question includes conversation history."""
        mock_adapter = MagicMock()
        mock_adapter.complete = AsyncMock(return_value=Result.ok(create_mock_completion_response()))

        engine = InterviewEngine(llm_adapter=mock_adapter)
        state = InterviewState(
            interview_id="test_001",
            initial_context="Build a CLI tool",
        )
        state.rounds.append(
            InterviewRound(
                round_number=1,
                question="What problem does it solve?",
                user_response="Task management",
            )
        )

        await engine.ask_next_question(state)

        call_args = mock_adapter.complete.call_args
        messages = call_args[0][0]

        # Should have: system + Q1 + A1
        assert len(messages) == 3
        assert messages[1].role == MessageRole.ASSISTANT
        assert messages[1].content == "What problem does it solve?"
        assert messages[2].role == MessageRole.USER
        assert messages[2].content == "Task management"

    @pytest.mark.asyncio
    async def test_ask_question_when_complete(self) -> None:
        """ask_next_question returns error when interview is complete."""
        mock_adapter = MagicMock()
        engine = InterviewEngine(llm_adapter=mock_adapter)

        state = InterviewState(
            interview_id="test_001",
            status=InterviewStatus.COMPLETED,
        )

        result = await engine.ask_next_question(state)

        assert result.is_err
        error = result.error
        assert isinstance(error, ValidationError)
        assert error.field == "status"

    @pytest.mark.asyncio
    async def test_ask_question_provider_error(self) -> None:
        """ask_next_question propagates provider errors."""
        mock_adapter = MagicMock()
        provider_error = ProviderError("Rate limit exceeded", provider="openai")
        mock_adapter.complete = AsyncMock(return_value=Result.err(provider_error))

        engine = InterviewEngine(llm_adapter=mock_adapter)
        state = InterviewState(
            interview_id="test_001",
            initial_context="Build a CLI tool",
        )

        result = await engine.ask_next_question(state)

        assert result.is_err
        assert result.error == provider_error


class TestInterviewEngineRecordResponse:
    """Test InterviewEngine.record_response method."""

    @pytest.mark.asyncio
    async def test_record_response(self) -> None:
        """record_response adds round to state."""
        mock_adapter = MagicMock()
        engine = InterviewEngine(llm_adapter=mock_adapter)

        state = InterviewState(
            interview_id="test_001",
            initial_context="Build a CLI tool",
        )

        result = await engine.record_response(
            state,
            user_response="Task management and tracking",
            question="What problem does it solve?",
        )

        assert result.is_ok
        updated_state = result.value
        assert len(updated_state.rounds) == 1
        assert updated_state.rounds[0].round_number == 1
        assert updated_state.rounds[0].question == "What problem does it solve?"
        assert updated_state.rounds[0].user_response == "Task management and tracking"

    @pytest.mark.asyncio
    async def test_record_empty_response(self) -> None:
        """record_response rejects empty responses."""
        mock_adapter = MagicMock()
        engine = InterviewEngine(llm_adapter=mock_adapter)

        state = InterviewState(interview_id="test_001")

        result = await engine.record_response(
            state,
            user_response="",
            question="Test question",
        )

        assert result.is_err
        error = result.error
        assert isinstance(error, ValidationError)
        assert error.field == "user_response"

    @pytest.mark.asyncio
    async def test_record_response_when_complete(self) -> None:
        """record_response rejects responses when interview is complete."""
        mock_adapter = MagicMock()
        engine = InterviewEngine(llm_adapter=mock_adapter)

        state = InterviewState(
            interview_id="test_001",
            status=InterviewStatus.COMPLETED,
        )

        result = await engine.record_response(
            state,
            user_response="Some response",
            question="Test question",
        )

        assert result.is_err
        assert isinstance(result.error, ValidationError)

    @pytest.mark.asyncio
    async def test_record_response_reopens_completed_long_context_for_summary(self) -> None:
        """Completed long-context interviews can record the missing summary."""
        mock_adapter = MagicMock()
        engine = InterviewEngine(llm_adapter=mock_adapter)

        state = InterviewState(
            interview_id="test_completed_summary_repair",
            initial_context=("A" * 4_000) + "ORIGINAL_TAIL",
            status=InterviewStatus.COMPLETED,
        )

        result = await engine.record_response(
            state,
            user_response="Concise product summary",
            question=INITIAL_CONTEXT_SUMMARY_QUESTION,
        )

        assert result.is_ok
        assert state.status == InterviewStatus.IN_PROGRESS
        assert state.rounds[-1].question == INITIAL_CONTEXT_SUMMARY_QUESTION
        assert prompt_safe_initial_context(state) == "Concise product summary"

    @pytest.mark.asyncio
    async def test_record_response_does_not_auto_complete(self) -> None:
        """record_response does NOT auto-complete (user controls when to stop)."""
        mock_adapter = MagicMock()
        engine = InterviewEngine(llm_adapter=mock_adapter)

        state = InterviewState(interview_id="test_001")

        # Add many rounds
        for i in range(19):
            state.rounds.append(
                InterviewRound(
                    round_number=i + 1,
                    question=f"Q{i + 1}",
                    user_response=f"A{i + 1}",
                )
            )

        assert not state.is_complete

        # Add another round - should NOT auto-complete
        result = await engine.record_response(
            state,
            user_response="Round 20 answer",
            question="Round 20 question",
        )

        assert result.is_ok
        updated_state = result.value
        # Still NOT complete - user must explicitly complete
        assert not updated_state.is_complete
        assert updated_state.status == InterviewStatus.IN_PROGRESS
        assert len(updated_state.rounds) == 20


class TestInterviewEnginePersistence:
    """Test InterviewEngine state persistence."""

    @pytest.mark.asyncio
    async def test_save_state(self, tmp_path: Path) -> None:
        """save_state writes state to disk."""
        mock_adapter = MagicMock()
        engine = InterviewEngine(llm_adapter=mock_adapter, state_dir=tmp_path)

        state = InterviewState(
            interview_id="test_001",
            initial_context="Build a CLI tool",
        )
        state.rounds.append(
            InterviewRound(
                round_number=1,
                question="What problem?",
                user_response="Task management",
            )
        )

        result = await engine.save_state(state)

        assert result.is_ok
        file_path = result.value
        assert file_path.exists()
        assert file_path.name == "interview_test_001.json"

        # Verify content
        content = file_path.read_text()
        data = json.loads(content)
        assert data["interview_id"] == "test_001"
        assert data["initial_context"] == "Build a CLI tool"
        assert len(data["rounds"]) == 1

    @pytest.mark.asyncio
    async def test_load_state(self, tmp_path: Path) -> None:
        """load_state reads state from disk."""
        mock_adapter = MagicMock()
        engine = InterviewEngine(llm_adapter=mock_adapter, state_dir=tmp_path)

        # Create and save state
        original_state = InterviewState(
            interview_id="test_001",
            initial_context="Build a CLI tool",
        )
        original_state.rounds.append(
            InterviewRound(
                round_number=1,
                question="What problem?",
                user_response="Task management",
            )
        )

        await engine.save_state(original_state)

        # Load state
        result = await engine.load_state("test_001")

        assert result.is_ok
        loaded_state = result.value
        assert loaded_state.interview_id == "test_001"
        assert loaded_state.initial_context == "Build a CLI tool"
        assert len(loaded_state.rounds) == 1
        assert loaded_state.rounds[0].question == "What problem?"
        assert loaded_state.rounds[0].user_response == "Task management"

    @pytest.mark.asyncio
    async def test_load_nonexistent_state(self, tmp_path: Path) -> None:
        """load_state returns error for nonexistent state."""
        mock_adapter = MagicMock()
        engine = InterviewEngine(llm_adapter=mock_adapter, state_dir=tmp_path)

        result = await engine.load_state("nonexistent_id")

        assert result.is_err
        error = result.error
        assert isinstance(error, ValidationError)
        assert error.field == "interview_id"
        assert "not found" in error.message.lower()

    @pytest.mark.asyncio
    async def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        """State survives save/load roundtrip."""
        mock_adapter = MagicMock()
        engine = InterviewEngine(llm_adapter=mock_adapter, state_dir=tmp_path)

        # Create complex state
        state = InterviewState(
            interview_id="roundtrip_test",
            initial_context="Complex project",
            status=InterviewStatus.IN_PROGRESS,
        )

        for i in range(5):
            state.rounds.append(
                InterviewRound(
                    round_number=i + 1,
                    question=f"Question {i + 1}?",
                    user_response=f"Answer {i + 1}",
                )
            )

        # Save
        save_result = await engine.save_state(state)
        assert save_result.is_ok

        # Load
        load_result = await engine.load_state("roundtrip_test")
        assert load_result.is_ok

        loaded = load_result.value

        # Verify all data preserved
        assert loaded.interview_id == state.interview_id
        assert loaded.initial_context == state.initial_context
        assert loaded.status == state.status
        assert len(loaded.rounds) == len(state.rounds)

        for i, round_data in enumerate(loaded.rounds):
            original = state.rounds[i]
            assert round_data.round_number == original.round_number
            assert round_data.question == original.question
            assert round_data.user_response == original.user_response


class TestInterviewEngineCompleteInterview:
    """Test InterviewEngine.complete_interview method."""

    @pytest.mark.asyncio
    async def test_complete_interview(self) -> None:
        """complete_interview marks interview as completed."""
        mock_adapter = MagicMock()
        engine = InterviewEngine(llm_adapter=mock_adapter)

        state = InterviewState(
            interview_id="test_001",
            status=InterviewStatus.IN_PROGRESS,
        )

        result = await engine.complete_interview(state)

        assert result.is_ok
        completed_state = result.value
        assert completed_state.status == InterviewStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_complete_already_completed(self) -> None:
        """complete_interview is idempotent for completed interviews."""
        mock_adapter = MagicMock()
        engine = InterviewEngine(llm_adapter=mock_adapter)

        state = InterviewState(
            interview_id="test_001",
            status=InterviewStatus.COMPLETED,
        )

        result = await engine.complete_interview(state)

        assert result.is_ok
        assert result.value.status == InterviewStatus.COMPLETED


class TestInterviewEngineListInterviews:
    """Test InterviewEngine.list_interviews method."""

    @pytest.mark.asyncio
    async def test_list_empty_directory(self, tmp_path: Path) -> None:
        """list_interviews returns empty list for empty directory."""
        mock_adapter = MagicMock()
        engine = InterviewEngine(llm_adapter=mock_adapter, state_dir=tmp_path)

        interviews = await engine.list_interviews()

        assert interviews == []

    @pytest.mark.asyncio
    async def test_list_interviews(self, tmp_path: Path) -> None:
        """list_interviews returns all interview metadata."""
        mock_adapter = MagicMock()
        engine = InterviewEngine(llm_adapter=mock_adapter, state_dir=tmp_path)

        # Create multiple interviews
        for i in range(3):
            state = InterviewState(
                interview_id=f"test_{i:03d}",
                initial_context=f"Project {i}",
            )
            for j in range(i + 1):
                state.rounds.append(
                    InterviewRound(
                        round_number=j + 1,
                        question=f"Q{j + 1}",
                        user_response=f"A{j + 1}",
                    )
                )
            await engine.save_state(state)

        interviews = await engine.list_interviews()

        assert len(interviews) == 3

        # Verify metadata
        ids = [i["interview_id"] for i in interviews]
        assert "test_000" in ids
        assert "test_001" in ids
        assert "test_002" in ids

        # Check rounds count
        for interview in interviews:
            if interview["interview_id"] == "test_001":
                assert interview["rounds"] == 2
            elif interview["interview_id"] == "test_002":
                assert interview["rounds"] == 3

    @pytest.mark.asyncio
    async def test_list_interviews_sorted_by_updated(self, tmp_path: Path) -> None:
        """list_interviews sorts by updated_at descending."""
        mock_adapter = MagicMock()
        engine = InterviewEngine(llm_adapter=mock_adapter, state_dir=tmp_path)

        # Create interviews with different update times
        state1 = InterviewState(interview_id="old")
        await engine.save_state(state1)

        import time

        time.sleep(0.01)

        state2 = InterviewState(interview_id="new")
        await engine.save_state(state2)

        interviews = await engine.list_interviews()

        assert len(interviews) == 2
        assert interviews[0]["interview_id"] == "new"
        assert interviews[1]["interview_id"] == "old"


class TestInterviewEngineSystemPrompt:
    """Test InterviewEngine system prompt generation."""

    def test_system_prompt_includes_round_info(self) -> None:
        """_build_system_prompt includes current round number."""
        mock_adapter = MagicMock()
        engine = InterviewEngine(llm_adapter=mock_adapter)

        state = InterviewState(
            interview_id="test_001",
            initial_context="Build a CLI tool",
        )

        prompt = engine._build_system_prompt(state)

        # Now just shows "Round N" without max limit
        assert "Round 1" in prompt

    def test_system_prompt_includes_context(self) -> None:
        """_build_system_prompt includes initial context."""
        mock_adapter = MagicMock()
        engine = InterviewEngine(llm_adapter=mock_adapter)

        state = InterviewState(
            interview_id="test_001",
            initial_context="Build a task manager",
        )

        prompt = engine._build_system_prompt(state)

        assert "Build a task manager" in prompt

    def test_system_prompt_includes_live_ambiguity_snapshot(self) -> None:
        """_build_system_prompt includes the latest ambiguity snapshot when available."""
        mock_adapter = MagicMock()
        engine = InterviewEngine(llm_adapter=mock_adapter)

        state = InterviewState(
            interview_id="test_001",
            initial_context="Build a task manager",
            ambiguity_score=0.24,
            ambiguity_breakdown={
                "goal_clarity": {
                    "name": "Goal Clarity",
                    "clarity_score": 0.82,
                    "weight": 0.4,
                    "justification": "Goal is mostly clear.",
                },
                "constraint_clarity": {
                    "name": "Constraint Clarity",
                    "clarity_score": 0.61,
                    "weight": 0.3,
                    "justification": "Constraints need work.",
                },
                "success_criteria_clarity": {
                    "name": "Success Criteria Clarity",
                    "clarity_score": 0.73,
                    "weight": 0.3,
                    "justification": "Criteria are somewhat measurable.",
                },
            },
        )

        prompt = engine._build_system_prompt(state)

        assert "## Current Ambiguity Snapshot" in prompt
        assert "Overall ambiguity: 0.24" in prompt
        assert "Milestone:" in prompt
        assert "Weakest area: Constraint Clarity" in prompt
        assert "Constraints need work." in prompt

    def test_system_prompt_includes_perspective_panel(self) -> None:
        """_build_system_prompt includes the internal perspective panel."""
        mock_adapter = MagicMock()
        engine = InterviewEngine(llm_adapter=mock_adapter)

        state = InterviewState(
            interview_id="test_001",
            initial_context="Review a PR and decide what to implement",
        )

        prompt = engine._build_system_prompt(state)

        assert "## Perspective Panel" in prompt
        assert "### breadth-keeper" in prompt
        assert "### researcher" in prompt
        assert "### simplifier" in prompt

    def test_system_prompt_uses_seed_closer_when_closure_mode_is_active(self) -> None:
        """Closure mode should activate the seed-closer perspective."""
        mock_adapter = MagicMock()
        engine = InterviewEngine(llm_adapter=mock_adapter)

        state = InterviewState(
            interview_id="test_001",
            initial_context="Refine requirements",
            ambiguity_score=0.24,
            ambiguity_breakdown={
                "goal_clarity": {
                    "name": "Goal Clarity",
                    "clarity_score": 0.82,
                    "weight": 0.4,
                    "justification": "Goal is mostly clear.",
                },
                "constraint_clarity": {
                    "name": "Constraint Clarity",
                    "clarity_score": 0.70,
                    "weight": 0.3,
                    "justification": "Constraints are getting clearer.",
                },
                "success_criteria_clarity": {
                    "name": "Success Criteria Clarity",
                    "clarity_score": 0.76,
                    "weight": 0.3,
                    "justification": "Criteria are becoming measurable.",
                },
            },
        )

        prompt = engine._build_system_prompt(state)

        assert "### seed-closer" in prompt
        assert "Closure mode active: yes" in prompt

    def test_system_prompt_omits_seed_closer_when_closure_mode_is_inactive(self) -> None:
        """High ambiguity should keep the closure perspective disabled."""
        mock_adapter = MagicMock()
        engine = InterviewEngine(llm_adapter=mock_adapter)

        state = InterviewState(
            interview_id="test_001",
            initial_context="Refine requirements",
            rounds=[
                InterviewRound(round_number=1, question="Q1", user_response="A1"),
                InterviewRound(round_number=2, question="Q2", user_response="A2"),
                InterviewRound(round_number=3, question="Q3", user_response="A3"),
                InterviewRound(round_number=4, question="Q4", user_response="A4"),
                InterviewRound(round_number=5, question="Q5", user_response="A5"),
            ],
            ambiguity_score=0.41,
        )

        prompt = engine._build_system_prompt(state)

        assert "### seed-closer" not in prompt
        assert "Closure mode active: no" in prompt


class TestInterviewEngineConversationHistory:
    """Test InterviewEngine conversation history building."""

    def test_empty_history(self) -> None:
        """_build_conversation_history returns empty for no rounds."""
        mock_adapter = MagicMock()
        engine = InterviewEngine(llm_adapter=mock_adapter)

        state = InterviewState(interview_id="test_001")
        history = engine._build_conversation_history(state)

        assert history == []

    def test_history_with_rounds(self) -> None:
        """_build_conversation_history creates message pairs."""
        mock_adapter = MagicMock()
        engine = InterviewEngine(llm_adapter=mock_adapter)

        state = InterviewState(interview_id="test_001")
        state.rounds.append(
            InterviewRound(
                round_number=1,
                question="Q1",
                user_response="A1",
            )
        )
        state.rounds.append(
            InterviewRound(
                round_number=2,
                question="Q2",
                user_response="A2",
            )
        )

        history = engine._build_conversation_history(state)

        assert len(history) == 4
        assert history[0].role == MessageRole.ASSISTANT
        assert history[0].content == "Q1"
        assert history[1].role == MessageRole.USER
        assert history[1].content == "A1"
        assert history[2].role == MessageRole.ASSISTANT
        assert history[2].content == "Q2"
        assert history[3].role == MessageRole.USER
        assert history[3].content == "A2"


class TestInterviewEngineBrownfieldDetection:
    """Test brownfield auto-detection in start_interview."""

    @pytest.mark.asyncio
    async def test_start_interview_detects_brownfield(self, tmp_path: Path) -> None:
        """start_interview sets is_brownfield when cwd has config files."""
        (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n")

        mock_adapter = MagicMock()
        engine = InterviewEngine(llm_adapter=mock_adapter)

        result = await engine.start_interview("Add a REST endpoint", cwd=str(tmp_path))

        assert result.is_ok
        state = result.value
        assert state.is_brownfield is True
        assert state.codebase_paths == [{"path": str(tmp_path), "role": "primary"}]

    @pytest.mark.asyncio
    async def test_start_interview_no_cwd_stays_greenfield(self) -> None:
        """start_interview without cwd keeps is_brownfield=False."""
        mock_adapter = MagicMock()
        engine = InterviewEngine(llm_adapter=mock_adapter)

        result = await engine.start_interview("Build something new")

        assert result.is_ok
        assert result.value.is_brownfield is False

    @pytest.mark.asyncio
    async def test_start_interview_brownfield_no_exploration(self, tmp_path: Path) -> None:
        """start_interview detects brownfield but does NOT trigger exploration.

        In the new architecture, main session handles code reading.
        MCP only sets is_brownfield flag.
        """
        (tmp_path / "package.json").write_text('{"name":"demo"}')

        mock_adapter = MagicMock()
        engine = InterviewEngine(llm_adapter=mock_adapter)

        result = await engine.start_interview("Add a feature", cwd=str(tmp_path))

        assert result.is_ok
        state = result.value
        assert state.is_brownfield is True
        # No codebase_context populated (main session handles this)
        assert not state.codebase_context
        assert not state.explore_completed

    @pytest.mark.asyncio
    async def test_start_interview_empty_dir_stays_greenfield(self, tmp_path: Path) -> None:
        """start_interview with cwd pointing to empty dir stays greenfield."""
        mock_adapter = MagicMock()
        engine = InterviewEngine(llm_adapter=mock_adapter)

        result = await engine.start_interview("Build something", cwd=str(tmp_path))

        assert result.is_ok
        assert result.value.is_brownfield is False


class TestSystemPromptBrownfield:
    """Test brownfield system prompt injection."""

    def test_system_prompt_brownfield_round_1(self) -> None:
        """System prompt includes brownfield hint when is_brownfield is set."""
        mock_adapter = MagicMock()
        engine = InterviewEngine(llm_adapter=mock_adapter)

        state = InterviewState(
            interview_id="test_bf",
            initial_context="Add a REST endpoint",
            is_brownfield=True,
        )

        prompt = engine._build_system_prompt(state)

        # New architecture: no codebase_context stuffing, just a brownfield hint
        assert "BROWNFIELD" in prompt
        assert "INTENT" in prompt or "DECISIONS" in prompt
        assert "[from-code]" in prompt
        assert "### architect" in prompt

    def test_system_prompt_hard_cap_enforced(self) -> None:
        """Final prompt must never exceed _MAX_SYSTEM_PROMPT_CHARS (3500)."""
        mock_adapter = MagicMock()
        engine = InterviewEngine(llm_adapter=mock_adapter)

        # Create state with oversized context to blow past the cap
        state = InterviewState(
            interview_id="test_cap",
            initial_context="X" * 6000,
            is_brownfield=True,
            codebase_context="Y" * 3000,
        )

        prompt = engine._build_system_prompt(state)

        assert len(prompt) <= engine._MAX_SYSTEM_PROMPT_CHARS

    def test_system_prompt_cap_when_header_and_panel_exceed_budget(self) -> None:
        """Cap holds even when dynamic_header + perspective_panel alone exceed 3500."""
        mock_adapter = MagicMock()
        engine = InterviewEngine(llm_adapter=mock_adapter)

        # Brownfield with huge codebase_context inflates dynamic_header;
        # perspective panel also adds content. Together they can exceed 4800.
        state = InterviewState(
            interview_id="test_cap2",
            initial_context="Z" * 4000,
            is_brownfield=True,
            codebase_context="W" * 4000,
        )

        prompt = engine._build_system_prompt(state)

        assert len(prompt) <= engine._MAX_SYSTEM_PROMPT_CHARS
