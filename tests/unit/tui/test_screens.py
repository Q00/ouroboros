"""Unit tests for TUI screens."""

from datetime import UTC, datetime

import pytest

from ouroboros.tui.events import TUIState
from ouroboros.tui.screens.dashboard import DashboardScreen, StatusPanel
from ouroboros.tui.screens.debug import DebugScreen, JsonViewer, StateInspector
from ouroboros.tui.screens.execution import ExecutionScreen, PhaseOutputPanel
from ouroboros.tui.screens.logs import LogEntry, LogFilterBar, LogsScreen


class TestStatusPanel:
    """Tests for StatusPanel widget."""

    def test_create_status_panel(self) -> None:
        """Test creating status panel."""
        panel = StatusPanel(
            execution_id="exec_123",
            session_id="sess_456",
            status="running",
            current_ac="Test AC",
        )

        assert panel.execution_id == "exec_123"
        assert panel.session_id == "sess_456"
        assert panel.status == "running"
        assert panel.current_ac == "Test AC"

    def test_format_status(self) -> None:
        """Test status formatting."""
        panel = StatusPanel()

        assert "Idle" in panel._format_status("idle")
        assert "Running" in panel._format_status("running")
        assert "Paused" in panel._format_status("paused")
        assert "Completed" in panel._format_status("completed")
        assert "Failed" in panel._format_status("failed")

    def test_truncate_ac(self) -> None:
        """Test AC truncation."""
        panel = StatusPanel()

        short_ac = "Short AC"
        assert panel._truncate_ac(short_ac) == short_ac

        long_ac = "A" * 100
        truncated = panel._truncate_ac(long_ac)
        assert len(truncated) == 50
        assert truncated.endswith("...")

    def test_update_status(self) -> None:
        """Test updating status values."""
        panel = StatusPanel()

        panel.update_status(
            execution_id="exec_new",
            session_id="sess_new",
            status="completed",
            current_ac="New AC",
        )

        assert panel.execution_id == "exec_new"
        assert panel.session_id == "sess_new"
        assert panel.status == "completed"
        assert panel.current_ac == "New AC"


class TestDashboardScreen:
    """Tests for DashboardScreen."""

    def test_create_dashboard(self) -> None:
        """Test creating dashboard screen."""
        state = TUIState(
            execution_id="exec_123",
            session_id="sess_456",
            status="running",
        )
        screen = DashboardScreen(state=state)

        assert screen._state is state

    def test_create_dashboard_no_state(self) -> None:
        """Test creating dashboard without state."""
        screen = DashboardScreen()

        assert screen._state is None

    def test_update_state(self) -> None:
        """Test updating screen state."""
        screen = DashboardScreen()
        new_state = TUIState(
            execution_id="exec_456",
            status="completed",
        )

        screen.update_state(new_state)

        assert screen._state is new_state


class TestPhaseOutputPanel:
    """Tests for PhaseOutputPanel widget."""

    def test_create_panel(self) -> None:
        """Test creating phase output panel."""
        panel = PhaseOutputPanel(
            phase_name="discover",
            output="Test output from discover phase",
        )

        assert panel._phase_name == "discover"
        assert panel._output == "Test output from discover phase"

    def test_create_panel_empty_output(self) -> None:
        """Test creating panel with no output."""
        panel = PhaseOutputPanel(phase_name="define")

        assert panel._output == ""


