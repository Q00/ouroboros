"""End-to-end tests for session persistence and resumption.

This module tests the complete session lifecycle:
- Session creation and event tracking
- Session reconstruction from events
- Session resumption after interruption
- State consistency across restarts
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.runner import OrchestratorRunner
from ouroboros.orchestrator.session import SessionRepository, SessionStatus

if TYPE_CHECKING:
    from ouroboros.core.seed import Seed
    from ouroboros.persistence.event_store import EventStore
    from tests.e2e.conftest import MockClaudeAgentAdapter


class TestSessionCreation:
    """Test session creation and initial state."""

    async def test_session_created_on_execution_start(
        self,
        event_store: EventStore,
        sample_seed: Seed,
        mock_successful_agent_adapter: MockClaudeAgentAdapter,
    ) -> None:
        """Test that a session is created when execution starts."""
        runner = OrchestratorRunner(
            adapter=mock_successful_agent_adapter,
            event_store=event_store,
            console=MagicMock(),
        )

        result = await runner.execute_seed(sample_seed)

        assert result.is_ok
        session_id = result.value.session_id

        # Verify session exists in event store
        events = await event_store.replay("session", session_id)
        assert len(events) > 0

        # First event should be session.started
        start_event = events[0]
        assert start_event.type == "orchestrator.session.started"
        assert start_event.data["seed_id"] == sample_seed.metadata.seed_id

    async def test_session_has_unique_id(
        self,
        event_store: EventStore,
        sample_seed: Seed,
        mock_successful_agent_adapter: MockClaudeAgentAdapter,
    ) -> None:
        """Test that each execution creates a unique session ID."""
        runner = OrchestratorRunner(
            adapter=mock_successful_agent_adapter,
            event_store=event_store,
            console=MagicMock(),
        )

        # Execute twice
        result1 = await runner.execute_seed(sample_seed)
        result2 = await runner.execute_seed(sample_seed)

        assert result1.is_ok and result2.is_ok
        assert result1.value.session_id != result2.value.session_id

    async def test_session_tracks_execution_id(
        self,
        event_store: EventStore,
        sample_seed: Seed,
        mock_successful_agent_adapter: MockClaudeAgentAdapter,
    ) -> None:
        """Test that session tracks the execution ID."""
        runner = OrchestratorRunner(
            adapter=mock_successful_agent_adapter,
            event_store=event_store,
            console=MagicMock(),
        )

        custom_exec_id = "exec_custom_001"
        result = await runner.execute_seed(sample_seed, execution_id=custom_exec_id)

        assert result.is_ok
        assert result.value.execution_id == custom_exec_id

        # Verify in events
        events = await event_store.replay("session", result.value.session_id)
        start_event = events[0]
        assert start_event.data["execution_id"] == custom_exec_id


class TestSessionEventTracking:
    """Test event tracking during session execution."""

    async def test_progress_events_emitted(
        self,
        event_store: EventStore,
        sample_seed: Seed,
        mock_claude_agent_adapter: MockClaudeAgentAdapter,
    ) -> None:
        """Test that progress events are emitted during execution."""
        # Create a sequence with enough messages to trigger progress events
        messages = []
        for i in range(15):  # PROGRESS_EMIT_INTERVAL is 10
            messages.append(AgentMessage(type="assistant", content=f"Step {i}"))
        messages.append(
            AgentMessage(
                type="result",
                content="Done",
                data={"subtype": "success"},
            )
        )
        mock_claude_agent_adapter.add_execution_sequence(messages)

        runner = OrchestratorRunner(
            adapter=mock_claude_agent_adapter,
            event_store=event_store,
            console=MagicMock(),
        )

        result = await runner.execute_seed(sample_seed)
        assert result.is_ok

        # Check for session events
        events = await event_store.replay("session", result.value.session_id)
        event_types = [e.type for e in events]

        # Should have session lifecycle events
        assert "orchestrator.session.started" in event_types
        assert "orchestrator.session.completed" in event_types

    async def test_tool_called_events_emitted(
        self,
        event_store: EventStore,
        sample_seed: Seed,
        mock_claude_agent_adapter: MockClaudeAgentAdapter,
    ) -> None:
        """Test that session events track execution."""
        messages = [
            AgentMessage(type="tool", content="Reading", tool_name="Read"),
            AgentMessage(type="tool", content="Writing", tool_name="Write"),
            AgentMessage(
                type="result",
                content="Done",
                data={"subtype": "success"},
            ),
        ]
        mock_claude_agent_adapter.add_execution_sequence(messages)

        runner = OrchestratorRunner(
            adapter=mock_claude_agent_adapter,
            event_store=event_store,
            console=MagicMock(),
        )

        result = await runner.execute_seed(sample_seed)
        assert result.is_ok

        # Verify session was created and completed
        events = await event_store.replay("session", result.value.session_id)
        event_types = [e.type for e in events]

        assert "orchestrator.session.started" in event_types
        assert "orchestrator.session.completed" in event_types
        assert result.value.success

    async def test_completion_event_emitted_on_success(
        self,
        event_store: EventStore,
        sample_seed: Seed,
        mock_successful_agent_adapter: MockClaudeAgentAdapter,
    ) -> None:
        """Test that completion event is emitted on successful execution."""
        runner = OrchestratorRunner(
            adapter=mock_successful_agent_adapter,
            event_store=event_store,
            console=MagicMock(),
        )

        result = await runner.execute_seed(sample_seed)
        assert result.is_ok

        events = await event_store.replay("session", result.value.session_id)
        event_types = [e.type for e in events]

        assert "orchestrator.session.completed" in event_types

    async def test_failure_event_emitted_on_error(
        self,
        event_store: EventStore,
        sample_seed: Seed,
        mock_claude_agent_adapter: MockClaudeAgentAdapter,
    ) -> None:
        """Test that failure event is emitted on failed execution."""
        mock_claude_agent_adapter.add_failed_execution(
            error_message="Execution failed due to timeout"
        )

        runner = OrchestratorRunner(
            adapter=mock_claude_agent_adapter,
            event_store=event_store,
            console=MagicMock(),
        )

        result = await runner.execute_seed(sample_seed)
        assert result.is_ok  # Result is ok but success is false

        events = await event_store.replay("session", result.value.session_id)
        event_types = [e.type for e in events]

        assert "orchestrator.session.failed" in event_types


class TestSessionReconstruction:
    """Test session reconstruction from events."""

    async def test_reconstruct_running_session(
        self,
        event_store: EventStore,
        mock_session_id: str,
        mock_execution_id: str,
        sample_seed: Seed,
    ) -> None:
        """Test reconstructing a running session."""
        repo = SessionRepository(event_store)

        # Create and partially track session
        result = await repo.create_session(
            execution_id=mock_execution_id,
            seed_id=sample_seed.metadata.seed_id,
            session_id=mock_session_id,
        )
        assert result.is_ok

        await repo.track_progress(mock_session_id, {"step": 1})
        await repo.track_progress(mock_session_id, {"step": 2})

        # Reconstruct
        reconstruct_result = await repo.reconstruct_session(mock_session_id)

        assert reconstruct_result.is_ok
        tracker = reconstruct_result.value

        assert tracker.session_id == mock_session_id
        assert tracker.execution_id == mock_execution_id
        assert tracker.status == SessionStatus.RUNNING
        assert tracker.messages_processed == 2

    async def test_reconstruct_completed_session(
        self,
        event_store: EventStore,
        mock_session_id: str,
        mock_execution_id: str,
        sample_seed: Seed,
    ) -> None:
        """Test reconstructing a completed session."""
        repo = SessionRepository(event_store)

        await repo.create_session(
            execution_id=mock_execution_id,
            seed_id=sample_seed.metadata.seed_id,
            session_id=mock_session_id,
        )
        await repo.mark_completed(mock_session_id, {"total": 10})

        reconstruct_result = await repo.reconstruct_session(mock_session_id)

        assert reconstruct_result.is_ok
        assert reconstruct_result.value.status == SessionStatus.COMPLETED

    async def test_reconstruct_failed_session(
        self,
        event_store: EventStore,
        mock_session_id: str,
        mock_execution_id: str,
        sample_seed: Seed,
    ) -> None:
        """Test reconstructing a failed session."""
        repo = SessionRepository(event_store)

        await repo.create_session(
            execution_id=mock_execution_id,
            seed_id=sample_seed.metadata.seed_id,
            session_id=mock_session_id,
        )
        await repo.mark_failed(mock_session_id, "Connection error")

        reconstruct_result = await repo.reconstruct_session(mock_session_id)

        assert reconstruct_result.is_ok
        assert reconstruct_result.value.status == SessionStatus.FAILED

    async def test_reconstruct_nonexistent_session(
        self,
        event_store: EventStore,
    ) -> None:
        """Test reconstructing a session that doesn't exist."""
        repo = SessionRepository(event_store)

        result = await repo.reconstruct_session("nonexistent_session")

        assert result.is_err
        assert "No events found" in str(result.error)


