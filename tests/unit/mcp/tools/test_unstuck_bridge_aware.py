"""Confirm LateralThinkHandler inherits BridgeAwareMixin (slice 2 of #475).

Same shape as the EvolveStepHandler slice (#530): the composition
root's loop-injection (#529) populates BridgeAwareMixin fields
automatically, so this test verifies inheritance + injection without
touching dispatch internals.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ouroboros.mcp.tools.bridge_mixin import BridgeAwareMixin, inject_runtime_context
from ouroboros.mcp.tools.evaluation_handlers import LateralThinkHandler
from ouroboros.orchestrator.agent_runtime_context import AgentRuntimeContext
from ouroboros.persistence.event_store import EventStore


@dataclass
class _FakeBridge:
    manager: Any = None
    tool_prefix: str = ""


def test_lateral_think_handler_inherits_bridge_aware_mixin() -> None:
    handler = LateralThinkHandler()
    assert isinstance(handler, BridgeAwareMixin)
    assert handler.mcp_manager is None
    assert handler.mcp_tool_prefix == ""


def test_inject_runtime_context_populates_lateral_think_handler() -> None:
    manager = object()
    bridge = _FakeBridge(manager=manager, tool_prefix="ctx_")
    store = EventStore("sqlite+aiosqlite:///:memory:")
    context = AgentRuntimeContext(event_store=store, mcp_bridge=bridge)
    handler = LateralThinkHandler()

    assert inject_runtime_context(handler, context) is True
    assert handler.mcp_manager is manager
    assert handler.mcp_tool_prefix == "ctx_"


def test_existing_lateral_think_handler_constructor_unchanged() -> None:
    handler = LateralThinkHandler(
        agent_runtime_backend="opencode",
        opencode_mode="plugin",
    )
    assert handler.agent_runtime_backend == "opencode"
    assert handler.opencode_mode == "plugin"
    assert handler.mcp_manager is None
    assert handler.mcp_tool_prefix == ""
