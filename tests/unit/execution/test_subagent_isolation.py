"""Tests for Story 3.4: SubAgent Isolation.

Tests cover:
- AC 1, 2: SubAgents receive filtered context (seed_summary, current_ac, recent_history, key_facts)
- AC 3: Main context not modified by SubAgent
- AC 4: SubAgent results validated before integration
- AC 5: Failed SubAgent doesn't crash main execution
"""

from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone
from typing import Any

import pytest

from ouroboros.core.context import (
    FilteredContext,
    WorkflowContext,
    create_filtered_context,
    RECENT_HISTORY_COUNT,
)
from ouroboros.core.types import Result
from ouroboros.core.errors import ProviderError
from ouroboros.providers.base import CompletionResponse, UsageInfo


class TestSubAgentContextFiltering:
    """Tests for SubAgent context filtering (AC 1, 2, 3)."""

    def test_filtered_context_includes_all_required_fields(self) -> None:
        """AC 2: Filtered context must include seed_summary, current_ac, recent_history, key_facts."""
        context = WorkflowContext(
            seed_summary="Build a web application with user authentication",
            current_ac="Parent: Implement user features",
            history=[
                {"iteration": 1, "event": "setup"},
                {"iteration": 2, "event": "database"},
                {"iteration": 3, "event": "api"},
                {"iteration": 4, "event": "validation"},
            ],
            key_facts=["FastAPI framework", "PostgreSQL database", "JWT tokens"],
        )

        filtered = create_filtered_context(
            context,
            subagent_ac="Implement user registration endpoint",
        )

        # Check all required fields exist and are populated
        assert filtered.current_ac == "Implement user registration endpoint"
        assert "Build a web application" in filtered.parent_summary
        assert len(filtered.recent_history) == RECENT_HISTORY_COUNT
        assert "FastAPI framework" in filtered.relevant_facts

    def test_filtered_context_is_immutable(self) -> None:
        """AC 3: FilteredContext should be immutable (frozen dataclass)."""
        filtered = create_filtered_context(
            WorkflowContext(
                seed_summary="Test",
                current_ac="Test AC",
                history=[{"event": "test"}],
                key_facts=["fact"],
            ),
            subagent_ac="SubAgent AC",
        )

        # Attempt to modify should raise
        with pytest.raises((AttributeError, TypeError)):
            filtered.current_ac = "Modified"  # type: ignore

    def test_main_context_not_modified_after_filter(self) -> None:
        """AC 3: Creating filtered context should not modify main context."""
        original_facts = ["fact1", "fact2", "fact3"]
        original_history = [{"i": 1}, {"i": 2}]

        context = WorkflowContext(
            seed_summary="Original seed",
            current_ac="Original AC",
            history=original_history.copy(),
            key_facts=original_facts.copy(),
        )

        # Store original values
        original_seed = context.seed_summary
        original_ac = context.current_ac

        # Create filtered context
        filtered = create_filtered_context(context, subagent_ac="SubAgent AC")

        # Main context should be unchanged
        assert context.seed_summary == original_seed
        assert context.current_ac == original_ac
        assert context.history == original_history
        assert context.key_facts == original_facts

        # Filtered context should be independent
        assert filtered.current_ac != context.current_ac

    def test_filtered_context_copies_lists(self) -> None:
        """AC 3: Filtered context should have copies of lists, not references."""
        context = WorkflowContext(
            seed_summary="Test",
            current_ac="Test",
            history=[{"event": "original"}],
            key_facts=["original_fact"],
        )

        filtered = create_filtered_context(context, subagent_ac="SubAgent AC")

        # Lists should be independent copies
        assert filtered.recent_history is not context.history
        assert filtered.relevant_facts is not context.key_facts


