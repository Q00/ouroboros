"""Unit tests for ouroboros.bigbang.interview module."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.bigbang.interview import (
    InterviewEngine,
    InterviewRound,
    InterviewState,
    InterviewStatus,
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
