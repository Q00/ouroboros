from typing import Any

import pytest

from ouroboros.core.types import Result
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.openclaw.adapter import OpenClawWorkflowAdapter
from ouroboros.openclaw.contracts import OpenClawChannelEvent


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> Result[MCPToolResult, Exception]:
        self.calls.append((name, arguments))
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text="ok"),),
                is_error=False,
                meta=arguments or {},
            )
        )


@pytest.mark.asyncio
async def test_handle_event_routes_plain_message_to_channel_workflow() -> None:
    client = FakeClient()
    adapter = OpenClawWorkflowAdapter(client=client)

    result = await adapter.handle_event(
        OpenClawChannelEvent(
            channel_id="c1",
            guild_id="g1",
            user_id="u1",
            message="work on feature x",
        )
    )

    assert result.is_ok
    assert client.calls[0][0] == "ouroboros_channel_workflow"
    assert client.calls[0][1]["action"] == "message"
    assert client.calls[0][1]["mode"] == "auto"


@pytest.mark.asyncio
async def test_handle_event_routes_repo_set_command() -> None:
    client = FakeClient()
    adapter = OpenClawWorkflowAdapter(client=client)

    result = await adapter.handle_event(
        OpenClawChannelEvent(
            channel_id="c1",
            guild_id="g1",
            user_id="u1",
            message="/ouro repo set /repo/demo",
        )
    )

    assert result.is_ok
    assert client.calls[0][1]["action"] == "set_repo"
    assert client.calls[0][1]["repo"] == "/repo/demo"


@pytest.mark.asyncio
async def test_handle_event_routes_answer_mode() -> None:
    client = FakeClient()
    adapter = OpenClawWorkflowAdapter(client=client)

    result = await adapter.handle_event(
        OpenClawChannelEvent(
            channel_id="c1",
            guild_id="g1",
            user_id="u1",
            message="/ouro answer use stripe",
        )
    )

    assert result.is_ok
    assert client.calls[0][1]["mode"] == "answer"
    assert client.calls[0][1]["message"] == "use stripe"


@pytest.mark.asyncio
async def test_handle_event_routes_wait_mode() -> None:
    client = FakeClient()
    adapter = OpenClawWorkflowAdapter(client=client)

    result = await adapter.handle_event(
        OpenClawChannelEvent(
            channel_id="c1",
            guild_id="g1",
            user_id="u1",
            message="/ouro wait",
        )
    )

    assert result.is_ok
    assert client.calls[0][1]["action"] == "wait"


@pytest.mark.asyncio
async def test_handle_event_returns_usage_for_invalid_control_command() -> None:
    client = FakeClient()
    adapter = OpenClawWorkflowAdapter(client=client)

    result = await adapter.handle_event(
        OpenClawChannelEvent(
            channel_id="c1",
            guild_id="g1",
            user_id="u1",
            message="/ouro answer",
        )
    )

    assert result.is_ok
    assert result.value.is_error is True
    assert "Usage:" in result.value.reply_text
    assert client.calls == []


@pytest.mark.asyncio
async def test_handle_event_validates_repo_set_usage() -> None:
    client = FakeClient()
    adapter = OpenClawWorkflowAdapter(client=client)

    result = await adapter.handle_event(
        OpenClawChannelEvent(
            channel_id="c1",
            guild_id="g1",
            user_id="u1",
            message="/ouro repo set",
        )
    )

    assert result.is_ok
    assert result.value.is_error is True
    assert "Usage:" in result.value.reply_text
