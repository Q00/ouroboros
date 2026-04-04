"""Engine-owned capability graph derived from tool catalog state."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ouroboros.mcp.types import MCPToolDefinition
from ouroboros.orchestrator.mcp_tools import (
    SessionToolCatalog,
    SessionToolCatalogEntry,
    ToolCatalogSourceMetadata,
)


class CapabilityMutationClass(StrEnum):
    """How a capability can mutate state."""

    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    EXTERNAL_SIDE_EFFECT = "external_side_effect"
    DESTRUCTIVE = "destructive"


class CapabilityParallelSafety(StrEnum):
    """How safely a capability can be used in parallel."""

    SAFE = "safe"
    SERIALIZED = "serialized"
    ISOLATED_SESSION_REQUIRED = "isolated_session_required"


class CapabilityInterruptibility(StrEnum):
    """How safely a running capability can be interrupted."""

    NONE = "none"
    SOFT = "soft"
    HARD = "hard"


class CapabilityApprovalClass(StrEnum):
    """Approval sensitivity for a capability."""

    DEFAULT = "default"
    ELEVATED = "elevated"
    BYPASS_FORBIDDEN = "bypass_forbidden"


class CapabilityOrigin(StrEnum):
    """Engine-level provenance classes for capabilities."""

    BUILTIN = "builtin"
    ATTACHED_MCP = "attached_mcp"
    PROVIDER_NATIVE = "provider_native"
    FUTURE_RUNTIME = "future_runtime"


class CapabilityScope(StrEnum):
    """Where a capability conceptually belongs."""

    KERNEL = "kernel"
    SIDECAR = "sidecar"
    ATTACHMENT = "attachment"
    SHELL_ONLY = "shell_only"


@dataclass(frozen=True, slots=True)
class CapabilitySemantics:
    """Engine semantics attached to a tool capability."""

    mutation_class: CapabilityMutationClass
    parallel_safety: CapabilityParallelSafety
    interruptibility: CapabilityInterruptibility
    approval_class: CapabilityApprovalClass
    origin: CapabilityOrigin
    scope: CapabilityScope


@dataclass(frozen=True, slots=True)
class CapabilityDescriptor:
    """Capability wrapper around a normalized tool definition."""

    stable_id: str
    name: str
    original_name: str
    description: str
    server_name: str | None
    source_kind: str
    source_name: str
    semantics: CapabilitySemantics


@dataclass(frozen=True, slots=True)
class CapabilityGraph:
    """Deterministic engine-owned capability graph."""

    capabilities: tuple[CapabilityDescriptor, ...] = field(default_factory=tuple)

    def names(self) -> tuple[str, ...]:
        """Return capability names in graph order."""
        return tuple(descriptor.name for descriptor in self.capabilities)


_BUILTIN_SEMANTICS: dict[str, CapabilitySemantics] = {
    "Read": CapabilitySemantics(
        mutation_class=CapabilityMutationClass.READ_ONLY,
        parallel_safety=CapabilityParallelSafety.SAFE,
        interruptibility=CapabilityInterruptibility.NONE,
        approval_class=CapabilityApprovalClass.DEFAULT,
        origin=CapabilityOrigin.BUILTIN,
        scope=CapabilityScope.KERNEL,
    ),
    "Glob": CapabilitySemantics(
        mutation_class=CapabilityMutationClass.READ_ONLY,
        parallel_safety=CapabilityParallelSafety.SAFE,
        interruptibility=CapabilityInterruptibility.NONE,
        approval_class=CapabilityApprovalClass.DEFAULT,
        origin=CapabilityOrigin.BUILTIN,
        scope=CapabilityScope.KERNEL,
    ),
    "Grep": CapabilitySemantics(
        mutation_class=CapabilityMutationClass.READ_ONLY,
        parallel_safety=CapabilityParallelSafety.SAFE,
        interruptibility=CapabilityInterruptibility.NONE,
        approval_class=CapabilityApprovalClass.DEFAULT,
        origin=CapabilityOrigin.BUILTIN,
        scope=CapabilityScope.KERNEL,
    ),
    "WebFetch": CapabilitySemantics(
        mutation_class=CapabilityMutationClass.READ_ONLY,
        parallel_safety=CapabilityParallelSafety.SAFE,
        interruptibility=CapabilityInterruptibility.NONE,
        approval_class=CapabilityApprovalClass.DEFAULT,
        origin=CapabilityOrigin.BUILTIN,
        scope=CapabilityScope.SIDECAR,
    ),
    "WebSearch": CapabilitySemantics(
        mutation_class=CapabilityMutationClass.READ_ONLY,
        parallel_safety=CapabilityParallelSafety.SAFE,
        interruptibility=CapabilityInterruptibility.NONE,
        approval_class=CapabilityApprovalClass.DEFAULT,
        origin=CapabilityOrigin.BUILTIN,
        scope=CapabilityScope.SIDECAR,
    ),
    "Edit": CapabilitySemantics(
        mutation_class=CapabilityMutationClass.WORKSPACE_WRITE,
        parallel_safety=CapabilityParallelSafety.SERIALIZED,
        interruptibility=CapabilityInterruptibility.SOFT,
        approval_class=CapabilityApprovalClass.DEFAULT,
        origin=CapabilityOrigin.BUILTIN,
        scope=CapabilityScope.KERNEL,
    ),
    "Write": CapabilitySemantics(
        mutation_class=CapabilityMutationClass.WORKSPACE_WRITE,
        parallel_safety=CapabilityParallelSafety.SERIALIZED,
        interruptibility=CapabilityInterruptibility.SOFT,
        approval_class=CapabilityApprovalClass.ELEVATED,
        origin=CapabilityOrigin.BUILTIN,
        scope=CapabilityScope.KERNEL,
    ),
    "NotebookEdit": CapabilitySemantics(
        mutation_class=CapabilityMutationClass.WORKSPACE_WRITE,
        parallel_safety=CapabilityParallelSafety.SERIALIZED,
        interruptibility=CapabilityInterruptibility.SOFT,
        approval_class=CapabilityApprovalClass.ELEVATED,
        origin=CapabilityOrigin.BUILTIN,
        scope=CapabilityScope.SIDECAR,
    ),
    "Bash": CapabilitySemantics(
        mutation_class=CapabilityMutationClass.EXTERNAL_SIDE_EFFECT,
        parallel_safety=CapabilityParallelSafety.ISOLATED_SESSION_REQUIRED,
        interruptibility=CapabilityInterruptibility.HARD,
        approval_class=CapabilityApprovalClass.ELEVATED,
        origin=CapabilityOrigin.BUILTIN,
        scope=CapabilityScope.SHELL_ONLY,
    ),
}


def _fallback_source_metadata(tool: MCPToolDefinition) -> ToolCatalogSourceMetadata:
    source_kind = "attached_mcp" if tool.server_name else "builtin"
    source_name = tool.server_name or "built-in"
    return ToolCatalogSourceMetadata(
        kind=source_kind,
        name=source_name,
        original_name=tool.name,
        server_name=tool.server_name,
    )


def _infer_attached_semantics(tool: MCPToolDefinition) -> CapabilitySemantics:
    fingerprint = f"{tool.name} {tool.description}".lower()
    if any(token in fingerprint for token in ("delete", "destroy", "drop", "remove", "kill")):
        mutation_class = CapabilityMutationClass.DESTRUCTIVE
        parallel_safety = CapabilityParallelSafety.ISOLATED_SESSION_REQUIRED
        interruptibility = CapabilityInterruptibility.HARD
        approval_class = CapabilityApprovalClass.BYPASS_FORBIDDEN
    elif any(token in fingerprint for token in ("read", "list", "search", "fetch", "query")):
        mutation_class = CapabilityMutationClass.READ_ONLY
        parallel_safety = CapabilityParallelSafety.SAFE
        interruptibility = CapabilityInterruptibility.NONE
        approval_class = CapabilityApprovalClass.DEFAULT
    elif any(token in fingerprint for token in ("exec", "run", "shell", "command")):
        mutation_class = CapabilityMutationClass.EXTERNAL_SIDE_EFFECT
        parallel_safety = CapabilityParallelSafety.ISOLATED_SESSION_REQUIRED
        interruptibility = CapabilityInterruptibility.HARD
        approval_class = CapabilityApprovalClass.ELEVATED
    else:
        mutation_class = CapabilityMutationClass.EXTERNAL_SIDE_EFFECT
        parallel_safety = CapabilityParallelSafety.SERIALIZED
        interruptibility = CapabilityInterruptibility.SOFT
        approval_class = CapabilityApprovalClass.ELEVATED

    return CapabilitySemantics(
        mutation_class=mutation_class,
        parallel_safety=parallel_safety,
        interruptibility=interruptibility,
        approval_class=approval_class,
        origin=CapabilityOrigin.ATTACHED_MCP,
        scope=CapabilityScope.ATTACHMENT,
    )


def _semantics_for_entry(
    tool: MCPToolDefinition,
    source: ToolCatalogSourceMetadata,
) -> CapabilitySemantics:
    if source.kind == "builtin":
        return _BUILTIN_SEMANTICS.get(
            tool.name,
            CapabilitySemantics(
                mutation_class=CapabilityMutationClass.READ_ONLY,
                parallel_safety=CapabilityParallelSafety.SAFE,
                interruptibility=CapabilityInterruptibility.NONE,
                approval_class=CapabilityApprovalClass.DEFAULT,
                origin=CapabilityOrigin.BUILTIN,
                scope=CapabilityScope.KERNEL,
            ),
        )
    return _infer_attached_semantics(tool)


def _stable_id(tool: MCPToolDefinition, source: ToolCatalogSourceMetadata) -> str:
    if source.kind == "builtin":
        return f"builtin:{tool.name}"
    source_name = source.server_name or source.name
    return f"mcp:{source_name}:{tool.name}"


def _descriptor_from_tool(
    tool: MCPToolDefinition,
    source: ToolCatalogSourceMetadata | None = None,
    *,
    stable_id: str | None = None,
) -> CapabilityDescriptor:
    resolved_source = source or _fallback_source_metadata(tool)
    return CapabilityDescriptor(
        stable_id=stable_id or _stable_id(tool, resolved_source),
        name=tool.name,
        original_name=resolved_source.original_name,
        description=tool.description,
        server_name=tool.server_name,
        source_kind=resolved_source.kind,
        source_name=resolved_source.name,
        semantics=_semantics_for_entry(tool, resolved_source),
    )


def build_capability_graph(
    tool_catalog: SessionToolCatalog
    | Sequence[MCPToolDefinition]
    | Sequence[SessionToolCatalogEntry],
) -> CapabilityGraph:
    """Build a deterministic capability graph from the current tool surface."""
    descriptors: list[CapabilityDescriptor] = []

    if isinstance(tool_catalog, SessionToolCatalog):
        entries = tool_catalog.entries
    else:
        entries = tool_catalog

    for entry in entries:
        if isinstance(entry, SessionToolCatalogEntry):
            descriptors.append(
                _descriptor_from_tool(
                    entry.tool,
                    entry.source,
                    stable_id=entry.stable_id,
                )
            )
        else:
            descriptors.append(_descriptor_from_tool(entry))

    return CapabilityGraph(capabilities=tuple(descriptors))


def serialize_capability_graph(
    graph: CapabilityGraph | Sequence[CapabilityDescriptor],
) -> list[dict[str, Any]]:
    """Serialize a capability graph into JSON-safe metadata."""
    capabilities = graph.capabilities if isinstance(graph, CapabilityGraph) else tuple(graph)
    return [
        {
            "stable_id": descriptor.stable_id,
            "name": descriptor.name,
            "original_name": descriptor.original_name,
            "description": descriptor.description,
            "server_name": descriptor.server_name,
            "source_kind": descriptor.source_kind,
            "source_name": descriptor.source_name,
            "semantics": {
                "mutation_class": descriptor.semantics.mutation_class.value,
                "parallel_safety": descriptor.semantics.parallel_safety.value,
                "interruptibility": descriptor.semantics.interruptibility.value,
                "approval_class": descriptor.semantics.approval_class.value,
                "origin": descriptor.semantics.origin.value,
                "scope": descriptor.semantics.scope.value,
            },
        }
        for descriptor in capabilities
    ]


def normalize_serialized_capability_graph(
    payload: Sequence[Mapping[str, Any]] | None,
) -> CapabilityGraph | None:
    """Rehydrate a serialized capability graph payload."""
    if not payload:
        return None

    descriptors: list[CapabilityDescriptor] = []
    for entry in payload:
        semantics = entry.get("semantics")
        if not isinstance(semantics, Mapping):
            continue
        try:
            descriptors.append(
                CapabilityDescriptor(
                    stable_id=str(entry.get("stable_id", "")),
                    name=str(entry.get("name", "")),
                    original_name=str(entry.get("original_name", "")),
                    description=str(entry.get("description", "")),
                    server_name=entry.get("server_name")
                    if isinstance(entry.get("server_name"), str)
                    else None,
                    source_kind=str(entry.get("source_kind", "")),
                    source_name=str(entry.get("source_name", "")),
                    semantics=CapabilitySemantics(
                        mutation_class=CapabilityMutationClass(
                            str(semantics.get("mutation_class"))
                        ),
                        parallel_safety=CapabilityParallelSafety(
                            str(semantics.get("parallel_safety"))
                        ),
                        interruptibility=CapabilityInterruptibility(
                            str(semantics.get("interruptibility"))
                        ),
                        approval_class=CapabilityApprovalClass(
                            str(semantics.get("approval_class"))
                        ),
                        origin=CapabilityOrigin(str(semantics.get("origin"))),
                        scope=CapabilityScope(str(semantics.get("scope"))),
                    ),
                )
            )
        except ValueError:
            continue

    return CapabilityGraph(capabilities=tuple(descriptors))


__all__ = [
    "CapabilityApprovalClass",
    "CapabilityDescriptor",
    "CapabilityGraph",
    "CapabilityInterruptibility",
    "CapabilityMutationClass",
    "CapabilityOrigin",
    "CapabilityParallelSafety",
    "CapabilityScope",
    "CapabilitySemantics",
    "build_capability_graph",
    "normalize_serialized_capability_graph",
    "serialize_capability_graph",
]