class TestSessionResumption:
    """Test session resumption functionality."""

    async def test_resume_running_session(
        self,
        persisted_session: dict[str, Any],
        event_store: EventStore,
        sample_seed: Seed,
        mock_claude_agent_adapter: MockClaudeAgentAdapter,
    ) -> None:
        """Test resuming a running session."""
        mock_claude_agent_adapter.add_successful_execution(final_message="Resumed and completed")

        runner = OrchestratorRunner(
            adapter=mock_claude_agent_adapter,
            event_store=event_store,
            console=MagicMock(),
        )

        result = await runner.resume_session(
            persisted_session["session_id"],
            sample_seed,
        )

        assert result.is_ok
        assert result.value.success

    async def test_resume_preserves_session_id(
        self,
        persisted_session: dict[str, Any],
        event_store: EventStore,
        sample_seed: Seed,
        mock_claude_agent_adapter: MockClaudeAgentAdapter,
    ) -> None:
        """Test that resuming preserves the original session ID."""
        mock_claude_agent_adapter.add_successful_execution()

        runner = OrchestratorRunner(
            adapter=mock_claude_agent_adapter,
            event_store=event_store,
            console=MagicMock(),
        )

        result = await runner.resume_session(
            persisted_session["session_id"],
            sample_seed,
        )

        assert result.is_ok
        assert result.value.session_id == persisted_session["session_id"]

    async def test_cannot_resume_completed_session(
        self,
        event_store: EventStore,
        sample_seed: Seed,
        mock_claude_agent_adapter: MockClaudeAgentAdapter,
    ) -> None:
        """Test that completed sessions cannot be resumed."""
        repo = SessionRepository(event_store)

        # Create and complete a session
        _create_result = await repo.create_session(
            execution_id="exec_completed",
            seed_id=sample_seed.metadata.seed_id,
            session_id="completed_session",
        )
        await repo.mark_completed("completed_session")

        runner = OrchestratorRunner(
            adapter=mock_claude_agent_adapter,
            event_store=event_store,
            console=MagicMock(),
        )

        result = await runner.resume_session("completed_session", sample_seed)

        assert result.is_err
        assert "already completed" in str(result.error).lower()

    async def test_resume_nonexistent_session(
        self,
        event_store: EventStore,
        sample_seed: Seed,
        mock_claude_agent_adapter: MockClaudeAgentAdapter,
    ) -> None:
        """Test resuming a session that doesn't exist."""
        runner = OrchestratorRunner(
            adapter=mock_claude_agent_adapter,
            event_store=event_store,
            console=MagicMock(),
        )

        result = await runner.resume_session("nonexistent_session", sample_seed)

        assert result.is_err

    async def test_resume_accumulates_messages(
        self,
        persisted_session: dict[str, Any],
        event_store: EventStore,
        sample_seed: Seed,
        mock_claude_agent_adapter: MockClaudeAgentAdapter,
    ) -> None:
        """Test that resuming accumulates message count correctly."""
        # The persisted_session fixture tracks 2 progress events
        initial_messages = 2

        # Add 5 more messages in the resumed execution
        messages = [
            AgentMessage(type="assistant", content="Resuming..."),
            AgentMessage(type="tool", content="Reading", tool_name="Read"),
            AgentMessage(type="assistant", content="Continuing"),
            AgentMessage(type="tool", content="Writing", tool_name="Write"),
            AgentMessage(
                type="result",
                content="Completed",
                data={"subtype": "success"},
            ),
        ]
        mock_claude_agent_adapter.add_execution_sequence(messages)

        runner = OrchestratorRunner(
            adapter=mock_claude_agent_adapter,
            event_store=event_store,
            console=MagicMock(),
        )

        result = await runner.resume_session(
            persisted_session["session_id"],
            sample_seed,
        )

        assert result.is_ok
        # Messages should include both previous and new
        assert result.value.messages_processed == initial_messages + len(messages)


