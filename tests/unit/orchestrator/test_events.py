"""Unit tests for orchestrator event helpers."""

from __future__ import annotations

from ouroboros.orchestrator.capabilities import build_capability_graph
from ouroboros.orchestrator.events import (
    create_ac_postmortem_captured_event,
    create_policy_capabilities_evaluated_event,
    create_postmortem_chain_truncated_event,
    create_progress_event,
    create_session_cancelled_event,
    create_session_completed_event,
    create_session_failed_event,
    create_session_paused_event,
    create_session_started_event,
    create_task_completed_event,
    create_task_started_event,
    create_tool_called_event,
)
from ouroboros.orchestrator.level_context import (
    ACContextSummary,
    ACPostmortem,
    Invariant,
)
from ouroboros.orchestrator.mcp_tools import assemble_session_tool_catalog
from ouroboros.orchestrator.policy import (
    PolicyContext,
    PolicyExecutionPhase,
    PolicySessionRole,
    evaluate_capability_policy,
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

    def test_create_policy_capabilities_evaluated_event_batches_decisions(self) -> None:
        """Batched policy events should preserve per-capability decisions."""
        graph = build_capability_graph(assemble_session_tool_catalog(["Read", "Edit"]))
        context = PolicyContext(
            runtime_backend="opencode",
            session_role=PolicySessionRole.INTERVIEW,
            execution_phase=PolicyExecutionPhase.INTERVIEW,
        )
        decisions = evaluate_capability_policy(graph, context)

        event = create_policy_capabilities_evaluated_event(
            session_id="sess_123",
            graph=graph,
            decisions=decisions,
            context=context,
        )

        assert event.type == "orchestrator.policy.capabilities.evaluated"
        assert event.aggregate_type == "session"
        assert event.aggregate_id == "sess_123"
        assert event.data["capability_count"] == 2
        evaluations = {
            item["capability"]["name"]: item["decision"] for item in event.data["evaluations"]
        }
        assert evaluations["Read"]["executable"] is True
        assert evaluations["Edit"]["executable"] is False
        assert event.data["context"]["session_role"] == "interview"

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

    def test_create_session_cancelled_event(self) -> None:
        """Test creating session cancelled event."""
        event = create_session_cancelled_event(
            session_id="sess_123",
            reason="User requested cancellation",
            cancelled_by="user",
        )

        assert event.type == "orchestrator.session.cancelled"
        assert event.aggregate_type == "session"
        assert event.aggregate_id == "sess_123"
        assert event.data["reason"] == "User requested cancellation"
        assert event.data["cancelled_by"] == "user"
        assert "cancelled_at" in event.data

    def test_create_session_cancelled_event_auto_cleanup(self) -> None:
        """Test creating session cancelled event from auto-cleanup."""
        event = create_session_cancelled_event(
            session_id="sess_123",
            reason="Stale execution detected (>1 hour)",
            cancelled_by="auto_cleanup",
        )

        assert event.data["cancelled_by"] == "auto_cleanup"
        assert event.data["reason"] == "Stale execution detected (>1 hour)"

    def test_create_session_cancelled_event_default_cancelled_by(self) -> None:
        """Test creating session cancelled event with default cancelled_by."""
        event = create_session_cancelled_event(
            session_id="sess_123",
            reason="No longer needed",
        )

        assert event.data["cancelled_by"] == "user"

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
            ac_id="ac_1",
            retry_attempt=2,
        )

        assert event.type == "orchestrator.task.started"
        assert event.aggregate_id == "sess_123"
        assert event.data["task_description"] == "Implement user authentication"
        assert event.data["acceptance_criterion"] == "Users can log in with email and password"
        assert event.data["ac_id"] == "ac_1"
        assert event.data["retry_attempt"] == 2
        assert event.data["attempt_number"] == 3
        assert "started_at" in event.data

    def test_create_task_completed_event_success(self) -> None:
        """Test creating successful task completion event."""
        event = create_task_completed_event(
            session_id="sess_123",
            acceptance_criterion="AC #1",
            success=True,
            result_summary="Implemented login endpoint",
            ac_id="ac_1",
            retry_attempt=1,
        )

        assert event.type == "orchestrator.task.completed"
        assert event.aggregate_id == "sess_123"
        assert event.data["acceptance_criterion"] == "AC #1"
        assert event.data["success"] is True
        assert event.data["result_summary"] == "Implemented login endpoint"
        assert event.data["ac_id"] == "ac_1"
        assert event.data["retry_attempt"] == 1
        assert event.data["attempt_number"] == 2
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
            create_session_cancelled_event("s", "reason"),
            create_session_paused_event("s", "reason"),
            create_progress_event("s", "type", "content"),
            create_task_started_event("s", "desc", "ac"),
            create_task_completed_event("s", "ac", True),
            create_tool_called_event("s", "tool"),
        ]

        for event in events:
            assert event.aggregate_type == "session"


