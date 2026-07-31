"""Contract tests for the high-level MCP SDK client adapter."""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from mcp import types as sdk_types
import pytest

from ouroboros.mcp.client.adapter import MCPClientAdapter
from ouroboros.mcp.client.sdk_factory import SDKClientResources
from ouroboros.mcp.errors import MCPConnectionError
from ouroboros.mcp.types import ContentType, MCPServerConfig, TransportType


def _config() -> MCPServerConfig:
    return MCPServerConfig(name="configured", transport=TransportType.STDIO, command="server")


def _client() -> MagicMock:
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.protocol_version = "2026-07-28"
    client.server_info = sdk_types.Implementation(
        name="peer",
        title="Peer Server",
        version="2.3.4",
        description="A complete SDK v2 identity",
        websiteUrl="https://example.test/mcp",
        icons=[
            sdk_types.Icon(
                src="https://example.test/icon.svg",
                mimeType="image/svg+xml",
                sizes=["any"],
                theme="dark",
            )
        ],
    )
    client.server_capabilities = sdk_types.ServerCapabilities(
        tools=sdk_types.ToolsCapability(),
        resources=sdk_types.ResourcesCapability(),
        prompts=sdk_types.PromptsCapability(),
        extensions={"dev.ouroboros/example": {"enabled": True}},
    )
    client.instructions = "Use carefully"
    client.session = SimpleNamespace(
        discover_result=sdk_types.DiscoverResult.model_validate(
            {
                "_meta": {"trace": {"id": "discovery-1"}},
                "ttlMs": 4_500,
                "cacheScope": "public",
                "supportedVersions": ["2026-07-28", "2025-11-25"],
                "capabilities": client.server_capabilities,
                "instructions": "Use carefully",
                "resultType": "input_required",
            }
        )
    )
    return client


async def _connected(client: MagicMock | None = None) -> tuple[MCPClientAdapter, MagicMock]:
    sdk_client = client or _client()
    adapter = MCPClientAdapter(max_retries=1)
    with patch(
        "ouroboros.mcp.client.adapter.build_sdk_client",
        return_value=SDKClientResources(sdk_client),
    ):
        result = await adapter.connect(_config())
    assert result.is_ok
    return adapter, sdk_client


class TestConnection:
    def test_initial_state(self) -> None:
        adapter = MCPClientAdapter()
        assert not adapter.is_connected
        assert adapter.server_info is None
        assert adapter.server_snapshot is None
        assert adapter.protocol_version is None

    async def test_connect_publishes_distinct_application_and_protocol_versions(self) -> None:
        adapter, client = await _connected()

        client.__aenter__.assert_awaited_once()
        assert adapter.server_info is not None
        assert adapter.server_info.name == "peer"
        assert adapter.server_info.version == "2.3.4"
        assert adapter.server_info.application_version == "2.3.4"
        assert adapter.server_info.protocol_version == "2026-07-28"
        assert adapter.protocol_version == "2026-07-28"
        assert adapter.server_snapshot is not None
        assert adapter.server_snapshot.identity is not None
        snapshot = adapter.server_snapshot
        identity = snapshot.identity
        assert identity.application_version == "2.3.4"
        assert identity.title == "Peer Server"
        assert identity.description == "A complete SDK v2 identity"
        assert identity.website_url == "https://example.test/mcp"
        assert identity.icons[0]["src"] == "https://example.test/icon.svg"
        identity_aliases = {
            field.alias or name for name, field in sdk_types.Implementation.model_fields.items()
        }
        assert set(identity.details) == identity_aliases
        assert snapshot.supported_protocol_versions == (
            "2026-07-28",
            "2025-11-25",
        )
        assert snapshot.extensions["dev.ouroboros/example"]["enabled"] is True
        assert snapshot.instructions == "Use carefully"
        assert snapshot.meta["trace"]["id"] == "discovery-1"
        assert snapshot.ttl_ms == 4_500
        assert snapshot.cache_scope == "public"
        assert snapshot.result_type == "input_required"
        discovery_aliases = {
            field.alias or name for name, field in sdk_types.DiscoverResult.model_fields.items()
        }
        assert set(snapshot.discovery_details) == discovery_aliases
        try:
            snapshot.extensions["new"] = {}  # type: ignore[index]
        except TypeError:
            pass
        else:  # pragma: no cover - assertion branch
            raise AssertionError("snapshot extensions must be immutable")
        with pytest.raises(TypeError):
            identity.icons[0]["theme"] = "light"
        with pytest.raises(TypeError):
            snapshot.meta["trace"]["id"] = "mutated"  # type: ignore[index]
        with pytest.raises(TypeError):
            snapshot.discovery_details["capabilities"]["tools"] = None  # type: ignore[index]

    async def test_anonymous_modern_server_uses_non_protocol_version_fallback(self) -> None:
        client = _client()
        client.server_info = None
        adapter, _ = await _connected(client)
        assert adapter.server_info is not None
        assert adapter.server_info.name == "configured"
        assert adapter.server_info.version == "unknown"
        assert adapter.server_info.protocol_version == "2026-07-28"

    async def test_context_manager_and_disconnected_guard(self) -> None:
        async with MCPClientAdapter() as adapter:
            result = await adapter.list_tools()
        assert result.is_err
        assert isinstance(result.error, MCPConnectionError)

    def test_production_path_does_not_drive_low_level_handshake(self) -> None:
        import ouroboros.mcp.client.adapter as module

        source = inspect.getsource(module)
        assert "ClientSession" not in source
        assert ".initialize(" not in source
        assert "session_id" not in source.lower()


