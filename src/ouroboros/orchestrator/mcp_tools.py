"""MCP Tool Provider for OrchestratorRunner.

This module provides the MCPToolProvider class that wraps external MCP tools
as agent-callable tools during workflow execution.

Features:
    - Converts MCPClientManager tools to agent tool format
    - Handles tool execution with configurable timeouts
    - Implements retry policy for transient failures
    - Provides graceful error handling (no crashes on MCP failures)

Usage:
    provider = MCPToolProvider(mcp_manager)
    tools = await provider.get_tools()
    result = await provider.call_tool("tool_name", {"arg": "value"})
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from ouroboros.core.retry import retry_async
from ouroboros.core.types import Result
from ouroboros.mcp.errors import (
    MCPClientError,
    MCPConnectionError,
    MCPToolError,
)
from ouroboros.mcp.types import (
    ContentType,
    MCPContentItem,
    MCPToolDefinition,
    MCPToolParameter,
    MCPToolResult,
    ToolInputType,
)
from ouroboros.observability.logging import get_logger

if TYPE_CHECKING:
    from ouroboros.mcp.client.manager import MCPClientManager

log = get_logger(__name__)

_RUNTIME_TOOL_DESCRIPTIONS: dict[str, str] = {
    "Read": "Read a file from the workspace.",
    "Write": "Write a file in the workspace.",
    "Edit": "Edit an existing file in the workspace.",
    "Bash": "Run a shell command in the workspace.",
    "Glob": "Match files in the workspace using a glob pattern.",
    "Grep": "Search workspace files for a pattern.",
    "WebFetch": "Fetch content from a URL for reference.",
    "WebSearch": "Search the web for supporting information.",
    "NotebookEdit": "Edit a notebook file in the workspace.",
}

_RUNTIME_TOOL_PRIMARY_INPUT_KEYS: dict[str, str] = {
    "Read": "file_path",
    "Write": "file_path",
    "Edit": "file_path",
    "Bash": "command",
    "Glob": "pattern",
    "Grep": "pattern",
    "WebFetch": "url",
    "WebSearch": "query",
    "NotebookEdit": "notebook_path",
}

_RUNTIME_TOOL_NAME_ALIASES: dict[str, str] = {
    "read": "Read",
    "fileread": "Read",
    "readfile": "Read",
    "write": "Write",
    "filewrite": "Write",
    "writefile": "Write",
    "edit": "Edit",
    "fileedit": "Edit",
    "editfile": "Edit",
    "filechange": "Edit",
    "bash": "Bash",
    "commandexecution": "Bash",
    "glob": "Glob",
    "grep": "Grep",
    "webfetch": "WebFetch",
    "websearch": "WebSearch",
    "notebookedit": "NotebookEdit",
}

_RUNTIME_TOOL_PARAMETER_TEMPLATES: dict[str, tuple[MCPToolParameter, ...]] = {
    tool_name: (
        MCPToolParameter(
            name=parameter_name,
            type=ToolInputType.STRING,
        ),
    )
    for tool_name, parameter_name in _RUNTIME_TOOL_PRIMARY_INPUT_KEYS.items()
}

_SESSION_BUILTIN_TOOL_KEYS = (
    "builtin_tools",
    "builtinTools",
    "available_tools",
    "availableTools",
    "runtime_tools",
    "runtimeTools",
)
_SESSION_ATTACHED_TOOL_KEYS = (
    "attached_tools",
    "attachedTools",
    "mcp_tools",
    "mcpTools",
    "external_tools",
    "externalTools",
)
_SESSION_SERVER_LIST_KEYS = (
    "mcp_servers",
    "mcpServers",
    "attached_mcp_servers",
    "attachedMcpServers",
    "servers",
)
_SESSION_SERVER_TOOL_KEYS = (
    "tools",
    "tool_definitions",
    "toolDefinitions",
    "mcp_tools",
    "mcpTools",
    "attached_tools",
    "attachedTools",
)
_TOOL_CATALOG_SOURCE_BUILTIN = "builtin"
_TOOL_CATALOG_SOURCE_ATTACHED_MCP = "attached_mcp"


# Default timeout for tool execution (30 seconds)
DEFAULT_TOOL_TIMEOUT = 30.0

# Maximum retries for transient failures
MAX_RETRIES = 3

# Retry wait range (exponential backoff)
RETRY_WAIT_MIN = 0.5
RETRY_WAIT_MAX = 5.0


@dataclass(frozen=True, slots=True)
class ToolConflict:
    """Information about a tool name conflict.

    Attributes:
        tool_name: Name of the conflicting tool.
        source: Where the conflict originated (built-in, server name).
        shadowed_by: What is shadowing this tool.
        resolution: How the conflict was resolved.
    """

    tool_name: str
    source: str
    shadowed_by: str
    resolution: str


@dataclass(frozen=True, slots=True)
class MCPToolInfo:
    """Information about an available MCP tool.

    Attributes:
        name: Tool name (possibly prefixed).
        original_name: Original tool name from MCP server.
        server_name: Name of the MCP server providing this tool.
        description: Tool description.
        input_schema: JSON Schema for tool parameters.
    """

    name: str
    original_name: str
    server_name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolCatalogSourceMetadata:
    """Normalized provenance metadata for a session tool-catalog entry."""

    kind: str
    name: str
    original_name: str
    server_name: str | None = None


@dataclass(frozen=True, slots=True)
class SessionToolCatalogEntry:
    """Stable tool-catalog entry with normalized source metadata."""

    stable_id: str
    tool: MCPToolDefinition
    source: ToolCatalogSourceMetadata


@dataclass(frozen=True, slots=True)
class SessionToolCatalog:
    """Deterministic merged tool catalog for a single runtime session."""

    tools: tuple[MCPToolDefinition, ...] = field(default_factory=tuple)
    attached_tools: tuple[MCPToolDefinition, ...] = field(default_factory=tuple)
    entries: tuple[SessionToolCatalogEntry, ...] = field(default_factory=tuple)
    attached_entries: tuple[SessionToolCatalogEntry, ...] = field(default_factory=tuple)
    conflicts: tuple[ToolConflict, ...] = field(default_factory=tuple)


def _infer_tool_input_type(value: Any) -> ToolInputType:
    """Infer an MCP-compatible JSON Schema type from a runtime tool argument."""
    if isinstance(value, bool):
        return ToolInputType.BOOLEAN
    if isinstance(value, int) and not isinstance(value, bool):
        return ToolInputType.INTEGER
    if isinstance(value, float):
        return ToolInputType.NUMBER
    if isinstance(value, list | tuple):
        return ToolInputType.ARRAY
    if isinstance(value, Mapping):
        return ToolInputType.OBJECT
    return ToolInputType.STRING


def _coerce_tool_input_type(value: Any) -> ToolInputType:
    """Coerce a JSON Schema type value into the shared `ToolInputType` enum."""
    if isinstance(value, list | tuple):
        value = next(
            (
                item
                for item in value
                if isinstance(item, str) and item.strip() and item.strip().lower() != "null"
            ),
            "string",
        )

    if isinstance(value, str) and value.strip():
        normalized = value.strip().lower()
        try:
            return ToolInputType(normalized)
        except ValueError:
            return ToolInputType.STRING

    return ToolInputType.STRING


def _normalize_runtime_tool_name(tool_name: str, *, server_name: str | None = None) -> str:
    """Normalize built-in runtime tool names while preserving external MCP names."""
    normalized_name = tool_name.strip()
    if not normalized_name or server_name is not None:
        return normalized_name

    alias_key = "".join(character for character in normalized_name if character.isalnum()).lower()
    return _RUNTIME_TOOL_NAME_ALIASES.get(alias_key, normalized_name)


def _resolve_runtime_tool_description(
    normalized_name: str,
    *,
    server_name: str | None = None,
    description: str | None = None,
    tool_metadata: Mapping[str, Any] | None = None,
) -> str:
    """Resolve a canonical description for built-ins while preserving MCP tool metadata."""
    if server_name is None and normalized_name in _RUNTIME_TOOL_DESCRIPTIONS:
        return _RUNTIME_TOOL_DESCRIPTIONS[normalized_name]

    resolved_description = description or _extract_tool_metadata_description(tool_metadata)
    if isinstance(resolved_description, str) and resolved_description.strip():
        return resolved_description.strip()
    return normalized_name


def _default_runtime_tool_definition(normalized_name: str) -> MCPToolDefinition | None:
    """Return a canonical built-in runtime tool definition when one is known."""
    description = _RUNTIME_TOOL_DESCRIPTIONS.get(normalized_name)
    if description is None:
        return None

    return MCPToolDefinition(
        name=normalized_name,
        description=description,
        parameters=_RUNTIME_TOOL_PARAMETER_TEMPLATES.get(normalized_name, ()),
    )


def enumerate_runtime_builtin_tool_definitions(
    tool_names: Sequence[str | MCPToolDefinition] | None = None,
) -> tuple[MCPToolDefinition, ...]:
    """Enumerate shared built-in runtime tools as canonical MCP definitions."""
    candidates = tool_names if tool_names is not None else tuple(_RUNTIME_TOOL_DESCRIPTIONS)
    seen_names: set[str] = set()
    definitions: list[MCPToolDefinition] = []

    for tool in candidates:
        definition = _normalize_builtin_tool_definition(tool)
        if definition is None or definition.name in seen_names:
            continue
        seen_names.add(definition.name)
        definitions.append(definition)

    return tuple(definitions)


def _parameters_from_input_schema(input_schema: Mapping[str, Any]) -> tuple[MCPToolParameter, ...]:
    """Convert JSON Schema input metadata into `MCPToolParameter` entries."""
    properties = input_schema.get("properties", {})
    if not isinstance(properties, Mapping):
        return ()

    raw_required = input_schema.get("required", ())
    required: set[str] = set()
    if isinstance(raw_required, Sequence) and not isinstance(raw_required, str):
        required = {str(name) for name in raw_required if str(name).strip()}

    parameters: list[MCPToolParameter] = []
    for name, prop in properties.items():
        parameter_name = str(name).strip()
        if not parameter_name:
            continue

        parameter_schema = prop if isinstance(prop, Mapping) else {}
        enum_values = parameter_schema.get("enum")
        parameters.append(
            MCPToolParameter(
                name=parameter_name,
                type=_coerce_tool_input_type(parameter_schema.get("type", "string")),
                description=str(parameter_schema.get("description", "") or ""),
                required=parameter_name in required,
                default=parameter_schema.get("default"),
                enum=(
                    tuple(str(value) for value in enum_values)
                    if isinstance(enum_values, Sequence) and not isinstance(enum_values, str)
                    else None
                ),
            )
        )

    return tuple(parameters)


def _extract_tool_metadata_schema(
    tool_metadata: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    """Extract a JSON Schema object from runtime-emitted tool metadata."""
    if not isinstance(tool_metadata, Mapping):
        return None

    for key in ("input_schema", "inputSchema", "schema"):
        value = tool_metadata.get(key)
        if isinstance(value, Mapping):
            return value

    for key in ("tool", "tool_definition", "tool_metadata", "definition"):
        nested = tool_metadata.get(key)
        if not isinstance(nested, Mapping):
            continue
        nested_schema = _extract_tool_metadata_schema(nested)
        if nested_schema is not None:
            return nested_schema

    return None


def _extract_tool_metadata_description(tool_metadata: Mapping[str, Any] | None) -> str | None:
    """Extract a description from runtime-emitted tool metadata."""
    if not isinstance(tool_metadata, Mapping):
        return None

    description = tool_metadata.get("description")
    if isinstance(description, str) and description.strip():
        return description.strip()

    for key in ("tool", "tool_definition", "tool_metadata", "definition"):
        nested = tool_metadata.get(key)
        if not isinstance(nested, Mapping):
            continue
        nested_description = _extract_tool_metadata_description(nested)
        if nested_description:
            return nested_description

    return None


def _extract_tool_metadata_server_name(tool_metadata: Mapping[str, Any] | None) -> str | None:
    """Extract an MCP server name from runtime-emitted tool metadata."""
    if not isinstance(tool_metadata, Mapping):
        return None

    for key in ("server_name", "tool_server", "provider"):
        value = tool_metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    server = tool_metadata.get("server")
    if isinstance(server, Mapping):
        for key in ("name", "id", "server_name"):
            value = server.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    for key in ("tool", "tool_definition", "tool_metadata", "definition"):
        nested = tool_metadata.get(key)
        if not isinstance(nested, Mapping):
            continue
        nested_server_name = _extract_tool_metadata_server_name(nested)
        if nested_server_name:
            return nested_server_name

    return None


def normalize_runtime_tool_definition(
    tool_name: str,
    tool_input: Mapping[str, Any] | None = None,
    *,
    server_name: str | None = None,
    description: str | None = None,
    tool_metadata: Mapping[str, Any] | None = None,
    input_schema: Mapping[str, Any] | None = None,
) -> MCPToolDefinition:
    """Normalize a runtime-observed tool call into an `MCPToolDefinition`."""
    normalized_name = _normalize_runtime_tool_name(tool_name, server_name=server_name)
    normalized_input = tool_input if isinstance(tool_input, Mapping) else {}
    resolved_server_name = server_name or _extract_tool_metadata_server_name(tool_metadata)
    normalized_input_schema = (
        input_schema
        if isinstance(input_schema, Mapping)
        else _extract_tool_metadata_schema(tool_metadata)
    )
    if isinstance(normalized_input_schema, Mapping):
        parameters = _parameters_from_input_schema(normalized_input_schema)
    elif (
        resolved_server_name is None
        and (default_definition := _default_runtime_tool_definition(normalized_name)) is not None
    ):
        parameters = default_definition.parameters + tuple(
            MCPToolParameter(
                name=str(key),
                type=_infer_tool_input_type(value),
                required=value is not None,
            )
            for key, value in normalized_input.items()
            if str(key).strip()
            and str(key) not in {parameter.name for parameter in default_definition.parameters}
        )
    else:
        parameters = tuple(
            MCPToolParameter(
                name=str(key),
                type=_infer_tool_input_type(value),
                required=value is not None,
            )
            for key, value in normalized_input.items()
            if str(key).strip()
        )
    resolved_description = _resolve_runtime_tool_description(
        normalized_name,
        server_name=resolved_server_name,
        description=description,
        tool_metadata=tool_metadata,
    )
    return MCPToolDefinition(
        name=normalized_name,
        description=resolved_description,
        parameters=parameters,
        server_name=resolved_server_name,
    )


def _normalize_builtin_tool_definition(tool: str | MCPToolDefinition) -> MCPToolDefinition | None:
    """Coerce a built-in tool entry into a normalized definition."""
    if isinstance(tool, MCPToolDefinition):
        name = tool.name.strip()
        if not name:
            return None
        normalized_name = _normalize_runtime_tool_name(name)
        default_definition = _default_runtime_tool_definition(normalized_name)
        normalized_description = _resolve_runtime_tool_description(
            normalized_name,
            description=tool.description,
        )
        normalized_parameters = (
            tool.parameters
            if tool.parameters
            else default_definition.parameters
            if default_definition is not None
            else ()
        )
        if (
            normalized_name == tool.name
            and normalized_description == tool.description
            and normalized_parameters == tool.parameters
        ):
            return tool
        return replace(
            tool,
            name=normalized_name,
            description=normalized_description,
            parameters=normalized_parameters,
        )

    if not isinstance(tool, str):
        return None

    name = tool.strip()
    if not name:
        return None
    return normalize_runtime_tool_definition(name)


def _normalize_attached_tool_definition(
    tool: MCPToolDefinition,
    *,
    tool_prefix: str = "",
) -> MCPToolDefinition | None:
    """Normalize an attached MCP definition into session-catalog form."""
    normalized_name = f"{tool_prefix}{tool.name}".strip()
    if not normalized_name:
        return None
    if normalized_name == tool.name:
        return tool
    return replace(tool, name=normalized_name)


def _resolve_tool_catalog_source_name(
    *,
    source_kind: str,
    server_name: str | None = None,
) -> str:
    """Return a stable source label for serialized catalog entries."""
    if source_kind == _TOOL_CATALOG_SOURCE_BUILTIN:
        return "built-in"
    return server_name or "unknown"


def _build_tool_catalog_source_metadata(
    *,
    source_kind: str,
    original_name: str,
    server_name: str | None = None,
) -> ToolCatalogSourceMetadata:
    """Create normalized source metadata for a catalog entry."""
    normalized_original_name = original_name.strip()
    return ToolCatalogSourceMetadata(
        kind=source_kind,
        name=_resolve_tool_catalog_source_name(
            source_kind=source_kind,
            server_name=server_name,
        ),
        original_name=normalized_original_name,
        server_name=server_name,
    )


def _build_tool_catalog_entry_stable_id(
    tool: MCPToolDefinition,
    *,
    source: ToolCatalogSourceMetadata,
) -> str:
    """Return a deterministic identifier for a catalog entry."""
    if source.kind == _TOOL_CATALOG_SOURCE_BUILTIN:
        return f"builtin:{tool.name}"
    source_name = source.server_name or source.name
    return f"mcp:{source_name}:{tool.name}"


def _build_session_tool_catalog_entry(
    tool: MCPToolDefinition,
    *,
    source: ToolCatalogSourceMetadata,
) -> SessionToolCatalogEntry:
    """Bind a normalized tool definition to stable catalog metadata."""
    return SessionToolCatalogEntry(
        stable_id=_build_tool_catalog_entry_stable_id(tool, source=source),
        tool=tool,
        source=source,
    )


def _normalize_builtin_tool_catalog_entry(
    tool: str | MCPToolDefinition,
) -> tuple[MCPToolDefinition, ToolCatalogSourceMetadata] | None:
    """Normalize a built-in tool and capture its source metadata."""
    original_name = tool.name if isinstance(tool, MCPToolDefinition) else tool
    if not isinstance(original_name, str) or not original_name.strip():
        return None

    definition = _normalize_builtin_tool_definition(tool)
    if definition is None:
        return None

    return (
        definition,
        _build_tool_catalog_source_metadata(
            source_kind=_TOOL_CATALOG_SOURCE_BUILTIN,
            original_name=original_name,
        ),
    )


def _normalize_attached_tool_catalog_entry(
    tool: MCPToolDefinition,
    *,
    tool_prefix: str = "",
) -> tuple[MCPToolDefinition, ToolCatalogSourceMetadata] | None:
    """Normalize an attached MCP tool and capture its source metadata."""
    if not tool.name.strip():
        return None

    definition = _normalize_attached_tool_definition(tool, tool_prefix=tool_prefix)
    if definition is None:
        return None

    return (
        definition,
        _build_tool_catalog_source_metadata(
            source_kind=_TOOL_CATALOG_SOURCE_ATTACHED_MCP,
            original_name=tool.name,
            server_name=definition.server_name or tool.server_name,
        ),
    )


def assemble_session_tool_catalog(
    builtin_tools: Sequence[str | MCPToolDefinition] | None = None,
    attached_tools: Sequence[MCPToolDefinition] | None = None,
    *,
    tool_prefix: str = "",
) -> SessionToolCatalog:
    """Merge built-in and attached tools into a deterministic session catalog."""
    catalog: list[MCPToolDefinition] = []
    selected_attached_tools: list[MCPToolDefinition] = []
    catalog_entries: list[SessionToolCatalogEntry] = []
    selected_attached_entries: list[SessionToolCatalogEntry] = []
    conflicts: list[ToolConflict] = []
    selected_sources: dict[str, str] = {}

    for builtin_tool in builtin_tools or ():
        normalized_entry = _normalize_builtin_tool_catalog_entry(builtin_tool)
        if normalized_entry is None:
            continue
        definition, source = normalized_entry

        if definition.name in selected_sources:
            conflicts.append(
                ToolConflict(
                    tool_name=definition.name,
                    source="built-in",
                    shadowed_by=selected_sources[definition.name],
                    resolution="Later built-in tool skipped",
                )
            )
            continue

        catalog.append(definition)
        catalog_entries.append(_build_session_tool_catalog_entry(definition, source=source))
        selected_sources[definition.name] = "built-in"

    normalized_attached = [
        normalized
        for tool in attached_tools or ()
        if (
            normalized := _normalize_attached_tool_catalog_entry(
                tool,
                tool_prefix=tool_prefix,
            )
        )
        is not None
    ]
    normalized_attached.sort(
        key=lambda normalized_entry: (
            normalized_entry[0].name.casefold(),
            (normalized_entry[0].server_name or "").casefold(),
            normalized_entry[0].description.casefold(),
            normalized_entry[1].original_name.casefold(),
        )
    )

    for definition, source_metadata in normalized_attached:
        source = definition.server_name or "unknown"
        if definition.name in selected_sources:
            shadowed_by = selected_sources[definition.name]
            resolution = (
                "MCP tool skipped" if shadowed_by == "built-in" else "Later server's tool skipped"
            )
            conflicts.append(
                ToolConflict(
                    tool_name=definition.name,
                    source=source,
                    shadowed_by=shadowed_by,
                    resolution=resolution,
                )
            )
            continue

        catalog.append(definition)
        selected_attached_tools.append(definition)
        entry = _build_session_tool_catalog_entry(definition, source=source_metadata)
        catalog_entries.append(entry)
        selected_attached_entries.append(entry)
        selected_sources[definition.name] = source

    return SessionToolCatalog(
        tools=tuple(catalog),
        attached_tools=tuple(selected_attached_tools),
        entries=tuple(catalog_entries),
        attached_entries=tuple(selected_attached_entries),
        conflicts=tuple(conflicts),
    )


def _sequence_items(value: object) -> tuple[Any, ...]:
    """Return a tuple of sequence items when the value is list-like."""
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return tuple(value)
    return ()


def _looks_like_session_tool_definition(value: object) -> bool:
    """Return True when a mapping already resembles a single tool definition."""
    if not isinstance(value, Mapping):
        return False

    return (
        _extract_session_tool_name(value) is not None
        or _extract_tool_metadata_description(value) is not None
        or _extract_tool_metadata_schema(value) is not None
        or _extract_tool_metadata_server_name(value) is not None
    )


def _session_tool_entries(value: object) -> tuple[object, ...]:
    """Return tool entries from either sequence or keyed-mapping catalog shapes."""
    sequence_items = _sequence_items(value)
    if sequence_items:
        return sequence_items

    if not isinstance(value, Mapping):
        return ()

    if _looks_like_session_tool_definition(value):
        return (value,)

    entries: list[object] = []
    for raw_name, raw_value in value.items():
        tool_name = str(raw_name).strip()
        if not tool_name:
            continue

        if isinstance(raw_value, Mapping):
            if _extract_session_tool_name(raw_value):
                entries.append(raw_value)
                continue

            named_tool = dict(raw_value)
            named_tool["name"] = tool_name
            entries.append(named_tool)
            continue

        if isinstance(raw_value, str):
            entries.append(
                {
                    "name": tool_name,
                    "description": raw_value.strip(),
                }
            )
            continue

        entries.append({"name": tool_name})

    return tuple(entries)


def _extract_session_tool_name(tool: object) -> str | None:
    """Extract a tool name from a session-catalog payload entry."""
    if isinstance(tool, MCPToolDefinition):
        return tool.name.strip() or None

    if isinstance(tool, str):
        normalized = tool.strip()
        return normalized or None

    if not isinstance(tool, Mapping):
        return None

    for key in ("name", "tool_name"):
        value = tool.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    for key in ("tool", "tool_definition", "definition", "tool_metadata"):
        nested = tool.get(key)
        tool_name = _extract_session_tool_name(nested)
        if tool_name:
            return tool_name

    return None


def _extract_session_server_name(server: Mapping[str, Any]) -> str | None:
    """Extract an MCP server name from a session-catalog server container."""
    for key in ("server_name", "name", "id"):
        value = server.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    nested_server = server.get("server")
    if isinstance(nested_server, Mapping):
        for key in ("server_name", "name", "id"):
            value = nested_server.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    return None


def _looks_like_session_server_container(value: object) -> bool:
    """Return True when a mapping already resembles a single server container."""
    if not isinstance(value, Mapping):
        return False

    return _extract_session_server_name(value) is not None or any(
        _session_tool_entries(value.get(key)) for key in _SESSION_SERVER_TOOL_KEYS
    )


def _session_server_entries(value: object) -> tuple[Mapping[str, Any], ...]:
    """Return server entries from either sequence or keyed-mapping catalog shapes."""
    sequence_items = _sequence_items(value)
    if sequence_items:
        return tuple(entry for entry in sequence_items if isinstance(entry, Mapping))

    if not isinstance(value, Mapping):
        return ()

    if _looks_like_session_server_container(value):
        return (value,)

    entries: list[Mapping[str, Any]] = []
    for raw_name, raw_value in value.items():
        server_name = str(raw_name).strip()
        if not server_name:
            continue

        if isinstance(raw_value, Mapping):
            if _looks_like_session_server_container(raw_value):
                named_server = dict(raw_value)
                if _extract_session_server_name(named_server) is None:
                    named_server["name"] = server_name
                entries.append(named_server)
                continue

            entries.append({"name": server_name, "tools": raw_value})
            continue

        tool_entries = _sequence_items(raw_value)
        if tool_entries:
            entries.append({"name": server_name, "tools": list(tool_entries)})

    return tuple(entries)


def _is_session_server_container(value: object) -> bool:
    """Return True when a session payload entry represents an MCP server container."""
    if not isinstance(value, Mapping):
        return False

    if _extract_session_server_name(value) is None:
        return False

    return any(_session_tool_entries(value.get(key)) for key in _SESSION_SERVER_TOOL_KEYS)


def _normalize_session_catalog_tool_definition(
    tool: str | MCPToolDefinition | Mapping[str, Any],
    *,
    inherited_server_name: str | None = None,
) -> MCPToolDefinition | None:
    """Normalize a session-catalog entry into a shared `MCPToolDefinition`."""
    if isinstance(tool, MCPToolDefinition):
        if inherited_server_name and not tool.server_name:
            return replace(tool, server_name=inherited_server_name)
        return tool

    if isinstance(tool, str):
        normalized_name = tool.strip()
        if not normalized_name:
            return None
        return normalize_runtime_tool_definition(
            normalized_name,
            server_name=inherited_server_name,
        )

    if not isinstance(tool, Mapping):
        return None

    tool_name = _extract_session_tool_name(tool)
    if not tool_name:
        return None

    resolved_server_name = _extract_tool_metadata_server_name(tool) or inherited_server_name
    tool_metadata = dict(tool)
    if resolved_server_name and _extract_tool_metadata_server_name(tool) is None:
        tool_metadata["server_name"] = resolved_server_name

    return normalize_runtime_tool_definition(
        tool_name,
        server_name=resolved_server_name,
        description=_extract_tool_metadata_description(tool),
        tool_metadata=tool_metadata,
        input_schema=_extract_tool_metadata_schema(tool),
    )


def _tool_identity(tool: MCPToolDefinition) -> tuple[str, str, str, tuple[MCPToolParameter, ...]]:
    """Return a stable identity key for deduplicating normalized tool definitions."""
    return (
        tool.name,
        tool.server_name or "",
        tool.description,
        tool.parameters,
    )


def _append_unique_tool(
    target: list[MCPToolDefinition],
    seen: set[tuple[str, str, str, tuple[MCPToolParameter, ...]]],
    tool: MCPToolDefinition | None,
) -> None:
    """Append a tool definition only when an identical definition has not been seen."""
    if tool is None:
        return

    identity = _tool_identity(tool)
    if identity in seen:
        return
    seen.add(identity)
    target.append(tool)


def _collect_session_server_tools(
    entries: Sequence[object],
    *,
    attached_tools: list[MCPToolDefinition],
    attached_seen: set[tuple[str, str, str, tuple[MCPToolParameter, ...]]],
) -> None:
    """Collect attached MCP tool definitions from server-container payloads."""
    for entry in _session_server_entries(entries):
        server_name = _extract_session_server_name(entry)
        for key in _SESSION_SERVER_TOOL_KEYS:
            for tool in _session_tool_entries(entry.get(key)):
                _append_unique_tool(
                    attached_tools,
                    attached_seen,
                    _normalize_session_catalog_tool_definition(
                        tool,
                        inherited_server_name=server_name,
                    ),
                )


def _collect_session_tool_entries(
    entries: Sequence[object],
    *,
    builtin_tools: list[MCPToolDefinition],
    attached_tools: list[MCPToolDefinition],
    builtin_seen: set[tuple[str, str, str, tuple[MCPToolParameter, ...]]],
    attached_seen: set[tuple[str, str, str, tuple[MCPToolParameter, ...]]],
    default_attached: bool,
) -> None:
    """Collect tool definitions from a mixed session payload list."""
    for entry in entries:
        if _is_session_server_container(entry):
            _collect_session_server_tools(
                [entry],
                attached_tools=attached_tools,
                attached_seen=attached_seen,
            )
            continue

        definition = _normalize_session_catalog_tool_definition(entry)
        if definition is None:
            continue

        if default_attached or definition.server_name is not None:
            _append_unique_tool(attached_tools, attached_seen, definition)
            continue

        _append_unique_tool(builtin_tools, builtin_seen, definition)


def _iter_session_catalog_sources(
    payload: Mapping[str, Any],
) -> tuple[Mapping[str, Any], ...]:
    """Return the nested mappings that may expose OpenCode session tool catalogs."""
    candidates: list[Mapping[str, Any]] = [payload]

    session = payload.get("session")
    if isinstance(session, Mapping):
        candidates.append(session)

    mcp = payload.get("mcp")
    if isinstance(mcp, Mapping):
        candidates.append(mcp)

    if isinstance(session, Mapping):
        session_mcp = session.get("mcp")
        if isinstance(session_mcp, Mapping):
            candidates.append(session_mcp)

    seen_ids: set[int] = set()
    unique_candidates: list[Mapping[str, Any]] = []
    for candidate in candidates:
        candidate_id = id(candidate)
        if candidate_id in seen_ids:
            continue
        seen_ids.add(candidate_id)
        unique_candidates.append(candidate)

    return tuple(unique_candidates)


def normalize_opencode_session_tool_catalog(
    payload: Mapping[str, Any],
    *,
    tool_prefix: str = "",
) -> SessionToolCatalog | None:
    """Normalize an OpenCode session payload into a deterministic tool catalog."""
    builtin_tools: list[MCPToolDefinition] = []
    attached_tools: list[MCPToolDefinition] = []
    builtin_seen: set[tuple[str, str, str, tuple[MCPToolParameter, ...]]] = set()
    attached_seen: set[tuple[str, str, str, tuple[MCPToolParameter, ...]]] = set()
    found_catalog_data = False

    for source in _iter_session_catalog_sources(payload):
        for key in _SESSION_BUILTIN_TOOL_KEYS:
            entries = _session_tool_entries(source.get(key))
            if not entries:
                continue
            found_catalog_data = True
            _collect_session_tool_entries(
                entries,
                builtin_tools=builtin_tools,
                attached_tools=attached_tools,
                builtin_seen=builtin_seen,
                attached_seen=attached_seen,
                default_attached=False,
            )

        mixed_entries = _session_tool_entries(source.get("tools"))
        if mixed_entries:
            found_catalog_data = True
            _collect_session_tool_entries(
                mixed_entries,
                builtin_tools=builtin_tools,
                attached_tools=attached_tools,
                builtin_seen=builtin_seen,
                attached_seen=attached_seen,
                default_attached=False,
            )

        for key in _SESSION_ATTACHED_TOOL_KEYS:
            entries = _session_tool_entries(source.get(key))
            if not entries:
                continue
            found_catalog_data = True
            _collect_session_tool_entries(
                entries,
                builtin_tools=builtin_tools,
                attached_tools=attached_tools,
                builtin_seen=builtin_seen,
                attached_seen=attached_seen,
                default_attached=True,
            )

        for key in _SESSION_SERVER_LIST_KEYS:
            server_entries = _session_server_entries(source.get(key))
            if not server_entries:
                continue
            found_catalog_data = True
            _collect_session_server_tools(
                server_entries,
                attached_tools=attached_tools,
                attached_seen=attached_seen,
            )

    if not found_catalog_data:
        return None

    return assemble_session_tool_catalog(
        builtin_tools=builtin_tools,
        attached_tools=attached_tools,
        tool_prefix=tool_prefix,
    )


def _extract_serialized_tool_source_metadata(
    serialized_tool: Mapping[str, Any],
    *,
    definition: MCPToolDefinition,
) -> ToolCatalogSourceMetadata:
    """Reconstruct source metadata from a serialized tool-catalog entry."""
    nested_source = serialized_tool.get("source")
    source_mapping = nested_source if isinstance(nested_source, Mapping) else {}

    source_kind_value = source_mapping.get("kind", serialized_tool.get("source_kind"))
    source_kind = (
        str(source_kind_value).strip()
        if isinstance(source_kind_value, str) and str(source_kind_value).strip()
        else ""
    )
    if source_kind not in {
        _TOOL_CATALOG_SOURCE_BUILTIN,
        _TOOL_CATALOG_SOURCE_ATTACHED_MCP,
    }:
        source_kind = (
            _TOOL_CATALOG_SOURCE_ATTACHED_MCP
            if definition.server_name
            else _TOOL_CATALOG_SOURCE_BUILTIN
        )

    server_name_value = source_mapping.get(
        "server_name",
        serialized_tool.get("server_name"),
    )
    server_name = (
        str(server_name_value).strip()
        if isinstance(server_name_value, str) and str(server_name_value).strip()
        else None
    )
    if source_kind == _TOOL_CATALOG_SOURCE_BUILTIN:
        server_name = None

    source_name_value = source_mapping.get("name", serialized_tool.get("source_name"))
    source_name = (
        str(source_name_value).strip()
        if isinstance(source_name_value, str) and str(source_name_value).strip()
        else _resolve_tool_catalog_source_name(
            source_kind=source_kind,
            server_name=server_name,
        )
    )

    original_name_value = source_mapping.get(
        "original_name",
        serialized_tool.get("original_name"),
    )
    original_name = (
        str(original_name_value).strip()
        if isinstance(original_name_value, str) and str(original_name_value).strip()
        else definition.name
    )

    return ToolCatalogSourceMetadata(
        kind=source_kind,
        name=source_name,
        original_name=original_name,
        server_name=server_name,
    )


def _restore_tool_definition_for_catalog_source(
    tool: MCPToolDefinition,
    *,
    source: ToolCatalogSourceMetadata,
) -> MCPToolDefinition:
    """Restore a normalized tool definition to the raw source-facing catalog shape."""
    restored_server_name = (
        source.server_name if source.kind == _TOOL_CATALOG_SOURCE_ATTACHED_MCP else None
    )
    if tool.name == source.original_name and tool.server_name == restored_server_name:
        return tool
    return replace(tool, name=source.original_name, server_name=restored_server_name)


def normalize_serialized_tool_catalog(
    tool_catalog: Sequence[Mapping[str, Any]] | None,
    *,
    tool_prefix: str = "",
) -> SessionToolCatalog | None:
    """Rehydrate a serialized startup/session catalog into `SessionToolCatalog`."""
    if not tool_catalog:
        return None

    builtin_tools: list[MCPToolDefinition] = []
    attached_tools: list[MCPToolDefinition] = []
    for entry in tool_catalog:
        definition = _normalize_session_catalog_tool_definition(entry)
        if definition is None:
            continue

        source_metadata = _extract_serialized_tool_source_metadata(entry, definition=definition)
        restored_definition = _restore_tool_definition_for_catalog_source(
            definition,
            source=source_metadata,
        )
        if source_metadata.kind == _TOOL_CATALOG_SOURCE_ATTACHED_MCP:
            attached_tools.append(restored_definition)
        else:
            builtin_tools.append(restored_definition)

    if not builtin_tools and not attached_tools:
        return None

    return assemble_session_tool_catalog(
        builtin_tools=builtin_tools,
        attached_tools=attached_tools,
        tool_prefix=tool_prefix,
    )


def _builtin_tools_from_catalog(catalog: SessionToolCatalog) -> tuple[MCPToolDefinition, ...]:
    """Return only the builtin tools from a merged session catalog."""
    return tuple(
        _restore_tool_definition_for_catalog_source(entry.tool, source=entry.source)
        for entry in catalog.entries
        if entry.source.kind == _TOOL_CATALOG_SOURCE_BUILTIN
    )


def _attached_tools_from_catalog(catalog: SessionToolCatalog) -> tuple[MCPToolDefinition, ...]:
    """Return attached MCP tools with their original catalog metadata restored."""
    return tuple(
        _restore_tool_definition_for_catalog_source(entry.tool, source=entry.source)
        for entry in catalog.attached_entries
    )


def merge_session_tool_catalogs(
    *catalogs: SessionToolCatalog | None,
    tool_prefix: str = "",
) -> SessionToolCatalog | None:
    """Merge multiple session catalogs while preserving builtin/attached ownership."""
    present_catalogs = tuple(catalog for catalog in catalogs if catalog is not None)
    if not present_catalogs:
        return None

    builtin_tools: list[MCPToolDefinition] = []
    attached_tools: list[MCPToolDefinition] = []
    for catalog in present_catalogs:
        builtin_tools.extend(_builtin_tools_from_catalog(catalog))
        attached_tools.extend(_attached_tools_from_catalog(catalog))

    return assemble_session_tool_catalog(
        builtin_tools=builtin_tools,
        attached_tools=attached_tools,
        tool_prefix=tool_prefix,
    )


def normalize_runtime_tool_result(
    content: str,
    *,
    is_error: bool = False,
    meta: Mapping[str, Any] | None = None,
) -> MCPToolResult:
    """Normalize runtime tool output into the shared `MCPToolResult` abstraction."""
    text = content.strip()
    result_content: tuple[MCPContentItem, ...] = ()
    if text:
        result_content = (MCPContentItem(type=ContentType.TEXT, text=text),)
    return MCPToolResult(
        content=result_content,
        is_error=is_error,
        meta=dict(meta or {}),
    )


def _extract_runtime_text(value: object) -> str:
    """Extract readable text from nested runtime payloads."""
    if isinstance(value, str):
        return value.strip()

    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        parts = [_extract_runtime_text(item) for item in value]
        return "\n".join(part for part in parts if part)

    if isinstance(value, Mapping):
        preferred_keys = (
            "content",
            "delta",
            "text",
            "message",
            "summary",
            "output",
            "output_text",
            "stdout",
            "stderr",
            "reasoning",
            "result",
            "error",
            "details",
        )
        dict_parts: list[str] = []
        for key in preferred_keys:
            if key in value:
                text = _extract_runtime_text(value[key])
                if text:
                    dict_parts.append(text)
        if dict_parts:
            return "\n".join(dict_parts)

        fallback = [_extract_runtime_text(item) for item in value.values()]
        return "\n".join(part for part in fallback if part)

    return ""


def _extract_nested_mapping(source: Mapping[str, Any], key: str) -> Mapping[str, Any] | None:
    """Return a nested mapping when present."""
    value = source.get(key)
    return value if isinstance(value, Mapping) else None


def _extract_nested_value(source: Mapping[str, Any], *path: str) -> object:
    """Extract a nested value from a mapping path when present."""
    value: object = source
    for key in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def _normalize_json_safe_value(value: object) -> Any:
    """Normalize runtime payload fragments into JSON-safe metadata values."""
    if value is None or isinstance(value, str | int | float | bool):
        return value

    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, nested_value in value.items():
            normalized_key = str(key).strip()
            if not normalized_key:
                continue
            normalized[normalized_key] = _normalize_json_safe_value(nested_value)
        return normalized

    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_normalize_json_safe_value(item) for item in value]

    return str(value)


def _extract_opencode_tool_name(raw_event: Mapping[str, Any]) -> str | None:
    """Extract the runtime-reported tool name from an OpenCode result event."""
    for key in ("tool_name", "tool", "name"):
        value = raw_event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, Mapping):
            nested_name = _extract_opencode_tool_name(value)
            if nested_name:
                return nested_name

    for key in ("tool_definition", "tool_metadata", "definition"):
        nested_value = raw_event.get(key)
        if not isinstance(nested_value, Mapping):
            continue
        nested_name = _extract_opencode_tool_name(nested_value)
        if nested_name:
            return nested_name

    event_type = str(raw_event.get("type", "") or "").strip().lower()
    if event_type == "command_execution":
        return "command_execution"
    if event_type == "file_change":
        return "file_change"
    if event_type == "web_search":
        return "web_search"

    command = raw_event.get("command")
    if isinstance(command, str) and command.strip():
        return "Bash"

    for key in ("path", "file_path", "target_file"):
        value = raw_event.get(key)
        if isinstance(value, str) and value.strip():
            return "Edit"

    return None


def _extract_opencode_tool_input(raw_event: Mapping[str, Any]) -> Mapping[str, Any]:
    """Extract normalized tool input from an OpenCode result event."""
    for key in ("input", "arguments", "args", "params"):
        value = raw_event.get(key)
        if isinstance(value, Mapping):
            return value

    command = raw_event.get("command")
    if isinstance(command, str) and command.strip():
        return {"command": command.strip()}

    path = raw_event.get("path")
    if isinstance(path, str) and path.strip():
        return {"file_path": path.strip()}

    for key in ("file_path", "target_file"):
        value = raw_event.get(key)
        if isinstance(value, str) and value.strip():
            return {"file_path": value.strip()}

    tool_name = _extract_opencode_tool_name(raw_event)
    if tool_name == "web_search":
        query = _extract_runtime_text(raw_event)
        if query:
            return {"query": query}

    return {}


def _normalize_opencode_tool_definition(
    raw_tool_name: str,
    tool_input: Mapping[str, Any],
    *,
    server_name: str | None,
    raw_event: Mapping[str, Any],
) -> MCPToolDefinition:
    """Normalize OpenCode tool identity while preserving host-runtime server labels."""
    normalized_server_name = server_name.strip().lower() if isinstance(server_name, str) else None
    host_runtime_server_names = {
        "workspace",
        "runtime",
        "local",
        "opencode",
        "builtin",
        "built-in",
    }
    tool_definition = normalize_runtime_tool_definition(
        raw_tool_name,
        tool_input,
        server_name=None if normalized_server_name in host_runtime_server_names else server_name,
        description=_extract_tool_metadata_description(raw_event),
        tool_metadata=raw_event,
        input_schema=_extract_tool_metadata_schema(raw_event),
    )
    if server_name and tool_definition.server_name != server_name:
        return replace(tool_definition, server_name=server_name)
    return tool_definition


def _extract_opencode_tool_call_id(raw_event: Mapping[str, Any]) -> str | None:
    """Extract an OpenCode tool-call identifier when present."""
    candidate_paths = (
        ("tool_call_id",),
        ("toolCallId",),
        ("call_id",),
        ("callId",),
        ("result", "tool_call_id"),
        ("result", "toolCallId"),
        ("output", "tool_call_id"),
        ("output", "toolCallId"),
        ("error", "tool_call_id"),
        ("error", "toolCallId"),
    )

    for path in candidate_paths:
        value = _extract_nested_value(raw_event, *path)
        if isinstance(value, str) and value.strip():
            return value.strip()

    return None


def _extract_opencode_tool_duration_ms(raw_event: Mapping[str, Any]) -> int | float | str | None:
    """Extract a normalized tool duration value when present."""
    candidate_paths = (
        ("duration_ms",),
        ("durationMs",),
        ("duration",),
        ("result", "duration_ms"),
        ("result", "durationMs"),
        ("output", "duration_ms"),
        ("output", "durationMs"),
    )

    for path in candidate_paths:
        value = _extract_nested_value(raw_event, *path)
        if isinstance(value, bool):
            continue
        if isinstance(value, int | float):
            return value
        if isinstance(value, str) and value.strip():
            normalized = value.strip()
            if normalized.lstrip("-").isdigit():
                return int(normalized)
            return normalized

    return None


def _extract_opencode_tool_exit_status(raw_event: Mapping[str, Any]) -> int | str | None:
    """Extract a normalized exit-status value from an OpenCode tool payload."""
    candidate_paths = (
        ("exit_status",),
        ("exitStatus",),
        ("exit_code",),
        ("exitCode",),
        ("returncode",),
        ("return_code",),
        ("status_code",),
        ("statusCode",),
        ("result", "exit_status"),
        ("result", "exit_code"),
        ("output", "exit_status"),
        ("output", "exit_code"),
        ("error", "exit_status"),
        ("error", "exit_code"),
    )

    for path in candidate_paths:
        value: object = raw_event
        for key in path:
            if not isinstance(value, Mapping):
                value = None
                break
            value = value.get(key)

        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            normalized = value.strip()
            if not normalized:
                continue
            if normalized.lstrip("-").isdigit():
                return int(normalized)
            return normalized

    return None


def _extract_opencode_tool_error_details(
    raw_event: Mapping[str, Any],
) -> tuple[str | None, str | None, str | None]:
    """Extract normalized tool error details from an OpenCode payload."""
    error = _extract_nested_mapping(raw_event, "error")
    message: str | None = None
    error_type: str | None = None
    error_code: str | None = None

    if error is not None:
        for key in ("message", "text", "details", "summary"):
            value = error.get(key)
            if isinstance(value, str) and value.strip():
                message = value.strip()
                break
        for key in ("type", "name"):
            value = error.get(key)
            if isinstance(value, str) and value.strip():
                error_type = value.strip()
                break
        for key in ("code", "exit_code", "exit_status"):
            value = error.get(key)
            if isinstance(value, str | int) and str(value).strip():
                error_code = str(value).strip()
                break

    if message is None:
        for key in ("error_message", "message"):
            value = raw_event.get(key)
            if isinstance(value, str) and value.strip():
                message = value.strip()
                break

    if error_type is None:
        value = raw_event.get("error_type")
        if isinstance(value, str) and value.strip():
            error_type = value.strip()

    if error_code is None:
        for key in ("error_code", "code"):
            value = raw_event.get(key)
            if isinstance(value, str | int) and str(value).strip():
                error_code = str(value).strip()
                break

    return message, error_type, error_code


def _extract_opencode_tool_status(
    raw_event: Mapping[str, Any],
    *,
    event_type: str,
    success: bool | None,
    exit_status: int | str | None,
    is_error: bool,
) -> str | None:
    """Extract a normalized status string for an OpenCode tool result."""
    candidate_paths = (
        ("status",),
        ("state",),
        ("result", "status"),
        ("result", "state"),
        ("output", "status"),
        ("output", "state"),
        ("error", "status"),
        ("error", "state"),
    )

    for path in candidate_paths:
        value = _extract_nested_value(raw_event, *path)
        if isinstance(value, str) and value.strip():
            normalized = value.strip().lower()
            if normalized in {"ok", "success", "succeeded"}:
                return "completed"
            if normalized in {"error", "failed", "failure"}:
                return "failed"
            return normalized
        if isinstance(value, bool):
            return "completed" if value else "failed"

    if event_type.endswith(".failed") or is_error:
        return "failed"
    if event_type.endswith(".completed") or event_type == "tool.result":
        return "completed"
    if success is True:
        return "completed"
    if success is False:
        return "failed"
    if isinstance(exit_status, int):
        return "completed" if exit_status == 0 else "failed"

    return None


def _extract_opencode_tool_payload(
    raw_event: Mapping[str, Any],
    key: str,
) -> Any | None:
    """Extract a normalized tool result payload fragment when present."""
    value = raw_event.get(key)
    if value is None:
        return None

    normalized = _normalize_json_safe_value(value)
    if isinstance(normalized, str):
        normalized = normalized.strip()
        return normalized or None
    if isinstance(normalized, Mapping | list) and not normalized:
        return None
    return normalized


def normalize_opencode_tool_result(
    raw_event: Mapping[str, Any],
    *,
    runtime_backend: str = "opencode",
) -> MCPToolResult:
    """Normalize an OpenCode-native tool result into the shared `MCPToolResult` model."""
    event_type = str(raw_event.get("type", "") or "").strip().lower()
    exit_status = _extract_opencode_tool_exit_status(raw_event)
    error_message, error_type, error_code = _extract_opencode_tool_error_details(raw_event)

    success_value = raw_event.get("success")
    success: bool | None = success_value if isinstance(success_value, bool) else None
    if success is None and isinstance(exit_status, int):
        success = exit_status == 0

    raw_tool_name = _extract_opencode_tool_name(raw_event)
    tool_input = _extract_opencode_tool_input(raw_event)
    resolved_server_name = _extract_tool_metadata_server_name(raw_event)
    tool_definition = (
        _normalize_opencode_tool_definition(
            raw_tool_name,
            tool_input,
            server_name=resolved_server_name,
            raw_event=raw_event,
        )
        if raw_tool_name
        else None
    )

    is_error = (
        event_type.endswith(".failed")
        or bool(raw_event.get("is_error"))
        or success is False
        or (isinstance(exit_status, int) and exit_status != 0)
        or error_message is not None
    )

    seen: set[str] = set()
    content_parts: list[str] = []
    for value in (
        raw_event.get("result"),
        raw_event.get("output"),
        raw_event.get("output_text"),
        raw_event.get("summary"),
        raw_event.get("text"),
        raw_event.get("message"),
        raw_event.get("stdout"),
        raw_event.get("stderr"),
        raw_event.get("error"),
    ):
        text = _extract_runtime_text(value)
        if text and text not in seen:
            seen.add(text)
            content_parts.append(text)

    if not content_parts and exit_status is not None:
        content_parts.append(f"Tool exited with status {exit_status}.")

    status = _extract_opencode_tool_status(
        raw_event,
        event_type=event_type,
        success=success,
        exit_status=exit_status,
        is_error=is_error,
    )
    meta: dict[str, Any] = {
        "runtime_backend": runtime_backend,
        "runtime_event_type": event_type,
    }
    for key, value in (
        ("tool_name", tool_definition.name if tool_definition is not None else None),
        ("raw_tool_name", raw_tool_name),
        (
            "tool_definition",
            serialize_tool_definition(tool_definition) if tool_definition is not None else None,
        ),
        ("server_name", resolved_server_name),
        ("tool_call_id", _extract_opencode_tool_call_id(raw_event)),
        ("duration_ms", _extract_opencode_tool_duration_ms(raw_event)),
        ("status", status),
        ("stdout", raw_event.get("stdout")),
        ("stderr", raw_event.get("stderr")),
        ("exit_status", exit_status),
        ("success", success),
        ("result_payload", _extract_opencode_tool_payload(raw_event, "result")),
        ("output_payload", _extract_opencode_tool_payload(raw_event, "output")),
        ("error_payload", _extract_opencode_tool_payload(raw_event, "error")),
        ("error_message", error_message),
        ("error_type", error_type),
        ("error_code", error_code),
    ):
        if value is None:
            continue
        if isinstance(value, str):
            normalized = value.strip()
            if not normalized:
                continue
            meta[key] = normalized
            continue
        meta[key] = value

    return normalize_runtime_tool_result(
        "\n".join(content_parts),
        is_error=is_error,
        meta=meta,
    )


def _default_tool_catalog_source_metadata(
    tool_definition: MCPToolDefinition,
) -> ToolCatalogSourceMetadata:
    """Infer source metadata when only a normalized tool definition is available."""
    source_kind = (
        _TOOL_CATALOG_SOURCE_ATTACHED_MCP
        if tool_definition.server_name
        else _TOOL_CATALOG_SOURCE_BUILTIN
    )
    return _build_tool_catalog_source_metadata(
        source_kind=source_kind,
        original_name=tool_definition.name,
        server_name=tool_definition.server_name,
    )


def serialize_tool_definition(
    tool_definition: MCPToolDefinition,
    *,
    stable_id: str | None = None,
    source: ToolCatalogSourceMetadata | None = None,
) -> dict[str, Any]:
    """Serialize an `MCPToolDefinition` into JSON-safe event payload data."""
    source_metadata = source or _default_tool_catalog_source_metadata(tool_definition)
    serialized_id = stable_id or _build_tool_catalog_entry_stable_id(
        tool_definition,
        source=source_metadata,
    )
    return {
        "id": serialized_id,
        "stable_id": serialized_id,
        "name": tool_definition.name,
        "original_name": source_metadata.original_name,
        "description": tool_definition.description,
        "server_name": tool_definition.server_name,
        "source_kind": source_metadata.kind,
        "source_name": source_metadata.name,
        "source": {
            "kind": source_metadata.kind,
            "name": source_metadata.name,
            "original_name": source_metadata.original_name,
            "server_name": source_metadata.server_name,
        },
        "parameters": [
            {
                "name": parameter.name,
                "type": parameter.type.value,
                "description": parameter.description,
                "required": parameter.required,
                "default": parameter.default,
                "enum": list(parameter.enum) if parameter.enum is not None else None,
            }
            for parameter in tool_definition.parameters
        ],
        "input_schema": tool_definition.to_input_schema(),
    }


def serialize_tool_result(tool_result: MCPToolResult) -> dict[str, Any]:
    """Serialize an `MCPToolResult` into JSON-safe event payload data."""
    return {
        "content": [
            {
                "type": item.type.value,
                "text": item.text,
                "data": item.data,
                "mime_type": item.mime_type,
                "uri": item.uri,
            }
            for item in tool_result.content
        ],
        "text_content": tool_result.text_content,
        "is_error": tool_result.is_error,
        "meta": dict(tool_result.meta),
    }


def serialize_tool_catalog(
    tool_catalog: SessionToolCatalog | Sequence[MCPToolDefinition],
) -> list[dict[str, Any]]:
    """Serialize a startup tool catalog into JSON-safe metadata."""
    if isinstance(tool_catalog, SessionToolCatalog):
        return [
            serialize_tool_definition(
                entry.tool,
                stable_id=entry.stable_id,
                source=entry.source,
            )
            for entry in tool_catalog.entries
        ]
    return [serialize_tool_definition(tool_definition) for tool_definition in tool_catalog]


class MCPToolProvider:
    """Provider for MCP tools to integrate with OrchestratorRunner.

    This class wraps an MCPClientManager and provides:
    - Tool discovery and conversion to agent format
    - Tool execution with timeout handling
    - Retry policy for transient failures
    - Graceful error handling

    All errors are wrapped and returned as results, not raised as exceptions,
    to ensure MCP failures don't crash the orchestrator.

    Example:
        manager = MCPClientManager()
        await manager.add_server(config)
        await manager.connect_all()

        provider = MCPToolProvider(manager)
        tools = await provider.get_tools()

        result = await provider.call_tool("file_read", {"path": "/tmp/test"})
        if result.is_ok:
            print(result.value.text_content)
    """

    def __init__(
        self,
        mcp_manager: MCPClientManager,
        *,
        default_timeout: float = DEFAULT_TOOL_TIMEOUT,
        tool_prefix: str = "",
    ) -> None:
        """Initialize the MCP tool provider.

        Args:
            mcp_manager: MCPClientManager with connected servers.
            default_timeout: Default timeout for tool execution in seconds.
            tool_prefix: Optional prefix to add to all MCP tool names
                        (e.g., "mcp_" to namespace tools).
        """
        self._manager = mcp_manager
        self._default_timeout = default_timeout
        self._tool_prefix = tool_prefix
        self._tool_map: dict[str, MCPToolInfo] = {}
        self._session_catalog = SessionToolCatalog()
        self._conflicts: list[ToolConflict] = []

    @property
    def tool_prefix(self) -> str:
        """Return the tool name prefix."""
        return self._tool_prefix

    @property
    def conflicts(self) -> Sequence[ToolConflict]:
        """Return any tool conflicts detected during tool loading."""
        return tuple(self._conflicts)

    @property
    def session_catalog(self) -> SessionToolCatalog:
        """Return the merged session catalog from the last discovery pass."""
        return self._session_catalog

    async def get_tools(
        self,
        builtin_tools: Sequence[str] | None = None,
    ) -> Sequence[MCPToolInfo]:
        """Get all available MCP tools.

        Discovers tools from all connected MCP servers and converts them
        to the agent tool format. Handles tool name conflicts by:
        - Skipping tools that conflict with built-in tools
        - Using first server's tool when multiple servers provide same name

        Args:
            builtin_tools: List of built-in tool names to avoid conflicts with.

        Returns:
            Sequence of MCPToolInfo for available tools.
        """
        self._tool_map.clear()
        self._conflicts.clear()
        self._session_catalog = SessionToolCatalog()

        try:
            mcp_tools = await self._manager.list_all_tools()
        except Exception as e:
            log.error(
                "orchestrator.mcp_tools.list_failed",
                error=str(e),
            )
            return ()

        self._session_catalog = assemble_session_tool_catalog(
            builtin_tools=builtin_tools,
            attached_tools=mcp_tools,
            tool_prefix=self._tool_prefix,
        )
        self._conflicts = list(self._session_catalog.conflicts)

        for entry in self._session_catalog.attached_entries:
            normalized_tool = entry.tool
            tool_info = MCPToolInfo(
                name=normalized_tool.name,
                original_name=entry.source.original_name,
                server_name=entry.source.server_name or "unknown",
                description=normalized_tool.description,
                input_schema=normalized_tool.to_input_schema(),
            )
            self._tool_map[normalized_tool.name] = tool_info

        for conflict in self._conflicts:
            if conflict.shadowed_by == "built-in":
                log.warning(
                    "orchestrator.mcp_tools.shadowed_by_builtin",
                    tool_name=conflict.tool_name,
                    server=conflict.source,
                )
            else:
                log.warning(
                    "orchestrator.mcp_tools.shadowed_by_server",
                    tool_name=conflict.tool_name,
                    server=conflict.source,
                    shadowed_by=conflict.shadowed_by,
                )

        log.info(
            "orchestrator.mcp_tools.loaded",
            tool_count=len(self._tool_map),
            conflict_count=len(self._conflicts),
            servers=list({t.server_name for t in self._tool_map.values()}),
        )

        return tuple(self._tool_map.values())

    def get_tool_names(self) -> Sequence[str]:
        """Get list of available tool names.

        Returns:
            Sequence of tool names (with prefix if configured).
        """
        return tuple(self._tool_map.keys())

    def has_tool(self, name: str) -> bool:
        """Check if a tool is available.

        Args:
            name: Tool name to check (with prefix if applicable).

        Returns:
            True if tool is available.
        """
        return name in self._tool_map

    def get_tool_info(self, name: str) -> MCPToolInfo | None:
        """Get info for a specific tool.

        Args:
            name: Tool name (with prefix if applicable).

        Returns:
            MCPToolInfo or None if not found.
        """
        return self._tool_map.get(name)

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Result[MCPToolResult, MCPToolError]:
        """Call an MCP tool with the given arguments.

        Handles:
        - Timeout with configurable duration
        - Retry for transient failures (network errors, connection issues)
        - Graceful error handling (returns error result, doesn't raise)

        Args:
            name: Tool name (with prefix if applicable).
            arguments: Tool arguments as a dict.
            timeout: Optional timeout override in seconds.

        Returns:
            Result containing MCPToolResult on success or MCPToolError on failure.
        """
        tool_info = self._tool_map.get(name)
        if not tool_info:
            return Result.err(
                MCPToolError(
                    f"Tool not found: {name}",
                    tool_name=name,
                    is_retriable=False,
                )
            )

        effective_timeout = timeout or self._default_timeout

        log.debug(
            "orchestrator.mcp_tools.call_start",
            tool_name=name,
            server=tool_info.server_name,
            timeout=effective_timeout,
        )

        try:
            # Retry transient failures with bounded backoff
            result = await self._call_with_retry(
                tool_info=tool_info,
                arguments=arguments or {},
                timeout=effective_timeout,
            )
            return result
        except Exception as e:
            # Catch any unexpected errors and wrap them
            log.exception(
                "orchestrator.mcp_tools.unexpected_error",
                tool_name=name,
                error=str(e),
            )
            return Result.err(
                MCPToolError(
                    f"Unexpected error calling tool {name}: {e}",
                    tool_name=name,
                    server_name=tool_info.server_name,
                    is_retriable=False,
                    details={"exception_type": type(e).__name__},
                )
            )

    async def _call_with_retry(
        self,
        tool_info: MCPToolInfo,
        arguments: dict[str, Any],
        timeout: float,
    ) -> Result[MCPToolResult, MCPToolError]:
        """Call tool with retry logic for transient failures.

        Uses internal exponential backoff retries on:
        - Connection errors
        - Timeout errors (if marked retriable)
        - Other transient MCPClientErrors

        Args:
            tool_info: Information about the tool to call.
            arguments: Tool arguments.
            timeout: Timeout in seconds.

        Returns:
            Result containing MCPToolResult or MCPToolError.
        """

        @retry_async(
            on=(MCPConnectionError, asyncio.TimeoutError),
            attempts=MAX_RETRIES,
            wait_initial=RETRY_WAIT_MIN,
            wait_max=RETRY_WAIT_MAX,
            wait_jitter=0.5,
        )
        async def _do_call() -> Result[MCPToolResult, MCPClientError]:
            # Use call_tool with server name for explicit routing
            return await self._manager.call_tool(
                server_name=tool_info.server_name,
                tool_name=tool_info.original_name,
                arguments=arguments,
                timeout=timeout,
            )

        try:
            result = await _do_call()
        except TimeoutError:
            log.warning(
                "orchestrator.mcp_tools.timeout_after_retries",
                tool_name=tool_info.name,
                timeout=timeout,
            )
            return Result.err(
                MCPToolError(
                    f"Tool call timed out after {MAX_RETRIES} retries: {tool_info.name}",
                    tool_name=tool_info.name,
                    server_name=tool_info.server_name,
                    is_retriable=False,
                    details={"timeout_seconds": timeout, "retries": MAX_RETRIES},
                )
            )
        except MCPConnectionError as e:
            log.warning(
                "orchestrator.mcp_tools.connection_failed_after_retries",
                tool_name=tool_info.name,
                error=str(e),
            )
            return Result.err(
                MCPToolError(
                    f"Connection failed after {MAX_RETRIES} retries: {e}",
                    tool_name=tool_info.name,
                    server_name=tool_info.server_name,
                    is_retriable=False,
                    details={"retries": MAX_RETRIES},
                )
            )

        # Convert MCPClientError to MCPToolError for consistency
        if result.is_err:
            error = result.error
            log.warning(
                "orchestrator.mcp_tools.call_failed",
                tool_name=tool_info.name,
                error=str(error),
            )
            return Result.err(
                MCPToolError(
                    f"Tool execution failed: {error}",
                    tool_name=tool_info.name,
                    server_name=tool_info.server_name,
                    is_retriable=error.is_retriable if isinstance(error, MCPClientError) else False,
                    details={"original_error": str(error)},
                )
            )

        log.debug(
            "orchestrator.mcp_tools.call_success",
            tool_name=tool_info.name,
            is_error=result.value.is_error,
        )

        return Result.ok(result.value)


@dataclass(frozen=True, slots=True)
class MCPToolsLoadedEvent:
    """Event data when MCP tools are loaded.

    Attributes:
        tool_count: Number of tools loaded.
        server_names: Names of servers providing tools.
        conflict_count: Number of tool conflicts detected.
        conflicts: Details of any conflicts.
    """

    tool_count: int
    server_names: tuple[str, ...]
    conflict_count: int
    conflicts: tuple[ToolConflict, ...] = field(default_factory=tuple)


def create_mcp_tools_loaded_event(
    session_id: str,
    provider: MCPToolProvider,
) -> dict[str, Any]:
    """Create event data for MCP tools loaded.

    Args:
        session_id: Current session ID.
        provider: MCPToolProvider with loaded tools.

    Returns:
        Event data dict for inclusion in BaseEvent.
    """
    tools = list(provider._tool_map.values())
    server_names = tuple({t.server_name for t in tools})

    return {
        "session_id": session_id,
        "tool_count": len(tools),
        "server_names": server_names,
        "conflict_count": len(provider.conflicts),
        "tool_names": [t.name for t in tools],
    }


__all__ = [
    "DEFAULT_TOOL_TIMEOUT",
    "MAX_RETRIES",
    "MCPToolError",
    "MCPToolInfo",
    "MCPToolProvider",
    "MCPToolsLoadedEvent",
    "SessionToolCatalogEntry",
    "SessionToolCatalog",
    "ToolCatalogSourceMetadata",
    "ToolConflict",
    "assemble_session_tool_catalog",
    "create_mcp_tools_loaded_event",
    "enumerate_runtime_builtin_tool_definitions",
    "merge_session_tool_catalogs",
    "normalize_opencode_session_tool_catalog",
    "normalize_opencode_tool_result",
    "normalize_serialized_tool_catalog",
    "normalize_runtime_tool_definition",
    "normalize_runtime_tool_result",
    "serialize_tool_catalog",
    "serialize_tool_definition",
    "serialize_tool_result",
]
