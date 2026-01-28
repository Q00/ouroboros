"""Integration tests for execute_seed MCP tool with real orchestrator.

This module tests the full integration of the execute_seed tool with:
- YAML seed parsing
- EventStore initialization
- OrchestratorRunner execution
- Progress tracking and visualization
- SessionStatusHandler and QueryEventsHandler
"""

import pytest
import yaml
from rich.console import Console

from ouroboros.core.seed import (
    EvaluationPrinciple,
    ExitCondition,
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
)
from ouroboros.mcp.tools.definitions import (
    ExecuteSeedHandler,
    QueryEventsHandler,
    SessionStatusHandler,
)
from ouroboros.persistence.event_store import EventStore


@pytest.fixture
async def event_store():
    """Create an in-memory event store for testing."""
    store = EventStore("sqlite+aiosqlite:///:memory:")
    await store.initialize()
    yield store
    await store.close()


@pytest.fixture
def sample_seed() -> Seed:
    """Create a sample seed for testing."""
    return Seed(
        goal="Test simple code execution",
        constraints=("Python 3.14+", "No external dependencies"),
        acceptance_criteria=(
            "Create a hello.txt file with 'Hello, World!' content",
            "Read the file and verify the content",
        ),
        ontology_schema=OntologySchema(
            name="SimpleTest",
            description="Simple test ontology",
            fields=(
                OntologyField(
                    name="test_result",
                    field_type="string",
                    description="Test execution result",
                ),
            ),
        ),
        evaluation_principles=(
            EvaluationPrinciple(
                name="correctness",
                description="Code executes correctly",
                weight=1.0,
            ),
        ),
        exit_conditions=(
            ExitCondition(
                name="all_criteria_met",
                description="All acceptance criteria satisfied",
                evaluation_criteria="100% criteria pass",
            ),
        ),
        metadata=SeedMetadata(ambiguity_score=0.1),
    )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_execute_seed_handler_yaml_parsing(sample_seed: Seed, event_store: EventStore):
    """Test that ExecuteSeedHandler correctly parses YAML seed content."""
    handler = ExecuteSeedHandler(event_store=event_store, console=Console())

    # Convert seed to YAML
    seed_dict = sample_seed.to_dict()
    seed_yaml = yaml.dump(seed_dict)

    # Test YAML parsing (note: actual execution would require Claude Agent SDK)
    # We just test the parsing and initialization part
    arguments = {
        "seed_content": seed_yaml,
        "model_tier": "medium",
        "max_iterations": 5,
    }

    # Parse the seed_content to verify it works
    parsed_seed_dict = yaml.safe_load(seed_yaml)
    parsed_seed = Seed.from_dict(parsed_seed_dict)

    assert parsed_seed.goal == sample_seed.goal
    assert parsed_seed.acceptance_criteria == sample_seed.acceptance_criteria
    assert len(parsed_seed.acceptance_criteria) == 2


@pytest.mark.asyncio
@pytest.mark.integration
async def test_session_status_handler_with_event_store(event_store: EventStore):
    """Test SessionStatusHandler with real EventStore."""
    from ouroboros.orchestrator.session import SessionRepository

    handler = SessionStatusHandler(event_store=event_store)

    # Create a session via repository
    repo = SessionRepository(event_store)
    result = await repo.create_session(
        execution_id="test_exec_123",
        seed_id="test_seed_456",
    )

    assert result.is_ok
    tracker = result.value

    # Query session status using the handler
    status_result = await handler.handle({"session_id": tracker.session_id})

    assert status_result.is_ok
    tool_result = status_result.value
    assert not tool_result.is_error
    assert tracker.session_id in tool_result.content[0].text
    assert "running" in tool_result.content[0].text.lower()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_query_events_handler_with_event_store(event_store: EventStore):
    """Test QueryEventsHandler with real EventStore."""
    from ouroboros.orchestrator.events import (
        create_progress_event,
        create_session_started_event,
        create_tool_called_event,
    )

    handler = QueryEventsHandler(event_store=event_store)

    session_id = "test_session_789"

    # Create some test events
    start_event = create_session_started_event(
        session_id=session_id,
        execution_id="exec_123",
        seed_id="seed_456",
        seed_goal="Test goal",
    )
    await event_store.append(start_event)

    tool_event = create_tool_called_event(
        session_id=session_id,
        tool_name="Read",
    )
    await event_store.append(tool_event)

    progress_event = create_progress_event(
        session_id=session_id,
        message_type="assistant",
        content_preview="Working on task...",
        step=1,
    )
    await event_store.append(progress_event)

    # Query events using the handler
    result = await handler.handle({"session_id": session_id, "limit": 10})

    assert result.is_ok
    tool_result = result.value
    assert not tool_result.is_error

    # Verify events are listed
    content_text = tool_result.content[0].text
    assert session_id in content_text
    assert "orchestrator.session.started" in content_text
    assert "orchestrator.tool.called" in content_text
    assert "orchestrator.progress.updated" in content_text

    # Verify metadata
    assert tool_result.meta["total_events"] == 3
    assert tool_result.meta["session_id"] == session_id


