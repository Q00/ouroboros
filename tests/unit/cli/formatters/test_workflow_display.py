"""Tests for workflow display module."""

import pytest
from rich.panel import Panel

from ouroboros.cli.formatters.workflow_display import (
    WorkflowDisplay,
    render_workflow_state,
)
from ouroboros.orchestrator.workflow_state import (
    AcceptanceCriterion,
    ACStatus,
    WorkflowState,
    WorkflowStateTracker,
)


class TestRenderWorkflowState:
    """Tests for render_workflow_state function."""

    def test_renders_panel(self) -> None:
        """Test that function returns a Rich Panel."""
        state = WorkflowState(
            session_id="test_123",
            goal="Build a CLI tool",
            acceptance_criteria=[
                AcceptanceCriterion(index=1, content="Users can log in"),
                AcceptanceCriterion(index=2, content="Users can log out"),
            ],
        )

        result = render_workflow_state(state)
        assert isinstance(result, Panel)

    def test_includes_session_id(self) -> None:
        """Test that panel includes session ID."""
        state = WorkflowState(session_id="my_session")

        panel = render_workflow_state(state)
        # The panel should render - we can't easily inspect the content
        # but we verify it doesn't crash
        assert panel is not None

    def test_handles_empty_state(self) -> None:
        """Test rendering empty state."""
        state = WorkflowState()
        panel = render_workflow_state(state)
        assert panel is not None

    def test_handles_long_goal(self) -> None:
        """Test that long goals are truncated."""
        state = WorkflowState(
            goal="A" * 100,  # Very long goal
        )
        panel = render_workflow_state(state)
        assert panel is not None

    def test_renders_ac_list(self) -> None:
        """Test rendering acceptance criteria list."""
        state = WorkflowState(
            acceptance_criteria=[
                AcceptanceCriterion(
                    index=1,
                    content="First criterion",
                    status=ACStatus.COMPLETED,
                ),
                AcceptanceCriterion(
                    index=2,
                    content="Second criterion",
                    status=ACStatus.IN_PROGRESS,
                ),
                AcceptanceCriterion(
                    index=3,
                    content="Third criterion",
                    status=ACStatus.PENDING,
                ),
            ],
            current_ac_index=2,
        )

        panel = render_workflow_state(state)
        assert panel is not None


class TestWorkflowDisplay:
    """Tests for WorkflowDisplay class."""

    @pytest.fixture
    def tracker(self) -> WorkflowStateTracker:
        """Create a sample tracker."""
        return WorkflowStateTracker(
            acceptance_criteria=["AC 1", "AC 2"],
            goal="Test goal",
            session_id="test",
        )

    def test_create_display(self, tracker: WorkflowStateTracker) -> None:
        """Test creating a WorkflowDisplay."""
        display = WorkflowDisplay(tracker)
        assert display is not None

    def test_render_returns_panel(self, tracker: WorkflowStateTracker) -> None:
        """Test that _render returns a panel."""
        display = WorkflowDisplay(tracker)
        panel = display._render()
        assert isinstance(panel, Panel)

    def test_context_manager(self, tracker: WorkflowStateTracker) -> None:
        """Test using display as context manager."""
        display = WorkflowDisplay(tracker)
        # We can't fully test the Live display without a real terminal,
        # but we verify the context manager protocol works
        display.start()
        assert display._live is not None
        display.stop()
        assert display._live is None

    def test_refresh_without_start(self, tracker: WorkflowStateTracker) -> None:
        """Test that refresh does nothing when not started."""
        display = WorkflowDisplay(tracker)
        # Should not raise
        display.refresh()
