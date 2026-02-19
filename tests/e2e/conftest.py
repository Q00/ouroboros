"""Shared fixtures for E2E tests.

This module provides fixtures for mocking external services (LLM providers,
Claude Agent SDK) and setting up isolated test environments.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
import tempfile
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ouroboros.core.seed import (
    EvaluationPrinciple,
    ExitCondition,
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
)
from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.providers.base import CompletionConfig, CompletionResponse, Message, UsageInfo

# =============================================================================
# Seed Fixtures
# =============================================================================


@pytest.fixture
def sample_seed() -> Seed:
    """Create a sample seed for testing complete workflows."""
    return Seed(
        goal="Build a task management CLI application",
        constraints=(
            "Python 3.12+",
            "No external database required",
            "Must support CRUD operations",
        ),
        acceptance_criteria=(
            "Tasks can be created with title and description",
            "Tasks can be listed with filtering by status",
            "Tasks can be marked as complete",
            "Tasks can be deleted",
        ),
        ontology_schema=OntologySchema(
            name="TaskManager",
            description="Task management application ontology",
            fields=(
                OntologyField(
                    name="tasks",
                    field_type="array",
                    description="List of task objects",
                ),
                OntologyField(
                    name="config",
                    field_type="object",
                    description="Application configuration",
                ),
            ),
        ),
        evaluation_principles=(
            EvaluationPrinciple(
                name="completeness",
                description="All acceptance criteria are fully met",
                weight=1.0,
            ),
            EvaluationPrinciple(
                name="code_quality",
                description="Code follows best practices and is maintainable",
                weight=0.8,
            ),
        ),
        exit_conditions=(
            ExitCondition(
                name="all_criteria_met",
                description="All acceptance criteria have been satisfied",
                evaluation_criteria="100% of acceptance criteria pass verification",
            ),
        ),
        metadata=SeedMetadata(
            ambiguity_score=0.15,
            interview_id="test_interview_001",
        ),
    )


@pytest.fixture
def minimal_seed() -> Seed:
    """Create a minimal seed for simple workflow tests."""
    return Seed(
        goal="Create a hello world script",
        acceptance_criteria=("Script prints 'Hello, World!'",),
        ontology_schema=OntologySchema(
            name="HelloWorld",
            description="Simple hello world",
        ),
        metadata=SeedMetadata(ambiguity_score=0.1),
    )


@pytest.fixture
def seed_yaml_content(sample_seed: Seed) -> str:
    """Generate YAML content for a seed file."""
    import yaml

    return yaml.dump(sample_seed.to_dict(), default_flow_style=False)


# =============================================================================
# Temporary Directory Fixtures
# =============================================================================


@pytest.fixture
def temp_dir() -> Path:
    """Create a temporary directory for test isolation."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def temp_state_dir(temp_dir: Path) -> Path:
    """Create a temporary state directory for interview/session persistence."""
    state_dir = temp_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


@pytest.fixture
def temp_seed_file(temp_dir: Path, seed_yaml_content: str) -> Path:
    """Create a temporary seed YAML file."""
    seed_file = temp_dir / "seed.yaml"
    seed_file.write_text(seed_yaml_content)
    return seed_file


@pytest.fixture
def temp_db_path(temp_dir: Path) -> str:
    """Create a temporary database path for event store testing."""
    return f"sqlite+aiosqlite:///{temp_dir}/events.db"


# =============================================================================
# Mock LLM Provider Fixtures
# =============================================================================


@dataclass
class MockLLMResponse:
    """Configuration for a mock LLM response."""

    content: str
    model: str = "mock-model"
    prompt_tokens: int = 10
    completion_tokens: int = 20
    finish_reason: str = "stop"


