"""MCP Layer separation for State/Agent architecture.

Controls whether MCP handlers act as pure state APIs (native mode)
or call LLM internally (internal mode).

Modules:
- gate: AgentMode enum and get_agent_mode() resolver
"""

from ouroboros.mcp.layers.gate import AgentMode, get_agent_mode

__all__ = [
    "AgentMode",
    "get_agent_mode",
]
