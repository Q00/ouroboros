"""Contract tests for the MCP SDK v2 mapping boundary."""

from typing import Any

from mcp import types as sdk_types
import pytest

from ouroboros.mcp.sdk_mapping import (
    content_from_sdk,
    content_to_sdk,
    prompt_from_sdk,
    prompt_to_sdk,
    resource_from_sdk,
    resource_result_from_sdk,
    resource_result_to_sdk,
    resource_to_sdk,
    tool_from_sdk,
    tool_result_from_sdk,
    tool_result_to_sdk,
    tool_to_sdk,
)
from ouroboros.mcp.types import ContentType


def _wire(model: Any) -> dict[str, Any]:
    return model.model_dump(by_alias=True, mode="json", exclude_none=False)


def test_tool_schema_and_metadata_round_trip_losslessly() -> None:
    """Tool conversion keeps full schemas and every supported descriptor field."""
    input_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": {"id": {"type": "string", "minLength": 1}},
        "type": "object",
        "properties": {
            "id": {"$ref": "#/$defs/id"},
            "label": {"type": "string", "description": "Legacy-compatible view"},
            "choice": {"oneOf": [{"type": "number"}, {"type": "boolean"}]},
        },
        "required": ["id"],
        "unevaluatedProperties": False,
        "x-vendor": {"enabled": True},
    }
    tool = sdk_types.Tool(
        name="complete_tool",
        title="Complete tool",
        description="Preserves its schema",
        input_schema=input_schema,
        output_schema={"$ref": "#/$defs/id", "$defs": input_schema["$defs"]},
        execution=sdk_types.ToolExecution(task_support="optional"),
        icons=[sdk_types.Icon(src="https://example.com/icon.svg", theme="dark")],
        annotations=sdk_types.ToolAnnotations(read_only_hint=True, idempotent_hint=True),
        meta={"extension": {"level": 2}},
    )

    internal = tool_from_sdk(tool, server_name="peer")
    restored = tool_to_sdk(internal)

    assert internal.server_name == "peer"
    assert internal.input_schema == input_schema
    assert [parameter.name for parameter in internal.parameters] == ["label"]
    assert _wire(restored) == _wire(tool)


@pytest.mark.parametrize(
    "structured_content",
    [
        {"object": [1, True, None]},
        ["array", 2],
        "string",
        1.25,
        False,
        None,
    ],
)
def test_every_structured_json_value_round_trips(structured_content: Any) -> None:
    """SDK arbitrary structuredContent values survive both conversions."""
    result = sdk_types.CallToolResult(
        content=[sdk_types.TextContent(text="ok")],
        structured_content=structured_content,
        is_error=False,
        result_type="input_required",
        meta={"trace": "abc"},
    )

    restored = tool_result_to_sdk(tool_result_from_sdk(result))

    assert restored.structured_content == structured_content
    assert restored.result_type == "input_required"
    assert _wire(restored) == _wire(result)


def test_all_sdk_content_models_preserve_order_and_payloads() -> None:
    """Text, image, audio, link, and embedded resources retain their types."""
    annotation = sdk_types.Annotations(
        audience=["assistant"], priority=0.7, last_modified="2026-07-28T00:00:00Z"
    )
    contents: list[sdk_types.ContentBlock] = [
        sdk_types.TextContent(text="text", annotations=annotation, meta={"index": 0}),
        sdk_types.ImageContent(data="aW1hZ2U=", mime_type="image/png", meta={"index": 1}),
        sdk_types.AudioContent(data="YXVkaW8=", mime_type="audio/wav", meta={"index": 2}),
        sdk_types.ResourceLink(
            uri="docs://guide",
            name="Guide",
            title="The guide",
            description="Documentation",
            mime_type="text/markdown",
            size=42,
            icons=[sdk_types.Icon(src="https://example.com/guide.svg")],
            meta={"index": 3},
        ),
        sdk_types.EmbeddedResource(
            resource=sdk_types.TextResourceContents(
                uri="embedded://text",
                text="embedded",
                mime_type="text/plain",
                meta={"inside": True},
            ),
            meta={"index": 4},
        ),
        sdk_types.EmbeddedResource(
            resource=sdk_types.BlobResourceContents(
                uri="embedded://blob",
                blob="YmxvYg==",
                mime_type="application/octet-stream",
            ),
            meta={"index": 5},
        ),
    ]

    internal = tuple(content_from_sdk(item) for item in contents)
    restored = [content_to_sdk(item) for item in internal]

    assert tuple(item.type for item in internal) == (
        ContentType.TEXT,
        ContentType.IMAGE,
        ContentType.AUDIO,
        ContentType.RESOURCE_LINK,
        ContentType.RESOURCE,
        ContentType.RESOURCE,
    )
    assert [_wire(item) for item in restored] == [_wire(item) for item in contents]


