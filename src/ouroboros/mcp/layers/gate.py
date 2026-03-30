"""Agent mode configuration for MCP State/Agent layer separation.

Controls whether MCP handlers act as pure state APIs (native) or call LLM
internally (internal compatibility mode).

Mode is controlled by:
    - Environment variable: OUROBOROS_AGENT_MODE=native|internal
    - Handler-level override via constructor
    - Default: native

native (default): MCP = pure state CRUD. Platform agents handle reasoning.
                  Claude Code plugin metadata points at project-root
                  ``agents/`` via ``.claude-plugin/plugin.json``.
internal:         MCP calls LLM internally. Compatibility mode for environments
                  that do not support platform-native subagents.
                  Activate with: OUROBOROS_AGENT_MODE=internal
"""

from __future__ import annotations

import os
from enum import StrEnum

import structlog

log = structlog.get_logger(__name__)


class AgentMode(StrEnum):
    """Controls whether MCP tools call LLM internally or delegate to platform agents."""

    INTERNAL = "internal"
    """MCP calls LLM internally (compatibility mode, works everywhere)."""

    NATIVE = "native"
    """MCP is pure state. Platform-native agents handle reasoning."""


def get_agent_mode(override: AgentMode | None = None) -> AgentMode:
    """Resolve the effective agent mode.

    Priority:
        1. Explicit override (handler-level)
        2. Environment variable OUROBOROS_AGENT_MODE
        3. Default: NATIVE
    """
    if override is not None:
        return override

    env_mode = os.environ.get("OUROBOROS_AGENT_MODE", "").strip().lower()
    if env_mode == "internal":
        return AgentMode.INTERNAL
    return AgentMode.NATIVE
