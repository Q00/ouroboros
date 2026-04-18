"""Tests for the engine capability graph."""

from __future__ import annotations

from dataclasses import replace

from ouroboros.mcp.types import MCPToolDefinition
from ouroboros.orchestrator.capabilities import (
    CapabilityApprovalClass,
    CapabilityMutationClass,
    CapabilityOrigin,
    CapabilityParallelSafety,
    CapabilityScope,
    build_capability_graph,
    load_tool_capability_overrides,
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


def test_build_capability_graph_records_inherited_capabilities_without_entries() -> None:
    catalog = replace(
        assemble_session_tool_catalog(["Read"]),
        inherited_capabilities=frozenset({"mcp__chrome-devtools__click"}),
    )

    graph = build_capability_graph(catalog)

    descriptors = {descriptor.name: descriptor for descriptor in graph.capabilities}
    inherited = descriptors["mcp__chrome-devtools__click"]
    assert [descriptor.name for descriptor in graph.capabilities] == [
        "Read",
        "mcp__chrome-devtools__click",
    ]
    assert inherited.stable_id == "inherited:mcp__chrome-devtools__click"
    assert inherited.source_kind == "inherited_capability"
    assert inherited.semantics.origin is CapabilityOrigin.ATTACHED_MCP
    assert inherited.semantics.scope is CapabilityScope.ATTACHMENT


def test_build_capability_graph_applies_attached_tool_override(tmp_path) -> None:
    override_path = tmp_path / "tool_capabilities.yaml"
    override_path.write_text(
        """
tools:
  browser:chrome_navigate:
    mutation_class: read_only
    parallel_safety: safe
    interruptibility: none
    approval_class: default
""",
        encoding="utf-8",
    )
    overrides = load_tool_capability_overrides(override_path)
    catalog = assemble_session_tool_catalog(
        attached_tools=(
            MCPToolDefinition(
                name="chrome_navigate",
                description="Navigate the browser",
                server_name="browser",
            ),
        ),
    )

    graph = build_capability_graph(catalog, capability_overrides=overrides)

    descriptor = graph.capabilities[0]
    assert descriptor.semantics.mutation_class is CapabilityMutationClass.READ_ONLY
    assert descriptor.semantics.parallel_safety is CapabilityParallelSafety.SAFE
    assert descriptor.semantics.approval_class is CapabilityApprovalClass.DEFAULT


def test_partial_override_merges_onto_inferred_semantics(tmp_path, monkeypatch) -> None:
    """Partial overrides should retain inferred fields the user did not set."""
    override_path = tmp_path / "tool_capabilities.yaml"
    override_path.write_text(
        """
tools:
  browser:chrome_screenshot:
    mutation_class: read_only
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("OUROBOROS_TOOL_CAPABILITIES", str(override_path))
    catalog = assemble_session_tool_catalog(
        attached_tools=(
            MCPToolDefinition(
                name="chrome_screenshot",
                description="Capture a screenshot",
                server_name="browser",
            ),
        ),
    )

    graph = build_capability_graph(catalog)

    descriptor = graph.capabilities[0]
    # User only reclassified the mutation_class; remaining dimensions come
    # from the conservative inferred defaults for an attached MCP tool.
    assert descriptor.semantics.mutation_class is CapabilityMutationClass.READ_ONLY
    assert descriptor.semantics.origin is CapabilityOrigin.ATTACHED_MCP
    assert descriptor.semantics.scope is CapabilityScope.ATTACHMENT
    assert descriptor.semantics.approval_class is not None


def test_invalid_override_enum_value_is_logged_and_skipped(tmp_path, monkeypatch) -> None:
    """Malformed overrides should log a warning instead of being silenced."""
    import structlog

    override_path = tmp_path / "tool_capabilities.yaml"
    override_path.write_text(
        """
tools:
  browser:chrome_navigate:
    mutation_class: totally-not-a-real-enum
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("OUROBOROS_TOOL_CAPABILITIES", str(override_path))
    catalog = assemble_session_tool_catalog(
        attached_tools=(
            MCPToolDefinition(
                name="chrome_navigate",
                description="Navigate the browser",
                server_name="browser",
            ),
        ),
    )

    with structlog.testing.capture_logs() as captured_events:
        graph = build_capability_graph(catalog)

    # Graph still produced with inferred semantics (fail-open classification
    # rather than silent discard of the tool itself).
    assert len(graph.capabilities) == 1
    # A structlog warning was emitted so user typos do not go unnoticed.
    assert any(
        event.get("event") == "capability_override.invalid_enum" for event in captured_events
    )


def test_malformed_yaml_does_not_break_capability_graph(tmp_path, monkeypatch) -> None:
    """YAML parse failures in the user override file must not propagate.

    Regression guard for the design note that a single bad user config
    line would otherwise take down unrelated orchestration paths
    (interview, evaluation, execution) because they all build a
    capability graph on the default path.
    """
    import structlog

    override_path = tmp_path / "tool_capabilities.yaml"
    # Invalid YAML: unmatched indentation + stray tabs.
    override_path.write_text(
        "tools:\n  browser:\n\tchrome_navigate:\n  mutation_class: [unclosed\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OUROBOROS_TOOL_CAPABILITIES", str(override_path))
    catalog = assemble_session_tool_catalog(
        attached_tools=(
            MCPToolDefinition(
                name="chrome_navigate",
                description="Navigate the browser",
                server_name="browser",
            ),
        ),
    )

    with structlog.testing.capture_logs() as captured_events:
        # Must not raise — override layer is optional enhancement.
        graph = build_capability_graph(catalog)

    assert len(graph.capabilities) == 1
    # The failure must still be visible to operators.
    assert any(
        event.get("event") == "capability_override.yaml_parse_failed"
        for event in captured_events
    )


def test_unreadable_override_path_does_not_break_capability_graph(
    tmp_path, monkeypatch
) -> None:
    """A directory (or other non-regular path) at the override location
    must be handled gracefully rather than raising ``IsADirectoryError``.
    """
    # Point the override env var at a directory instead of a file.
    monkeypatch.setenv("OUROBOROS_TOOL_CAPABILITIES", str(tmp_path))
    catalog = assemble_session_tool_catalog(["Read"])

    # Must not raise.
    graph = build_capability_graph(catalog)

    assert [descriptor.name for descriptor in graph.capabilities] == ["Read"]
