"""Tests for capability-derived control-plane hints."""

from __future__ import annotations

from ouroboros.orchestrator.capabilities import build_capability_graph
from ouroboros.orchestrator.control_plane import (
    ControlPlaneExecutionMode,
    build_control_plane_state,
)
from ouroboros.orchestrator.mcp_tools import assemble_session_tool_catalog
from ouroboros.orchestrator.policy import (
    PolicyContext,
    PolicyExecutionPhase,
    PolicySessionRole,
    evaluate_capability_policy,
)


def test_control_plane_state_reflects_semantics_and_policy() -> None:
    graph = build_capability_graph(assemble_session_tool_catalog(["Read", "Edit", "Bash"]))
    decisions = evaluate_capability_policy(
        graph,
        PolicyContext(
            runtime_backend="codex",
            session_role=PolicySessionRole.IMPLEMENTATION,
            execution_phase=PolicyExecutionPhase.IMPLEMENTATION,
        ),
    )

    state = build_control_plane_state(graph, decisions)
    hints = {hint.name: hint for hint in state.hints}

    assert hints["Read"].execution_mode is ControlPlaneExecutionMode.PARALLEL
    assert hints["Edit"].execution_mode is ControlPlaneExecutionMode.SERIALIZED
    assert hints["Bash"].execution_mode is ControlPlaneExecutionMode.ISOLATED
