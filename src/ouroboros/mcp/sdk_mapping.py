"""Lossless conversions between Ouroboros DTOs and MCP SDK v2 models.

The SDK is the wire compatibility boundary.  This module intentionally uses
its public, snake_case model attributes and explicit model discrimination so
protocol fields cannot disappear through attribute-presence heuristics.
"""

from __future__ import annotations

from typing import Any, cast

from mcp import types as sdk_types

from ouroboros.mcp.types import (
    ContentType,
    JSONObject,
    MCPContentItem,
    MCPPromptArgument,
    MCPPromptDefinition,
    MCPPromptMessage,
    MCPPromptResult,
    MCPResourceContent,
    MCPResourceDefinition,
    MCPResourceResult,
    MCPToolDefinition,
    MCPToolParameter,
    MCPToolResult,
    ToolInputType,
)


def _model_object(value: Any | None) -> JSONObject | None:
    if value is None:
        return None
    return cast(
        JSONObject,
        value.model_dump(by_alias=True, mode="json", exclude_none=False),
    )


def _model_objects(values: list[Any] | None) -> tuple[JSONObject, ...]:
    return tuple(
        cast(JSONObject, value.model_dump(by_alias=True, mode="json")) for value in values or ()
    )


def _parameters_from_schema(schema: JSONObject) -> tuple[MCPToolParameter, ...]:
    """Build the legacy parameter view without constraining the full schema."""
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return ()
    required_value = schema.get("required", [])
    required = set(required_value) if isinstance(required_value, list) else set()
    parameters: list[MCPToolParameter] = []
    for name, raw_property in properties.items():
        if not isinstance(name, str) or not isinstance(raw_property, dict):
            continue
        raw_type = raw_property.get("type")
        try:
            parameter_type = ToolInputType(raw_type)
        except (TypeError, ValueError):
            # Complex schemas remain intact in input_schema even when they do
            # not have a meaningful legacy MCPToolParameter representation.
            continue
        raw_enum = raw_property.get("enum")
        enum = (
            tuple(raw_enum)
            if isinstance(raw_enum, list) and all(isinstance(item, str) for item in raw_enum)
            else None
        )
        raw_items = raw_property.get("items")
        items = raw_items if isinstance(raw_items, (dict, bool)) else None
        description = raw_property.get("description")
        parameters.append(
            MCPToolParameter(
                name=name,
                type=parameter_type,
                description=description if isinstance(description, str) else "",
                required=name in required,
                default=raw_property.get("default"),
                enum=enum,
                items=items,
            )
        )
    return tuple(parameters)


def tool_from_sdk(tool: sdk_types.Tool, server_name: str | None = None) -> MCPToolDefinition:
    """Convert an SDK tool definition without flattening its JSON schemas."""
    schema = cast(JSONObject, tool.input_schema)
    return MCPToolDefinition(
        name=tool.name,
        description=tool.description or "",
        parameters=_parameters_from_schema(schema),
        server_name=server_name,
        input_schema=schema,
        output_schema=cast(JSONObject | None, tool.output_schema),
        title=tool.title,
        execution=_model_object(tool.execution),
        icons=_model_objects(tool.icons),
        annotations=_model_object(tool.annotations),
        meta=cast(JSONObject, tool.meta or {}),
    )


def tool_to_sdk(tool: MCPToolDefinition) -> sdk_types.Tool:
    """Convert an Ouroboros tool definition to an SDK v2 model."""
    return sdk_types.Tool(
        name=tool.name,
        title=tool.title,
        description=tool.description or None,
        input_schema=tool.to_input_schema(),
        output_schema=tool.output_schema,
        execution=(
            sdk_types.ToolExecution.model_validate(tool.execution) if tool.execution else None
        ),
        icons=[sdk_types.Icon.model_validate(icon) for icon in tool.icons] or None,
        annotations=(
            sdk_types.ToolAnnotations.model_validate(tool.annotations) if tool.annotations else None
        ),
        meta=tool.meta or None,
    )


def resource_content_from_sdk(
    content: sdk_types.TextResourceContents | sdk_types.BlobResourceContents,
) -> MCPResourceContent:
    """Convert one typed SDK resource content item."""
    if isinstance(content, sdk_types.TextResourceContents):
        return MCPResourceContent(
            uri=content.uri,
            text=content.text,
            mime_type=content.mime_type,
            meta=cast(JSONObject, content.meta or {}),
        )
    if isinstance(content, sdk_types.BlobResourceContents):
        return MCPResourceContent(
            uri=content.uri,
            blob=content.blob,
            mime_type=content.mime_type,
            meta=cast(JSONObject, content.meta or {}),
        )
    msg = f"Unsupported MCP resource content model: {type(content).__name__}"
    raise TypeError(msg)


