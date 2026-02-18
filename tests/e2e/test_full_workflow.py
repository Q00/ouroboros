"""End-to-end tests for complete workflow execution.

This module tests the full Ouroboros workflow from seed to completion:
- Seed loading and validation
- Orchestrator execution with mocked LLM providers
- Progress tracking and event emission
- Success and failure scenarios
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from ouroboros.core.seed import Seed
from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.runner import (
    OrchestratorRunner,
    build_system_prompt,
    build_task_prompt,
)

if TYPE_CHECKING:
    from tests.e2e.conftest import MockClaudeAgentAdapter, MockLLMProvider, WorkflowSimulator


class TestSeedValidation:
    """Test seed loading and validation in E2E context."""

    def test_seed_from_yaml(self, temp_seed_file: Path, sample_seed: Seed) -> None:
        """Test loading seed from YAML file."""
        import yaml

        with open(temp_seed_file) as f:
            data = yaml.safe_load(f)

        loaded_seed = Seed.from_dict(data)

        assert loaded_seed.goal == sample_seed.goal
        assert loaded_seed.acceptance_criteria == sample_seed.acceptance_criteria
        assert loaded_seed.constraints == sample_seed.constraints

    def test_minimal_seed_validation(self, minimal_seed: Seed) -> None:
        """Test that minimal seed is valid."""
        assert minimal_seed.goal
        assert len(minimal_seed.acceptance_criteria) > 0
        assert minimal_seed.ontology_schema.name

    def test_seed_to_dict_roundtrip(self, sample_seed: Seed) -> None:
        """Test seed serialization/deserialization roundtrip."""
        data = sample_seed.to_dict()
        restored = Seed.from_dict(data)

        assert restored.goal == sample_seed.goal
        assert restored.constraints == sample_seed.constraints
        assert restored.acceptance_criteria == sample_seed.acceptance_criteria


class TestPromptBuilding:
    """Test prompt building for orchestrator execution."""

    def test_system_prompt_includes_goal(self, sample_seed: Seed) -> None:
        """Test that system prompt includes seed goal."""
        prompt = build_system_prompt(sample_seed)

        assert sample_seed.goal in prompt

    def test_system_prompt_includes_constraints(self, sample_seed: Seed) -> None:
        """Test that system prompt includes constraints."""
        prompt = build_system_prompt(sample_seed)

        for constraint in sample_seed.constraints:
            assert constraint in prompt

    def test_system_prompt_includes_evaluation_principles(
        self, sample_seed: Seed
    ) -> None:
        """Test that system prompt includes evaluation principles."""
        prompt = build_system_prompt(sample_seed)

        for principle in sample_seed.evaluation_principles:
            assert principle.name in prompt

    def test_task_prompt_includes_acceptance_criteria(
        self, sample_seed: Seed
    ) -> None:
        """Test that task prompt includes all acceptance criteria."""
        prompt = build_task_prompt(sample_seed)

        for criterion in sample_seed.acceptance_criteria:
            assert criterion in prompt

    def test_task_prompt_numbers_criteria(self, sample_seed: Seed) -> None:
        """Test that task prompt numbers acceptance criteria."""
        prompt = build_task_prompt(sample_seed)

        for i in range(len(sample_seed.acceptance_criteria)):
            assert f"{i + 1}." in prompt


class TestOrchestratorExecution:
    """Test orchestrator execution with mocked components."""

    async def test_successful_execution(
        self,
        temp_db_path: str,
        sample_seed: Seed,
        mock_successful_agent_adapter: "MockClaudeAgentAdapter",
    ) -> None:
        """Test successful workflow execution from start to finish."""
        from ouroboros.persistence.event_store import EventStore

        event_store = EventStore(temp_db_path)
        await event_store.initialize()

        try:
            runner = OrchestratorRunner(
                adapter=mock_successful_agent_adapter,
                event_store=event_store,
                console=MagicMock(),
            )

            result = await runner.execute_seed(sample_seed)

            assert result.is_ok
            orch_result = result.value

            assert orch_result.success is True
            assert orch_result.session_id.startswith("orch_")
            assert orch_result.execution_id.startswith("exec_")
            assert orch_result.messages_processed > 0
            assert orch_result.duration_seconds >= 0
        finally:
            await event_store.close()

    async def test_failed_execution(
        self,
        temp_db_path: str,
        sample_seed: Seed,
        mock_claude_agent_adapter: "MockClaudeAgentAdapter",
    ) -> None:
        """Test handling of failed workflow execution."""
        from ouroboros.persistence.event_store import EventStore

        # Configure adapter for failure
        mock_claude_agent_adapter.add_failed_execution(
            error_message="Task execution failed: connection timeout"
        )

        event_store = EventStore(temp_db_path)
        await event_store.initialize()

        try:
            runner = OrchestratorRunner(
                adapter=mock_claude_agent_adapter,
                event_store=event_store,
                console=MagicMock(),
            )

            result = await runner.execute_seed(sample_seed)

            assert result.is_ok  # Result itself is ok, but success is false
            orch_result = result.value

            assert orch_result.success is False
            assert "failed" in orch_result.final_message.lower()
        finally:
            await event_store.close()

    async def test_execution_emits_events(
        self,
        temp_db_path: str,
        sample_seed: Seed,
        mock_successful_agent_adapter: "MockClaudeAgentAdapter",
    ) -> None:
        """Test that execution emits proper events to the event store."""
        from ouroboros.persistence.event_store import EventStore

        event_store = EventStore(temp_db_path)
        await event_store.initialize()

        try:
            runner = OrchestratorRunner(
                adapter=mock_successful_agent_adapter,
                event_store=event_store,
                console=MagicMock(),
            )

            result = await runner.execute_seed(sample_seed)
            assert result.is_ok

            session_id = result.value.session_id

            # Replay events for this session
            events = await event_store.replay("session", session_id)

            # Should have at least started and completed events
            event_types = [e.type for e in events]

            assert "orchestrator.session.started" in event_types
            assert (
                "orchestrator.session.completed" in event_types
                or "orchestrator.session.failed" in event_types
            )
        finally:
            await event_store.close()

    async def test_execution_with_custom_execution_id(
        self,
        temp_db_path: str,
        sample_seed: Seed,
        mock_successful_agent_adapter: "MockClaudeAgentAdapter",
    ) -> None:
        """Test execution with custom execution ID."""
        from ouroboros.persistence.event_store import EventStore

        event_store = EventStore(temp_db_path)
        await event_store.initialize()

        try:
            runner = OrchestratorRunner(
                adapter=mock_successful_agent_adapter,
                event_store=event_store,
                console=MagicMock(),
            )

            custom_exec_id = "custom_exec_e2e_test"
            result = await runner.execute_seed(sample_seed, execution_id=custom_exec_id)

            assert result.is_ok
            assert result.value.execution_id == custom_exec_id
        finally:
            await event_store.close()


class TestWorkflowWithMultipleSteps:
    """Test workflows with multiple execution steps."""

    async def test_multi_step_workflow(
        self,
        temp_db_path: str,
        sample_seed: Seed,
        mock_claude_agent_adapter: "MockClaudeAgentAdapter",
    ) -> None:
        """Test workflow with multiple tool calls and steps."""
        from ouroboros.persistence.event_store import EventStore

        # Configure a complex execution with many steps
        messages = [
            AgentMessage(type="assistant", content="Starting task analysis..."),
            AgentMessage(type="tool", content="Reading files", tool_name="Glob"),
            AgentMessage(type="assistant", content="Found relevant files"),
            AgentMessage(type="tool", content="Reading main.py", tool_name="Read"),
            AgentMessage(type="assistant", content="Analyzing code structure"),
            AgentMessage(type="tool", content="Creating new file", tool_name="Write"),
            AgentMessage(type="assistant", content="File created successfully"),
            AgentMessage(type="tool", content="Running tests", tool_name="Bash"),
            AgentMessage(type="assistant", content="Tests passed"),
            AgentMessage(
                type="result",
                content="All acceptance criteria completed",
                data={"subtype": "success"},
            ),
        ]
        mock_claude_agent_adapter.add_execution_sequence(messages)

        event_store = EventStore(temp_db_path)
        await event_store.initialize()

        try:
            runner = OrchestratorRunner(
                adapter=mock_claude_agent_adapter,
                event_store=event_store,
                console=MagicMock(),
            )

            result = await runner.execute_seed(sample_seed)

            assert result.is_ok
            assert result.value.success
            # Note: messages_processed may be higher than len(messages) due to
            # parallel execution of acceptance criteria in actual orchestrator
            assert result.value.messages_processed >= len(messages)
        finally:
            await event_store.close()

    async def test_workflow_with_all_tool_types(
        self,
        temp_db_path: str,
        sample_seed: Seed,
        mock_claude_agent_adapter: "MockClaudeAgentAdapter",
    ) -> None:
        """Test workflow using all default tool types."""
        from ouroboros.persistence.event_store import EventStore

        # Use all default tools: Read, Write, Edit, Bash, Glob, Grep
        messages = [
            AgentMessage(type="tool", content="Searching files", tool_name="Glob"),
            AgentMessage(type="tool", content="Searching content", tool_name="Grep"),
            AgentMessage(type="tool", content="Reading file", tool_name="Read"),
            AgentMessage(type="tool", content="Editing file", tool_name="Edit"),
            AgentMessage(type="tool", content="Writing file", tool_name="Write"),
            AgentMessage(type="tool", content="Running command", tool_name="Bash"),
            AgentMessage(
                type="result",
                content="Completed using all tools",
                data={"subtype": "success"},
            ),
        ]
        mock_claude_agent_adapter.add_execution_sequence(messages)

        event_store = EventStore(temp_db_path)
        await event_store.initialize()

        try:
            runner = OrchestratorRunner(
                adapter=mock_claude_agent_adapter,
                event_store=event_store,
                console=MagicMock(),
            )

            result = await runner.execute_seed(sample_seed)

            assert result.is_ok
            assert result.value.success
        finally:
            await event_store.close()


class TestWorkflowEdgeCases:
    """Test edge cases in workflow execution."""

    async def test_empty_acceptance_criteria(
        self,
        temp_db_path: str,
        mock_successful_agent_adapter: "MockClaudeAgentAdapter",
    ) -> None:
        """Test workflow with minimal seed (empty lists)."""
        from ouroboros.core.seed import OntologySchema, SeedMetadata
        from ouroboros.persistence.event_store import EventStore

        minimal_seed = Seed(
            goal="Simple test goal",
            acceptance_criteria=("Basic requirement",),
            ontology_schema=OntologySchema(
                name="Minimal",
                description="Minimal schema",
            ),
            metadata=SeedMetadata(ambiguity_score=0.1),
        )

        event_store = EventStore(temp_db_path)
        await event_store.initialize()

        try:
            runner = OrchestratorRunner(
                adapter=mock_successful_agent_adapter,
                event_store=event_store,
                console=MagicMock(),
            )

            result = await runner.execute_seed(minimal_seed)

            assert result.is_ok
        finally:
            await event_store.close()

    async def test_very_long_execution(
        self,
        temp_db_path: str,
        sample_seed: Seed,
        mock_claude_agent_adapter: "MockClaudeAgentAdapter",
    ) -> None:
        """Test workflow with many messages (simulating long execution)."""
        from ouroboros.persistence.event_store import EventStore

        # Create a long sequence of messages
        messages = []
        for i in range(50):
            messages.append(
                AgentMessage(
                    type="assistant", content=f"Working on step {i + 1}..."
                )
            )
            if i % 3 == 0:
                messages.append(
                    AgentMessage(
                        type="tool",
                        content=f"Tool call {i}",
                        tool_name="Read",
                    )
                )

        messages.append(
            AgentMessage(
                type="result",
                content="Long execution completed",
                data={"subtype": "success"},
            )
        )

        mock_claude_agent_adapter.add_execution_sequence(messages)

        event_store = EventStore(temp_db_path)
        await event_store.initialize()

        try:
            runner = OrchestratorRunner(
                adapter=mock_claude_agent_adapter,
                event_store=event_store,
                console=MagicMock(),
            )

            result = await runner.execute_seed(sample_seed)

            assert result.is_ok
            # Note: messages_processed may be higher than len(messages) due to
            # parallel execution of acceptance criteria in actual orchestrator
            assert result.value.messages_processed >= len(messages)
        finally:
            await event_store.close()

    async def test_execution_with_unicode_content(
        self,
        temp_db_path: str,
        mock_claude_agent_adapter: "MockClaudeAgentAdapter",
    ) -> None:
        """Test workflow with unicode content in seed and messages."""
        from ouroboros.core.seed import OntologySchema, SeedMetadata
        from ouroboros.persistence.event_store import EventStore

        unicode_seed = Seed(
            goal="Build application with internationalization support",
            constraints=("Support UTF-8 encoding",),
            acceptance_criteria=(
                "Display messages in multiple languages",
                "Handle special characters properly",
            ),
            ontology_schema=OntologySchema(
                name="i18n",
                description="Internationalization schema",
            ),
            metadata=SeedMetadata(ambiguity_score=0.2),
        )

        mock_claude_agent_adapter.add_successful_execution(
            final_message="Completed! Tested with: Hello, World!"
        )

        event_store = EventStore(temp_db_path)
        await event_store.initialize()

        try:
            runner = OrchestratorRunner(
                adapter=mock_claude_agent_adapter,
                event_store=event_store,
                console=MagicMock(),
            )

            result = await runner.execute_seed(unicode_seed)

            assert result.is_ok
            assert result.value.success
        finally:
            await event_store.close()


class TestWorkflowWithSimulator:
    """Test workflows using the WorkflowSimulator helper."""

    async def test_complete_workflow_scenario(
        self,
        workflow_simulator: "WorkflowSimulator",
        sample_seed: Seed,
        temp_db_path: str,
    ) -> None:
        """Test a complete workflow scenario from start to finish."""
        from ouroboros.persistence.event_store import EventStore

        # Configure the simulator
        workflow_simulator.configure_successful_execution(steps=3)

        event_store = EventStore(temp_db_path)
        await event_store.initialize()

        try:
            runner = OrchestratorRunner(
                adapter=workflow_simulator.mock_agent,
                event_store=event_store,
                console=MagicMock(),
            )

            # Create seed file
            seed_file = workflow_simulator.create_seed_file(sample_seed)
            assert seed_file.exists()

            # Execute workflow
            result = await runner.execute_seed(sample_seed)

            assert result.is_ok
            assert result.value.success
        finally:
            await event_store.close()
