"""BridgeAwareMixin for MCP tool handlers that need external MCP access.

Handlers that inherit from this mixin will automatically receive
an MCPClientManager reference when an MCPBridge is configured,
via loop-based injection in the composition root.

This module exposes two equivalent injection helpers:

* :func:`inject_bridge` — original entry point. Accepts a raw
  ``MCPBridge`` instance and populates the mixin fields.
* :func:`inject_runtime_context` — context-aware entry point added by
  #474. Accepts an :class:`AgentRuntimeContext` and pulls the bridge
  off it. Subsequent ``mcp_manager`` plumbing migrations (#474 PR-3
  through PR-5) gradually swap call sites at the composition root from
  the legacy form to this one, then move handler internals to read
  ``context.mcp_bridge`` directly instead of ``self.mcp_manager``.

Both helpers are additive: the legacy form continues to work for
callers that have not yet adopted the runtime context, so the
migration can land in small reviewable slices without a flag day.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ouroboros.orchestrator.agent_runtime_context import AgentRuntimeContext


@dataclass
class BridgeAwareMixin:
    """Mixin for handlers that need access to external MCP servers.

    Provides ``mcp_manager`` and ``mcp_tool_prefix`` fields that are
    injected by the composition root when an MCPBridge is configured.
    Both fields default to None/"" so handlers work without a bridge.

    Usage::

        @dataclass
        class MyHandler(BridgeAwareMixin):
            other_field: str = ""

            async def handle(self, arguments):
                if self.mcp_manager:
                    tools = await self.mcp_manager.list_all_tools()
    """

    mcp_manager: Any | None = field(default=None, repr=False)
    mcp_tool_prefix: str = ""


def inject_bridge(handler: object, bridge: object | None) -> bool:
    """Inject bridge manager into a BridgeAwareMixin handler.

    Args:
        handler: A tool handler, possibly BridgeAwareMixin.
        bridge: An MCPBridge instance (must have .manager and .tool_prefix).

    Returns:
        True if injection was performed, False otherwise.
    """
    if bridge is None or not isinstance(handler, BridgeAwareMixin):
        return False

    handler.mcp_manager = getattr(bridge, "manager", None)
    handler.mcp_tool_prefix = getattr(bridge, "tool_prefix", "")
    return True


def inject_runtime_context(
    handler: object,
    context: AgentRuntimeContext | None,
) -> bool:
    """Inject the ``AgentRuntimeContext``'s bridge into a mixin handler.

    Equivalent to calling :func:`inject_bridge` with
    ``context.mcp_bridge`` but accepts the runtime context directly.
    This is the entry point subsequent #474 PRs use at the composition
    root so handlers can reach the bridge through the same context
    object that already carries the EventStore and the
    :class:`ControlBus` (per #476 Q1's narrow-membership commitment).

    Args:
        handler: A tool handler, possibly :class:`BridgeAwareMixin`.
        context: The :class:`AgentRuntimeContext` carrying the runtime's
            optional MCP bridge. Passing ``None`` is treated as
            "no bridge available" and skips injection — that keeps the
            non-MCP code paths valid.

    Returns:
        ``True`` if injection was performed; ``False`` otherwise.

    The function is intentionally a thin adapter so the legacy
    :func:`inject_bridge` code path remains the single source of truth
    for the actual attribute write. When the migration is complete and
    every caller flows through this helper, the legacy entry point can
    be deprecated without behaviour change.
    """
    if context is None:
        return False
    return inject_bridge(handler, context.mcp_bridge)
