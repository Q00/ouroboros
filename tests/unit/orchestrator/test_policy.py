"""Tests for engine capability policy decisions."""

from __future__ import annotations

from dataclasses import replace

from ouroboros.orchestrator.capabilities import build_capability_graph
from ouroboros.orchestrator.mcp_tools import assemble_session_tool_catalog
from ouroboros.orchestrator.policy import (
    PolicyContext,
    PolicyExecutionPhase,
    PolicySessionRole,
    allowed_capability_names,
    evaluate_capability_policy,
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


def test_inherited_capability_is_auditable_but_not_executable() -> None:
    catalog = replace(
        assemble_session_tool_catalog(["Read"]),
        inherited_capabilities=frozenset({"mcp__chrome-devtools__click"}),
    )
    graph = build_capability_graph(catalog)
    context = PolicyContext(
        runtime_backend="opencode",
        session_role=PolicySessionRole.IMPLEMENTATION,
        execution_phase=PolicyExecutionPhase.IMPLEMENTATION,
    )

    decisions = {decision.name: decision for decision in evaluate_capability_policy(graph, context)}
    allowed = allowed_capability_names(graph, context)

    assert allowed == ["Read"]
    inherited = decisions["mcp__chrome-devtools__click"]
    assert inherited.visible is True
    assert inherited.executable is False
    assert inherited.reasons == (
        "inherited_capability requires live provider discovery before execution",
    )
