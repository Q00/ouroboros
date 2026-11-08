"""An explicitly incapable MCP host can receive the interview question now."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.bigbang.interview import InterviewRound, InterviewState
from ouroboros.core.types import Result
from ouroboros.mcp.host_context import subagent_capability_extensions
from ouroboros.mcp.server.adapter import MCPServerAdapter, _OuroborosSDKServer
from ouroboros.mcp.tools.authoring_handlers import InterviewHandler
from ouroboros.mcp.tools.fanout import FanoutRegistry


@pytest.mark.asyncio
@pytest.mark.parametrize("question_only", [True, False])
async def test_sdk_host_question_only_returns_question_without_a_fanout(tmp_path, question_only):
    """An SDK client's advertised mode reaches the real interview handler."""
    import anyio

    pytest.importorskip("mcp.server")
    from mcp.client.session import ClientSession

    state = InterviewState(
        interview_id="interview_delivery_test",
        initial_context="Build a video tool",
        rounds=[InterviewRound(round_number=1, question="What format?", user_response="MP4")],
    )
    engine = MagicMock()
    engine.state_dir = tmp_path
    engine.load_state = AsyncMock(return_value=Result.ok(state))
    engine.ask_next_question = AsyncMock(return_value=Result.ok("What clip length?"))
    engine.save_state = AsyncMock(return_value=MagicMock(is_err=False))
    registry = FanoutRegistry(tmp_path / "fanout")
    handler = InterviewHandler(
        interview_engine=engine, llm_adapter=MagicMock(), fanout_registry=registry
    )
    handler._emit_event_bg = MagicMock()
    adapter = MCPServerAdapter()
    adapter.register_tool(handler)
    server = _OuroborosSDKServer(adapter, name="test", extensions=subagent_capability_extensions())
    client_send, server_receive = anyio.create_memory_object_stream(10)
    server_send, client_receive = anyio.create_memory_object_stream(10)
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(
            server._lowlevel_server.run,
            server_receive,
            server_send,
            server._lowlevel_server.create_initialization_options(),
        )
        async with ClientSession(
            client_receive,
            client_send,
            extensions={"io.ouroboros/question-delivery": {"mode": "question_only_degraded"}}
            if question_only
            else None,
        ) as client:
            await client.initialize()
            result = await client.call_tool(
                "ouroboros_interview", {"session_id": state.interview_id}
            )
        tasks.cancel_scope.cancel()

    assert "What clip length?" in result.content[0].text
    if question_only:
        assert "Host action" not in result.content[0].text
        assert result.meta["question_advisory_delivery_mode"] == "question_only_degraded"
        assert result.meta["question_advisory_degradation_reason"] == "host_requested_question_only"
        assert "question_advisory_subagents" not in result.meta
        assert "question_advisory_fanout_id" not in result.meta
        assert not registry.directory.exists()
    else:
        assert "Host action" in result.content[0].text
        assert result.meta["question_advisory_subagents"]
        assert result.meta["question_advisory_fanout_id"]
        assert registry.directory.exists()
