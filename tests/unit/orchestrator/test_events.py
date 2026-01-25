"""Unit tests for orchestrator event helpers."""

from __future__ import annotations

import pytest

from ouroboros.orchestrator.events import (
    create_progress_event,
    create_session_completed_event,
    create_session_failed_event,
    create_session_paused_event,
    create_session_started_event,
    create_task_completed_event,
    create_task_started_event,
    create_tool_called_event,
)


class TestSessionEvents:
    """Tests for session-related event helpers."""

    def test_create_session_started_event(self) -> None:
        """Test creating session started event."""
        event = create_session_started_event(
            session_id="sess_123",
            execution_id="exec_456",
            seed_id="seed_789",
            seed_goal="Build a CLI tool",
        )

        assert event.type == "orchestrator.session.started"
        assert event.aggregate_type == "session"
        assert event.aggregate_id == "sess_123"
        assert event.data["execution_id"] == "exec_456"
        assert event.data["seed_id"] == "seed_789"
        assert event.data["seed_goal"] == "Build a CLI tool"
        assert "start_time" in event.data

    def test_create_session_completed_event(self) -> None:
        """Test creating session completed event."""
        summary = {"total_tasks": 5, "success_rate": 1.0}
        event = create_session_completed_event(
            session_id="sess_123",
            summary=summary,
            messages_processed=100,
        )

        assert event.type == "orchestrator.session.completed"
        assert event.aggregate_id == "sess_123"
        assert event.data["summary"] == summary
        assert event.data["messages_processed"] == 100
        assert "completed_at" in event.data

    def test_create_session_failed_event(self) -> None:
        """Test creating session failed event."""
        event = create_session_failed_event(
            session_id="sess_123",
            error_message="Connection timeout",
            error_type="TimeoutError",
            messages_processed=50,
        )

        assert event.type == "orchestrator.session.failed"
        assert event.aggregate_id == "sess_123"
        assert event.data["error"] == "Connection timeout"
        assert event.data["error_type"] == "TimeoutError"
        assert event.data["messages_processed"] == 50
        assert "failed_at" in event.data

    def test_create_session_failed_event_minimal(self) -> None:
        """Test creating session failed event with minimal data."""
        event = create_session_failed_event(
            session_id="sess_123",
            error_message="Unknown error",
        )

        assert event.data["error"] == "Unknown error"
        assert event.data["error_type"] is None
        assert event.data["messages_processed"] == 0

    def test_create_session_paused_event(self) -> None:
        """Test creating session paused event."""
        event = create_session_paused_event(
            session_id="sess_123",
            reason="User requested pause",
            resume_hint="Continue from AC #3",
        )

        assert event.type == "orchestrator.session.paused"
        assert event.aggregate_id == "sess_123"
        assert event.data["reason"] == "User requested pause"
        assert event.data["resume_hint"] == "Continue from AC #3"
        assert "paused_at" in event.data


class TestProgressEvents:
    """Tests for progress event helpers."""

    def test_create_progress_event(self) -> None:
        """Test creating progress event."""
        event = create_progress_event(
            session_id="sess_123",
            message_type="assistant",
            content_preview="I am analyzing the code...",
            step=5,
        )

        assert event.type == "orchestrator.progress.updated"
        assert event.aggregate_id == "sess_123"
        assert event.data["message_type"] == "assistant"
        assert event.data["content_preview"] == "I am analyzing the code..."
        assert event.data["step"] == 5
        assert "timestamp" in event.data

    def test_create_progress_event_with_tool(self) -> None:
        """Test creating progress event with tool name."""
        event = create_progress_event(
            session_id="sess_123",
            message_type="tool",
            content_preview="Reading file...",
            tool_name="Read",
        )

        assert event.data["tool_name"] == "Read"

    def test_create_progress_event_truncates_content(self) -> None:
        """Test that long content is truncated."""
        long_content = "x" * 500
        event = create_progress_event(
            session_id="sess_123",
            message_type="assistant",
            content_preview=long_content,
        )

        assert len(event.data["content_preview"]) == 200


class TestTaskEvents:
    """Tests for task-related event helpers."""

    def test_create_task_started_event(self) -> None:
        """Test creating task started event."""
        event = create_task_started_event(
            session_id="sess_123",
            task_description="Implement user authentication",
            acceptance_criterion="Users can log in with email and password",
        )

        assert event.type == "orchestrator.task.started"
        assert event.aggregate_id == "sess_123"
        assert event.data["task_description"] == "Implement user authentication"
        assert event.data["acceptance_criterion"] == "Users can log in with email and password"
        assert "started_at" in event.data

    def test_create_task_completed_event_success(self) -> None:
        """Test creating successful task completion event."""
        event = create_task_completed_event(
            session_id="sess_123",
            acceptance_criterion="AC #1",
            success=True,
            result_summary="Implemented login endpoint",
        )

        assert event.type == "orchestrator.task.completed"
        assert event.aggregate_id == "sess_123"
        assert event.data["acceptance_criterion"] == "AC #1"
        assert event.data["success"] is True
        assert event.data["result_summary"] == "Implemented login endpoint"
        assert "completed_at" in event.data

    def test_create_task_completed_event_failure(self) -> None:
        """Test creating failed task completion event."""
        event = create_task_completed_event(
            session_id="sess_123",
            acceptance_criterion="AC #2",
            success=False,
        )

        assert event.data["success"] is False
        assert event.data["result_summary"] is None


class TestToolEvents:
    """Tests for tool-related event helpers."""

    def test_create_tool_called_event(self) -> None:
        """Test creating tool called event."""
        event = create_tool_called_event(
            session_id="sess_123",
            tool_name="Edit",
            tool_input_preview="file_path: /src/auth.py",
        )

        assert event.type == "orchestrator.tool.called"
        assert event.aggregate_id == "sess_123"
        assert event.data["tool_name"] == "Edit"
        assert event.data["tool_input_preview"] == "file_path: /src/auth.py"
        assert "called_at" in event.data

    def test_create_tool_called_event_no_preview(self) -> None:
        """Test creating tool called event without input preview."""
        event = create_tool_called_event(
            session_id="sess_123",
            tool_name="Bash",
        )

        assert event.data["tool_name"] == "Bash"
        assert "tool_input_preview" not in event.data

    def test_create_tool_called_event_truncates_input(self) -> None:
        """Test that long tool input is truncated."""
        long_input = "y" * 200
        event = create_tool_called_event(
            session_id="sess_123",
            tool_name="Read",
            tool_input_preview=long_input,
        )

        assert len(event.data["tool_input_preview"]) == 100


class TestEventAggregateTypes:
    """Tests that all events have correct aggregate types."""

    def test_all_events_use_session_aggregate(self) -> None:
        """Verify all orchestrator events use 'session' aggregate type."""
        events = [
            create_session_started_event("s", "e", "sd", "g"),
            create_session_completed_event("s", {}, 0),
            create_session_failed_event("s", "error"),
            create_session_paused_event("s", "reason"),
            create_progress_event("s", "type", "content"),
            create_task_started_event("s", "desc", "ac"),
            create_task_completed_event("s", "ac", True),
            create_tool_called_event("s", "tool"),
        ]

        for event in events:
            assert event.aggregate_type == "session"
