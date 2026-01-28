"""Tests for Ouroboros tool definitions."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.core.types import Result
from ouroboros.mcp.tools.definitions import (
    OUROBOROS_TOOLS,
    ExecuteSeedHandler,
    QueryEventsHandler,
    SessionStatusHandler,
)
from ouroboros.mcp.types import ToolInputType
from ouroboros.orchestrator.session import SessionStatus, SessionTracker


class TestExecuteSeedHandler:
    """Test ExecuteSeedHandler class."""

    def test_definition_name(self) -> None:
        """ExecuteSeedHandler has correct name."""
        handler = ExecuteSeedHandler()
        assert handler.definition.name == "ouroboros_execute_seed"

    def test_definition_has_required_parameters(self) -> None:
        """ExecuteSeedHandler has required seed_content parameter."""
        handler = ExecuteSeedHandler()
        defn = handler.definition

        param_names = {p.name for p in defn.parameters}
        assert "seed_content" in param_names

        seed_param = next(p for p in defn.parameters if p.name == "seed_content")
        assert seed_param.required is True
        assert seed_param.type == ToolInputType.STRING

    def test_definition_has_optional_parameters(self) -> None:
        """ExecuteSeedHandler has optional parameters."""
        handler = ExecuteSeedHandler()
        defn = handler.definition

        param_names = {p.name for p in defn.parameters}
        assert "session_id" in param_names
        assert "model_tier" in param_names
        assert "max_iterations" in param_names

    async def test_handle_requires_seed_content(self) -> None:
        """handle returns error when seed_content is missing."""
        handler = ExecuteSeedHandler()
        result = await handler.handle({})

        assert result.is_err
        assert "seed_content is required" in str(result.error)

    async def test_handle_success(self) -> None:
        """handle returns success with valid input."""
        handler = ExecuteSeedHandler()

        # Valid YAML seed content
        seed_yaml = """
goal: Test seed execution
acceptance_criteria:
  - Test criterion 1
  - Test criterion 2
ontology_schema:
  name: TestOntology
  description: Test ontology for unit testing
  fields: []
metadata:
  ambiguity_score: 0.15
"""

        result = await handler.handle({
            "seed_content": seed_yaml,
            "model_tier": "medium",
        })

        assert result.is_ok
        assert "Session ID:" in result.value.text_content


class TestSessionStatusHandler:
    """Test SessionStatusHandler class."""

    def test_definition_name(self) -> None:
        """SessionStatusHandler has correct name."""
        handler = SessionStatusHandler()
        assert handler.definition.name == "ouroboros_session_status"

    def test_definition_requires_session_id(self) -> None:
        """SessionStatusHandler requires session_id parameter."""
        handler = SessionStatusHandler()
        defn = handler.definition

        assert len(defn.parameters) == 1
        assert defn.parameters[0].name == "session_id"
        assert defn.parameters[0].required is True

    async def test_handle_requires_session_id(self) -> None:
        """handle returns error when session_id is missing."""
        handler = SessionStatusHandler()
        result = await handler.handle({})

        assert result.is_err
        assert "session_id is required" in str(result.error)

    async def test_handle_success(self) -> None:
        """handle returns success with valid session_id."""
        from datetime import UTC, datetime

        # Create a mock event store that returns a valid session
        mock_event_store = MagicMock()
        mock_event_store.initialize = AsyncMock()

        # Create a mock session tracker
        mock_tracker = SessionTracker(
            session_id="test-session",
            execution_id="exec_test123",
            seed_id="seed_test456",
            status=SessionStatus.RUNNING,
            start_time=datetime.now(UTC),
            messages_processed=5,
        )

        # Create handler with mocked components
        handler = SessionStatusHandler(event_store=mock_event_store)

        # Mock the SessionRepository.reconstruct_session to return our tracker
        from unittest.mock import patch

        with patch(
            "ouroboros.mcp.tools.definitions.SessionRepository.reconstruct_session",
            new_callable=AsyncMock,
        ) as mock_reconstruct:
            mock_reconstruct.return_value = Result.ok(mock_tracker)

            result = await handler.handle({"session_id": "test-session"})

            assert result.is_ok
            assert "test-session" in result.value.text_content


class TestQueryEventsHandler:
    """Test QueryEventsHandler class."""

    def test_definition_name(self) -> None:
        """QueryEventsHandler has correct name."""
        handler = QueryEventsHandler()
        assert handler.definition.name == "ouroboros_query_events"

    def test_definition_has_optional_filters(self) -> None:
        """QueryEventsHandler has optional filter parameters."""
        handler = QueryEventsHandler()
        defn = handler.definition

        param_names = {p.name for p in defn.parameters}
        assert "session_id" in param_names
        assert "event_type" in param_names
        assert "limit" in param_names
        assert "offset" in param_names

        # session_id is required, others are optional
        session_id_param = next(p for p in defn.parameters if p.name == "session_id")
        assert session_id_param.required is True

        # Other filter parameters should be optional
        for param in defn.parameters:
            if param.name != "session_id":
                assert param.required is False

    async def test_handle_success_no_filters(self) -> None:
        """handle returns success with only session_id (no other filters)."""
        # Create a mock event store that returns empty events
        mock_event_store = MagicMock()
        mock_event_store.initialize = AsyncMock()
        mock_event_store.replay = AsyncMock(return_value=[])

        handler = QueryEventsHandler(event_store=mock_event_store)
        result = await handler.handle({"session_id": "test-session"})

        assert result.is_ok
        assert "Event Query Results" in result.value.text_content
        assert "test-session" in result.value.text_content

    async def test_handle_with_filters(self) -> None:
        """handle accepts filter parameters."""
        handler = QueryEventsHandler()
        result = await handler.handle({
            "session_id": "test-session",
            "event_type": "execution",
            "limit": 10,
        })

        assert result.is_ok
        assert "test-session" in result.value.text_content


class TestOuroborosTools:
    """Test OUROBOROS_TOOLS constant."""

    def test_ouroboros_tools_contains_all_handlers(self) -> None:
        """OUROBOROS_TOOLS contains all standard handlers."""
        assert len(OUROBOROS_TOOLS) == 3

        handler_types = {type(h) for h in OUROBOROS_TOOLS}
        assert ExecuteSeedHandler in handler_types
        assert SessionStatusHandler in handler_types
        assert QueryEventsHandler in handler_types

    def test_all_tools_have_unique_names(self) -> None:
        """All tools have unique names."""
        names = [h.definition.name for h in OUROBOROS_TOOLS]
        assert len(names) == len(set(names))

    def test_all_tools_have_descriptions(self) -> None:
        """All tools have non-empty descriptions."""
        for handler in OUROBOROS_TOOLS:
            assert handler.definition.description
            assert len(handler.definition.description) > 10