class TestOperations:
    async def test_lists_all_tool_pages_with_lossless_schema(self) -> None:
        adapter, client = await _connected()
        client.list_tools = AsyncMock(
            side_effect=[
                SimpleNamespace(
                    tools=[
                        sdk_types.Tool(
                            name="nested",
                            input_schema={
                                "type": "object",
                                "properties": {
                                    "value": {"oneOf": [{"type": "string"}, {"type": "integer"}]}
                                },
                            },
                        )
                    ],
                    next_cursor="next",
                ),
                SimpleNamespace(
                    tools=[sdk_types.Tool(name="second", input_schema={"type": "object"})],
                    next_cursor=None,
                ),
            ]
        )

        result = await adapter.list_tools()

        assert result.is_ok
        assert [tool.name for tool in result.value] == ["nested", "second"]
        assert "oneOf" in result.value[0].input_schema["properties"]["value"]
        assert client.list_tools.await_args_list[1].kwargs == {"cursor": "next"}

    async def test_call_tool_preserves_all_content_and_arbitrary_structured_json(self) -> None:
        adapter, client = await _connected()
        client.call_tool = AsyncMock(
            return_value=sdk_types.CallToolResult(
                content=[
                    sdk_types.TextContent(text="hello"),
                    sdk_types.AudioContent(data="YQ==", mime_type="audio/wav"),
                ],
                structured_content=[1, {"nested": True}],
                result_type="complete",
            )
        )

        result = await adapter.call_tool("demo", {"x": 1})

        assert result.is_ok
        assert [item.type for item in result.value.content] == [ContentType.TEXT, ContentType.AUDIO]
        assert result.value.structured_content == [1, {"nested": True}]
        assert result.value.result_type == "complete"

    async def test_read_resource_result_is_lossless_and_compatibility_view_is_first(self) -> None:
        adapter, client = await _connected()
        client.read_resource = AsyncMock(
            return_value=sdk_types.ReadResourceResult(
                contents=[
                    sdk_types.TextResourceContents(
                        uri="memo://x", text="one", mime_type="text/plain"
                    ),
                    sdk_types.BlobResourceContents(uri="memo://x", blob="dHdv", mime_type="x/test"),
                ],
                ttl_ms=30,
                cache_scope="private",
                result_type="complete",
            )
        )

        complete = await adapter.read_resource_result("memo://x")
        first = await adapter.read_resource("memo://x")

        assert complete.is_ok
        assert [item.text or item.blob for item in complete.value.contents] == ["one", "dHdv"]
        assert complete.value.ttl_ms == 30
        assert first.is_ok
        assert first.value.text == "one"

    async def test_lists_all_resource_and_prompt_pages(self) -> None:
        adapter, client = await _connected()
        client.list_resources = AsyncMock(
            side_effect=[
                SimpleNamespace(
                    resources=[sdk_types.Resource(uri="memo://1", name="one")], next_cursor="r2"
                ),
                SimpleNamespace(
                    resources=[sdk_types.Resource(uri="memo://2", name="two")], next_cursor=None
                ),
            ]
        )
        client.list_prompts = AsyncMock(
            side_effect=[
                SimpleNamespace(prompts=[sdk_types.Prompt(name="one")], next_cursor="p2"),
                SimpleNamespace(prompts=[sdk_types.Prompt(name="two")], next_cursor=None),
            ]
        )

        resources = await adapter.list_resources()
        prompts = await adapter.list_prompts()

        assert resources.is_ok and [resource.name for resource in resources.value] == ["one", "two"]
        assert prompts.is_ok and [prompt.name for prompt in prompts.value] == ["one", "two"]

    async def test_get_prompt_joins_only_text_blocks(self) -> None:
        adapter, client = await _connected()
        client.get_prompt = AsyncMock(
            return_value=sdk_types.GetPromptResult(
                description="mixed prompt",
                meta={"trace": "prompt"},
                messages=[
                    sdk_types.PromptMessage(
                        role="user", content=sdk_types.TextContent(text="first")
                    ),
                    sdk_types.PromptMessage(
                        role="assistant",
                        content=sdk_types.ImageContent(data="a", mime_type="image/png"),
                    ),
                    sdk_types.PromptMessage(
                        role="assistant", content=sdk_types.TextContent(text="second")
                    ),
                ],
            )
        )

        rich = await adapter.get_prompt_result("demo")
        result = await adapter.get_prompt("demo")

        assert rich.is_ok
        assert rich.value.description == "mixed prompt"
        assert rich.value.meta == {"trace": "prompt"}
        assert [message.role for message in rich.value.messages] == [
            "user",
            "assistant",
            "assistant",
        ]
        assert rich.value.messages[1].content.type == ContentType.IMAGE
        assert result.is_ok and result.value == "first\nsecond"