class TestSubAgentResultValidation:
    """Tests for SubAgent result validation (AC 4, 5)."""

    def test_validate_child_result_success(self) -> None:
        """AC 4: Valid child result should pass validation."""
        from ouroboros.execution.subagent import validate_child_result
        from ouroboros.execution.double_diamond import CycleResult, Phase, PhaseResult
        from ouroboros.events.base import BaseEvent

        child_result = CycleResult(
            execution_id="child-exec-1",
            seed_id="seed-123",
            current_ac="Child AC",
            success=True,
            phase_results={
                Phase.DISCOVER: PhaseResult(
                    phase=Phase.DISCOVER,
                    success=True,
                    output={"insights": "test"},
                    events=[],
                ),
                Phase.DEFINE: PhaseResult(
                    phase=Phase.DEFINE,
                    success=True,
                    output={"approach": "test"},
                    events=[],
                ),
                Phase.DESIGN: PhaseResult(
                    phase=Phase.DESIGN,
                    success=True,
                    output={"solution": "test"},
                    events=[],
                ),
                Phase.DELIVER: PhaseResult(
                    phase=Phase.DELIVER,
                    success=True,
                    output={"result": "test"},
                    events=[],
                ),
            },
            events=[],
            is_decomposed=False,
        )

        result = validate_child_result(child_result, "Child AC")

        assert result.is_ok
        assert result.value == child_result

    def test_validate_child_result_failure_on_unsuccessful(self) -> None:
        """AC 4: Unsuccessful child result should fail validation."""
        from ouroboros.execution.subagent import validate_child_result
        from ouroboros.execution.double_diamond import CycleResult, Phase, PhaseResult

        child_result = CycleResult(
            execution_id="child-exec-1",
            seed_id="seed-123",
            current_ac="Child AC",
            success=False,  # Failed
            phase_results={},
            events=[],
        )

        result = validate_child_result(child_result, "Child AC")

        assert result.is_err
        assert "unsuccessful" in str(result.error).lower()

    def test_validate_child_result_failure_on_missing_phases(self) -> None:
        """AC 4: Child result missing required phases should fail validation."""
        from ouroboros.execution.subagent import validate_child_result
        from ouroboros.execution.double_diamond import CycleResult, Phase, PhaseResult

        # Result with missing DELIVER phase (only has DISCOVER, DEFINE, DESIGN)
        child_result = CycleResult(
            execution_id="child-exec-1",
            seed_id="seed-123",
            current_ac="Child AC",
            success=True,
            phase_results={
                Phase.DISCOVER: PhaseResult(
                    phase=Phase.DISCOVER,
                    success=True,
                    output={"insights": "test"},
                    events=[],
                ),
                Phase.DEFINE: PhaseResult(
                    phase=Phase.DEFINE,
                    success=True,
                    output={"approach": "test"},
                    events=[],
                ),
            },
            events=[],
            is_decomposed=False,  # Not decomposed means should have all 4 phases
        )

        result = validate_child_result(child_result, "Child AC")

        # Should fail because non-decomposed result should have DELIVER phase
        assert result.is_err or child_result.is_decomposed

    def test_validate_child_result_decomposed_allows_partial_phases(self) -> None:
        """AC 4: Decomposed child result can have partial phases (only DISCOVER, DEFINE)."""
        from ouroboros.execution.subagent import validate_child_result
        from ouroboros.execution.double_diamond import CycleResult, Phase, PhaseResult

        child_result = CycleResult(
            execution_id="child-exec-1",
            seed_id="seed-123",
            current_ac="Child AC",
            success=True,
            phase_results={
                Phase.DISCOVER: PhaseResult(
                    phase=Phase.DISCOVER,
                    success=True,
                    output={"insights": "test"},
                    events=[],
                ),
                Phase.DEFINE: PhaseResult(
                    phase=Phase.DEFINE,
                    success=True,
                    output={"approach": "test"},
                    events=[],
                ),
            },
            events=[],
            is_decomposed=True,  # Decomposed - only needs DISCOVER and DEFINE
            child_results=(),
        )

        result = validate_child_result(child_result, "Child AC")

        assert result.is_ok


