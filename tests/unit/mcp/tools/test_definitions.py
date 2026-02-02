"""Tests for Ouroboros tool definitions."""

import pytest

from ouroboros.mcp.tools.definitions import (
    OUROBOROS_TOOLS,
    ExecuteSeedHandler,
    QueryEventsHandler,
    SessionStatusHandler,
)
from ouroboros.mcp.types import ToolInputType


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
        result = await handler.handle({
            "seed_content": "Test seed content",
            "model_tier": "medium",
        })

        assert result.is_ok
        assert "Seed execution initiated" in result.value.text_content


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
        handler = SessionStatusHandler()
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

        # All should be optional
        for param in defn.parameters:
            assert param.required is False

    async def test_handle_success_no_filters(self) -> None:
        """handle returns success without filters."""
        handler = QueryEventsHandler()
        result = await handler.handle({})

        assert result.is_ok
        assert "Event Query Results" in result.value.text_content

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
