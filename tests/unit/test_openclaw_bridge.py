"""Unit tests for the OpenClaw bridge CLI wrapper."""

import json
from pathlib import Path
import sys
from unittest.mock import AsyncMock, patch

import pytest

# Add project root to path for bridge import
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from openclaw_bridge import (
    cmd_complete,
    cmd_respond,
    cmd_start,
    cmd_status,
    load_state,
    save_state,
)
from ouroboros.bigbang.interview import (
    InterviewRound,
    InterviewState,
    InterviewStatus,
)
from ouroboros.core.types import Result


@pytest.fixture
def tmp_sessions(tmp_path, monkeypatch):
    """Override SESSIONS_DIR to use a temp directory."""
    import openclaw_bridge

    monkeypatch.setattr(openclaw_bridge, "SESSIONS_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def sample_state(tmp_sessions):
    """Create a sample interview state with 3 rounds."""
    state = InterviewState(
        interview_id="test_session_001",
        status=InterviewStatus.IN_PROGRESS,
        initial_context="Build a task management CLI",
        rounds=[
            InterviewRound(
                round_number=1,
                question="What specific task operations should the CLI support?",
                user_response="Create, list, complete, and delete tasks.",
            ),
            InterviewRound(
                round_number=2,
                question="Should tasks persist between sessions?",
                user_response="Yes, save to a local JSON file.",
            ),
            InterviewRound(
                round_number=3,
                question="What defines a task's priority system?",
                user_response=None,  # Unanswered — pending
            ),
        ],
    )
    import openclaw_bridge

    state_file = openclaw_bridge.SESSIONS_DIR / "test_session_001.json"
    state_file.write_text(state.model_dump_json(indent=2))
    return state


class TestStateIO:
    """Test state load/save operations."""

    def test_save_and_load_roundtrip(self, tmp_sessions):
        """State survives a save/load cycle."""
        state = InterviewState(
            interview_id="roundtrip_test",
            initial_context="Test project",
        )
        save_state(state)

        loaded = load_state("roundtrip_test")
        assert loaded.interview_id == "roundtrip_test"
        assert loaded.initial_context == "Test project"
        assert loaded.status == InterviewStatus.IN_PROGRESS

    def test_load_nonexistent_exits(self, tmp_sessions):
        """Loading a missing session exits with error."""
        with pytest.raises(SystemExit):
            load_state("does_not_exist")

    def test_save_creates_directory(self, tmp_path, monkeypatch):
        """Save creates the sessions directory if it doesn't exist."""
        import openclaw_bridge

        nested = tmp_path / "deep" / "nested" / "dir"
        monkeypatch.setattr(openclaw_bridge, "SESSIONS_DIR", nested)

        state = InterviewState(
            interview_id="mkdir_test",
            initial_context="Test",
        )
        save_state(state)
        assert (nested / "mkdir_test.json").exists()


class TestCmdStatus:
    """Test the status command."""

    def test_status_output(self, sample_state, capsys):
        """Status command outputs correct JSON."""
        import asyncio

        asyncio.run(cmd_status("test_session_001"))
        output = json.loads(capsys.readouterr().out)

        assert output["session_id"] == "test_session_001"
        assert output["status"] == "in_progress"
        assert output["rounds"] == 3
        assert len(output["rounds_detail"]) == 3
        assert output["rounds_detail"][0]["answered"] is True
        assert output["rounds_detail"][2]["answered"] is False


class TestCmdComplete:
    """Test the complete command."""

    def test_marks_interview_complete(self, sample_state, capsys):
        """Complete command marks interview as completed."""
        import asyncio

        asyncio.run(cmd_complete("test_session_001"))
        output = json.loads(capsys.readouterr().out)

        assert output["status"] == "completed"
        assert output["total_rounds"] == 3

        # Verify state persisted
        reloaded = load_state("test_session_001")
        assert reloaded.status == InterviewStatus.COMPLETED


class TestCmdStart:
    """Test the start command (mocked LLM)."""

    def test_start_creates_session(self, tmp_sessions, capsys):
        """Start creates a session and returns first question."""
        import asyncio

        mock_result = Result.ok("What problem are you trying to solve?")

        with (
            patch(
                "openclaw_bridge.InterviewEngine.start_interview",
                new_callable=AsyncMock,
                return_value=Result.ok(
                    InterviewState(
                        interview_id="new_session",
                        initial_context="Build an app",
                    )
                ),
            ),
            patch(
                "openclaw_bridge.InterviewEngine.ask_next_question",
                new_callable=AsyncMock,
                return_value=mock_result,
            ),
        ):
            asyncio.run(cmd_start("Build an app"))

        output = json.loads(capsys.readouterr().out)
        assert output["session_id"] == "new_session"
        assert output["round"] == 1
        assert output["question"] == "What problem are you trying to solve?"
        assert output["status"] == "in_progress"


class TestCmdRespond:
    """Test the respond command (mocked LLM)."""

    def test_respond_records_and_asks_next(self, sample_state, capsys):
        """Respond records answer and generates next question."""
        import asyncio

        mock_result = Result.ok("How should errors be handled?")

        with patch(
            "openclaw_bridge.InterviewEngine.ask_next_question",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            asyncio.run(cmd_respond("test_session_001", "High, medium, low priority."))

        output = json.loads(capsys.readouterr().out)
        assert output["round"] == 4
        assert output["question"] == "How should errors be handled?"

        # Verify response was recorded
        reloaded = load_state("test_session_001")
        assert reloaded.rounds[2].user_response == "High, medium, low priority."