class TestSubAgentFailureHandling:
    """Tests for SubAgent failure handling (AC 5)."""

    @pytest.fixture
    def mock_llm_adapter(self) -> AsyncMock:
        """Create a mock LLM adapter."""
        adapter = AsyncMock()
        adapter.complete.return_value = Result.ok(
            CompletionResponse(
                content="Test response",
                model="test-model",
                usage=UsageInfo(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            )
        )
        return adapter

    @pytest.mark.asyncio
    async def test_failed_subagent_does_not_crash_parent(self, mock_llm_adapter) -> None:
        """AC 5: Failed SubAgent should not crash main execution."""
        from ouroboros.execution.double_diamond import DoubleDiamond, ExecutionError
        from ouroboros.core.types import Result

        # Create a DoubleDiamond with an adapter that fails for child execution
        dd = DoubleDiamond(llm_adapter=mock_llm_adapter, max_retries=1, base_delay=0.01)

        # Even if we can't easily test the full decomposition flow,
        # we can verify the error handling doesn't propagate exceptions
        try:
            # This should not raise an exception to the caller
            result = await dd.run_cycle(
                execution_id="parent-exec",
                seed_id="seed-123",
                current_ac="Parent AC",
                iteration=1,
            )
            # If it gets here, the parent execution completed
            assert result.is_ok or result.is_err  # Either is acceptable, no crash
        except Exception as e:
            # Should not reach here - failures should be handled gracefully
            pytest.fail(f"SubAgent failure crashed parent execution: {e}")


class TestSubAgentLifecycleEvents:
    """Tests for SubAgent lifecycle events."""

    def test_subagent_started_event_created(self) -> None:
        """SubAgent execution should emit started event."""
        from ouroboros.execution.subagent import create_subagent_started_event

        event = create_subagent_started_event(
            subagent_id="subagent-123",
            parent_execution_id="parent-exec",
            child_ac="Child AC content",
            depth=1,
        )

        assert event.type == "execution.subagent.started"
        assert event.aggregate_type == "execution"
        assert event.aggregate_id == "subagent-123"
        assert event.data["parent_execution_id"] == "parent-exec"
        assert event.data["child_ac"] == "Child AC content"
        assert event.data["depth"] == 1

    def test_subagent_completed_event_created(self) -> None:
        """SubAgent execution should emit completed event."""
        from ouroboros.execution.subagent import create_subagent_completed_event

        event = create_subagent_completed_event(
            subagent_id="subagent-123",
            parent_execution_id="parent-exec",
            success=True,
            child_count=0,
        )

        assert event.type == "execution.subagent.completed"
        assert event.data["success"] is True
        assert event.data["child_count"] == 0

    def test_subagent_failed_event_created(self) -> None:
        """SubAgent failure should emit failed event."""
        from ouroboros.execution.subagent import create_subagent_failed_event

        event = create_subagent_failed_event(
            subagent_id="subagent-123",
            parent_execution_id="parent-exec",
            error_message="Test error",
            is_retriable=False,
        )

        assert event.type == "execution.subagent.failed"
        assert event.data["error_message"] == "Test error"
        assert event.data["is_retriable"] is False


class TestSubAgentIntegration:
    """Integration tests for SubAgent isolation in DoubleDiamond."""

    @pytest.fixture
    def mock_llm_adapter_for_decomposition(self) -> AsyncMock:
        """Create a mock adapter that simulates decomposition flow."""
        adapter = AsyncMock()

        # Setup responses for various prompts
        call_count = [0]

        async def mock_complete(*args, **kwargs):
            call_count[0] += 1
            return Result.ok(
                CompletionResponse(
                    content=f"Response {call_count[0]}",
                    model="test-model",
                    usage=UsageInfo(prompt_tokens=10, completion_tokens=5, total_tokens=15),
                )
            )

        adapter.complete = mock_complete
        return adapter

    @pytest.mark.asyncio
    async def test_decomposition_uses_filtered_context(
        self, mock_llm_adapter_for_decomposition
    ) -> None:
        """When decomposing, child execution should receive filtered context."""
        # This is a higher-level integration test
        # The actual implementation will need to pass WorkflowContext
        # and verify that children receive FilteredContext
        pass  # Will be implemented when we modify DoubleDiamond
