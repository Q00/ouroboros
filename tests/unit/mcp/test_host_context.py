"""Request-scoped MCP host capability normalization and dispatch authority."""

from __future__ import annotations

from types import SimpleNamespace

from ouroboros.backends.capabilities import SubagentDispatchMode
from ouroboros.mcp.host_context import (
    DispatchAuthority,
    HostCapabilitySource,
    HostFamily,
    HostIdentityStatus,
    HostSubagentCapability,
    MCPHostContext,
    current_mcp_host_context,
    from_sdk_context,
    resolve_request_subagent_dispatch,
    use_mcp_host_context,
)


def _sdk_context(
    *,
    name: str | None = None,
    extensions: dict[str, dict[str, object]] | None = None,
    experimental: dict[str, dict[str, object]] | None = None,
) -> SimpleNamespace:
    client_info = SimpleNamespace(name=name) if name is not None else None
    client_params = SimpleNamespace(client_info=client_info) if client_info is not None else None
    capabilities = SimpleNamespace(extensions=extensions, experimental=experimental)
    return SimpleNamespace(
        session=SimpleNamespace(
            client_params=client_params,
            client_capabilities=capabilities,
        )
    )


def test_missing_client_info_is_external_unknown_with_undeclared_capability() -> None:
    context = from_sdk_context(_sdk_context())

    assert context == MCPHostContext(
        dispatch_authority=DispatchAuthority.MCP_HOST,
    )


def test_known_client_identity_does_not_imply_subagent_capability() -> None:
    context = from_sdk_context(_sdk_context(name="claude-code"))

    assert context.host_family is HostFamily.CLAUDE_CODE
    assert context.identity_status is HostIdentityStatus.KNOWN
    assert context.subagent_capability is HostSubagentCapability.UNDECLARED
    assert context.capability_source is HostCapabilitySource.NONE


def test_extension_declares_parallel_capability() -> None:
    context = from_sdk_context(
        _sdk_context(
            name="codex",
            extensions={"io.ouroboros/subagents": {"mode": "parallel"}},
        )
    )

    assert context.host_family is HostFamily.CODEX
    assert context.subagent_capability is HostSubagentCapability.PARALLEL
    assert context.capability_source is HostCapabilitySource.MCP_EXTENSION


def test_legacy_experimental_extension_declares_sequential_capability() -> None:
    context = from_sdk_context(
        _sdk_context(
            name="custom-private-client-name",
            experimental={"io.ouroboros/subagents": {"parallel": False}},
        )
    )

    assert context.host_family is HostFamily.OTHER_KNOWN
    assert context.subagent_capability is HostSubagentCapability.SEQUENTIAL


def test_external_undeclared_host_gets_host_decides_not_worker_sequential() -> None:
    context = MCPHostContext(dispatch_authority=DispatchAuthority.MCP_HOST)

    with use_mcp_host_context(context):
        dispatch = resolve_request_subagent_dispatch("gemini", None)

    assert dispatch is SubagentDispatchMode.HOST_DECIDES
    assert current_mcp_host_context().dispatch_authority is DispatchAuthority.INTERNAL_RUNTIME


def test_external_declared_parallel_overrides_worker_backend() -> None:
    context = MCPHostContext(
        subagent_capability=HostSubagentCapability.PARALLEL,
        capability_source=HostCapabilitySource.MCP_EXTENSION,
        dispatch_authority=DispatchAuthority.MCP_HOST,
    )

    with use_mcp_host_context(context):
        dispatch = resolve_request_subagent_dispatch("gemini", None)

    assert dispatch is SubagentDispatchMode.HOST_DRIVEN


def test_external_declared_sequential_overrides_host_driven_worker() -> None:
    context = MCPHostContext(
        subagent_capability=HostSubagentCapability.SEQUENTIAL,
        capability_source=HostCapabilitySource.MCP_EXTENSION,
        dispatch_authority=DispatchAuthority.MCP_HOST,
    )

    with use_mcp_host_context(context):
        dispatch = resolve_request_subagent_dispatch("claude", None)

    assert dispatch is SubagentDispatchMode.SEQUENTIAL


def test_external_host_does_not_inherit_worker_passive_bridge() -> None:
    context = MCPHostContext(dispatch_authority=DispatchAuthority.MCP_HOST)

    with use_mcp_host_context(context):
        dispatch = resolve_request_subagent_dispatch("opencode", "plugin")

    assert dispatch is SubagentDispatchMode.HOST_DECIDES


def test_external_opencode_identity_with_plugin_mode_keeps_passive_bridge() -> None:
    context = MCPHostContext(
        host_family=HostFamily.OPENCODE,
        identity_status=HostIdentityStatus.KNOWN,
        dispatch_authority=DispatchAuthority.MCP_HOST,
    )

    with use_mcp_host_context(context):
        dispatch = resolve_request_subagent_dispatch("opencode", "plugin")

    assert dispatch is SubagentDispatchMode.PLUGIN_PASSIVE


def test_internal_runtime_keeps_existing_worker_resolution() -> None:
    assert resolve_request_subagent_dispatch("gemini", None) is SubagentDispatchMode.SEQUENTIAL
    assert resolve_request_subagent_dispatch("claude", None) is SubagentDispatchMode.HOST_DRIVEN


def test_internal_plugin_passive_remains_server_owned() -> None:
    assert (
        resolve_request_subagent_dispatch("opencode", "plugin")
        is SubagentDispatchMode.PLUGIN_PASSIVE
    )
