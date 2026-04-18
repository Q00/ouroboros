"""Tests for engine capability policy decisions."""

from __future__ import annotations

from dataclasses import replace

from ouroboros.orchestrator.capabilities import (
    CapabilityApprovalClass,
    CapabilityDescriptor,
    CapabilityGraph,
    CapabilityInterruptibility,
    CapabilityMutationClass,
    CapabilityOrigin,
    CapabilityParallelSafety,
    CapabilityScope,
    CapabilitySemantics,
    build_capability_graph,
)
from ouroboros.orchestrator.mcp_tools import assemble_session_tool_catalog
from ouroboros.orchestrator.policy import (
    PolicyContext,
    PolicyExecutionPhase,
    PolicySessionRole,
    allowed_capability_names,
    allowed_runtime_builtin_tool_names,
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


def test_read_only_roles_allow_provider_native_by_origin_and_scope() -> None:
    graph = CapabilityGraph(
        capabilities=(
            CapabilityDescriptor(
                stable_id="provider:opencode:workspace_snapshot",
                name="workspace_snapshot",
                original_name="workspace_snapshot",
                description="Provider-native workspace inspection",
                server_name=None,
                source_kind="provider_native",
                source_name="opencode",
                semantics=CapabilitySemantics(
                    mutation_class=CapabilityMutationClass.READ_ONLY,
                    parallel_safety=CapabilityParallelSafety.SAFE,
                    interruptibility=CapabilityInterruptibility.NONE,
                    approval_class=CapabilityApprovalClass.DEFAULT,
                    origin=CapabilityOrigin.PROVIDER_NATIVE,
                    scope=CapabilityScope.SIDECAR,
                ),
            ),
        )
    )

    allowed = allowed_capability_names(
        graph,
        PolicyContext(
            runtime_backend="opencode",
            session_role=PolicySessionRole.EVALUATION,
            execution_phase=PolicyExecutionPhase.EVALUATION,
        ),
    )

    assert allowed == ["workspace_snapshot"]


def test_read_only_roles_still_hide_unknown_attached_tools() -> None:
    graph = CapabilityGraph(
        capabilities=(
            CapabilityDescriptor(
                stable_id="mcp:browser:browser_snapshot",
                name="browser_snapshot",
                original_name="browser_snapshot",
                description="Attached browser screenshot",
                server_name="browser",
                source_kind="attached_mcp",
                source_name="browser",
                semantics=CapabilitySemantics(
                    mutation_class=CapabilityMutationClass.READ_ONLY,
                    parallel_safety=CapabilityParallelSafety.SAFE,
                    interruptibility=CapabilityInterruptibility.NONE,
                    approval_class=CapabilityApprovalClass.DEFAULT,
                    origin=CapabilityOrigin.ATTACHED_MCP,
                    scope=CapabilityScope.ATTACHMENT,
                ),
            ),
        )
    )

    decisions = evaluate_capability_policy(
        graph,
        PolicyContext(
            runtime_backend="opencode",
            session_role=PolicySessionRole.EVALUATION,
            execution_phase=PolicyExecutionPhase.EVALUATION,
        ),
    )

    assert decisions[0].visible is False
    assert decisions[0].executable is False


def test_read_only_roles_derive_runtime_builtin_envelope_from_policy() -> None:
    allowed = allowed_runtime_builtin_tool_names(
        PolicyContext(
            runtime_backend="opencode",
            session_role=PolicySessionRole.EVALUATION,
            execution_phase=PolicyExecutionPhase.EVALUATION,
        )
    )

    assert allowed == ["Read", "Glob", "Grep", "WebFetch", "WebSearch"]
    assert "Edit" not in allowed
    assert "Write" not in allowed
    assert "Bash" not in allowed