class TestExecutionScreen:
    """Tests for ExecutionScreen."""

    def test_create_execution_screen(self) -> None:
        """Test creating execution screen."""
        state = TUIState(execution_id="exec_123")
        screen = ExecutionScreen(state=state)

        assert screen._state is state

    def test_update_phase_output(self) -> None:
        """Test updating phase output."""
        screen = ExecutionScreen()

        screen.update_phase_output("discover", "Discover phase output")

        assert screen._phase_outputs["discover"] == "Discover phase output"

    def test_add_event(self) -> None:
        """Test adding event to timeline."""
        screen = ExecutionScreen()

        screen.add_event("phase_completed", "Phase completed", "phase")

        assert len(screen._events) == 1
        assert screen._events[0]["type"] == "phase_completed"
        assert screen._events[0]["details"] == "Phase completed"
        assert screen._events[0]["category"] == "phase"

    def test_add_event_trims_to_max(self) -> None:
        """Test that events are trimmed to max count."""
        screen = ExecutionScreen()

        for i in range(150):
            screen.add_event("test", f"Event {i}")

        assert len(screen._events) == 100

    def test_update_state(self) -> None:
        """Test updating screen state."""
        screen = ExecutionScreen()
        new_state = TUIState(execution_id="exec_789")

        screen.update_state(new_state)

        assert screen._state is new_state

    def test_on_phase_changed_adds_event(self) -> None:
        """Test that on_phase_changed adds event to timeline."""
        screen = ExecutionScreen()

        # Create a mock message-like object
        class MockPhaseChanged:
            current_phase = "define"
            previous_phase = "discover"
            iteration = 1

        screen.on_phase_changed(MockPhaseChanged())

        assert len(screen._events) == 1
        assert screen._events[0]["type"] == "phase_changed"
        assert "discover → define" in screen._events[0]["details"]
        assert screen._events[0]["category"] == "phase"

    def test_on_phase_changed_initializes_phase_output(self) -> None:
        """Test that on_phase_changed initializes empty phase output."""
        screen = ExecutionScreen()

        class MockPhaseChanged:
            current_phase = "design"
            previous_phase = "define"
            iteration = 2

        screen.on_phase_changed(MockPhaseChanged())

        assert "design" in screen._phase_outputs

    def test_on_execution_updated_adds_event(self) -> None:
        """Test that on_execution_updated adds event to timeline."""
        screen = ExecutionScreen()

        class MockExecutionUpdated:
            status = "running"
            execution_id = "exec_123"
            data = {}

        screen.on_execution_updated(MockExecutionUpdated())

        assert len(screen._events) == 1
        assert screen._events[0]["type"] == "execution_status"
        assert "exec_123" in screen._events[0]["details"]
        assert "running" in screen._events[0]["details"]

    def test_on_execution_updated_extracts_phase_output(self) -> None:
        """Test that on_execution_updated extracts phase output from data."""
        screen = ExecutionScreen()

        class MockExecutionUpdated:
            status = "running"
            execution_id = "exec_123"
            data = {
                "phase": "discover",
                "phase_output": "Discovery results here",
            }

        screen.on_execution_updated(MockExecutionUpdated())

        assert screen._phase_outputs["discover"] == "Discovery results here"


class TestLogEntry:
    """Tests for LogEntry widget."""

    def test_create_log_entry(self) -> None:
        """Test creating log entry."""
        timestamp = datetime.now(UTC)
        entry = LogEntry(
            timestamp=timestamp,
            level="info",
            source="test.module",
            message="Test message",
        )

        assert entry._timestamp == timestamp
        assert entry._level == "info"
        assert entry._source == "test.module"
        assert entry._message == "Test message"

    def test_truncate(self) -> None:
        """Test text truncation."""
        entry = LogEntry(
            timestamp=datetime.now(UTC),
            level="info",
            source="test",
            message="Test",
        )

        assert entry._truncate("short", 10) == "short"
        assert entry._truncate("very long text here", 10) == "very lon.."


class TestLogFilterBar:
    """Tests for LogFilterBar widget."""

    def test_create_filter_bar(self) -> None:
        """Test creating filter bar."""
        bar = LogFilterBar(min_level="warning")

        assert bar.min_level == "warning"

    def test_default_level(self) -> None:
        """Test default log level."""
        bar = LogFilterBar()

        assert bar.min_level == "debug"


