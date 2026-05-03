"""Confirm EvolveStepHandler now inherits BridgeAwareMixin (slice 1 of #475).

The composition root's loop-injection (#529) populates BridgeAwareMixin
fields automatically, so this test verifies (a) the handler inherits
the mixin and (b) ``inject_runtime_context`` populates its bridge
fields exactly like any other BridgeAwareMixin handler.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ouroboros.mcp.tools.bridge_mixin import BridgeAwareMixin, inject_runtime_context
from ouroboros.mcp.tools.evolution_handlers import EvolveStepHandler
from ouroboros.orchestrator.agent_runtime_context import AgentRuntimeContext
from ouroboros.persistence.event_store import EventStore


@dataclass
class _FakeBridge:
    manager: Any = None
    tool_prefix: str = ""


def test_evolve_step_handler_inherits_bridge_aware_mixin() -> None:
    """The class hierarchy now includes BridgeAwareMixin."""
    handler = EvolveStepHandler()
    assert isinstance(handler, BridgeAwareMixin)
    # Default field values come from the mixin.
    assert handler.mcp_manager is None
    assert handler.mcp_tool_prefix == ""


def test_inject_runtime_context_populates_evolve_step_handler() -> None:
    """``inject_runtime_context`` writes bridge fields onto the handler."""
    manager = object()
    bridge = _FakeBridge(manager=manager, tool_prefix="ctx_")
    store = EventStore("sqlite+aiosqlite:///:memory:")
    context = AgentRuntimeContext(event_store=store, mcp_bridge=bridge)
    handler = EvolveStepHandler()

    assert inject_runtime_context(handler, context) is True
    assert handler.mcp_manager is manager
    assert handler.mcp_tool_prefix == "ctx_"


def test_existing_evolve_step_handler_constructor_unchanged() -> None:
    """Pre-existing keyword args still construct the handler unchanged.

    The mixin adds two fields with defaults so callers that did not pass
    ``mcp_manager`` or ``mcp_tool_prefix`` continue to work; this test
    pins the contract.
    """
    handler = EvolveStepHandler(
        evolutionary_loop=None,
        event_store=None,
        agent_runtime_backend="codex_cli",
        opencode_mode=None,
    )
    assert handler.agent_runtime_backend == "codex_cli"
    assert handler.opencode_mode is None
    assert handler.mcp_manager is None
    assert handler.mcp_tool_prefix == ""