class TestSessionConsistency:
    """Test session state consistency across operations."""

    async def test_session_state_consistent_after_multiple_operations(
        self,
        event_store: EventStore,
        sample_seed: Seed,
    ) -> None:
        """Test that session state remains consistent after multiple operations."""
        repo = SessionRepository(event_store)
        session_id = "consistency_test_session"

        # Create session
        await repo.create_session(
            execution_id="exec_consistency",
            seed_id=sample_seed.metadata.seed_id,
            session_id=session_id,
        )

        # Track multiple progress updates
        for i in range(10):
            await repo.track_progress(session_id, {"step": i, "progress": i * 10})

        # Reconstruct and verify
        result = await repo.reconstruct_session(session_id)

        assert result.is_ok
        tracker = result.value

        assert tracker.session_id == session_id
        assert tracker.messages_processed == 10
        assert tracker.status == SessionStatus.RUNNING

    async def test_session_events_ordered_by_timestamp(
        self,
        event_store: EventStore,
        sample_seed: Seed,
        mock_successful_agent_adapter: MockClaudeAgentAdapter,
    ) -> None:
        """Test that session events are ordered by timestamp."""
        runner = OrchestratorRunner(
            adapter=mock_successful_agent_adapter,
            event_store=event_store,
            console=MagicMock(),
        )

        result = await runner.execute_seed(sample_seed)
        assert result.is_ok

        events = await event_store.replay("session", result.value.session_id)

        # Events should be in chronological order
        timestamps = [e.timestamp for e in events]
        assert timestamps == sorted(timestamps)

    async def test_concurrent_session_isolation(
        self,
        event_store: EventStore,
        sample_seed: Seed,
        mock_successful_agent_adapter: MockClaudeAgentAdapter,
    ) -> None:
        """Test that concurrent sessions are properly isolated."""
        import asyncio

        runner = OrchestratorRunner(
            adapter=mock_successful_agent_adapter,
            event_store=event_store,
            console=MagicMock(),
        )

        # Start multiple executions concurrently
        results = await asyncio.gather(
            runner.execute_seed(sample_seed, execution_id="exec_1"),
            runner.execute_seed(sample_seed, execution_id="exec_2"),
            runner.execute_seed(sample_seed, execution_id="exec_3"),
        )

        # All should succeed
        for result in results:
            assert result.is_ok

        # Each should have unique session ID
        session_ids = [r.value.session_id for r in results]
        assert len(set(session_ids)) == 3

        # Each session should have its own events
        for session_id in session_ids:
            events = await event_store.replay("session", session_id)
            assert len(events) > 0
            # All events should belong to this session
            for event in events:
                assert event.aggregate_id == session_id


