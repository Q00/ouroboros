"""Unit tests for ouroboros.tui.events module."""

from datetime import UTC, datetime

from ouroboros.events.base import BaseEvent
from ouroboros.tui.events import (
    ACUpdated,
    CostUpdated,
    DriftUpdated,
    ExecutionUpdated,
    LogMessage,
    PauseRequested,
    PhaseChanged,
    ResumeRequested,
    TUIState,
    create_message_from_event,
)


class TestExecutionUpdated:
    """Tests for ExecutionUpdated message."""

    def test_create_execution_updated(self) -> None:
        """Test creating ExecutionUpdated message."""
        msg = ExecutionUpdated(
            execution_id="exec_123",
            session_id="sess_456",
            status="running",
            data={"key": "value"},
        )

        assert msg.execution_id == "exec_123"
        assert msg.session_id == "sess_456"
        assert msg.status == "running"
        assert msg.data == {"key": "value"}

    def test_execution_updated_default_data(self) -> None:
        """Test ExecutionUpdated with default empty data."""
        msg = ExecutionUpdated(
            execution_id="exec_123",
            session_id="sess_456",
            status="running",
        )

        assert msg.data == {}


class TestPhaseChanged:
    """Tests for PhaseChanged message."""

    def test_create_phase_changed(self) -> None:
        """Test creating PhaseChanged message."""
        msg = PhaseChanged(
            execution_id="exec_123",
            previous_phase="discover",
            current_phase="define",
            iteration=1,
        )

        assert msg.execution_id == "exec_123"
        assert msg.previous_phase == "discover"
        assert msg.current_phase == "define"
        assert msg.iteration == 1

    def test_phase_changed_none_previous(self) -> None:
        """Test PhaseChanged with no previous phase."""
        msg = PhaseChanged(
            execution_id="exec_123",
            previous_phase=None,
            current_phase="discover",
            iteration=1,
        )

        assert msg.previous_phase is None


class TestDriftUpdated:
    """Tests for DriftUpdated message."""

    def test_create_drift_updated(self) -> None:
        """Test creating DriftUpdated message."""
        msg = DriftUpdated(
            execution_id="exec_123",
            goal_drift=0.15,
            constraint_drift=0.1,
            ontology_drift=0.05,
            combined_drift=0.12,
            is_acceptable=True,
        )

        assert msg.execution_id == "exec_123"
        assert msg.goal_drift == 0.15
        assert msg.constraint_drift == 0.1
        assert msg.ontology_drift == 0.05
        assert msg.combined_drift == 0.12
        assert msg.is_acceptable is True

    def test_drift_updated_not_acceptable(self) -> None:
        """Test DriftUpdated with unacceptable drift."""
        msg = DriftUpdated(
            execution_id="exec_123",
            goal_drift=0.5,
            constraint_drift=0.3,
            ontology_drift=0.2,
            combined_drift=0.4,
            is_acceptable=False,
        )

        assert msg.is_acceptable is False


class TestCostUpdated:
    """Tests for CostUpdated message."""

    def test_create_cost_updated(self) -> None:
        """Test creating CostUpdated message."""
        msg = CostUpdated(
            execution_id="exec_123",
            total_tokens=10000,
            total_cost_usd=0.05,
            tokens_this_phase=2500,
        )

        assert msg.execution_id == "exec_123"
        assert msg.total_tokens == 10000
        assert msg.total_cost_usd == 0.05
        assert msg.tokens_this_phase == 2500


class TestLogMessage:
    """Tests for LogMessage message."""

    def test_create_log_message(self) -> None:
        """Test creating LogMessage message."""
        timestamp = datetime.now(UTC)
        msg = LogMessage(
            timestamp=timestamp,
            level="info",
            source="test.module",
            message="Test log message",
            data={"extra": "data"},
        )

        assert msg.timestamp == timestamp
        assert msg.level == "info"
        assert msg.source == "test.module"
        assert msg.message == "Test log message"
        assert msg.data == {"extra": "data"}

    def test_log_message_default_data(self) -> None:
        """Test LogMessage with default empty data."""
        msg = LogMessage(
            timestamp=datetime.now(UTC),
            level="error",
            source="test",
            message="Error",
        )

        assert msg.data == {}