def resource_content_to_sdk(
    content: MCPResourceContent,
) -> sdk_types.TextResourceContents | sdk_types.BlobResourceContents:
    """Convert one resource item, rejecting ambiguous text/blob payloads."""
    if content.text is not None and content.blob is not None:
        msg = "MCP resource content cannot contain both text and blob"
        raise ValueError(msg)
    if content.text is not None:
        return sdk_types.TextResourceContents(
            uri=content.uri,
            text=content.text,
            mime_type=content.mime_type,
            meta=content.meta or None,
        )
    if content.blob is not None:
        return sdk_types.BlobResourceContents(
            uri=content.uri,
            blob=content.blob,
            mime_type=content.mime_type,
            meta=content.meta or None,
        )
    msg = "MCP resource content requires text or blob"
    raise ValueError(msg)


def content_from_sdk(content: sdk_types.ContentBlock) -> MCPContentItem:
    """Convert one SDK content union member by its concrete public model."""
    common = {
        "annotations": _model_object(content.annotations),
        "meta": cast(JSONObject, content.meta or {}),
    }
    if isinstance(content, sdk_types.TextContent):
        return MCPContentItem(type=ContentType.TEXT, text=content.text, **common)
    if isinstance(content, sdk_types.ImageContent):
        return MCPContentItem(
            type=ContentType.IMAGE,
            data=content.data,
            mime_type=content.mime_type,
            **common,
        )
    if isinstance(content, sdk_types.AudioContent):
        return MCPContentItem(
            type=ContentType.AUDIO,
            data=content.data,
            mime_type=content.mime_type,
            **common,
        )
    if isinstance(content, sdk_types.ResourceLink):
        return MCPContentItem(
            type=ContentType.RESOURCE_LINK,
            uri=content.uri,
            name=content.name,
            title=content.title,
            description=content.description,
            mime_type=content.mime_type,
            size=content.size,
            icons=_model_objects(content.icons),
            **common,
        )
    if isinstance(content, sdk_types.EmbeddedResource):
        return MCPContentItem(
            type=ContentType.RESOURCE,
            resource=resource_content_from_sdk(content.resource),
            uri=content.resource.uri,
            **common,
        )
    msg = f"Unsupported MCP content model: {type(content).__name__}"
    raise TypeError(msg)


def _annotations(value: JSONObject | None) -> sdk_types.Annotations | None:
    return sdk_types.Annotations.model_validate(value) if value is not None else None


def content_to_sdk(content: MCPContentItem) -> sdk_types.ContentBlock:
    """Convert one Ouroboros content item to its exact SDK v2 model."""
    common = {"annotations": _annotations(content.annotations), "meta": content.meta or None}
    if content.type == ContentType.TEXT:
        if content.text is None:
            raise ValueError("Text MCP content requires text")
        return sdk_types.TextContent(text=content.text, **common)
    if content.type in (ContentType.IMAGE, ContentType.AUDIO):
        if content.data is None or content.mime_type is None:
            raise ValueError(f"{content.type.value} MCP content requires data and mime_type")
        cls = (
            sdk_types.ImageContent if content.type == ContentType.IMAGE else sdk_types.AudioContent
        )
        return cls(data=content.data, mime_type=content.mime_type, **common)
    if content.type == ContentType.RESOURCE_LINK:
        if content.uri is None or content.name is None:
            raise ValueError("Resource-link MCP content requires uri and name")
        return sdk_types.ResourceLink(
            uri=content.uri,
            name=content.name,
            title=content.title,
            description=content.description,
            mime_type=content.mime_type,
            size=content.size,
            icons=[sdk_types.Icon.model_validate(icon) for icon in content.icons] or None,
            **common,
        )
    if content.type == ContentType.RESOURCE:
        if content.resource is None:
            raise ValueError("Embedded-resource MCP content requires resource")
        return sdk_types.EmbeddedResource(
            resource=resource_content_to_sdk(content.resource), **common
        )
    msg = f"Unsupported Ouroboros MCP content type: {content.type}"
    raise ValueError(msg)


def tool_result_from_sdk(result: sdk_types.CallToolResult) -> MCPToolResult:
    """Convert an SDK call result, preserving arbitrary structured JSON."""
    return MCPToolResult(
        content=tuple(content_from_sdk(item) for item in result.content),
        is_error=result.is_error,
        meta=cast(JSONObject, result.meta or {}),
        structured_content=result.structured_content,
        result_type=result.result_type,
    )