def test_tool_result_preserves_mixed_content_and_result_metadata() -> None:
    """Call result mapping keeps content order, metadata, error, and result type."""
    result = sdk_types.CallToolResult(
        content=[
            sdk_types.TextContent(text="first"),
            sdk_types.AudioContent(data="c2Vjb25k", mime_type="audio/ogg"),
        ],
        structured_content=[1, {"two": None}],
        is_error=True,
        result_type="custom-result-type",
        meta={"request": 7},
    )

    restored = tool_result_to_sdk(tool_result_from_sdk(result))

    assert _wire(restored) == _wire(result)


def test_multiple_resource_contents_and_cache_fields_round_trip() -> None:
    """Resource reads retain ordered text/blob items and v2 cache fields."""
    result = sdk_types.ReadResourceResult(
        contents=[
            sdk_types.TextResourceContents(
                uri="data://one", text="first", mime_type="text/plain", meta={"part": 1}
            ),
            sdk_types.BlobResourceContents(
                uri="data://two",
                blob="c2Vjb25k",
                mime_type="application/octet-stream",
                meta={"part": 2},
            ),
        ],
        ttl_ms=5000,
        cache_scope="public",
        result_type="complete",
        meta={"etag": "v1"},
    )

    internal = resource_result_from_sdk(result)
    restored = resource_result_to_sdk(internal)

    assert [item.uri for item in internal.contents] == ["data://one", "data://two"]
    assert internal.first_content is internal.contents[0]
    assert _wire(restored) == _wire(result)


def test_resource_and_prompt_definitions_round_trip() -> None:
    """Resource/prompt descriptors retain optional fields and metadata."""
    resource = sdk_types.Resource(
        uri="docs://reference",
        name="Reference",
        title="Reference title",
        description="Description",
        mime_type=None,
        size=101,
        icons=[sdk_types.Icon(src="https://example.com/resource.svg", sizes=["any"])],
        annotations=sdk_types.Annotations(audience=["user"]),
        meta={"resource-extension": True},
    )
    prompt = sdk_types.Prompt(
        name="review",
        title="Review",
        description="Review code",
        arguments=[
            sdk_types.PromptArgument(
                name="focus", title="Focus", description="Review focus", required=None
            )
        ],
        icons=[sdk_types.Icon(src="https://example.com/prompt.svg")],
        meta={"prompt-extension": [1, 2]},
    )

    assert _wire(resource_to_sdk(resource_from_sdk(resource))) == _wire(resource)
    assert _wire(prompt_to_sdk(prompt_from_sdk(prompt))) == _wire(prompt)


def test_invalid_internal_content_is_rejected_explicitly() -> None:
    """The typed boundary fails clearly instead of guessing a content model."""
    from ouroboros.mcp.types import MCPContentItem

    with pytest.raises(ValueError, match="requires data and mime_type"):
        content_to_sdk(MCPContentItem(type=ContentType.AUDIO, data="missing-mime"))