class TestACUpdated:
    """Tests for ACUpdated message."""

    def test_create_ac_updated(self) -> None:
        """Test creating ACUpdated message."""
        msg = ACUpdated(
            execution_id="exec_123",
            ac_id="ac_abc123",
            status="atomic",
            depth=1,
            is_atomic=True,
        )

        assert msg.execution_id == "exec_123"
        assert msg.ac_id == "ac_abc123"
        assert msg.status == "atomic"
        assert msg.depth == 1
        assert msg.is_atomic is True


class TestPauseResumeMessages:
    """Tests for pause/resume messages."""

    def test_pause_requested(self) -> None:
        """Test creating PauseRequested message."""
        msg = PauseRequested(execution_id="exec_123")

        assert msg.execution_id == "exec_123"
        assert msg.reason == "user_request"

    def test_pause_requested_custom_reason(self) -> None:
        """Test PauseRequested with custom reason."""
        msg = PauseRequested(
            execution_id="exec_123",
            reason="drift_threshold",
        )

        assert msg.reason == "drift_threshold"

    def test_resume_requested(self) -> None:
        """Test creating ResumeRequested message."""
        msg = ResumeRequested(execution_id="exec_123")

        assert msg.execution_id == "exec_123"


class TestTUIState:
    """Tests for TUIState dataclass."""

    def test_default_state(self) -> None:
        """Test default TUIState values."""
        state = TUIState()

        assert state.execution_id == ""
        assert state.session_id == ""
        assert state.status == "idle"
        assert state.current_phase == ""
        assert state.iteration == 0
        assert state.goal_drift == 0.0
        assert state.constraint_drift == 0.0
        assert state.ontology_drift == 0.0
        assert state.combined_drift == 0.0
        assert state.total_tokens == 0
        assert state.total_cost_usd == 0.0
        assert state.is_paused is False
        assert state.ac_tree == {}
        assert state.logs == []
        assert state.max_logs == 100

    def test_add_log(self) -> None:
        """Test adding log entries."""
        state = TUIState()
        state.add_log("info", "test.source", "Test message", {"key": "value"})

        assert len(state.logs) == 1
        assert state.logs[0]["level"] == "info"
        assert state.logs[0]["source"] == "test.source"
        assert state.logs[0]["message"] == "Test message"
        assert state.logs[0]["data"] == {"key": "value"}
        assert "timestamp" in state.logs[0]

    def test_add_log_trims_to_max(self) -> None:
        """Test that logs are trimmed to max_logs."""
        state = TUIState(max_logs=5)

        for i in range(10):
            state.add_log("info", "test", f"Message {i}")

        assert len(state.logs) == 5
        # Should have the last 5 messages
        assert state.logs[0]["message"] == "Message 5"
        assert state.logs[-1]["message"] == "Message 9"


