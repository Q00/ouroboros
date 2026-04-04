"""Tests for engine capability policy decisions."""

from __future__ import annotations

from ouroboros.orchestrator.capabilities import build_capability_graph
from ouroboros.orchestrator.mcp_tools import assemble_session_tool_catalog
from ouroboros.orchestrator.policy import (
    PolicyContext,
    PolicyExecutionPhase,
    PolicySessionRole,
    allowed_capability_names,
)


def test_implementation_policy_allows_default_runtime_tools() -> None:
    graph = build_capability_graph(assemble_session_tool_catalog(["Read", "Edit", "Bash"]))

    allowed = allowed_capability_names(
        graph,
        PolicyContext(
            runtime_backend="codex",
            session_role=PolicySessionRole.IMPLEMENTATION,
            execution_phase=PolicyExecutionPhase.IMPLEMENTATION,
        ),
    )

    assert allowed == ["Read", "Edit", "Bash"]


def test_coordinator_policy_derives_conservative_envelope() -> None:
    graph = build_capability_graph(
        assemble_session_tool_catalog(["Read", "Write", "Edit", "Bash", "Glob", "Grep"])
    )

    allowed = allowed_capability_names(
        graph,
        PolicyContext(
            runtime_backend="opencode",
            session_role=PolicySessionRole.COORDINATOR,
            execution_phase=PolicyExecutionPhase.COORDINATOR_REVIEW,
        ),
    )

    assert allowed == ["Read", "Edit", "Bash", "Glob", "Grep"]
