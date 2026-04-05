from typing import Any

import pytest

from ouroboros.core.types import Result
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.openclaw.adapter import OpenClawWorkflowAdapter
from ouroboros.openclaw.bridge import OpenClawTransportBridge, event_from_payload
from ouroboros.openclaw.contracts import OpenClawChannelEvent
from ouroboros.openclaw.orchestrator import OpenClawWorkflowOrchestrator


class FakeClient:
    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None):
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text="reply"),),
                is_error=False,
                meta={"stage": "interviewing"},
            )
        )


class FakeTransport:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str | None, str, dict[str, Any]]] = []

    async def post_message(
        self,
        *,
        channel_id: str,
        guild_id: str | None,
        text: str,
        meta: dict[str, Any],
    ) -> None:
        self.messages.append((channel_id, guild_id, text, meta))


def test_event_from_payload_requires_channel_and_message() -> None:
    with pytest.raises(ValueError, match="channel_id"):
        event_from_payload({"message": "hello"})
    with pytest.raises(ValueError, match="message"):
        event_from_payload({"channel_id": "c1"})


def test_event_from_payload_normalizes_fields() -> None:
    event = event_from_payload(
        {
            "channel_id": " c1 ",
            "guild_id": " g1 ",
            "user_id": " u1 ",
            "message": " hello ",
            "message_id": " m1 ",
            "event_id": " e1 ",
        }
    )

    assert event == OpenClawChannelEvent(
        channel_id="c1",
        guild_id="g1",
        user_id="u1",
        message="hello",
        message_id="m1",
        event_id="e1",
    )


@pytest.mark.asyncio
async def test_transport_bridge_handles_payload_and_posts_reply() -> None:
    adapter = OpenClawWorkflowAdapter(client=FakeClient())
    orchestrator = OpenClawWorkflowOrchestrator(adapter=adapter)
    transport = FakeTransport()
    bridge = OpenClawTransportBridge(orchestrator=orchestrator, transport=transport)

    result = await bridge.handle_payload(
        {
            "channel_id": "c1",
            "guild_id": "g1",
            "user_id": "u1",
            "message": "work on feature x",
        }
    )

    assert result.is_ok
    assert transport.messages == [("c1", "g1", "reply", {"stage": "interviewing"})]
