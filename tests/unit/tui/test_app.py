"""Unit tests for OuroborosTUI application."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.events.base import BaseEvent
from ouroboros.persistence.event_store import EventStore
from ouroboros.tui.app import OuroborosTUI, _EventSubscriptionContext
from ouroboros.tui.events import (
    CostUpdated,
    DriftUpdated,
    ExecutionUpdated,
    PauseRequested,
    PhaseChanged,
    ResumeRequested,
    SubtaskUpdated,
    TUIState,
)


@pytest.fixture
async def memory_event_store() -> AsyncIterator[EventStore]:
    """Provide an initialized in-memory event store."""
    store = EventStore("sqlite+aiosqlite:///:memory:")
    await store.initialize()
    try:
        yield store
    finally:
        await store.close()


class TestOuroborosTUIConstruction:
    """Tests for OuroborosTUI construction."""

    def test_create_tui_default(self) -> None:
        """Test creating TUI with defaults."""
        app = OuroborosTUI()

        assert app._event_store is None
        assert app._execution_id is None
        assert isinstance(app._state, TUIState)
        assert app._is_paused is False

    def test_create_tui_with_event_store(self) -> None:
        """Test creating TUI with event store."""
        mock_store = MagicMock()

        app = OuroborosTUI(event_store=mock_store)

        assert app._event_store is mock_store

    def test_create_tui_with_execution_id(self) -> None:
        """Test creating TUI with execution ID."""
        app = OuroborosTUI(execution_id="exec_123")

        assert app._execution_id == "exec_123"


class TestOuroborosTUIState:
    """Tests for TUI state management."""

    def test_state_property(self) -> None:
        """Test accessing state property."""
        app = OuroborosTUI()

        state = app.state

        assert isinstance(state, TUIState)
        assert state.status == "idle"

    def test_set_execution(self) -> None:
        """Test setting execution to monitor."""
        app = OuroborosTUI()

        app.set_execution("exec_123", "sess_456")

        assert app._execution_id == "exec_123"
        assert app.state.execution_id == "exec_123"
        assert app.state.session_id == "sess_456"
        assert app.state.status == "running"

    def test_set_execution_resets_stale_hud_state(self) -> None:
        """Switching executions should clear HUD state from the previous context."""
        app = OuroborosTUI()
        app.state.current_phase = "deliver"
        app.state.iteration = 4
        app.state.total_tokens = 999
        app.state.ac_tree = {"nodes": {"ac_1": {"id": "ac_1"}}}
        app.state.active_tools["ac_1"] = {"tool_name": "Edit"}
        app.state.tool_history["ac_1"] = [{"tool_name": "Read"}]
        app.state.thinking["ac_1"] = "stale thought"

        app.set_execution("exec_fresh", "sess_fresh")

        assert app.state.execution_id == "exec_fresh"
        assert app.state.session_id == "sess_fresh"
        assert app.state.current_phase == ""
        assert app.state.iteration == 0
        assert app.state.total_tokens == 0
        assert app.state.ac_tree == {}
        assert app.state.active_tools == {}
        assert app.state.tool_history == {}
        assert app.state.thinking == {}

    def test_update_ac_tree(self) -> None:
        """Test updating AC tree data."""
        app = OuroborosTUI()
        tree_data = {
            "root_id": "ac_123",
            "nodes": {"ac_123": {"id": "ac_123", "content": "Test"}},
        }

        app.update_ac_tree(tree_data)

        assert app.state.ac_tree == tree_data


class TestOuroborosTUICallbacks:
    """Tests for pause/resume callbacks."""

    def test_set_pause_callback(self) -> None:
        """Test setting pause callback."""
        app = OuroborosTUI()
        callback = MagicMock()

        app.set_pause_callback(callback)

        assert app._pause_callback is callback

    def test_set_resume_callback(self) -> None:
        """Test setting resume callback."""
        app = OuroborosTUI()
        callback = MagicMock()

        app.set_resume_callback(callback)

        assert app._resume_callback is callback


class TestOuroborosTUIMessageHandlers:
    """Tests for TUI message handlers."""

    def test_on_execution_updated(self) -> None:
        """Test handling ExecutionUpdated message."""
        app = OuroborosTUI()
        msg = ExecutionUpdated(
            execution_id="exec_123",
            session_id="sess_456",
            status="running",
        )

        app.on_execution_updated(msg)

        assert app.state.execution_id == "exec_123"
        assert app.state.session_id == "sess_456"
        assert app.state.status == "running"
        assert app.state.is_paused is False

    def test_on_execution_updated_paused(self) -> None:
        """Test handling paused ExecutionUpdated."""
        app = OuroborosTUI()
        msg = ExecutionUpdated(
            execution_id="exec_123",
            session_id="sess_456",
            status="paused",
        )

        app.on_execution_updated(msg)

        assert app.state.status == "paused"
        assert app.state.is_paused is True

    def test_on_phase_changed(self) -> None:
        """Test handling PhaseChanged message."""
        app = OuroborosTUI()
        msg = PhaseChanged(
            execution_id="exec_123",
            previous_phase="discover",
            current_phase="define",
            iteration=2,
        )

        app.on_phase_changed(msg)

        assert app.state.current_phase == "define"
        assert app.state.iteration == 2

    def test_on_drift_updated(self) -> None:
        """Test handling DriftUpdated message."""
        app = OuroborosTUI()
        msg = DriftUpdated(
            execution_id="exec_123",
            goal_drift=0.15,
            constraint_drift=0.1,
            ontology_drift=0.05,
            combined_drift=0.12,
            is_acceptable=True,
        )

        app.on_drift_updated(msg)

        assert app.state.goal_drift == 0.15
        assert app.state.constraint_drift == 0.1
        assert app.state.ontology_drift == 0.05
        assert app.state.combined_drift == 0.12

    def test_on_cost_updated(self) -> None:
        """Test handling CostUpdated message."""
        app = OuroborosTUI()
        msg = CostUpdated(
            execution_id="exec_123",
            total_tokens=10000,
            total_cost_usd=0.05,
            tokens_this_phase=2500,
        )

        app.on_cost_updated(msg)

        assert app.state.total_tokens == 10000
        assert app.state.total_cost_usd == 0.05

    def test_on_subtask_updated_preserves_runtime_activity_on_tree_node(self) -> None:
        """Sub-AC runtime snapshots should remain attached to the rendered tree node."""
        app = OuroborosTUI()
        app.state.ac_tree = {
            "root_id": "root",
            "nodes": {
                "root": {
                    "id": "root",
                    "content": "Acceptance Criteria",
                    "children_ids": ["ac_1"],
                },
                "ac_1": {
                    "id": "ac_1",
                    "content": "Composite AC",
                    "status": "executing",
                    "children_ids": [],
                },
            },
        }

        app.on_subtask_updated(
            SubtaskUpdated(
                execution_id="exec_123",
                ac_index=1,
                sub_task_index=1,
                sub_task_id="ac_1_sub_1",
                content="Patch the event bridge",
                status="executing",
                current_tool_activity={
                    "message_type": "tool",
                    "tool_name": "Edit",
                    "tool_input": {"file_path": "src/ouroboros/tui/events.py"},
                },
                last_update={
                    "message_type": "tool",
                    "tool_name": "Edit",
                    "tool_input": {"file_path": "src/ouroboros/tui/events.py"},
                },
            )
        )

        subtask_node = app.state.ac_tree["nodes"]["ac_1_sub_1"]
        assert subtask_node["parent_id"] == "ac_1"
        assert subtask_node["current_tool_activity"] == {
            "message_type": "tool",
            "tool_name": "Edit",
            "tool_input": {"file_path": "src/ouroboros/tui/events.py"},
        }
        assert subtask_node["last_update"] == {
            "message_type": "tool",
            "tool_name": "Edit",
            "tool_input": {"file_path": "src/ouroboros/tui/events.py"},
        }
        assert app.state.ac_tree["nodes"]["ac_1"]["children_ids"] == ["ac_1_sub_1"]

    def test_on_pause_requested(self) -> None:
        """Test handling PauseRequested message."""
        app = OuroborosTUI()
        app.set_execution("exec_123")
        initial_log_count = len(app.state.logs)
        msg = PauseRequested(execution_id="exec_123")

        app.on_pause_requested(msg)

        assert app.state.is_paused is True
        assert app.state.status == "paused"
        # Check log was added (one more than before)
        assert len(app.state.logs) == initial_log_count + 1
        assert "Pause requested" in app.state.logs[-1]["message"]

    def test_on_resume_requested(self) -> None:
        """Test handling ResumeRequested message."""
        app = OuroborosTUI()
        app.set_execution("exec_123")
        app._state.is_paused = True
        app._state.status = "paused"
        initial_log_count = len(app.state.logs)
        msg = ResumeRequested(execution_id="exec_123")

        app.on_resume_requested(msg)

        assert app.state.is_paused is False
        assert app.state.status == "running"
        # Check log was added (one more than before)
        assert len(app.state.logs) == initial_log_count + 1
        assert "Resume requested" in app.state.logs[-1]["message"]


class TestOuroborosTUIActions:
    """Tests for TUI actions."""

    def test_action_pause_posts_message(self) -> None:
        """Test pause action posts message when execution active."""
        app = OuroborosTUI()
        app.set_execution("exec_123")
        app.post_message = MagicMock()  # type: ignore

        app.action_pause()

        # Should have posted a PauseRequested message
        app.post_message.assert_called_once()
        msg = app.post_message.call_args[0][0]
        assert isinstance(msg, PauseRequested)
        assert msg.execution_id == "exec_123"

    def test_action_pause_no_execution(self) -> None:
        """Test pause action does nothing without execution."""
        app = OuroborosTUI()
        app.post_message = MagicMock()  # type: ignore

        app.action_pause()

        app.post_message.assert_not_called()

    def test_action_pause_already_paused(self) -> None:
        """Test pause action does nothing when already paused."""
        app = OuroborosTUI()
        app.set_execution("exec_123")
        app._state.is_paused = True
        app.post_message = MagicMock()  # type: ignore

        app.action_pause()

        app.post_message.assert_not_called()

    def test_action_resume_posts_message(self) -> None:
        """Test resume action posts message when paused."""
        app = OuroborosTUI()
        app.set_execution("exec_123")
        app._state.is_paused = True
        app.post_message = MagicMock()  # type: ignore

        app.action_resume()

        app.post_message.assert_called_once()
        msg = app.post_message.call_args[0][0]
        assert isinstance(msg, ResumeRequested)

    def test_action_resume_not_paused(self) -> None:
        """Test resume action does nothing when not paused."""
        app = OuroborosTUI()
        app.set_execution("exec_123")
        app._state.is_paused = False
        app.post_message = MagicMock()  # type: ignore

        app.action_resume()

        app.post_message.assert_not_called()


class TestOuroborosTUIEventSubscription:
    """Tests for event store subscription."""

    @pytest.mark.asyncio
    async def test_subscription_reads_active_session_events(
        self,
        memory_event_store: EventStore,
    ) -> None:
        """Session aggregate events should update HUD state for the active context."""
        await memory_event_store.append(
            BaseEvent(
                type="orchestrator.session.started",
                aggregate_type="session",
                aggregate_id="sess_active",
                data={"execution_id": "exec_active"},
            )
        )
        await memory_event_store.append(
            BaseEvent(
                type="workflow.progress.updated",
                aggregate_type="execution",
                aggregate_id="exec_active",
                data={
                    "execution_id": "exec_active",
                    "current_phase": "deliver",
                    "completed_count": 1,
                    "total_count": 2,
                    "acceptance_criteria": [
                        {"index": 1, "content": "First criterion", "status": "completed"},
                        {"index": 2, "content": "Second criterion", "status": "in_progress"},
                    ],
                },
            )
        )

        app = OuroborosTUI(event_store=memory_event_store)
        app._poll_interval_seconds = 0.01
        app.set_execution("exec_active", "sess_active")
        await asyncio.sleep(0.03)

        await memory_event_store.append(
            BaseEvent(
                type="orchestrator.session.completed",
                aggregate_type="session",
                aggregate_id="sess_active",
                data={"execution_id": "exec_active"},
            )
        )
        await asyncio.sleep(0.03)

        assert app.state.execution_id == "exec_active"
        assert app.state.session_id == "sess_active"
        assert app.state.status == "completed"

        if app._subscription_task is not None:
            app._subscription_task.cancel()
            await app._subscription_task

    @pytest.mark.asyncio
    async def test_subscription_task_drops_stale_context_parameters(self) -> None:
        """A running poller must not start listening to a new context implicitly."""

        class RecordingEventStore:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str, int]] = []

            async def get_events_after(
                self,
                aggregate_type: str,
                aggregate_id: str,
                last_row_id: int = 0,
            ) -> tuple[list[BaseEvent], int]:
                self.calls.append((aggregate_type, aggregate_id, last_row_id))
                return [], last_row_id

        event_store = RecordingEventStore()
        app = OuroborosTUI(event_store=event_store)  # type: ignore[arg-type]
        app._poll_interval_seconds = 0.01
        app._execution_id = "exec_old"
        app.state.session_id = "sess_old"
        app._subscription_generation = 1
        context = _EventSubscriptionContext(
            execution_id="exec_old",
            session_id="sess_old",
            generation=1,
        )

        task = asyncio.create_task(app._subscribe_to_events(context))
        await asyncio.sleep(0.03)
        app._execution_id = "exec_new"
        app.state.session_id = "sess_new"
        await asyncio.wait_for(task, timeout=0.1)

        assert task.done()
        assert ("session", "sess_new", 0) not in event_store.calls
        assert ("execution", "exec_new", 0) not in event_store.calls
        assert any(call[:2] == ("session", "sess_old") for call in event_store.calls)
        assert any(call[:2] == ("execution", "exec_old") for call in event_store.calls)

    @pytest.mark.asyncio
    async def test_update_state_from_event_session_started(self) -> None:
        """Test state update from session started event."""
        app = OuroborosTUI()
        event = BaseEvent(
            type="orchestrator.session.started",
            aggregate_type="session",
            aggregate_id="sess_123",
            data={"execution_id": "exec_456"},
        )

        app._update_state_from_event(event)

        assert app.state.execution_id == "exec_456"
        assert app.state.session_id == "sess_123"
        assert app.state.status == "running"

    @pytest.mark.asyncio
    async def test_update_state_from_event_phase_completed(self) -> None:
        """Test state update from phase completed event."""
        app = OuroborosTUI()
        event = BaseEvent(
            type="execution.phase.completed",
            aggregate_type="execution",
            aggregate_id="exec_123",
            data={"phase": "design", "iteration": 3},
        )

        app._update_state_from_event(event)

        assert app.state.current_phase == "design"
        assert app.state.iteration == 3

    @pytest.mark.asyncio
    async def test_update_state_from_event_drift_measured(self) -> None:
        """Test state update from drift measured event."""
        app = OuroborosTUI()
        event = BaseEvent(
            type="observability.drift.measured",
            aggregate_type="execution",
            aggregate_id="exec_123",
            data={
                "goal_drift": 0.2,
                "constraint_drift": 0.15,
                "ontology_drift": 0.1,
                "combined_drift": 0.17,
            },
        )

        app._update_state_from_event(event)

        assert app.state.goal_drift == 0.2
        assert app.state.constraint_drift == 0.15
        assert app.state.ontology_drift == 0.1
        assert app.state.combined_drift == 0.17

    @pytest.mark.asyncio
    async def test_call_pause_callback_sync(self) -> None:
        """Test calling sync pause callback."""
        app = OuroborosTUI()
        callback = MagicMock()
        app.set_pause_callback(callback)

        await app._call_pause_callback("exec_123")

        callback.assert_called_once_with("exec_123")

    @pytest.mark.asyncio
    async def test_call_pause_callback_async(self) -> None:
        """Test calling async pause callback."""
        app = OuroborosTUI()
        callback = AsyncMock()
        app.set_pause_callback(callback)

        await app._call_pause_callback("exec_123")

        callback.assert_called_once_with("exec_123")

    @pytest.mark.asyncio
    async def test_call_resume_callback_sync(self) -> None:
        """Test calling sync resume callback."""
        app = OuroborosTUI()
        callback = MagicMock()
        app.set_resume_callback(callback)

        await app._call_resume_callback("exec_123")

        callback.assert_called_once_with("exec_123")

    @pytest.mark.asyncio
    async def test_call_resume_callback_async(self) -> None:
        """Test calling async resume callback."""
        app = OuroborosTUI()
        callback = AsyncMock()
        app.set_resume_callback(callback)

        await app._call_resume_callback("exec_123")

        callback.assert_called_once_with("exec_123")

    @pytest.mark.asyncio
    async def test_callback_error_handling(self) -> None:
        """Test that callback errors are logged."""
        app = OuroborosTUI()
        callback = MagicMock(side_effect=ValueError("Test error"))
        app.set_pause_callback(callback)

        await app._call_pause_callback("exec_123")

        # Should have logged the error
        assert len(app.state.logs) == 1
        assert "Pause callback failed" in app.state.logs[0]["message"]
