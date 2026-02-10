"""Plugin agent system for Ouroboros.

This package provides the agent orchestration engine:
- Agent Registry: Dynamic agent discovery and registration
- Agent Pool: Reusable agent pool with load balancing
- Agent Specs: Built-in agent specifications

Architecture:
- Extends existing orchestrator.adapter (ClaudeAgentAdapter)
- Integrates with routing.complexity for PAL routing
- Uses events.base for state tracking
"""

from ouroboros.plugin.agents.registry import (
    AgentRegistry,
    AgentRole,
    AgentSpec,
)
from ouroboros.plugin.agents.pool import (
    AgentInstance,
    AgentPool,
    AgentState,
    TaskRequest,
)

__all__ = [
    "AgentRegistry",
    "AgentRole",
    "AgentSpec",
    "AgentInstance",
    "AgentPool",
    "AgentState",
    "TaskRequest",
]
