"""Tests for the engine capability graph."""

from __future__ import annotations

from ouroboros.mcp.types import MCPToolDefinition
from ouroboros.orchestrator.capabilities import (
    CapabilityMutationClass,
    CapabilityOrigin,
    CapabilityScope,
    build_capability_graph,
    normalize_serialized_capability_graph,
    serialize_capability_graph,
)
from ouroboros.orchestrator.mcp_tools import assemble_session_tool_catalog


def test_build_capability_graph_preserves_builtin_and_attached_semantics() -> None:
    catalog = assemble_session_tool_catalog(
        builtin_tools=["Read", "Edit", "Bash"],
        attached_tools=(
            MCPToolDefinition(
                name="search_docs",
                description="Search project docs",
                server_name="docs",
            ),
        ),
    )

    graph = build_capability_graph(catalog)

    names = {descriptor.name: descriptor for descriptor in graph.capabilities}
    assert names["Read"].semantics.mutation_class is CapabilityMutationClass.READ_ONLY
    assert names["Read"].semantics.origin is CapabilityOrigin.BUILTIN
    assert names["Edit"].semantics.mutation_class is CapabilityMutationClass.WORKSPACE_WRITE
    assert names["Bash"].semantics.scope is CapabilityScope.SHELL_ONLY
    assert names["search_docs"].semantics.origin is CapabilityOrigin.ATTACHED_MCP
    assert names["search_docs"].semantics.scope is CapabilityScope.ATTACHMENT


def test_capability_graph_serialization_round_trips() -> None:
    graph = build_capability_graph(assemble_session_tool_catalog(["Read", "Edit"]))

    restored = normalize_serialized_capability_graph(serialize_capability_graph(graph))

    assert restored is not None
    assert [descriptor.name for descriptor in restored.capabilities] == ["Read", "Edit"]
    assert restored.capabilities[0].semantics.mutation_class is CapabilityMutationClass.READ_ONLY