@dataclass
class MockLLMProvider:
    """Mock LLM provider for testing.

    Supports configuring sequences of responses for multi-turn conversations.
    """

    responses: list[MockLLMResponse] = field(default_factory=list)
    _call_count: int = field(default=0, init=False)
    _call_history: list[tuple[list[Message], CompletionConfig]] = field(
        default_factory=list, init=False
    )

    def add_response(self, content: str, **kwargs: Any) -> MockLLMProvider:
        """Add a response to the sequence."""
        self.responses.append(MockLLMResponse(content=content, **kwargs))
        return self

    async def complete(self, messages: list[Message], config: CompletionConfig) -> Any:
        """Simulate LLM completion."""
        from ouroboros.core.types import Result

        self._call_history.append((messages, config))

        if not self.responses:
            # Return a default response if none configured
            response = MockLLMResponse(content="Default mock response")
        elif self._call_count >= len(self.responses):
            # Cycle back to the last response if exceeded
            response = self.responses[-1]
        else:
            response = self.responses[self._call_count]

        self._call_count += 1

        return Result.ok(
            CompletionResponse(
                content=response.content,
                model=response.model,
                usage=UsageInfo(
                    prompt_tokens=response.prompt_tokens,
                    completion_tokens=response.completion_tokens,
                    total_tokens=response.prompt_tokens + response.completion_tokens,
                ),
                finish_reason=response.finish_reason,
                raw_response={},
            )
        )

    @property
    def call_count(self) -> int:
        """Get the number of times complete was called."""
        return self._call_count

    @property
    def call_history(self) -> list[tuple[list[Message], CompletionConfig]]:
        """Get the history of calls made."""
        return self._call_history


@pytest.fixture
def mock_llm_provider() -> MockLLMProvider:
    """Create a mock LLM provider instance."""
    return MockLLMProvider()


@pytest.fixture
def mock_interview_llm_provider() -> MockLLMProvider:
    """Create a mock LLM provider pre-configured for interview testing."""
    provider = MockLLMProvider()
    # Pre-configure typical interview questions
    provider.add_response("What is the target audience for this application?")
    provider.add_response("What programming language and framework preferences do you have?")
    provider.add_response("Are there any specific constraints on dependencies or libraries?")
    provider.add_response("What is the expected scale and performance requirements?")
    provider.add_response("How should errors be handled and reported to users?")
    return provider


# =============================================================================
# Mock Claude Agent SDK Fixtures
# =============================================================================


@dataclass
class MockAgentMessage:
    """Mock message from Claude Agent SDK."""

    type: str
    content: str
    tool_name: str | None = None
    is_final: bool = False
    is_error: bool = False
    session_id: str | None = None


@dataclass
class MockClaudeAgentAdapter:
    """Mock Claude Agent adapter for E2E testing.

    Simulates Claude Agent SDK execution with configurable message sequences.
    """

    message_sequences: list[list[AgentMessage]] = field(default_factory=list)
    _execution_count: int = field(default=0, init=False)
    _execution_history: list[dict[str, Any]] = field(default_factory=list, init=False)

    def add_execution_sequence(self, messages: list[AgentMessage]) -> MockClaudeAgentAdapter:
        """Add a sequence of messages for a single execution."""
        self.message_sequences.append(messages)
        return self

    def add_successful_execution(
        self,
        steps: int = 3,
        final_message: str = "Task completed successfully.",
    ) -> MockClaudeAgentAdapter:
        """Add a successful execution sequence with typical tool usage."""
        messages = [
            AgentMessage(type="assistant", content="Analyzing the task..."),
        ]
        for i in range(steps):
            messages.append(
                AgentMessage(
                    type="tool",
                    content=f"Using tool for step {i + 1}",
                    tool_name="Read" if i % 2 == 0 else "Edit",
                )
            )
            messages.append(AgentMessage(type="assistant", content=f"Step {i + 1} completed"))

        messages.append(
            AgentMessage(
                type="result",
                content=final_message,
                data={"subtype": "success"},
            )
        )
        return self.add_execution_sequence(messages)

    def add_failed_execution(
        self, error_message: str = "Execution failed due to an error."
    ) -> MockClaudeAgentAdapter:
        """Add a failed execution sequence."""
        messages = [
            AgentMessage(type="assistant", content="Starting task..."),
            AgentMessage(
                type="result",
                content=error_message,
                data={"subtype": "error"},
            ),
        ]
        return self.add_execution_sequence(messages)

    async def execute_task(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_session_id: str | None = None,
    ) -> AsyncIterator[AgentMessage]:
        """Simulate Claude Agent execution."""
        self._execution_history.append(
            {
                "prompt": prompt,
                "tools": tools,
                "system_prompt": system_prompt,
                "resume_session_id": resume_session_id,
            }
        )

        if not self.message_sequences:
            # Default successful execution
            yield AgentMessage(type="assistant", content="Working on task...")
            yield AgentMessage(
                type="result",
                content="Default completion",
                data={"subtype": "success"},
            )
            return

        # Use the appropriate sequence
        idx = min(self._execution_count, len(self.message_sequences) - 1)
        sequence = self.message_sequences[idx]
        self._execution_count += 1

        for message in sequence:
            yield message

    @property
    def execution_count(self) -> int:
        """Get the number of executions."""
        return self._execution_count

    @property
    def execution_history(self) -> list[dict[str, Any]]:
        """Get the execution history."""
        return self._execution_history


