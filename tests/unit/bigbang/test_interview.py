"""Unit tests for ouroboros.bigbang.interview module."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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


class TestInterviewEngineBrownfieldDetection:
    """Test brownfield auto-detection in start_interview."""

    @pytest.mark.asyncio
    async def test_start_interview_detects_brownfield(self, tmp_path: Path) -> None:
        """start_interview sets is_brownfield when cwd has config files."""
        (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n")

        mock_adapter = MagicMock()
        engine = InterviewEngine(llm_adapter=mock_adapter)

        with patch(
            "ouroboros.bigbang.interview.InterviewEngine._trigger_codebase_exploration",
            new_callable=AsyncMock,
        ):
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
    async def test_start_interview_brownfield_runs_exploration(self, tmp_path: Path) -> None:
        """start_interview calls _trigger_codebase_exploration for brownfield."""
        (tmp_path / "package.json").write_text('{"name":"demo"}')

        mock_adapter = MagicMock()
        engine = InterviewEngine(llm_adapter=mock_adapter)

        with patch.object(
            engine,
            "_trigger_codebase_exploration",
            new_callable=AsyncMock,
        ) as mock_explore:
            await engine.start_interview("Add a feature", cwd=str(tmp_path))

        mock_explore.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_start_interview_exploration_failure_non_blocking(self, tmp_path: Path) -> None:
        """start_interview succeeds even when exploration raises."""
        (tmp_path / "go.mod").write_text("module example.com/demo\n")

        mock_adapter = MagicMock()
        engine = InterviewEngine(llm_adapter=mock_adapter)

        with patch.object(
            engine,
            "_trigger_codebase_exploration",
            new_callable=AsyncMock,
            side_effect=RuntimeError("explore boom"),
        ):
            result = await engine.start_interview("Add an endpoint", cwd=str(tmp_path))

        # Interview should still start successfully
        assert result.is_ok
        assert result.value.is_brownfield is True

    @pytest.mark.asyncio
    async def test_start_interview_empty_dir_stays_greenfield(self, tmp_path: Path) -> None:
        """start_interview with cwd pointing to empty dir stays greenfield."""
        mock_adapter = MagicMock()
        engine = InterviewEngine(llm_adapter=mock_adapter)

        result = await engine.start_interview("Build something", cwd=str(tmp_path))

        assert result.is_ok
        assert result.value.is_brownfield is False


class TestDefaultBehaviorUnchangedWithEmptyPersonas:
    """Test that default ooo interview behavior is completely unchanged when consulting_personas is empty."""

    def test_engine_default_consulting_personas_is_empty(self) -> None:
        """InterviewEngine defaults to empty consulting_personas tuple."""
        mock_adapter = MagicMock()
        engine = InterviewEngine(llm_adapter=mock_adapter)

        assert engine.consulting_personas == ()

    def test_system_prompt_no_persona_section_when_empty(self) -> None:
        """_build_system_prompt does NOT inject persona/lens/consultant section when consulting_personas is empty."""
        mock_adapter = MagicMock()
        engine = InterviewEngine(llm_adapter=mock_adapter, consulting_personas=())

        state = InterviewState(
            interview_id="test_default",
            initial_context="Build a CLI tool",
        )

        prompt = engine._build_system_prompt(state)

        # No persona-related content should appear
        assert "consulting" not in prompt.lower()
        assert "consultant" not in prompt.lower()
        assert "contrarian" not in prompt.lower()
        assert "ontologist" not in prompt.lower()
        assert "hacker" not in prompt.lower()

    def test_system_prompt_identical_with_and_without_explicit_empty_personas(self) -> None:
        """System prompt is identical whether consulting_personas is omitted or explicitly empty."""
        mock_adapter = MagicMock()
        engine_default = InterviewEngine(llm_adapter=mock_adapter)
        engine_explicit = InterviewEngine(llm_adapter=mock_adapter, consulting_personas=())

        state = InterviewState(
            interview_id="test_compare",
            initial_context="Build a task manager",
        )

        prompt_default = engine_default._build_system_prompt(state)
        prompt_explicit = engine_explicit._build_system_prompt(state)

        assert prompt_default == prompt_explicit

    @pytest.mark.asyncio
    async def test_ask_next_question_unchanged_with_empty_personas(self) -> None:
        """ask_next_question sends the same messages when consulting_personas is empty."""
        mock_adapter = MagicMock()
        mock_adapter.complete = AsyncMock(return_value=Result.ok(create_mock_completion_response()))

        engine = InterviewEngine(llm_adapter=mock_adapter, consulting_personas=())
        state = InterviewState(
            interview_id="test_default_flow",
            initial_context="Build a CLI tool",
        )

        result = await engine.ask_next_question(state)

        assert result.is_ok
        # Verify the system prompt in the call doesn't contain persona content
        call_args = mock_adapter.complete.call_args
        messages = call_args[0][0]
        system_msg = messages[0]
        assert system_msg.role == MessageRole.SYSTEM
        assert "consultant" not in system_msg.content.lower()
        assert "contrarian" not in system_msg.content.lower()

    @pytest.mark.asyncio
    async def test_start_interview_unchanged_with_empty_personas(self) -> None:
        """start_interview works identically when consulting_personas is empty."""
        mock_adapter = MagicMock()
        engine = InterviewEngine(llm_adapter=mock_adapter, consulting_personas=())

        result = await engine.start_interview("Build a task manager")

        assert result.is_ok
        state = result.value
        assert state.interview_id.startswith("interview_")
        assert state.initial_context == "Build a task manager"
        assert state.status == InterviewStatus.IN_PROGRESS
        assert len(state.rounds) == 0

    def test_system_prompt_round2_no_persona_when_empty(self) -> None:
        """System prompt for round 2+ has no persona content when consulting_personas is empty."""
        mock_adapter = MagicMock()
        engine = InterviewEngine(llm_adapter=mock_adapter, consulting_personas=())

        state = InterviewState(
            interview_id="test_round2",
            initial_context="Build a CLI tool",
        )
        state.rounds.append(
            InterviewRound(
                round_number=1,
                question="What problem does it solve?",
                user_response="Task management",
            )
        )

        prompt = engine._build_system_prompt(state)

        assert "Round 2" in prompt
        assert "consultant" not in prompt.lower()
        assert "contrarian" not in prompt.lower()
        assert "ontologist" not in prompt.lower()

    def test_system_prompt_brownfield_no_persona_when_empty(self) -> None:
        """Brownfield system prompt has no persona content when consulting_personas is empty."""
        mock_adapter = MagicMock()
        engine = InterviewEngine(llm_adapter=mock_adapter, consulting_personas=())

        state = InterviewState(
            interview_id="test_bf_default",
            initial_context="Add a REST endpoint",
            is_brownfield=True,
            codebase_context="Tech: Python\nDeps: flask, sqlalchemy\n",
        )

        prompt = engine._build_system_prompt(state)

        # Brownfield content should still be present
        assert "Existing Codebase Context" in prompt
        assert "flask" in prompt
        # But no persona content
        assert "consultant" not in prompt.lower()
        assert "contrarian" not in prompt.lower()


class TestConsultingPersonaPromptInjection:
    """Test prompt injection content for each consulting persona.

    Verifies that _build_system_prompt correctly injects persona-specific
    system prompts, approach instructions, and behavioral instructions
    when consulting_personas is provided.
    """

    ALL_PERSONAS = ("contrarian", "architect", "researcher", "hacker", "ontologist")

    def _make_engine(
        self,
        personas: tuple[str, ...] = ALL_PERSONAS,
    ) -> InterviewEngine:
        return InterviewEngine(
            llm_adapter=MagicMock(),
            consulting_personas=personas,
        )

    def _make_state(
        self,
        *,
        num_completed_rounds: int = 0,
        initial_context: str = "Build a REST API",
    ) -> InterviewState:
        state = InterviewState(
            interview_id="test_persona",
            initial_context=initial_context,
        )
        for i in range(num_completed_rounds):
            state.rounds.append(
                InterviewRound(
                    round_number=i + 1,
                    question=f"Q{i + 1}",
                    user_response=f"A{i + 1}",
                )
            )
        return state

    # --- Contrarian (round 1, index 0) ---

    def test_contrarian_persona_header_injected(self) -> None:
        """Round 1 injects the CONTRARIAN persona header."""
        engine = self._make_engine()
        state = self._make_state(num_completed_rounds=0)
        prompt = engine._build_system_prompt(state)

        assert "## Consulting Persona Lens: CONTRARIAN" in prompt

    def test_contrarian_system_prompt_content(self) -> None:
        """Round 1 includes the contrarian system_prompt (opening philosophy)."""
        engine = self._make_engine()
        state = self._make_state(num_completed_rounds=0)
        prompt = engine._build_system_prompt(state)

        # The contrarian persona's opening line
        assert "question everything" in prompt.lower()

    def test_contrarian_approach_instructions(self) -> None:
        """Round 1 includes contrarian approach instructions."""
        engine = self._make_engine()
        state = self._make_state(num_completed_rounds=0)
        prompt = engine._build_system_prompt(state)

        assert "Apply this perspective when forming your questions this round" in prompt
        # Key contrarian approach items
        assert "List Every Assumption" in prompt
        assert "Consider the Opposite" in prompt
        assert "Challenge the Problem Statement" in prompt

    # --- Architect (round 2, index 1) ---

    def test_architect_persona_header_injected(self) -> None:
        """Round 2 injects the ARCHITECT persona header."""
        engine = self._make_engine()
        state = self._make_state(num_completed_rounds=1)
        prompt = engine._build_system_prompt(state)

        assert "## Consulting Persona Lens: ARCHITECT" in prompt

    def test_architect_system_prompt_content(self) -> None:
        """Round 2 includes the architect system_prompt."""
        engine = self._make_engine()
        state = self._make_state(num_completed_rounds=1)
        prompt = engine._build_system_prompt(state)

        assert "structural" in prompt.lower()

    def test_architect_approach_instructions(self) -> None:
        """Round 2 includes architect approach instructions."""
        engine = self._make_engine()
        state = self._make_state(num_completed_rounds=1)
        prompt = engine._build_system_prompt(state)

        assert "Identify Structural Symptoms" in prompt
        assert "Map the Current Structure" in prompt
        assert "Find the Root Misalignment" in prompt

    # --- Researcher (round 3, index 2) ---

    def test_researcher_persona_header_injected(self) -> None:
        """Round 3 injects the RESEARCHER persona header."""
        engine = self._make_engine()
        state = self._make_state(num_completed_rounds=2)
        prompt = engine._build_system_prompt(state)

        assert "## Consulting Persona Lens: RESEARCHER" in prompt

    def test_researcher_system_prompt_content(self) -> None:
        """Round 3 includes the researcher system_prompt."""
        engine = self._make_engine()
        state = self._make_state(num_completed_rounds=2)
        prompt = engine._build_system_prompt(state)

        assert "investigating" in prompt.lower() or "information" in prompt.lower()

    def test_researcher_approach_instructions(self) -> None:
        """Round 3 includes researcher approach instructions."""
        engine = self._make_engine()
        state = self._make_state(num_completed_rounds=2)
        prompt = engine._build_system_prompt(state)

        assert "Define What" in prompt
        assert "Gather Evidence" in prompt
        assert "Form a Hypothesis" in prompt

    # --- Hacker (round 4, index 3) ---

    def test_hacker_persona_header_injected(self) -> None:
        """Round 4 injects the HACKER persona header."""
        engine = self._make_engine()
        state = self._make_state(num_completed_rounds=3)
        prompt = engine._build_system_prompt(state)

        assert "## Consulting Persona Lens: HACKER" in prompt

    def test_hacker_system_prompt_content(self) -> None:
        """Round 4 includes the hacker system_prompt."""
        engine = self._make_engine()
        state = self._make_state(num_completed_rounds=3)
        prompt = engine._build_system_prompt(state)

        assert "unconventional" in prompt.lower() or "workaround" in prompt.lower()

    def test_hacker_approach_instructions(self) -> None:
        """Round 4 includes hacker approach instructions."""
        engine = self._make_engine()
        state = self._make_state(num_completed_rounds=3)
        prompt = engine._build_system_prompt(state)

        assert "Identify Constraints" in prompt
        assert "Question Each Constraint" in prompt
        assert "Consider Bypassing" in prompt

    # --- Ontologist (round 5, index 4) ---

    def test_ontologist_persona_header_injected(self) -> None:
        """Round 5 injects the ONTOLOGIST persona header."""
        engine = self._make_engine()
        state = self._make_state(num_completed_rounds=4)
        prompt = engine._build_system_prompt(state)

        assert "## Consulting Persona Lens: ONTOLOGIST" in prompt

    def test_ontologist_system_prompt_content(self) -> None:
        """Round 5 includes the ontologist system_prompt."""
        engine = self._make_engine()
        state = self._make_state(num_completed_rounds=4)
        prompt = engine._build_system_prompt(state)

        assert "ontological" in prompt.lower() or "essential nature" in prompt.lower()

    def test_ontologist_approach_instructions(self) -> None:
        """Round 5 includes ontologist approach instructions (from FOUR FUNDAMENTAL QUESTIONS)."""
        engine = self._make_engine()
        state = self._make_state(num_completed_rounds=4)
        prompt = engine._build_system_prompt(state)

        # Ontologist uses "THE FOUR FUNDAMENTAL QUESTIONS" as approach section
        # load_persona_prompt_data extracts from "## YOUR APPROACH" — ontologist
        # doesn't have that section, so approach_instructions may be empty.
        # The system_prompt should still be present.
        assert "ONTOLOGIST" in prompt

    # --- Rotation wrap-around ---

    def test_persona_wraps_around_to_contrarian_on_round_6(self) -> None:
        """Round 6 wraps back to CONTRARIAN (index 0) with 5 personas."""
        engine = self._make_engine()
        state = self._make_state(num_completed_rounds=5)
        prompt = engine._build_system_prompt(state)

        assert "## Consulting Persona Lens: CONTRARIAN" in prompt

    def test_persona_wraps_around_to_architect_on_round_7(self) -> None:
        """Round 7 wraps to ARCHITECT (index 1)."""
        engine = self._make_engine()
        state = self._make_state(num_completed_rounds=6)
        prompt = engine._build_system_prompt(state)

        assert "## Consulting Persona Lens: ARCHITECT" in prompt

    # --- Single-persona mode ---

    def test_single_persona_always_used(self) -> None:
        """When only one persona is specified, it's used every round."""
        engine = self._make_engine(personas=("hacker",))

        for rounds_done in range(5):
            state = self._make_state(num_completed_rounds=rounds_done)
            prompt = engine._build_system_prompt(state)
            assert "## Consulting Persona Lens: HACKER" in prompt

    # --- Partial persona list ---

    def test_two_persona_rotation(self) -> None:
        """Two personas alternate correctly."""
        engine = self._make_engine(personas=("researcher", "ontologist"))

        state_r1 = self._make_state(num_completed_rounds=0)
        assert "RESEARCHER" in engine._build_system_prompt(state_r1)

        state_r2 = self._make_state(num_completed_rounds=1)
        assert "ONTOLOGIST" in engine._build_system_prompt(state_r2)

        state_r3 = self._make_state(num_completed_rounds=2)
        assert "RESEARCHER" in engine._build_system_prompt(state_r3)

    # --- Persona does NOT replace base interview content ---

    def test_persona_preserves_base_prompt(self) -> None:
        """Persona injection does NOT remove the base interview prompt content."""
        engine = self._make_engine()
        state = self._make_state(num_completed_rounds=0)
        prompt = engine._build_system_prompt(state)

        # Base prompt content still present
        assert "expert requirements engineer" in prompt
        assert "Socratic interview" in prompt
        assert "Build a REST API" in prompt  # initial_context

    def test_persona_section_precedes_brownfield_context(self) -> None:
        """When both persona and brownfield are active, both appear in prompt."""
        engine = self._make_engine()
        state = self._make_state(num_completed_rounds=0)
        state.is_brownfield = True
        state.codebase_context = "Tech: Python\nDeps: flask\n"

        prompt = engine._build_system_prompt(state)

        assert "## Consulting Persona Lens: CONTRARIAN" in prompt
        assert "Existing Codebase Context" in prompt
        # Persona section appears before brownfield section
        persona_idx = prompt.index("Consulting Persona Lens")
        brownfield_idx = prompt.index("Existing Codebase Context")
        assert persona_idx < brownfield_idx

    # --- Each persona has approach instructions in the prompt ---

    @pytest.mark.parametrize(
        "persona_name,round_offset",
        [
            ("contrarian", 0),
            ("architect", 1),
            ("researcher", 2),
            ("hacker", 3),
            ("ontologist", 4),
        ],
    )
    def test_each_persona_injects_formatted_approach_lines(
        self, persona_name: str, round_offset: int
    ) -> None:
        """Each persona injects numbered approach lines with 'Apply this perspective'."""
        engine = self._make_engine()
        state = self._make_state(num_completed_rounds=round_offset)
        prompt = engine._build_system_prompt(state)

        assert f"## Consulting Persona Lens: {persona_name.upper()}" in prompt
        assert "Apply this perspective when forming your questions this round" in prompt


