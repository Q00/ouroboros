"""MCPSessionActor — persistent connection actor (deterministic, fake MCP client).

The actor owns a connection in its own task and serves calls via a queue; these
tests verify reuse, startup failure, idle self-close, and explicit teardown
without a live codex.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from ouroboros.core.types import Result
from ouroboros.mcp.types import MCPServerConfig, MCPToolResult, TransportType
from ouroboros.orchestrator import codex_mcp_session_pool as pool_mod
from ouroboros.orchestrator.codex_mcp_session_pool import MCPSessionActor


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, tool: str, args: dict) -> Result[MCPToolResult, object]:
        self.calls.append((tool, args))
        return Result.ok(MCPToolResult(structured_content={"threadId": "t1", "content": "ok"}))


def _patch_client(monkeypatch, client_factory, *, raise_on_connect=False):
    created: list[_FakeClient] = []

    @contextlib.asynccontextmanager
    async def _fake_create(config):
        if raise_on_connect:
            raise RuntimeError("connect boom")
        client = client_factory()
        created.append(client)
        try:
            yield client
        finally:
            pass

    monkeypatch.setattr(pool_mod, "create_mcp_client", _fake_create)
    return created


def _config() -> MCPServerConfig:
    return MCPServerConfig(
        name="codex-mcp-worker",
        transport=TransportType.STDIO,
        command="codex",
        args=("mcp-server",),
    )


class TestSessionActor:
    @pytest.mark.asyncio
    async def test_single_call_succeeds(self, monkeypatch) -> None:
        _patch_client(monkeypatch, _FakeClient)
        actor = MCPSessionActor(_config())
        result = await actor.call("codex", {"prompt": "hi"})
        assert result.is_ok
        assert result.value.structured_content["threadId"] == "t1"
        await actor.aclose()

    @pytest.mark.asyncio
    async def test_reuses_one_connection_across_turns(self, monkeypatch) -> None:
        created = _patch_client(monkeypatch, _FakeClient)
        actor = MCPSessionActor(_config())
        await actor.call("codex", {"prompt": "1"})
        await actor.call("codex-reply", {"prompt": "2"})
        await actor.call("codex-reply", {"prompt": "3"})
        # One connection served all three turns.
        assert len(created) == 1
        assert [c[0] for c in created[0].calls] == ["codex", "codex-reply", "codex-reply"]
        await actor.aclose()

    @pytest.mark.asyncio
    async def test_startup_failure_surfaces_error(self, monkeypatch) -> None:
        _patch_client(monkeypatch, _FakeClient, raise_on_connect=True)
        actor = MCPSessionActor(_config())
        result = await actor.call("codex", {"prompt": "hi"})
        assert result.is_err
        assert "boom" in result.error
        await actor.aclose()

    @pytest.mark.asyncio
    async def test_idle_timeout_closes_connection(self, monkeypatch) -> None:
        _patch_client(monkeypatch, _FakeClient)
        actor = MCPSessionActor(_config(), idle_timeout=0.05)
        await actor.call("codex", {"prompt": "hi"})
        assert actor.is_alive
        await asyncio.sleep(0.15)  # exceed idle TTL
        assert not actor.is_alive
        # A call after idle-close fails clearly rather than hanging.
        result = await actor.call("codex-reply", {"prompt": "late"})
        assert result.is_err

    @pytest.mark.asyncio
    async def test_aclose_is_idempotent_and_safe(self, monkeypatch) -> None:
        _patch_client(monkeypatch, _FakeClient)
        actor = MCPSessionActor(_config())
        await actor.call("codex", {"prompt": "hi"})
        await actor.aclose()
        assert not actor.is_alive
        await actor.aclose()  # second close is a no-op, must not raise

    @pytest.mark.asyncio
    async def test_aclose_before_any_call_is_noop(self, monkeypatch) -> None:
        _patch_client(monkeypatch, _FakeClient)
        actor = MCPSessionActor(_config())
        await actor.aclose()  # never started → no-op
        assert not actor.is_alive

    @pytest.mark.asyncio
    async def test_concurrent_calls_are_serialized_on_one_connection(self, monkeypatch) -> None:
        created = _patch_client(monkeypatch, _FakeClient)
        actor = MCPSessionActor(_config())
        results = await asyncio.gather(*(actor.call("codex", {"i": i}) for i in range(5)))
        assert all(r.is_ok for r in results)
        assert len(created) == 1  # one shared connection
        assert len(created[0].calls) == 5
        await actor.aclose()