@pytest.fixture
def mock_claude_agent_adapter() -> MockClaudeAgentAdapter:
    """Create a mock Claude Agent adapter."""
    return MockClaudeAgentAdapter()


@pytest.fixture
def mock_successful_agent_adapter() -> MockClaudeAgentAdapter:
    """Create a mock adapter pre-configured for successful execution."""
    adapter = MockClaudeAgentAdapter()
    adapter.add_successful_execution(steps=5, final_message="All tasks completed!")
    return adapter


# =============================================================================
# Event Store Fixtures
# =============================================================================


@pytest.fixture
async def event_store(temp_db_path: str) -> AsyncIterator[Any]:
    """Create and initialize an event store for testing."""
    from ouroboros.persistence.event_store import EventStore

    store = EventStore(temp_db_path)
    await store.initialize()
    yield store
    await store.close()


# =============================================================================
# CLI Testing Fixtures
# =============================================================================


@pytest.fixture
def cli_runner():
    """Create a Typer CLI test runner."""
    from typer.testing import CliRunner

    return CliRunner()


@pytest.fixture
def mock_async_run():
    """Create a context manager for mocking asyncio.run in CLI tests."""

    def _mock_async_run(return_value: Any = None, side_effect: Exception | None = None):
        mock = MagicMock()
        if side_effect:
            mock.side_effect = side_effect
        else:
            mock.return_value = return_value
        return patch("asyncio.run", mock)

    return _mock_async_run


# =============================================================================
# Workflow Simulation Helpers
# =============================================================================


@dataclass
class WorkflowSimulator:
    """Helper class for simulating complete workflow scenarios."""

    mock_llm: MockLLMProvider
    mock_agent: MockClaudeAgentAdapter
    temp_dir: Path

    def configure_interview_flow(self, questions: list[str]) -> WorkflowSimulator:
        """Configure the interview flow with specific questions."""
        for question in questions:
            self.mock_llm.add_response(question)
        return self

    def configure_successful_execution(self, steps: int = 3) -> WorkflowSimulator:
        """Configure a successful workflow execution."""
        self.mock_agent.add_successful_execution(steps=steps)
        return self

    def create_seed_file(self, seed: Seed) -> Path:
        """Create a seed YAML file in the temp directory."""
        import yaml

        seed_file = self.temp_dir / "workflow_seed.yaml"
        seed_file.write_text(yaml.dump(seed.to_dict(), default_flow_style=False))
        return seed_file


@pytest.fixture
def workflow_simulator(
    mock_llm_provider: MockLLMProvider,
    mock_claude_agent_adapter: MockClaudeAgentAdapter,
    temp_dir: Path,
) -> WorkflowSimulator:
    """Create a workflow simulator for complex E2E scenarios."""
    return WorkflowSimulator(
        mock_llm=mock_llm_provider,
        mock_agent=mock_claude_agent_adapter,
        temp_dir=temp_dir,
    )


# =============================================================================
# Session Fixtures
# =============================================================================


@pytest.fixture
def mock_session_id() -> str:
    """Generate a mock session ID for testing."""
    return "orch_test_session_001"


@pytest.fixture
def mock_execution_id() -> str:
    """Generate a mock execution ID for testing."""
    return "exec_test_exec_001"


@pytest.fixture
async def persisted_session(
    event_store: Any, mock_session_id: str, mock_execution_id: str, sample_seed: Seed
) -> dict[str, Any]:
    """Create a persisted session in the event store for resumption testing."""
    from ouroboros.orchestrator.session import SessionRepository

    repo = SessionRepository(event_store)
    result = await repo.create_session(
        execution_id=mock_execution_id,
        seed_id=sample_seed.metadata.seed_id,
        session_id=mock_session_id,
    )

    if result.is_err:
        raise RuntimeError(f"Failed to create test session: {result.error}")

    tracker = result.value

    # Track some progress
    await repo.track_progress(mock_session_id, {"step": 1, "message": "Started"})
    await repo.track_progress(mock_session_id, {"step": 2, "message": "In progress"})

    return {
        "session_id": mock_session_id,
        "execution_id": mock_execution_id,
        "seed_id": sample_seed.metadata.seed_id,
        "tracker": tracker,
    }
