"""Tests for workflow state tracking module."""

import pytest

from ouroboros.orchestrator.workflow_state import (
    AcceptanceCriterion,
    ACStatus,
    ActivityType,
    WorkflowState,
    WorkflowStateTracker,
    get_ac_tracking_prompt,
)


class TestACStatus:
    """Tests for ACStatus enum."""

    def test_enum_values(self) -> None:
        """Test AC status enum values."""
        assert ACStatus.PENDING.value == "pending"
        assert ACStatus.IN_PROGRESS.value == "in_progress"
        assert ACStatus.COMPLETED.value == "completed"
        assert ACStatus.FAILED.value == "failed"


class TestActivityType:
    """Tests for ActivityType enum."""

    def test_enum_values(self) -> None:
        """Test activity type enum values."""
        assert ActivityType.IDLE.value == "idle"
        assert ActivityType.EXPLORING.value == "exploring"
        assert ActivityType.BUILDING.value == "building"
        assert ActivityType.TESTING.value == "testing"


class TestAcceptanceCriterion:
    """Tests for AcceptanceCriterion dataclass."""

    def test_create_criterion(self) -> None:
        """Test creating an acceptance criterion."""
        ac = AcceptanceCriterion(index=1, content="Users can log in")
        assert ac.index == 1
        assert ac.content == "Users can log in"
        assert ac.status == ACStatus.PENDING
        assert ac.started_at is None
        assert ac.completed_at is None

    def test_start_criterion(self) -> None:
        """Test starting work on a criterion."""
        ac = AcceptanceCriterion(index=1, content="Test")
        ac.start()

        assert ac.status == ACStatus.IN_PROGRESS
        assert ac.started_at is not None

    def test_complete_criterion(self) -> None:
        """Test completing a criterion."""
        ac = AcceptanceCriterion(index=1, content="Test")
        ac.start()
        ac.complete()

        assert ac.status == ACStatus.COMPLETED
        assert ac.completed_at is not None

    def test_fail_criterion(self) -> None:
        """Test failing a criterion."""
        ac = AcceptanceCriterion(index=1, content="Test")
        ac.start()
        ac.fail()

        assert ac.status == ACStatus.FAILED
        assert ac.completed_at is not None


class TestWorkflowState:
    """Tests for WorkflowState dataclass."""

    def test_empty_state(self) -> None:
        """Test creating empty workflow state."""
        state = WorkflowState()
        assert state.completed_count == 0
        assert state.total_count == 0
        assert state.progress_fraction == 0.0
        assert state.progress_percent == 0

    def test_state_with_criteria(self) -> None:
        """Test state with acceptance criteria."""
        state = WorkflowState(
            goal="Build a CLI tool",
            acceptance_criteria=[
                AcceptanceCriterion(index=1, content="AC 1", status=ACStatus.COMPLETED),
                AcceptanceCriterion(index=2, content="AC 2", status=ACStatus.IN_PROGRESS),
                AcceptanceCriterion(index=3, content="AC 3"),
            ],
        )

        assert state.completed_count == 1
        assert state.total_count == 3
        assert state.progress_fraction == pytest.approx(1 / 3)
        assert state.progress_percent == 33

    def test_elapsed_display(self) -> None:
        """Test elapsed time display format."""
        state = WorkflowState()
        # Just verify it doesn't crash and returns a string
        display = state.elapsed_display
        assert isinstance(display, str)
        assert "s" in display  # Should contain seconds indicator

    def test_add_activity(self) -> None:
        """Test adding activity log entries."""
        state = WorkflowState(max_activity_log=3)
        state.add_activity("Entry 1")
        state.add_activity("Entry 2")
        state.add_activity("Entry 3")
        state.add_activity("Entry 4")

        assert len(state.activity_log) == 3
        assert state.activity_log[0] == "Entry 2"  # Entry 1 was trimmed