class TestCreateMessageFromEvent:
    """Tests for create_message_from_event function."""

    def test_session_started_event(self) -> None:
        """Test converting session.started event."""
        event = BaseEvent(
            type="orchestrator.session.started",
            aggregate_type="session",
            aggregate_id="sess_123",
            data={"execution_id": "exec_456"},
        )

        msg = create_message_from_event(event)

        assert isinstance(msg, ExecutionUpdated)
        assert msg.execution_id == "exec_456"
        assert msg.session_id == "sess_123"
        assert msg.status == "running"

    def test_session_completed_event(self) -> None:
        """Test converting session.completed event."""
        event = BaseEvent(
            type="orchestrator.session.completed",
            aggregate_type="session",
            aggregate_id="sess_123",
            data={"execution_id": "exec_456"},
        )

        msg = create_message_from_event(event)

        assert isinstance(msg, ExecutionUpdated)
        assert msg.status == "completed"

    def test_session_failed_event(self) -> None:
        """Test converting session.failed event."""
        event = BaseEvent(
            type="orchestrator.session.failed",
            aggregate_type="session",
            aggregate_id="sess_123",
            data={"error": "Something went wrong"},
        )

        msg = create_message_from_event(event)

        assert isinstance(msg, ExecutionUpdated)
        assert msg.status == "failed"

    def test_session_paused_event(self) -> None:
        """Test converting session.paused event."""
        event = BaseEvent(
            type="orchestrator.session.paused",
            aggregate_type="session",
            aggregate_id="sess_123",
            data={"reason": "user_request"},
        )

        msg = create_message_from_event(event)

        assert isinstance(msg, ExecutionUpdated)
        assert msg.status == "paused"

    def test_phase_completed_event(self) -> None:
        """Test converting phase.completed event."""
        event = BaseEvent(
            type="execution.phase.completed",
            aggregate_type="execution",
            aggregate_id="exec_123",
            data={"phase": "define", "previous_phase": "discover", "iteration": 1},
        )

        msg = create_message_from_event(event)

        assert isinstance(msg, PhaseChanged)
        assert msg.current_phase == "define"
        assert msg.iteration == 1

    def test_drift_measured_event(self) -> None:
        """Test converting drift.measured event."""
        event = BaseEvent(
            type="observability.drift.measured",
            aggregate_type="execution",
            aggregate_id="exec_123",
            data={
                "goal_drift": 0.15,
                "constraint_drift": 0.1,
                "ontology_drift": 0.05,
                "combined_drift": 0.12,
                "is_acceptable": True,
            },
        )

        msg = create_message_from_event(event)

        assert isinstance(msg, DriftUpdated)
        assert msg.goal_drift == 0.15
        assert msg.constraint_drift == 0.1
        assert msg.ontology_drift == 0.05
        assert msg.combined_drift == 0.12
        assert msg.is_acceptable is True

    def test_ac_event(self) -> None:
        """Test converting AC-related events."""
        event = BaseEvent(
            type="decomposition.ac.marked_atomic",
            aggregate_type="execution",
            aggregate_id="exec_123",
            data={"ac_id": "ac_abc", "depth": 1, "is_atomic": True},
        )

        msg = create_message_from_event(event)

        assert isinstance(msg, ACUpdated)
        assert msg.ac_id == "ac_abc"
        assert msg.status == "atomic"
        assert msg.is_atomic is True

    def test_cost_updated_event(self) -> None:
        """Test converting cost.updated event."""
        event = BaseEvent(
            type="observability.cost.updated",
            aggregate_type="execution",
            aggregate_id="exec_123",
            data={
                "total_tokens": 15000,
                "total_cost_usd": 0.075,
                "tokens_this_phase": 3000,
            },
        )

        msg = create_message_from_event(event)

        assert isinstance(msg, CostUpdated)
        assert msg.execution_id == "exec_123"
        assert msg.total_tokens == 15000
        assert msg.total_cost_usd == 0.075
        assert msg.tokens_this_phase == 3000

    def test_cost_updated_event_defaults(self) -> None:
        """Test converting cost.updated event with missing fields."""
        event = BaseEvent(
            type="observability.cost.updated",
            aggregate_type="execution",
            aggregate_id="exec_123",
            data={},
        )

        msg = create_message_from_event(event)

        assert isinstance(msg, CostUpdated)
        assert msg.total_tokens == 0
        assert msg.total_cost_usd == 0.0
        assert msg.tokens_this_phase == 0

    def test_unhandled_event_returns_none(self) -> None:
        """Test that unhandled event types return None."""
        event = BaseEvent(
            type="some.unknown.event",
            aggregate_type="unknown",
            aggregate_id="unknown_123",
            data={},
        )

        msg = create_message_from_event(event)

        assert msg is None
