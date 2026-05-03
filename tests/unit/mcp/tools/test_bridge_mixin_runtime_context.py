"""Unit tests for the AgentRuntimeContext-aware bridge injection helper.

Issue: #474 PR-1 of the AgentRuntimeContext + ControlBus migration.
The new ``inject_runtime_context`` helper is purely additive — the
legacy ``inject_bridge`` continues to work, and handler internals are
unchanged. Subsequent migration PRs swap composition-root call sites
from the legacy form to this one, then move handler internals to read
``context.mcp_bridge`` directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ouroboros.mcp.tools.bridge_mixin import (
    BridgeAwareMixin,
    inject_bridge,
    inject_runtime_context,
)
from ouroboros.orchestrator.agent_runtime_context import AgentRuntimeContext
from ouroboros.persistence.event_store import EventStore


@dataclass
class _FakeBridge:
    """Minimal stand-in for ``MCPBridge`` carrying the two attributes the mixin reads."""

    manager: Any = None
    tool_prefix: str = ""


@dataclass
class _FakeHandler(BridgeAwareMixin):
    """Subclass that does nothing extra — exercises the mixin in isolation."""

    extra_field: str = field(default="")


def _runtime_context(bridge: _FakeBridge | None = None) -> AgentRuntimeContext:
    """Build a minimal runtime context for tests."""
    store = EventStore("sqlite+aiosqlite:///:memory:")
    return AgentRuntimeContext(event_store=store, mcp_bridge=bridge)


class TestInjectRuntimeContext:
    def test_injects_bridge_from_context(self) -> None:
        """A handler with the mixin receives the bridge from context.mcp_bridge."""
        manager = object()
        bridge = _FakeBridge(manager=manager, tool_prefix="ext_")
        context = _runtime_context(bridge=bridge)
        handler = _FakeHandler()

        assert inject_runtime_context(handler, context) is True
        assert handler.mcp_manager is manager
        assert handler.mcp_tool_prefix == "ext_"

    def test_returns_false_when_context_is_none(self) -> None:
        """``None`` context is treated as 'no bridge' — non-MCP paths stay valid."""
        handler = _FakeHandler()
        assert inject_runtime_context(handler, None) is False
        assert handler.mcp_manager is None
        assert handler.mcp_tool_prefix == ""

    def test_returns_false_when_context_has_no_bridge(self) -> None:
        """A context without an mcp_bridge does not write to the handler."""
        context = _runtime_context(bridge=None)
        handler = _FakeHandler()

        assert inject_runtime_context(handler, context) is False
        assert handler.mcp_manager is None

    def test_returns_false_when_handler_lacks_mixin(self) -> None:
        """Handlers that do not inherit from BridgeAwareMixin are skipped."""

        class _BareHandler:
            mcp_manager: Any = None
            mcp_tool_prefix: str = ""

        bridge = _FakeBridge(manager=object(), tool_prefix="ext_")
        context = _runtime_context(bridge=bridge)
        handler = _BareHandler()

        assert inject_runtime_context(handler, context) is False
        assert handler.mcp_manager is None
        assert handler.mcp_tool_prefix == ""

    def test_legacy_inject_bridge_still_works(self) -> None:
        """The original entry point keeps the same contract."""
        manager = object()
        bridge = _FakeBridge(manager=manager, tool_prefix="ext_")
        handler = _FakeHandler()

        assert inject_bridge(handler, bridge) is True
        assert handler.mcp_manager is manager
        assert handler.mcp_tool_prefix == "ext_"

    def test_runtime_context_and_legacy_paths_are_equivalent(self) -> None:
        """Both helpers must produce the same handler state for the same bridge."""
        manager = object()
        bridge = _FakeBridge(manager=manager, tool_prefix="ext_")
        context = _runtime_context(bridge=bridge)

        legacy = _FakeHandler()
        modern = _FakeHandler()

        inject_bridge(legacy, bridge)
        inject_runtime_context(modern, context)

        assert legacy.mcp_manager is modern.mcp_manager
        assert legacy.mcp_tool_prefix == modern.mcp_tool_prefix