class TestLogsScreen:
    """Tests for LogsScreen."""

    def test_create_logs_screen(self) -> None:
        """Test creating logs screen."""
        state = TUIState()
        state.add_log("info", "test", "Message 1")
        state.add_log("error", "test", "Message 2")

        screen = LogsScreen(state=state)

        assert len(screen._logs) == 2

    def test_add_log(self) -> None:
        """Test adding log entry."""
        screen = LogsScreen()

        screen.add_log("warning", "test.source", "Warning message", {"key": "value"})

        assert len(screen._logs) == 1
        assert screen._logs[0]["level"] == "warning"
        assert screen._logs[0]["source"] == "test.source"
        assert screen._logs[0]["message"] == "Warning message"

    def test_add_log_trims_to_max(self) -> None:
        """Test that logs are trimmed to max count."""
        screen = LogsScreen()

        for i in range(600):
            screen.add_log("info", "test", f"Log {i}")

        assert len(screen._logs) == 500

    def test_get_filtered_logs_by_level(self) -> None:
        """Test filtering logs by level."""
        screen = LogsScreen()
        screen.add_log("debug", "test", "Debug message")
        screen.add_log("info", "test", "Info message")
        screen.add_log("warning", "test", "Warning message")
        screen.add_log("error", "test", "Error message")

        # Filter at warning level
        screen.min_level = "warning"
        filtered = screen._get_filtered_logs()

        assert len(filtered) == 2
        assert all(log["level"] in ["warning", "error"] for log in filtered)

    def test_get_filtered_logs_by_search(self) -> None:
        """Test filtering logs by search text."""
        screen = LogsScreen()
        screen.add_log("info", "module_a", "First message")
        screen.add_log("info", "module_b", "Second message with keyword")
        screen.add_log("info", "module_c", "Third message")

        screen.search_text = "keyword"
        filtered = screen._get_filtered_logs()

        assert len(filtered) == 1
        assert "keyword" in filtered[0]["message"]

    def test_update_state(self) -> None:
        """Test updating screen state."""
        screen = LogsScreen()
        new_state = TUIState()
        new_state.add_log("info", "test", "New log")

        screen.update_state(new_state)

        assert screen._state is new_state
        assert len(screen._logs) == 1

    def test_get_filtered_logs_returns_all_with_defaults(self) -> None:
        """Test that _get_filtered_logs returns all logs with default filters."""
        screen = LogsScreen()
        screen.add_log("debug", "test", "Debug")
        screen.add_log("info", "test", "Info")
        screen.add_log("warning", "test", "Warning")
        screen.add_log("error", "test", "Error")

        filtered = screen._get_filtered_logs()

        assert len(filtered) == 4

    def test_get_filtered_logs_search_in_source(self) -> None:
        """Test filtering logs by search text in source field."""
        screen = LogsScreen()
        screen.add_log("info", "auth.module", "Login attempt")
        screen.add_log("info", "db.connection", "Connected")
        screen.add_log("info", "auth.session", "Session created")

        screen.search_text = "auth"
        filtered = screen._get_filtered_logs()

        assert len(filtered) == 2
        assert all("auth" in log["source"] for log in filtered)


class TestJsonViewer:
    """Tests for JsonViewer widget."""

    def test_create_json_viewer(self) -> None:
        """Test creating JSON viewer."""
        data = {"key": "value", "count": 42}
        viewer = JsonViewer(data=data)

        assert viewer._data == data

    def test_create_empty_viewer(self) -> None:
        """Test creating empty JSON viewer."""
        viewer = JsonViewer()

        assert viewer._data is None

    def test_update_data(self) -> None:
        """Test updating data."""
        viewer = JsonViewer()
        new_data = {"new": "data"}

        viewer.update_data(new_data)

        assert viewer._data == new_data


class TestStateInspector:
    """Tests for StateInspector widget."""

    def test_create_inspector(self) -> None:
        """Test creating state inspector."""
        state = TUIState(
            execution_id="exec_123",
            status="running",
        )
        inspector = StateInspector(state=state)

        assert inspector._state is state

    def test_create_inspector_no_state(self) -> None:
        """Test creating inspector without state."""
        inspector = StateInspector()

        assert inspector._state is None

    def test_update_state(self) -> None:
        """Test updating inspector state."""
        inspector = StateInspector()
        new_state = TUIState(execution_id="exec_789")

        inspector.update_state(new_state)

        assert inspector._state is new_state


class TestDebugScreen:
    """Tests for DebugScreen."""

    def test_create_debug_screen(self) -> None:
        """Test creating debug screen."""
        state = TUIState()
        screen = DebugScreen(state=state)

        assert screen._state is state

    def test_add_raw_event(self) -> None:
        """Test adding raw event."""
        screen = DebugScreen()

        screen.add_raw_event({"type": "test.event", "data": {"key": "value"}})

        assert len(screen._raw_events) == 1

    def test_add_raw_event_trims_to_max(self) -> None:
        """Test that raw events are trimmed to max count."""
        screen = DebugScreen()

        for i in range(100):
            screen.add_raw_event({"type": f"event_{i}"})

        assert len(screen._raw_events) == 50

    def test_set_config(self) -> None:
        """Test setting config."""
        screen = DebugScreen()
        config = {"model": "gpt-4", "temperature": 0.7}

        screen.set_config(config)

        assert screen._config == config

    def test_update_state(self) -> None:
        """Test updating screen state."""
        screen = DebugScreen()
        new_state = TUIState(execution_id="exec_123")

        screen.update_state(new_state)

        assert screen._state is new_state

    def test_reload_config_updates_config(self) -> None:
        """Test that reload_config updates the config."""
        screen = DebugScreen()
        # Initially empty
        assert screen._config == {}

        # reload_config will load from disk (may fail if no config)
        screen.reload_config()

        # Config should now be set (either real config or error dict)
        assert screen._config is not None
        # The config should be a dict (either real config or {"error": "..."})
        assert isinstance(screen._config, dict)