class TestInterviewStatePersistence:
    """Test interview state persistence for init command."""

    async def test_interview_state_saved_to_disk(
        self,
        temp_state_dir: Any,
        mock_interview_llm_provider: Any,
    ) -> None:
        """Test that interview state is saved to disk."""
        from ouroboros.bigbang.interview import InterviewEngine

        engine = InterviewEngine(
            llm_adapter=MagicMock(complete=mock_interview_llm_provider.complete),
            state_dir=temp_state_dir,
        )

        # Start interview
        result = await engine.start_interview("Build a task manager")
        assert result.is_ok
        state = result.value

        # Save state
        save_result = await engine.save_state(state)
        assert save_result.is_ok

        # Verify file exists
        state_file = temp_state_dir / f"interview_{state.interview_id}.json"
        assert state_file.exists()

        # Verify content
        import json

        with open(state_file) as f:
            saved_data = json.load(f)

        assert saved_data["interview_id"] == state.interview_id
        assert saved_data["initial_context"] == "Build a task manager"

    async def test_interview_state_loaded_from_disk(
        self,
        temp_state_dir: Any,
        mock_interview_llm_provider: Any,
    ) -> None:
        """Test that interview state can be loaded from disk."""
        from ouroboros.bigbang.interview import InterviewEngine

        engine = InterviewEngine(
            llm_adapter=MagicMock(complete=mock_interview_llm_provider.complete),
            state_dir=temp_state_dir,
        )

        # Create and save state
        result = await engine.start_interview("Build a CLI tool")
        assert result.is_ok
        state = result.value
        await engine.save_state(state)

        # Load state
        load_result = await engine.load_state(state.interview_id)

        assert load_result.is_ok
        loaded_state = load_result.value

        assert loaded_state.interview_id == state.interview_id
        assert loaded_state.initial_context == state.initial_context

    async def test_interview_state_persists_rounds(
        self,
        temp_state_dir: Any,
        mock_interview_llm_provider: Any,
    ) -> None:
        """Test that interview rounds are persisted correctly."""
        from ouroboros.bigbang.interview import InterviewEngine

        engine = InterviewEngine(
            llm_adapter=MagicMock(complete=mock_interview_llm_provider.complete),
            state_dir=temp_state_dir,
        )

        # Start interview and record responses
        result = await engine.start_interview("Build a web app")
        assert result.is_ok
        state = result.value

        # Ask question and record response
        q_result = await engine.ask_next_question(state)
        assert q_result.is_ok
        question = q_result.value

        r_result = await engine.record_response(state, "Use Python and Flask", question)
        assert r_result.is_ok
        state = r_result.value

        # Save and reload
        await engine.save_state(state)
        load_result = await engine.load_state(state.interview_id)

        assert load_result.is_ok
        loaded_state = load_result.value

        assert len(loaded_state.rounds) == 1
        assert loaded_state.rounds[0].question == question
        assert loaded_state.rounds[0].user_response == "Use Python and Flask"

    async def test_interview_list_returns_all_interviews(
        self,
        temp_state_dir: Any,
        mock_interview_llm_provider: Any,
    ) -> None:
        """Test that list_interviews returns all saved interviews."""
        from ouroboros.bigbang.interview import InterviewEngine

        engine = InterviewEngine(
            llm_adapter=MagicMock(complete=mock_interview_llm_provider.complete),
            state_dir=temp_state_dir,
        )

        # Create multiple interviews
        for i in range(3):
            result = await engine.start_interview(
                f"Project {i}",
                interview_id=f"interview_{i:03d}",
            )
            assert result.is_ok
            await engine.save_state(result.value)

        # List interviews
        interviews = await engine.list_interviews()

        assert len(interviews) == 3
        interview_ids = [i["interview_id"] for i in interviews]
        assert "interview_000" in interview_ids
        assert "interview_001" in interview_ids
        assert "interview_002" in interview_ids
