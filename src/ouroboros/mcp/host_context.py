"""Request-scoped MCP host identity and subagent capability facts.

The configured Ouroboros worker backend is not the same thing as the MCP
client that will consume an inline fan-out payload. This module keeps those
facts separate and exposes only normalized, privacy-safe values to handlers
and telemetry.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ouroboros.backends.capabilities import (
    SubagentDispatchMode,
    get_backend_capability,
    resolve_subagent_dispatch,
)


class HostFamily(StrEnum):
    CLAUDE_CODE = "claude_code"
    CODEX = "codex"
    OPENCODE = "opencode"
    OTHER_KNOWN = "other_known"
    UNKNOWN = "unknown"


class HostIdentityStatus(StrEnum):
    KNOWN = "known"
    UNKNOWN = "unknown"


class HostSubagentCapability(StrEnum):
    PARALLEL = "parallel"
    SEQUENTIAL = "sequential"
    UNAVAILABLE = "unavailable"
    UNDECLARED = "undeclared"


class HostCapabilitySource(StrEnum):
    MCP_EXTENSION = "mcp_extension"
    TRUSTED_SERVER_OPTION = "trusted_server_option"
    PASSIVE_BRIDGE = "passive_bridge"
    NONE = "none"


class DispatchAuthority(StrEnum):
    MCP_HOST = "mcp_host"
    INTERNAL_RUNTIME = "internal_runtime"
    PASSIVE_BRIDGE = "passive_bridge"


SUBAGENT_CAPABILITY_EXTENSION = "io.ouroboros/subagents"
_CAPABILITY_MODES = frozenset(item.value for item in HostSubagentCapability)


@dataclass(frozen=True, slots=True)
class MCPHostContext:
    """Normalized facts about the current MCP consumer.

    ``UNDECLARED`` is intentionally different from ``SEQUENTIAL`` and
    ``UNAVAILABLE``. It means the server has no authoritative host capability
    fact and must not emit either a positive or negative native-subagent claim.
    """

    host_family: HostFamily = HostFamily.UNKNOWN
    identity_status: HostIdentityStatus = HostIdentityStatus.UNKNOWN
    subagent_capability: HostSubagentCapability = HostSubagentCapability.UNDECLARED
    capability_source: HostCapabilitySource = HostCapabilitySource.NONE
    dispatch_authority: DispatchAuthority = DispatchAuthority.INTERNAL_RUNTIME

    @property
    def is_mcp_host(self) -> bool:
        return self.dispatch_authority is DispatchAuthority.MCP_HOST

    @property
    def is_capability_known(self) -> bool:
        return self.subagent_capability is not HostSubagentCapability.UNDECLARED

    def to_telemetry(self) -> dict[str, str]:
        return {
            "host_family": self.host_family.value,
            "host_identity_status": self.identity_status.value,
            "host_capability": self.subagent_capability.value,
            "capability_source": self.capability_source.value,
            "dispatch_authority": self.dispatch_authority.value,
        }


_DEFAULT_CONTEXT = MCPHostContext()
_current_context: ContextVar[MCPHostContext] = ContextVar(
    "ouroboros_mcp_host_context",
    default=_DEFAULT_CONTEXT,
)


def current_mcp_host_context() -> MCPHostContext:
    """Return the current request context, or internal-runtime defaults."""
    return _current_context.get()


@contextmanager
def use_mcp_host_context(context: MCPHostContext) -> Iterator[None]:
    """Install a request context for the duration of one handler call."""
    token: Token[MCPHostContext] = _current_context.set(context)
    try:
        yield
    finally:
        _current_context.reset(token)


def _host_family(name: str | None) -> HostFamily:
    normalized = (name or "").strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in {"claude", "claude_code", "claude_cli", "claude_code_cli"}:
        return HostFamily.CLAUDE_CODE
    if normalized in {"codex", "codex_cli", "codex_app"}:
        return HostFamily.CODEX
    if normalized in {"opencode", "opencode_cli"}:
        return HostFamily.OPENCODE
    if normalized:
        return HostFamily.OTHER_KNOWN
    return HostFamily.UNKNOWN


def _extension_mode(
    capabilities: Any,
) -> HostSubagentCapability | None:
    if capabilities is None:
        return None
    candidates: list[Mapping[str, Any]] = []
    for field_name in ("extensions", "experimental"):
        raw = getattr(capabilities, field_name, None)
        if isinstance(raw, Mapping):
            value = raw.get(SUBAGENT_CAPABILITY_EXTENSION)
            if isinstance(value, Mapping):
                candidates.append(value)
    for value in candidates:
        mode = value.get("mode")
        if isinstance(mode, str) and mode.strip().lower() in _CAPABILITY_MODES:
            return HostSubagentCapability(mode.strip().lower())
        if value.get("parallel") is True:
            return HostSubagentCapability.PARALLEL
        if value.get("parallel") is False:
            return HostSubagentCapability.SEQUENTIAL
    return None


def from_sdk_context(context: Any) -> MCPHostContext:
    """Normalize an MCP SDK ``Context`` without retaining raw client data."""
    if context is None:
        return _DEFAULT_CONTEXT
    try:
        session = context.session
        params = getattr(session, "client_params", None)
        client_info = getattr(params, "client_info", None)
        name = getattr(client_info, "name", None)
        identity = HostIdentityStatus.KNOWN if name else HostIdentityStatus.UNKNOWN
        capabilities = getattr(session, "client_capabilities", None)
        extension_mode = _extension_mode(capabilities)
        return MCPHostContext(
            host_family=_host_family(name),
            identity_status=identity,
            subagent_capability=(extension_mode or HostSubagentCapability.UNDECLARED),
            capability_source=(
                HostCapabilitySource.MCP_EXTENSION
                if extension_mode is not None
                else HostCapabilitySource.NONE
            ),
            dispatch_authority=DispatchAuthority.MCP_HOST,
        )
    except (AttributeError, TypeError):
        return MCPHostContext(
            dispatch_authority=DispatchAuthority.MCP_HOST,
        )


def resolve_request_subagent_dispatch(
    worker_backend: str | None,
    opencode_mode: str | None,
) -> SubagentDispatchMode:
    """Resolve dispatch without treating worker configuration as host capability.

    Direct/internal calls retain the backend resolver, including a trusted
    passive plugin bridge. External MCP calls use only an explicit request-scoped
    capability fact; neither plugin delivery nor execution capability is inferred
    from the configured worker. Absence becomes HOST_DECIDES.
    """

    worker_mode = resolve_subagent_dispatch(worker_backend, opencode_mode)
    context = current_mcp_host_context()
    if not context.is_mcp_host:
        return worker_mode
    if context.dispatch_authority is DispatchAuthority.PASSIVE_BRIDGE:
        return SubagentDispatchMode.PLUGIN_PASSIVE
    if (
        worker_mode is SubagentDispatchMode.PLUGIN_PASSIVE
        and context.host_family is HostFamily.OPENCODE
    ):
        return SubagentDispatchMode.PLUGIN_PASSIVE
    if context.subagent_capability is HostSubagentCapability.PARALLEL:
        return SubagentDispatchMode.HOST_DRIVEN
    if context.subagent_capability in {
        HostSubagentCapability.SEQUENTIAL,
        HostSubagentCapability.UNAVAILABLE,
    }:
        return SubagentDispatchMode.SEQUENTIAL
    return SubagentDispatchMode.HOST_DECIDES


def normalized_worker_backend(worker_backend: str | None) -> str:
    """Return a closed, telemetry-safe worker backend value."""

    capability = get_backend_capability((worker_backend or "").strip().lower())
    return capability.name if capability is not None else "other"


def host_worker_mismatch(context: MCPHostContext, worker_backend: str | None) -> bool:
    """Return whether a known host family differs from the configured worker."""
    expected = {
        HostFamily.CLAUDE_CODE: "claude",
        HostFamily.CODEX: "codex",
        HostFamily.OPENCODE: "opencode",
    }.get(context.host_family)
    return expected is not None and expected != normalized_worker_backend(worker_backend)


def context_for_internal_runtime(
    capability: HostSubagentCapability,
) -> MCPHostContext:
    """Create an explicit context for trusted in-process execution."""
    return MCPHostContext(
        host_family=HostFamily.UNKNOWN,
        identity_status=HostIdentityStatus.UNKNOWN,
        subagent_capability=capability,
        capability_source=HostCapabilitySource.TRUSTED_SERVER_OPTION,
        dispatch_authority=DispatchAuthority.INTERNAL_RUNTIME,
    )


def subagent_capability_extensions() -> list[Any] | None:
    """Return the optional SDK extension advertising this capability contract."""
    try:
        from mcp.server.extension import Extension
    except ImportError:
        return None

    class SubagentCapabilityExtension(Extension):
        identifier = SUBAGENT_CAPABILITY_EXTENSION

        def settings(self) -> dict[str, Any]:
            return {
                "modes": ["parallel", "sequential", "unavailable"],
                "undeclaredBehavior": "parallel_preferred_sequential_fallback",
            }

    return [SubagentCapabilityExtension()]


def render_lateral_host_banner(
    dispatch_mode: SubagentDispatchMode,
    payload_count: int,
) -> str:
    """Render the text-only counterpart of a lateral host dispatch contract."""
    if dispatch_mode is SubagentDispatchMode.HOST_DRIVEN:
        return (
            "> **Host action — spawn subagents:** this request declared a native "
            "parallel subagent primitive. Spawn one child per payload, correlate "
            f"results by `context.persona`, then synthesise. Payloads: {payload_count} "
            "(structured copy in `meta` and the dispatch block).\n\n"
        )
    if dispatch_mode is SubagentDispatchMode.HOST_DECIDES:
        return (
            "> **Host action — dispatch if supported:** this request did not "
            "declare whether parallel subagents are available. Use the host's native "
            "parallel primitive when available; otherwise process the same payloads "
            f"sequentially. Correlate by `context.persona`. Payloads: {payload_count} "
            "(structured copy in `meta` and the dispatch block).\n\n"
        )
    return ""


__all__ = [
    "DispatchAuthority",
    "HostCapabilitySource",
    "HostFamily",
    "HostIdentityStatus",
    "HostSubagentCapability",
    "MCPHostContext",
    "SUBAGENT_CAPABILITY_EXTENSION",
    "context_for_internal_runtime",
    "current_mcp_host_context",
    "from_sdk_context",
    "host_worker_mismatch",
    "normalized_worker_backend",
    "render_lateral_host_banner",
    "resolve_request_subagent_dispatch",
    "subagent_capability_extensions",
    "use_mcp_host_context",
]