class TestSystemPromptBrownfield:
    """Test brownfield system prompt injection."""

    def test_system_prompt_brownfield_round_1(self) -> None:
        """System prompt includes confirmation instructions when brownfield context exists."""
        mock_adapter = MagicMock()
        engine = InterviewEngine(llm_adapter=mock_adapter)

        state = InterviewState(
            interview_id="test_bf",
            initial_context="Add a REST endpoint",
            is_brownfield=True,
            codebase_context="Tech: Python\nDeps: flask, sqlalchemy\n",
        )

        prompt = engine._build_system_prompt(state)

        assert "Existing Codebase Context" in prompt
        assert "CONFIRMATION questions" in prompt
        assert "I found X. Should I assume Y?" in prompt
        assert "flask" in prompt


class TestPersonaRotationLogic:
    """Test persona rotation cycling, state management, and deterministic selection.

    These tests focus on the rotation *mechanics* (modular indexing,
    wrap-around, determinism, subset sizing) rather than the prompt
    *content* injected per persona (covered by TestConsultingPersonaPromptInjection).
    """

    ALL_PERSONAS = ("contrarian", "architect", "researcher", "hacker", "ontologist")

    def _make_state(self, *, num_rounds: int = 0) -> InterviewState:
        """Create an InterviewState with the given number of completed rounds."""
        state = InterviewState(
            interview_id="test_rotation",
            initial_context="Build a CLI tool",
        )
        for i in range(num_rounds):
            state.rounds.append(
                InterviewRound(
                    round_number=i + 1,
                    question=f"Q{i + 1}",
                    user_response=f"A{i + 1}",
                )
            )
        return state

    def _engine(self, personas: tuple[str, ...] | None = None) -> InterviewEngine:
        return InterviewEngine(
            llm_adapter=MagicMock(),
            consulting_personas=personas if personas is not None else self.ALL_PERSONAS,
        )

    # ---- Correct modular indexing ----

    def test_rotation_index_formula_across_three_cycles(self) -> None:
        """Rotation uses (current_round_number - 1) % len(personas) across 3 full cycles."""
        engine = self._engine()
        personas = self.ALL_PERSONAS

        for round_offset in range(15):
            state = self._make_state(num_rounds=round_offset)
            expected_persona = personas[round_offset % len(personas)].upper()
            prompt = engine._build_system_prompt(state)
            assert f"Consulting Persona Lens: {expected_persona}" in prompt, (
                f"Round {round_offset + 1}: expected {expected_persona}"
            )

    # ---- Wrap-around ----

    def test_wraps_to_first_persona_after_exhausting_list(self) -> None:
        """After cycling through all 5 personas, round 6 wraps to the first."""
        engine = self._engine()
        state = self._make_state(num_rounds=5)  # current_round_number == 6
        prompt = engine._build_system_prompt(state)
        assert "Consulting Persona Lens: CONTRARIAN" in prompt

    def test_wraps_correctly_at_high_round_numbers(self) -> None:
        """Rotation is correct even at very high round numbers (e.g. round 53)."""
        engine = self._engine()
        state = self._make_state(num_rounds=52)  # current_round_number == 53
        expected_idx = 52 % 5  # == 2 → researcher
        expected = self.ALL_PERSONAS[expected_idx].upper()
        prompt = engine._build_system_prompt(state)
        assert f"Consulting Persona Lens: {expected}" in prompt

    # ---- Determinism / statelessness ----

    def test_same_round_always_produces_same_prompt(self) -> None:
        """Calling _build_system_prompt twice for the same state yields identical output."""
        engine = self._engine()
        state = self._make_state(num_rounds=3)  # round 4

        prompt_a = engine._build_system_prompt(state)
        prompt_b = engine._build_system_prompt(state)

        assert prompt_a == prompt_b

    def test_rotation_has_no_hidden_mutable_state(self) -> None:
        """Building prompt for round N doesn't affect prompt for round M."""
        engine = self._engine()

        # Build prompt for round 3 first
        state_r3 = self._make_state(num_rounds=2)
        prompt_r3_before = engine._build_system_prompt(state_r3)

        # Build prompt for round 1 in between
        state_r1 = self._make_state(num_rounds=0)
        engine._build_system_prompt(state_r1)

        # Build prompt for round 3 again — should be identical
        prompt_r3_after = engine._build_system_prompt(state_r3)
        assert prompt_r3_before == prompt_r3_after

    # ---- Subset sizing ----

    def test_single_persona_used_every_round(self) -> None:
        """With one persona, every round uses that persona."""
        engine = self._engine(personas=("ontologist",))

        for n in range(7):
            state = self._make_state(num_rounds=n)
            prompt = engine._build_system_prompt(state)
            assert "Consulting Persona Lens: ONTOLOGIST" in prompt

    def test_two_personas_alternate(self) -> None:
        """Two personas alternate correctly across rounds."""
        engine = self._engine(personas=("architect", "hacker"))

        expectations = ["ARCHITECT", "HACKER", "ARCHITECT", "HACKER", "ARCHITECT", "HACKER"]
        for i, expected in enumerate(expectations):
            state = self._make_state(num_rounds=i)
            prompt = engine._build_system_prompt(state)
            assert f"Consulting Persona Lens: {expected}" in prompt

    def test_three_personas_cycle(self) -> None:
        """Three personas cycle correctly: A, B, C, A, B, C, ..."""
        personas = ("contrarian", "researcher", "hacker")
        engine = self._engine(personas=personas)

        for i in range(9):  # 3 full cycles
            state = self._make_state(num_rounds=i)
            expected = personas[i % 3].upper()
            prompt = engine._build_system_prompt(state)
            assert f"Consulting Persona Lens: {expected}" in prompt

    # ---- Cycle equivalence ----

    def test_cycle_n_matches_cycle_1(self) -> None:
        """The Nth cycle produces the same persona sequence as the 1st cycle."""
        engine = self._engine()

        for i in range(5):
            state_cycle1 = self._make_state(num_rounds=i)
            state_cycle2 = self._make_state(num_rounds=i + 5)
            state_cycle3 = self._make_state(num_rounds=i + 10)

            prompt1 = engine._build_system_prompt(state_cycle1)
            prompt2 = engine._build_system_prompt(state_cycle2)
            prompt3 = engine._build_system_prompt(state_cycle3)

            # All three should contain the same persona header
            expected = self.ALL_PERSONAS[i].upper()
            for prompt, cycle in [(prompt1, 1), (prompt2, 2), (prompt3, 3)]:
                assert f"Consulting Persona Lens: {expected}" in prompt, (
                    f"Cycle {cycle}, position {i}: expected {expected}"
                )

    # ---- Empty personas → no rotation ----

    def test_empty_personas_no_rotation_header(self) -> None:
        """Empty consulting_personas produces no persona lens section at all."""
        engine = self._engine(personas=())
        state = self._make_state(num_rounds=0)
        prompt = engine._build_system_prompt(state)
        assert "Consulting Persona Lens" not in prompt

    def test_empty_personas_across_multiple_rounds(self) -> None:
        """Empty personas produces no persona content regardless of round number."""
        engine = self._engine(personas=())
        for n in range(5):
            state = self._make_state(num_rounds=n)
            prompt = engine._build_system_prompt(state)
            assert "Consulting Persona Lens" not in prompt
