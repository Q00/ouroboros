"""Engine-owned capability graph derived from tool catalog state."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
import os
from pathlib import Path
from typing import Any

import yaml

from ouroboros.mcp.types import MCPToolDefinition
from ouroboros.observability.logging import get_logger
from ouroboros.orchestrator.mcp_tools import (
    SessionToolCatalog,
    SessionToolCatalogEntry,
    ToolCatalogSourceMetadata,
)

log = get_logger(__name__)


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

_INHERITED_CAPABILITY_SEMANTICS = CapabilitySemantics(
    mutation_class=CapabilityMutationClass.EXTERNAL_SIDE_EFFECT,
    parallel_safety=CapabilityParallelSafety.SERIALIZED,
    interruptibility=CapabilityInterruptibility.SOFT,
    approval_class=CapabilityApprovalClass.ELEVATED,
    origin=CapabilityOrigin.ATTACHED_MCP,
    scope=CapabilityScope.ATTACHMENT,
)


def _default_attached_semantics() -> CapabilitySemantics:
    return CapabilitySemantics(
        mutation_class=CapabilityMutationClass.EXTERNAL_SIDE_EFFECT,
        parallel_safety=CapabilityParallelSafety.SERIALIZED,
        interruptibility=CapabilityInterruptibility.SOFT,
        approval_class=CapabilityApprovalClass.ELEVATED,
        origin=CapabilityOrigin.ATTACHED_MCP,
        scope=CapabilityScope.ATTACHMENT,
    )


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


def _coerce_capability_semantics(
    raw: Mapping[str, Any],
    *,
    fallback: CapabilitySemantics | None = None,
    context: str | None = None,
) -> CapabilitySemantics:
    """Build semantics from a raw mapping, treating missing keys as fallback.

    Any unrecognized enum value raises ``ValueError``; callers that want
    lenient behavior should catch and log, not silence.
    """
    base = fallback or _default_attached_semantics()
    try:
        return CapabilitySemantics(
            mutation_class=CapabilityMutationClass(
                str(raw.get("mutation_class", base.mutation_class.value))
            ),
            parallel_safety=CapabilityParallelSafety(
                str(raw.get("parallel_safety", base.parallel_safety.value))
            ),
            interruptibility=CapabilityInterruptibility(
                str(raw.get("interruptibility", base.interruptibility.value))
            ),
            approval_class=CapabilityApprovalClass(
                str(raw.get("approval_class", base.approval_class.value))
            ),
            origin=CapabilityOrigin(str(raw.get("origin", base.origin.value))),
            scope=CapabilityScope(str(raw.get("scope", base.scope.value))),
        )
    except ValueError as exc:
        log.warning(
            "capability_override.invalid_enum",
            context=context,
            error=str(exc),
        )
        raise


def _default_tool_capability_override_path() -> Path:
    configured = os.environ.get("OUROBOROS_TOOL_CAPABILITIES")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".ouroboros" / "tool_capabilities.yaml"


# Mapping from resolved override path to (mtime, raw overrides).  Invalidated by
# mtime so edits to ~/.ouroboros/tool_capabilities.yaml take effect without a
# process restart, while repeated graph builds in the same process avoid
# re-reading and re-parsing the YAML file on every call.
_RAW_OVERRIDES_CACHE: dict[Path, tuple[float, dict[str, Mapping[str, Any]]]] = {}


def _read_raw_tool_capability_overrides(path: Path) -> dict[str, Mapping[str, Any]]:
    """Read and parse the override YAML, returning raw per-tool mappings.

    Every failure mode — missing file, unreadable file, malformed YAML,
    unexpected top-level shape — is handled locally.  A broken user
    config must never propagate out of this function, because the override
    loader sits on the default capability-graph construction path and is
    therefore reached from interview, evaluation, and execution sessions
    alike.  A single malformed YAML line in ``~/.ouroboros/`` would
    otherwise take down unrelated orchestration paths.
    """
    if not path.exists():
        return {}

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning(
            "capability_override.read_failed",
            path=str(path),
            error=str(exc),
        )
        return {}

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        log.warning(
            "capability_override.yaml_parse_failed",
            path=str(path),
            error=str(exc),
        )
        return {}

    if not isinstance(raw, Mapping):
        return {}

    raw_tools = raw.get("tools", raw)
    if not isinstance(raw_tools, Mapping):
        return {}

    parsed: dict[str, Mapping[str, Any]] = {}
    for key, value in raw_tools.items():
        if not isinstance(key, str) or not isinstance(value, Mapping):
            continue
        parsed[key] = dict(value)
    return parsed


def _load_raw_tool_capability_overrides(
    path: str | Path | None = None,
) -> dict[str, Mapping[str, Any]]:
    """Load raw override mappings with mtime-based caching.

    Fault-tolerant by design: any failure (missing file, permission error,
    non-regular path, filesystem glitch) returns an empty mapping so that
    downstream graph construction always succeeds.  The override layer is
    an optional enhancement, not a prerequisite for orchestration.
    """
    try:
        config_path = (
            Path(path).expanduser()
            if path is not None
            else _default_tool_capability_override_path()
        )
    except (OSError, ValueError) as exc:
        log.warning(
            "capability_override.path_resolution_failed",
            path=str(path),
            error=str(exc),
        )
        return {}

    try:
        mtime = config_path.stat().st_mtime
    except FileNotFoundError:
        _RAW_OVERRIDES_CACHE.pop(config_path, None)
        return {}
    except OSError as exc:
        log.warning(
            "capability_override.read_failed",
            path=str(config_path),
            error=str(exc),
        )
        return {}

    cached = _RAW_OVERRIDES_CACHE.get(config_path)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    raw = _read_raw_tool_capability_overrides(config_path)
    _RAW_OVERRIDES_CACHE[config_path] = (mtime, raw)
    return raw


def load_tool_capability_overrides(
    path: str | Path | None = None,
) -> dict[str, CapabilitySemantics]:
    """Load user-defined capability semantics overrides from YAML.

    The returned mapping contains fully coerced semantics for each declared
    tool, with missing fields filled from the default attached-semantics.
    When integrating into :func:`build_capability_graph` the override is
    applied *on top of inferred* semantics via
    :func:`_apply_raw_override_to_semantics`; this function is retained for
    external callers (tests, diagnostics) that want pre-coerced semantics.

    Expected format:

    ```yaml
    tools:
      chrome_navigate:
        mutation_class: read_only
        parallel_safety: safe
        interruptibility: none
        approval_class: default
    ```

    Invalid enum values are logged and the affected entry skipped.
    """
    raw_overrides = _load_raw_tool_capability_overrides(path)
    coerced: dict[str, CapabilitySemantics] = {}
    for key, value in raw_overrides.items():
        try:
            coerced[key] = _coerce_capability_semantics(value, context=f"tool:{key}")
        except ValueError:
            continue
    return coerced


def _apply_raw_override_to_semantics(
    inferred: CapabilitySemantics,
    raw: Mapping[str, Any],
    *,
    context: str,
) -> CapabilitySemantics:
    """Merge raw override fields onto already-inferred semantics.

    Unlike wholesale replacement, this preserves inferred values for any
    dimension the user did not explicitly set in their override YAML.  When
    the override declares an invalid enum value, the inferred semantics are
    returned unchanged (the warning is logged).
    """
    try:
        return _coerce_capability_semantics(raw, fallback=inferred, context=context)
    except ValueError:
        return inferred


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
    raw_capability_overrides: Mapping[str, Mapping[str, Any]] | None = None,
) -> CapabilityDescriptor:
    resolved_source = source or _fallback_source_metadata(tool)
    resolved_stable_id = stable_id or _stable_id(tool, resolved_source)
    semantics = _semantics_for_entry(tool, resolved_source)
    # Built-in tools deliberately bypass user overrides: their semantics are
    # part of the engine contract (e.g., Bash must remain EXTERNAL_SIDE_EFFECT
    # regardless of user YAML) so that role envelopes cannot be silently
    # widened.  Attached and provider-native tools are reclassifiable.
    if resolved_source.kind != "builtin" and raw_capability_overrides:
        raw = _match_raw_capability_override(
            tool,
            resolved_source,
            resolved_stable_id,
            raw_capability_overrides,
        )
        if raw is not None:
            semantics = _apply_raw_override_to_semantics(
                semantics,
                raw,
                context=f"tool:{resolved_stable_id}",
            )
    return CapabilityDescriptor(
        stable_id=resolved_stable_id,
        name=tool.name,
        original_name=resolved_source.original_name,
        description=tool.description,
        server_name=tool.server_name,
        source_kind=resolved_source.kind,
        source_name=resolved_source.name,
        semantics=semantics,
    )


def _match_raw_capability_override(
    tool: MCPToolDefinition,
    source: ToolCatalogSourceMetadata,
    stable_id: str,
    raw_capability_overrides: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    source_name = source.server_name or source.name
    candidates = (
        stable_id,
        f"{source.kind}:{source_name}:{tool.name}",
        f"{source_name}:{tool.name}",
        source.original_name,
        tool.name,
    )
    for candidate in candidates:
        if candidate in raw_capability_overrides:
            return raw_capability_overrides[candidate]
    return None


def _descriptor_from_inherited_capability(name: str) -> CapabilityDescriptor:
    """Represent a delegated MCP grant without making it executable."""
    return CapabilityDescriptor(
        stable_id=f"inherited:{name}",
        name=name,
        original_name=name,
        description="Inherited delegated capability pending live MCP discovery",
        server_name=None,
        source_kind="inherited_capability",
        source_name="delegated_parent",
        semantics=_INHERITED_CAPABILITY_SEMANTICS,
    )


def build_capability_graph(
    tool_catalog: SessionToolCatalog
    | Sequence[MCPToolDefinition]
    | Sequence[SessionToolCatalogEntry],
    *,
    capability_overrides: Mapping[str, CapabilitySemantics] | None = None,
) -> CapabilityGraph:
    """Build a deterministic capability graph from the current tool surface.

    ``capability_overrides`` accepts pre-coerced semantics (wholesale
    replacement) for backward compatibility.  When omitted, raw overrides
    are loaded from ``~/.ouroboros/tool_capabilities.yaml`` (cached by
    mtime) and merged *onto* the inferred semantics so callers can override
    only the specific dimensions they care about.
    """
    descriptors: list[CapabilityDescriptor] = []
    # When explicit fully-coerced overrides are passed, we honor wholesale
    # replacement (preserves the legacy external contract).  Otherwise we
    # load the raw YAML once per graph build and merge per-field.
    legacy_overrides = capability_overrides
    raw_overrides: Mapping[str, Mapping[str, Any]] = (
        {} if legacy_overrides is not None else _load_raw_tool_capability_overrides()
    )

    inherited_capabilities: frozenset[str] = frozenset()
    if isinstance(tool_catalog, SessionToolCatalog):
        entries = tool_catalog.entries
        inherited_capabilities = tool_catalog.inherited_capabilities
    else:
        entries = tool_catalog

    for entry in entries:
        if isinstance(entry, SessionToolCatalogEntry):
            descriptor = _descriptor_from_tool(
                entry.tool,
                entry.source,
                stable_id=entry.stable_id,
                raw_capability_overrides=raw_overrides,
            )
        else:
            descriptor = _descriptor_from_tool(
                entry,
                raw_capability_overrides=raw_overrides,
            )
        if legacy_overrides is not None and descriptor.source_kind != "builtin":
            replacement = _match_legacy_override(descriptor, legacy_overrides)
            if replacement is not None:
                descriptor = replace(descriptor, semantics=replacement)
        descriptors.append(descriptor)

    for capability_name in sorted(inherited_capabilities):
        descriptors.append(_descriptor_from_inherited_capability(capability_name))

    return CapabilityGraph(capabilities=tuple(descriptors))


def _match_legacy_override(
    descriptor: CapabilityDescriptor,
    capability_overrides: Mapping[str, CapabilitySemantics],
) -> CapabilitySemantics | None:
    """Resolve pre-coerced legacy-style overrides against a descriptor."""
    source_name = descriptor.server_name or descriptor.source_name
    candidates = (
        descriptor.stable_id,
        f"{descriptor.source_kind}:{source_name}:{descriptor.name}",
        f"{source_name}:{descriptor.name}",
        descriptor.original_name,
        descriptor.name,
    )
    for candidate in candidates:
        if candidate in capability_overrides:
            return capability_overrides[candidate]
    return None


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
    "load_tool_capability_overrides",
    "normalize_serialized_capability_graph",
    "serialize_capability_graph",
]
