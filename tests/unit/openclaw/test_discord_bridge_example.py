from typing import Any

import pytest

from docs.examples.openclaw_discord_bridge_example import (
    DiscordTransport,
    OpenClawDiscordBridge,
)
from ouroboros.core.types import Result
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult


class FakeClient:
    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None):
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text="hello from workflow"),),
                is_error=False,
                meta={"stage": "interviewing"},
            )
        )


@pytest.mark.asyncio
async def test_discord_bridge_example_posts_transport_reply() -> None:
    transport = DiscordTransport()
    bridge = OpenClawDiscordBridge.create(
        mcp_client=FakeClient(),
        transport=transport,
        wait_timeout_seconds=0,
        max_waits=1,
    )

    result = await bridge.on_channel_message(
        channel_id="c1",
        guild_id="g1",
        user_id="u1",
        message="work on feature x",
    )

    assert result.is_ok
    assert transport.sent_messages == [
        ("c1", "g1", "hello from workflow", {"stage": "interviewing"})
    ]