class TestPostmortemCapturedEvent:
    """Tests for execution.ac.postmortem.captured event."""

    def _mk_pm(self, *, status: str = "pass") -> ACPostmortem:
        summary = ACContextSummary(
            ac_index=2,
            ac_content="Add JWT auth",
            success=(status == "pass"),
            files_modified=("src/auth.py",),
        )
        return ACPostmortem(
            summary=summary,
            diff_summary=" src/auth.py | 42 +++",
            invariants_established=(Invariant(text="AUTH_HEADER required"),),
            status=status,  # type: ignore[arg-type]
            duration_seconds=1.5,
        )

    def test_basic_event_shape(self) -> None:
        event = create_ac_postmortem_captured_event(
            session_id="sess_1",
            ac_index=2,
            ac_id="ac_2",
            postmortem=self._mk_pm(),
            execution_id="exec_1",
            retry_attempt=0,
        )
        assert event.type == "execution.ac.postmortem.captured"
        assert event.aggregate_type == "execution"
        assert event.aggregate_id == "ac_2"
        assert event.data["session_id"] == "sess_1"
        assert event.data["execution_id"] == "exec_1"
        assert event.data["ac_index"] == 2
        assert event.data["retry_attempt"] == 0
        assert event.data["status"] == "pass"
        assert "timestamp" in event.data

    def test_postmortem_payload_round_trippable(self) -> None:
        from ouroboros.orchestrator.level_context import deserialize_postmortem_chain

        event = create_ac_postmortem_captured_event(
            session_id="sess_1",
            ac_index=2,
            ac_id="ac_2",
            postmortem=self._mk_pm(),
        )
        # The serialized single postmortem should round-trip via the
        # chain deserializer without loss.
        chain = deserialize_postmortem_chain([event.data["postmortem"]])
        assert len(chain.postmortems) == 1
        pm = chain.postmortems[0]
        assert pm.summary.ac_content == "Add JWT auth"
        assert len(pm.invariants_established) == 1
        assert pm.invariants_established[0].text == "AUTH_HEADER required"
        assert pm.diff_summary == " src/auth.py | 42 +++"
        assert pm.status == "pass"

    def test_retry_attempt_captured(self) -> None:
        event = create_ac_postmortem_captured_event(
            session_id="sess_1",
            ac_index=2,
            ac_id="ac_2",
            postmortem=self._mk_pm(status="fail"),
            retry_attempt=3,
        )
        assert event.data["retry_attempt"] == 3
        assert event.data["status"] == "fail"

    def test_execution_id_optional(self) -> None:
        event = create_ac_postmortem_captured_event(
            session_id="sess_1",
            ac_index=0,
            ac_id="ac_0",
            postmortem=self._mk_pm(),
        )
        assert "execution_id" not in event.data


class TestPostmortemChainTruncatedEvent:
    """Tests for execution.postmortem_chain.truncated event (Q7).

    Verifies that create_postmortem_chain_truncated_event mirrors the
    create_ac_postmortem_captured_event factory pattern: aggregate_type="execution",
    aggregate_id=execution_id, and all truncation-count fields present.

    [[INVARIANT: event type is execution.postmortem_chain.truncated]]
    [[INVARIANT: Truncation event uses aggregate_type execution, keyed on execution_id]]
    """

    def _call(self, **overrides) -> object:
        """Helper to construct the event with sensible defaults."""
        kwargs = {
            "session_id": "sess_trunc",
            "execution_id": "exec_trunc",
            "dropped_count": 3,
            "char_budget": 8000,
            "rendered_chars": 7500,
            "full_forms_preserved": 2,
            "cumulative_invariants_preserved": 5,
        }
        kwargs.update(overrides)
        return create_postmortem_chain_truncated_event(**kwargs)

    def test_event_type(self) -> None:
        """Event type must be execution.postmortem_chain.truncated."""
        event = self._call()
        assert event.type == "execution.postmortem_chain.truncated"

    def test_aggregate_mirrors_postmortem_captured_pattern(self) -> None:
        """aggregate_type must be 'execution', aggregate_id must be execution_id.

        Mirrors create_ac_postmortem_captured_event which also uses
        aggregate_type='execution' keyed on the execution unit id.
        """
        event = self._call(execution_id="exec_42")
        assert event.aggregate_type == "execution"
        assert event.aggregate_id == "exec_42"

    def test_all_truncation_fields_present(self) -> None:
        """All truncation-count fields appear in event data."""
        event = self._call(
            dropped_count=7,
            char_budget=16000,
            rendered_chars=14200,
            full_forms_preserved=3,
            cumulative_invariants_preserved=11,
        )
        assert event.data["dropped_count"] == 7
        assert event.data["char_budget"] == 16000
        assert event.data["rendered_chars"] == 14200
        assert event.data["full_forms_preserved"] == 3
        assert event.data["cumulative_invariants_preserved"] == 11

    def test_session_and_execution_ids_in_data(self) -> None:
        """Both session_id and execution_id appear in the data payload."""
        event = self._call(session_id="sess_99", execution_id="exec_99")
        assert event.data["session_id"] == "sess_99"
        assert event.data["execution_id"] == "exec_99"

    def test_timestamp_present(self) -> None:
        """A timestamp field is emitted (ISO-8601 string)."""
        event = self._call()
        assert "timestamp" in event.data
        # Should be parseable as an ISO-8601 datetime
        from datetime import datetime

        datetime.fromisoformat(event.data["timestamp"])

    def test_zero_dropped_count_valid(self) -> None:
        """The factory accepts dropped_count=0 as a valid input — constructor-level
        validation, not runtime emission. At runtime, the level_context truncation
        gate only invokes the callback (and thus this factory) when dropped_count
        is strictly positive; this test simply asserts the factory itself does
        not reject zero, in case a future caller passes the value verbatim.
        """
        event = self._call(dropped_count=0, rendered_chars=7999)
        assert event.data["dropped_count"] == 0
        assert event.type == "execution.postmortem_chain.truncated"

    def test_zero_invariants_preserved_valid(self) -> None:
        """cumulative_invariants_preserved=0 when no above-threshold invariants exist."""
        event = self._call(cumulative_invariants_preserved=0)
        assert event.data["cumulative_invariants_preserved"] == 0