def tool_result_to_sdk(result: MCPToolResult) -> sdk_types.CallToolResult:
    """Convert a complete Ouroboros tool result to the SDK model."""
    return sdk_types.CallToolResult(
        content=[content_to_sdk(item) for item in result.content],
        is_error=result.is_error,
        meta=result.meta or None,
        structured_content=result.structured_content,
        result_type=result.result_type,
    )


def resource_result_from_sdk(result: sdk_types.ReadResourceResult) -> MCPResourceResult:
    """Convert a read result while preserving every ordered content item."""
    return MCPResourceResult(
        contents=tuple(resource_content_from_sdk(item) for item in result.contents),
        meta=cast(JSONObject, result.meta or {}),
        ttl_ms=result.ttl_ms,
        cache_scope=result.cache_scope,
        result_type=result.result_type,
    )


def resource_result_to_sdk(result: MCPResourceResult) -> sdk_types.ReadResourceResult:
    """Convert a complete ordered resource result to the SDK model."""
    return sdk_types.ReadResourceResult(
        contents=[resource_content_to_sdk(item) for item in result.contents],
        meta=result.meta or None,
        ttl_ms=result.ttl_ms,
        cache_scope=result.cache_scope,
        result_type=result.result_type,
    )


def resource_from_sdk(resource: sdk_types.Resource) -> MCPResourceDefinition:
    """Convert an SDK resource definition."""
    return MCPResourceDefinition(
        uri=resource.uri,
        name=resource.name,
        description=resource.description or "",
        mime_type=resource.mime_type,
        title=resource.title,
        size=resource.size,
        icons=_model_objects(resource.icons),
        annotations=_model_object(resource.annotations),
        meta=cast(JSONObject, resource.meta or {}),
    )


def resource_to_sdk(resource: MCPResourceDefinition) -> sdk_types.Resource:
    """Convert an Ouroboros resource definition to an SDK model."""
    return sdk_types.Resource(
        uri=resource.uri,
        name=resource.name,
        title=resource.title,
        description=resource.description,
        mime_type=resource.mime_type,
        size=resource.size,
        icons=[sdk_types.Icon.model_validate(icon) for icon in resource.icons] or None,
        annotations=_annotations(resource.annotations),
        meta=resource.meta or None,
    )


def prompt_from_sdk(prompt: sdk_types.Prompt) -> MCPPromptDefinition:
    """Convert an SDK prompt definition."""
    return MCPPromptDefinition(
        name=prompt.name,
        description=prompt.description or "",
        arguments=tuple(
            MCPPromptArgument(
                name=argument.name,
                description=argument.description or "",
                required=argument.required,
                title=argument.title,
            )
            for argument in prompt.arguments or ()
        ),
        title=prompt.title,
        icons=_model_objects(prompt.icons),
        meta=cast(JSONObject, prompt.meta or {}),
    )


def prompt_to_sdk(prompt: MCPPromptDefinition) -> sdk_types.Prompt:
    """Convert an Ouroboros prompt definition to an SDK model."""
    return sdk_types.Prompt(
        name=prompt.name,
        title=prompt.title,
        description=prompt.description,
        arguments=[
            sdk_types.PromptArgument(
                name=argument.name,
                title=argument.title,
                description=argument.description,
                required=argument.required,
            )
            for argument in prompt.arguments
        ]
        or None,
        icons=[sdk_types.Icon.model_validate(icon) for icon in prompt.icons] or None,
        meta=prompt.meta or None,
    )


def prompt_result_from_sdk(result: sdk_types.GetPromptResult) -> MCPPromptResult:
    """Convert a prompt result without flattening roles or non-text content."""
    return MCPPromptResult(
        description=result.description,
        messages=tuple(
            MCPPromptMessage(
                role=message.role,
                content=content_from_sdk(message.content),
            )
            for message in result.messages
        ),
        meta=cast(JSONObject, result.meta or {}),
        result_type=result.result_type,
    )


def prompt_result_to_sdk(result: MCPPromptResult) -> sdk_types.GetPromptResult:
    """Convert a complete role-preserving prompt result to the SDK model."""
    return sdk_types.GetPromptResult(
        description=result.description,
        messages=[
            sdk_types.PromptMessage(
                role=cast(Any, message.role),
                content=content_to_sdk(message.content),
            )
            for message in result.messages
        ],
        meta=result.meta or None,
        result_type=result.result_type,
    )