class TestWorkflowStateTracker:
    """Tests for WorkflowStateTracker."""

    @pytest.fixture
    def tracker(self) -> WorkflowStateTracker:
        """Create a tracker with sample acceptance criteria."""
        return WorkflowStateTracker(
            acceptance_criteria=[
                "Users can create tasks",
                "Users can list tasks",
                "Tasks persist to database",
            ],
            goal="Build a task manager",
            session_id="test_session",
        )

    def test_init_creates_criteria(self, tracker: WorkflowStateTracker) -> None:
        """Test tracker initializes with correct criteria."""
        state = tracker.state
        assert len(state.acceptance_criteria) == 3
        assert state.acceptance_criteria[0].content == "Users can create tasks"
        assert state.acceptance_criteria[0].index == 1
        assert all(ac.status == ACStatus.PENDING for ac in state.acceptance_criteria)

    def test_process_message_updates_count(self, tracker: WorkflowStateTracker) -> None:
        """Test processing messages increments count."""
        tracker.process_message("Hello world", message_type="assistant")
        assert tracker.state.messages_count == 1

        tracker.process_message("More content", message_type="assistant")
        assert tracker.state.messages_count == 2

    def test_process_message_with_tool(self, tracker: WorkflowStateTracker) -> None:
        """Test processing tool messages updates activity."""
        tracker.process_message("Reading file.py", message_type="tool", tool_name="Read")

        state = tracker.state
        assert state.tool_calls_count == 1
        assert state.last_tool == "Read"
        assert state.activity == ActivityType.EXPLORING

    def test_parse_ac_start_marker(self, tracker: WorkflowStateTracker) -> None:
        """Test parsing AC_START marker."""
        tracker.process_message(
            "[AC_START: 1] I'll begin implementing the first criterion...",
            message_type="assistant",
        )

        state = tracker.state
        assert state.acceptance_criteria[0].status == ACStatus.IN_PROGRESS
        assert state.current_ac_index == 1

    def test_parse_ac_complete_marker(self, tracker: WorkflowStateTracker) -> None:
        """Test parsing AC_COMPLETE marker."""
        tracker.process_message("[AC_START: 1] Starting...", message_type="assistant")
        tracker.process_message("[AC_COMPLETE: 1] Done!", message_type="assistant")

        state = tracker.state
        assert state.acceptance_criteria[0].status == ACStatus.COMPLETED
        # Should advance to next pending AC
        assert state.current_ac_index == 2

    def test_parse_heuristic_completion(self, tracker: WorkflowStateTracker) -> None:
        """Test heuristic completion detection."""
        tracker.process_message(
            "Criterion #1 is complete. Moving on to the next.",
            message_type="assistant",
        )

        state = tracker.state
        assert state.acceptance_criteria[0].status == ACStatus.COMPLETED

    def test_token_estimation(self, tracker: WorkflowStateTracker) -> None:
        """Test token and cost estimation."""
        # Process a message with known length
        content = "a" * 400  # 400 chars ~ 100 tokens
        tracker.process_message(content, message_type="assistant", is_input=False)

        state = tracker.state
        assert state.estimated_tokens > 0
        assert state.estimated_cost_usd > 0

    def test_to_dict(self, tracker: WorkflowStateTracker) -> None:
        """Test exporting state as dictionary."""
        tracker.process_message("[AC_COMPLETE: 1] Done!", message_type="assistant")

        data = tracker.to_dict()
        assert data["session_id"] == "test_session"
        assert data["total_acs"] == 3
        assert data["completed_acs"] == 1
        assert data["progress_percent"] == 33
        assert len(data["acceptance_criteria"]) == 3

    def test_all_acs_completed(self, tracker: WorkflowStateTracker) -> None:
        """Test behavior when all ACs are completed."""
        tracker.process_message("[AC_COMPLETE: 1]", message_type="assistant")
        tracker.process_message("[AC_COMPLETE: 2]", message_type="assistant")
        tracker.process_message("[AC_COMPLETE: 3]", message_type="assistant")

        state = tracker.state
        assert state.completed_count == 3
        assert state.progress_percent == 100
        assert state.current_ac_index == 0  # No more pending
        assert state.activity == ActivityType.FINALIZING


class TestGetACTrackingPrompt:
    """Tests for AC tracking prompt."""

    def test_contains_markers(self) -> None:
        """Test prompt contains AC markers."""
        prompt = get_ac_tracking_prompt()
        assert "[AC_START:" in prompt
        assert "[AC_COMPLETE:" in prompt

    def test_contains_example(self) -> None:
        """Test prompt contains usage example."""
        prompt = get_ac_tracking_prompt()
        assert "Example:" in prompt
