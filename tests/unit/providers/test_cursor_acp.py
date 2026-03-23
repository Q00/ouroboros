"""Tests for CursorACPClient, CursorACPAdapter, and CursorACPRuntime."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.core.errors import ProviderError
from ouroboros.providers.cursor_acp_client import (
    ACPModel,
    ACPSession,
    CursorACPClient,
)


# ── Helpers ────────────────────────────────────────────────────────────


class FakeProcess:
    """Simulates an asyncio subprocess for cursor-agent acp."""

    def __init__(self, responses: list[dict[str, Any]] | None = None) -> None:
        self.returncode = None
        self._responses = list(responses or [])
        self._written: list[dict[str, Any]] = []
        self.stdin = self
        self.stdout = self
        self.stderr = MagicMock()

    # stdin
    def write(self, data: bytes) -> None:
        self._written.append(json.loads(data.decode()))

    async def drain(self) -> None:
        pass

    # stdout
    async def readline(self) -> bytes:
        if not self._responses:
            raise asyncio.TimeoutError
        resp = self._responses.pop(0)
        return (json.dumps(resp) + "\n").encode()

    def terminate(self) -> None:
        self.returncode = -15

    async def wait(self) -> int:
        self.returncode = 0
        return 0

    @property
    def pid(self) -> int:
        return 12345

    @property
    def written(self) -> list[dict[str, Any]]:
        return self._written


# ── CursorACPClient tests ─────────────────────────────────────────────


class TestCursorACPClient:

    @pytest.fixture
    def client(self):
        return CursorACPClient(cli_path="/usr/bin/cursor-agent")

    @pytest.mark.asyncio
    async def test_request_sends_and_receives(self, client):
        proc = FakeProcess(responses=[
            {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}},  # initialize
            {"jsonrpc": "2.0", "id": 2, "result": {"data": "hello"}},
        ])
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            await client.ensure_started()
            result = await client.request("test/method", {"key": "val"})

        assert result == {"data": "hello"}
        # Verify lock-protected: request IDs are sequential
        assert proc.written[0]["id"] == 1  # initialize
        assert proc.written[1]["id"] == 2  # test/method

    @pytest.mark.asyncio
    async def test_request_raises_on_error(self, client):
        proc = FakeProcess(responses=[
            {"jsonrpc": "2.0", "id": 1, "result": {}},  # initialize
            {"jsonrpc": "2.0", "id": 2, "error": {"code": -1, "message": "bad"}},
        ])
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            await client.ensure_started()
            with pytest.raises(ProviderError, match="bad"):
                await client.request("fail", {})

    @pytest.mark.asyncio
    async def test_request_skips_notifications(self, client):
        proc = FakeProcess(responses=[
            {"jsonrpc": "2.0", "id": 1, "result": {}},  # initialize
            {"jsonrpc": "2.0", "method": "session/update", "params": {}},  # notification
            {"jsonrpc": "2.0", "id": 2, "result": {"value": 42}},  # actual response
        ])
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            await client.ensure_started()
            result = await client.request("test", {})
            assert result == {"value": 42}

    @pytest.mark.asyncio
    async def test_create_session(self, client):
        proc = FakeProcess(responses=[
            {"jsonrpc": "2.0", "id": 1, "result": {}},  # initialize
            {"jsonrpc": "2.0", "id": 2, "result": {  # session/new
                "sessionId": "sess-1",
                "models": {
                    "currentModelId": "default[]",
                    "availableModels": [
                        {"modelId": "gpt-5.4", "name": "GPT-5.4"},
                    ],
                },
            }},
            {"jsonrpc": "2.0", "id": 3, "result": {}},  # set_config_option (mode)
        ])
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            await client.ensure_started()
            session = await client.create_session("/tmp")

        assert session.session_id == "sess-1"
        assert len(session.available_models) == 1
        assert session.available_models[0].model_id == "gpt-5.4"

    @pytest.mark.asyncio
    async def test_permission_bypass_approves_all(self, client):
        proc = FakeProcess(responses=[
            {"jsonrpc": "2.0", "id": 1, "result": {}},  # initialize
            {"jsonrpc": "2.0", "id": 2, "result": {  # session/new
                "sessionId": "sess-1", "models": {"currentModelId": "default[]", "availableModels": []},
            }},
            {"jsonrpc": "2.0", "id": 3, "result": {}},  # mode
            # prompt response includes a permission request
            {"jsonrpc": "2.0", "id": 100, "method": "session/permission", "params": {"type": "command"}},
            {"jsonrpc": "2.0", "id": 4, "result": {}},  # prompt complete
        ])
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            await client.ensure_started()
            session = await client.create_session("/tmp")
            updates = []
            async for u in client.prompt_stream(session.session_id, "test", permission_mode="bypass"):
                updates.append(u)

        # Permission was auto-approved
        approval = [w for w in proc.written if w.get("result", {}).get("approved") is not None]
        assert len(approval) == 1
        assert approval[0]["result"]["approved"] is True

    @pytest.mark.asyncio
    async def test_permission_default_denies_commands(self, client):
        proc = FakeProcess(responses=[
            {"jsonrpc": "2.0", "id": 1, "result": {}},
            {"jsonrpc": "2.0", "id": 2, "result": {
                "sessionId": "sess-1", "models": {"currentModelId": "default[]", "availableModels": []},
            }},
            {"jsonrpc": "2.0", "id": 3, "result": {}},
            {"jsonrpc": "2.0", "id": 100, "method": "session/permission", "params": {"type": "command_execution"}},
            {"jsonrpc": "2.0", "id": 4, "result": {}},
        ])
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            await client.ensure_started()
            session = await client.create_session("/tmp")
            async for _ in client.prompt_stream(session.session_id, "test", permission_mode="default"):
                pass

        approval = [w for w in proc.written if "approved" in w.get("result", {})]
        assert len(approval) == 1
        assert approval[0]["result"]["approved"] is False

    @pytest.mark.asyncio
    async def test_permission_default_allows_file_ops(self, client):
        proc = FakeProcess(responses=[
            {"jsonrpc": "2.0", "id": 1, "result": {}},
            {"jsonrpc": "2.0", "id": 2, "result": {
                "sessionId": "sess-1", "models": {"currentModelId": "default[]", "availableModels": []},
            }},
            {"jsonrpc": "2.0", "id": 3, "result": {}},
            {"jsonrpc": "2.0", "id": 100, "method": "session/permission", "params": {"type": "file_write"}},
            {"jsonrpc": "2.0", "id": 4, "result": {}},
        ])
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            await client.ensure_started()
            session = await client.create_session("/tmp")
            async for _ in client.prompt_stream(session.session_id, "test", permission_mode="acceptEdits"):
                pass

        approval = [w for w in proc.written if "approved" in w.get("result", {})]
        assert len(approval) == 1
        assert approval[0]["result"]["approved"] is True


# ── CursorACPAdapter tests ─────────────────────────────────────────────


class TestCursorACPAdapter:

    @pytest.mark.asyncio
    async def test_complete_returns_text(self):
        from ouroboros.providers.cursor_acp_adapter import CursorACPAdapter
        from ouroboros.providers.base import CompletionConfig, Message, MessageRole

        adapter = CursorACPAdapter(cli_path="/usr/bin/cursor-agent")

        mock_client = AsyncMock()
        mock_client.ensure_started = AsyncMock()
        mock_client.create_session = AsyncMock(return_value=ACPSession(
            session_id="s1",
            available_models=(ACPModel("gpt-5.4", "GPT-5.4"),),
            current_model_id="default[]",
        ))

        async def fake_stream(sid, text, **kw):
            yield {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Hello "}}
            yield {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "world"}}

        mock_client.prompt_stream = fake_stream
        adapter._client = mock_client

        result = await adapter.complete(
            [Message(role=MessageRole.USER, content="hi")],
            CompletionConfig(model="test"),
        )
        assert result.is_ok
        assert result.unwrap().content == "Hello world"

    @pytest.mark.asyncio
    async def test_model_applied_on_session_creation(self):
        from ouroboros.providers.cursor_acp_adapter import CursorACPAdapter

        adapter = CursorACPAdapter(cli_path="/usr/bin/cursor-agent", model="gpt-5.4")

        mock_client = AsyncMock()
        mock_client.ensure_started = AsyncMock()
        mock_client.create_session = AsyncMock(return_value=ACPSession(
            session_id="s1",
            available_models=(),
            current_model_id="default[]",
        ))
        mock_client.set_model = AsyncMock()
        adapter._client = mock_client

        await adapter._ensure_session()
        mock_client.set_model.assert_called_once_with("s1", "gpt-5.4")


# ── CursorACPRuntime tests ─────────────────────────────────────────────


class TestCursorACPRuntime:

    @pytest.mark.asyncio
    async def test_model_applied_on_execute(self):
        from ouroboros.orchestrator.cursor_acp_runtime import CursorACPRuntime

        runtime = CursorACPRuntime(
            cli_path="/usr/bin/cursor-agent",
            model="gpt-5.4",
            permission_mode="default",
        )

        mock_client = AsyncMock()
        mock_client.ensure_started = AsyncMock()
        mock_client.create_session = AsyncMock(return_value=ACPSession(
            session_id="s1",
            available_models=(),
            current_model_id="default[]",
        ))
        mock_client.set_model = AsyncMock()

        async def fake_stream(sid, text, **kw):
            yield {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "done"}}

        mock_client.prompt_stream = fake_stream
        runtime._client = mock_client

        messages = []
        async for msg in runtime.execute_task("Do something"):
            messages.append(msg)

        mock_client.set_model.assert_called_once_with("s1", "gpt-5.4")

    @pytest.mark.asyncio
    async def test_permission_mode_passed_to_stream(self):
        from ouroboros.orchestrator.cursor_acp_runtime import CursorACPRuntime

        runtime = CursorACPRuntime(
            cli_path="/usr/bin/cursor-agent",
            permission_mode="acceptEdits",
        )

        mock_client = AsyncMock()
        mock_client.ensure_started = AsyncMock()
        mock_client.create_session = AsyncMock(return_value=ACPSession(
            session_id="s1",
            available_models=(),
            current_model_id="default[]",
        ))

        call_kwargs = {}

        async def capture_stream(sid, text, **kw):
            call_kwargs.update(kw)
            yield {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "ok"}}

        mock_client.prompt_stream = capture_stream
        runtime._client = mock_client

        async for _ in runtime.execute_task("test"):
            pass

        assert call_kwargs.get("permission_mode") == "acceptEdits"
