"""Tests for context-aware factory kwargs added in slice 2 of #474.

The three factories (``execute_seed_handler``,
``start_execute_seed_handler``, ``get_ouroboros_tools``) now accept an
optional ``context: AgentRuntimeContext`` keyword. When the context
carries an ``mcp_bridge`` it supersedes the explicit ``mcp_manager`` /
``mcp_tool_prefix`` kwargs; otherwise the legacy kwargs are used
unchanged. This module pins both paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ouroboros.mcp.tools.definitions import (
    execute_seed_handler,
    get_ouroboros_tools,
    start_execute_seed_handler,
)
from ouroboros.orchestrator.agent_runtime_context import AgentRuntimeContext
from ouroboros.persistence.event_store import EventStore


@dataclass
class _FakeBridge:
    manager: Any = None
    tool_prefix: str = ""


def _context(bridge: _FakeBridge | None = None) -> AgentRuntimeContext:
    store = EventStore("sqlite+aiosqlite:///:memory:")
    return AgentRuntimeContext(event_store=store, mcp_bridge=bridge)


class TestExecuteSeedHandlerFactory:
    def test_context_bridge_overrides_explicit_kwargs(self) -> None:
        manager = object()
        bridge = _FakeBridge(manager=manager, tool_prefix="ctx_")
        ignored_manager = object()

        handler = execute_seed_handler(
            mcp_manager=ignored_manager,  # legacy explicit
            mcp_tool_prefix="legacy_",
            context=_context(bridge=bridge),
        )

        assert handler.mcp_manager is manager
        assert handler.mcp_tool_prefix == "ctx_"

    def test_legacy_kwargs_used_when_context_is_none(self) -> None:
        manager = object()
        handler = execute_seed_handler(mcp_manager=manager, mcp_tool_prefix="legacy_")

        assert handler.mcp_manager is manager
        assert handler.mcp_tool_prefix == "legacy_"

    def test_context_without_bridge_does_not_override(self) -> None:
        legacy_manager = object()
        handler = execute_seed_handler(
            mcp_manager=legacy_manager,
            mcp_tool_prefix="legacy_",
            context=_context(bridge=None),
        )

        assert handler.mcp_manager is legacy_manager
        assert handler.mcp_tool_prefix == "legacy_"


class TestStartExecuteSeedHandlerFactory:
    def test_context_bridge_overrides_explicit_kwargs(self) -> None:
        manager = object()
        bridge = _FakeBridge(manager=manager, tool_prefix="ctx_")

        handler = start_execute_seed_handler(
            mcp_manager=object(),
            mcp_tool_prefix="legacy_",
            context=_context(bridge=bridge),
        )

        assert handler.execute_handler.mcp_manager is manager
        assert handler.execute_handler.mcp_tool_prefix == "ctx_"


class TestGetOuroborosTools:
    def test_context_bridge_propagates_to_execute_seed_handler(self) -> None:
        manager = object()
        bridge = _FakeBridge(manager=manager, tool_prefix="ctx_")

        handlers = get_ouroboros_tools(
            mcp_manager=object(),  # legacy — must lose
            mcp_tool_prefix="legacy_",
            context=_context(bridge=bridge),
        )

        # ExecuteSeedHandler is exposed via the OuroborosToolHandlers tuple;
        # find it by class identity to avoid coupling to the field order.
        execute_handler = next(h for h in handlers if type(h).__name__ == "ExecuteSeedHandler")
        assert execute_handler.mcp_manager is manager
        assert execute_handler.mcp_tool_prefix == "ctx_"

    def test_no_context_keeps_legacy_kwargs(self) -> None:
        manager = object()
        handlers = get_ouroboros_tools(mcp_manager=manager, mcp_tool_prefix="legacy_")
        execute_handler = next(h for h in handlers if type(h).__name__ == "ExecuteSeedHandler")
        assert execute_handler.mcp_manager is manager
        assert execute_handler.mcp_tool_prefix == "legacy_"