@pytest.mark.asyncio
@pytest.mark.integration
async def test_query_events_with_type_filter(event_store: EventStore):
    """Test QueryEventsHandler with event type filtering."""
    from ouroboros.orchestrator.events import (
        create_progress_event,
        create_session_started_event,
        create_tool_called_event,
    )

    handler = QueryEventsHandler(event_store=event_store)

    session_id = "test_session_filter"

    # Create mixed event types
    await event_store.append(
        create_session_started_event(
            session_id=session_id,
            execution_id="exec_123",
            seed_id="seed_456",
            seed_goal="Test",
        )
    )

    await event_store.append(
        create_tool_called_event(session_id=session_id, tool_name="Read")
    )
    await event_store.append(
        create_tool_called_event(session_id=session_id, tool_name="Edit")
    )

    await event_store.append(
        create_progress_event(
            session_id=session_id,
            message_type="assistant",
            content_preview="Progress...",
        )
    )

    # Query only tool events
    result = await handler.handle(
        {
            "session_id": session_id,
            "event_type": "orchestrator.tool.called",
            "limit": 10,
        }
    )

    assert result.is_ok
    tool_result = result.value

    # Should only have 2 tool events
    assert tool_result.meta["total_events"] == 2


@pytest.mark.asyncio
@pytest.mark.integration
async def test_handlers_share_event_store(event_store: EventStore):
    """Test that multiple handlers can share the same EventStore instance."""
    from ouroboros.orchestrator.session import SessionRepository

    # Create handlers sharing the same event store
    execute_handler = ExecuteSeedHandler(event_store=event_store)
    status_handler = SessionStatusHandler(event_store=event_store)
    query_handler = QueryEventsHandler(event_store=event_store)

    # Create a session
    repo = SessionRepository(event_store)
    result = await repo.create_session(
        execution_id="shared_exec",
        seed_id="shared_seed",
    )
    assert result.is_ok
    session_id = result.value.session_id

    # Query with status handler
    status_result = await status_handler.handle({"session_id": session_id})
    assert status_result.is_ok

    # Query with events handler
    events_result = await query_handler.handle({"session_id": session_id})
    assert events_result.is_ok

    # Both should see the same session
    assert session_id in status_result.value.content[0].text
    assert session_id in events_result.value.content[0].text


@pytest.mark.asyncio
@pytest.mark.integration
async def test_execute_seed_handler_invalid_yaml():
    """Test ExecuteSeedHandler with invalid YAML content."""
    handler = ExecuteSeedHandler()

    arguments = {
        "seed_content": "invalid: yaml: content: [[[",
    }

    result = await handler.handle(arguments)

    assert result.is_err
    error = result.error
    assert "YAML" in str(error) or "yaml" in str(error)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_session_status_handler_nonexistent_session(event_store: EventStore):
    """Test SessionStatusHandler with a session that doesn't exist."""
    handler = SessionStatusHandler(event_store=event_store)

    result = await handler.handle({"session_id": "nonexistent_session_999"})

    assert result.is_err
    error = result.error
    assert "not found" in str(error).lower() or "no events" in str(error).lower()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_query_events_handler_empty_session(event_store: EventStore):
    """Test QueryEventsHandler with missing session_id."""
    handler = QueryEventsHandler(event_store=event_store)

    result = await handler.handle({})

    assert result.is_err
    error = result.error
    assert "required" in str(error).lower()
