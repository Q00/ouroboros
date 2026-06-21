"""CodexMcpWorkerTransport — deterministic parsing/mapping (no live codex)."""

from __future__ import annotations

import pytest

from ouroboros.core.types import Result
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.orchestrator import codex_mcp_runtime as codex_mod
from ouroboros.orchestrator.adapter import SubagentOrchestration, is_leader_driven_worker
from ouroboros.orchestrator.codex_mcp_runtime import (
    CodexMcpWorkerTransport,
    _map_permission_mode,
    build_codex_mcp_worker_runtime,
)


class TestPermissionMapping:
    @pytest.mark.parametrize(
        ("mode", "sandbox", "approval"),
        [
            (None, "workspace-write", "never"),
            ("acceptEdits", "workspace-write", "never"),
            ("bypassPermissions", "workspace-write", "never"),
            ("read-only", "read-only", "on-request"),
            ("plan", "read-only", "on-request"),
        ],
    )
    def test_maps_to_codex_sandbox_approval(self, mode, sandbox, approval) -> None:
        assert _map_permission_mode(mode) == (sandbox, approval)

    def test_never_danger_full_access(self) -> None:
        # No ouroboros permission mode should ever request danger-full-access.
        for mode in (None, "acceptEdits", "bypassPermissions", "read-only", "plan", "weird"):
            assert _map_permission_mode(mode)[0] != "danger-full-access"


class TestParseTurn:
    def test_threadid_from_structured_content(self) -> None:
        result = MCPToolResult(
            content=(MCPContentItem(type=ContentType.TEXT, text="PONG"),),
            structured_content={"threadId": "019ee5bc", "content": "PONG"},
        )
        turn = CodexMcpWorkerTransport._parse_turn(result)
        assert turn.session_id == "019ee5bc"
        assert turn.text == "PONG"
        assert turn.is_error is False

    def test_falls_back_to_text_content_when_no_structured_text(self) -> None:
        result = MCPToolResult(
            content=(MCPContentItem(type=ContentType.TEXT, text="hello"),),
            structured_content={"threadId": "t1"},
        )
        turn = CodexMcpWorkerTransport._parse_turn(result)
        assert turn.text == "hello"
        assert turn.session_id == "t1"

    def test_no_structured_content_yields_no_session(self) -> None:
        result = MCPToolResult(content=(MCPContentItem(type=ContentType.TEXT, text="x"),))
        turn = CodexMcpWorkerTransport._parse_turn(result)
        assert turn.session_id is None
        assert turn.text == "x"


class TestRuntimeWiring:
    def test_builds_leader_driven_runtime(self) -> None:
        rt = build_codex_mcp_worker_runtime(cwd="/tmp")
        assert rt.runtime_backend == "codex_mcp"
        caps = rt.capabilities
        assert caps.subagent_orchestration is SubagentOrchestration.EXTERNAL_LEADER_DRIVEN
        assert is_leader_driven_worker(caps) is True


class _RecordingActor:
    """Captures the codex tool arguments instead of spawning a real server."""

    last_args: dict = {}

    def __init__(self, server_config, *, idle_timeout) -> None:  # noqa: ARG002
        pass

    async def call(self, tool: str, arguments: dict):
        _RecordingActor.last_args = {"tool": tool, **arguments}
        return Result.ok(MCPToolResult(structured_content={"threadId": "t1", "content": "ok"}))

    async def aclose(self) -> None:
        pass


class TestRecursionHardening:
    """The worker must disable the ouroboros MCP server (self-recursion vector)
    while preserving native passthrough for every other server."""

    @pytest.mark.asyncio
    async def test_spawn_disables_ouroboros_mcp_via_config(self, monkeypatch) -> None:
        monkeypatch.setattr(codex_mod, "MCPSessionActor", _RecordingActor)
        transport = CodexMcpWorkerTransport(cli_path="codex")
        _RecordingActor.last_args = {}
        await transport.spawn(
            prompt="go",
            system_prompt=None,
            cwd="/tmp",
            permission_mode=None,
            model=None,
            reasoning_effort=None,
        )
        config = _RecordingActor.last_args.get("config", {})
        assert config["mcp_servers"]["ouroboros"] == {"enabled": False}

    @pytest.mark.asyncio
    async def test_disable_list_is_configurable_and_default_is_ouroboros(self, monkeypatch) -> None:
        monkeypatch.setattr(codex_mod, "MCPSessionActor", _RecordingActor)
        # Default disables exactly ouroboros (and nothing else — e.g. node_repl stays).
        transport = CodexMcpWorkerTransport(cli_path="codex")
        assert transport._disabled_mcp_servers == ("ouroboros",)
        _RecordingActor.last_args = {}
        await transport.spawn(
            prompt="go",
            system_prompt=None,
            cwd=None,
            permission_mode=None,
            model=None,
            reasoning_effort="high",
        )
        config = _RecordingActor.last_args["config"]
        # Effort and the MCP disable coexist; only ouroboros is touched.
        assert config["model_reasoning_effort"] == "high"
        assert set(config["mcp_servers"]) == {"ouroboros"}

    @pytest.mark.asyncio
    async def test_empty_disable_list_sends_no_mcp_override(self, monkeypatch) -> None:
        monkeypatch.setattr(codex_mod, "MCPSessionActor", _RecordingActor)
        transport = CodexMcpWorkerTransport(cli_path="codex", disabled_mcp_servers=())
        _RecordingActor.last_args = {}
        await transport.spawn(
            prompt="go",
            system_prompt=None,
            cwd=None,
            permission_mode=None,
            model=None,
            reasoning_effort=None,
        )
        # No effort, no disabled servers → no config key at all.
        assert "config" not in _RecordingActor.last_args


class TestObservability:
    """When enabled, spawning a worker registers it in the Codex app session index."""

    @pytest.mark.asyncio
    async def test_spawn_registers_session_when_index_enabled(self, monkeypatch, tmp_path) -> None:
        import json

        monkeypatch.setattr(codex_mod, "MCPSessionActor", _RecordingActor)
        transport = CodexMcpWorkerTransport(
            cli_path="codex", index_sessions=True, codex_home=str(tmp_path)
        )
        await transport.spawn(
            prompt="Build a CLI todo app",
            system_prompt=None,
            cwd="/tmp",
            permission_mode=None,
            model=None,
            reasoning_effort=None,
        )
        index = tmp_path / "session_index.jsonl"
        assert index.exists()
        entry = json.loads(index.read_text(encoding="utf-8").strip())
        assert entry["id"] == "t1"  # the recording actor's threadId
        assert entry["thread_name"].startswith("ooo: ")

    @pytest.mark.asyncio
    async def test_spawn_does_not_register_by_default(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(codex_mod, "MCPSessionActor", _RecordingActor)
        transport = CodexMcpWorkerTransport(cli_path="codex", codex_home=str(tmp_path))
        await transport.spawn(
            prompt="go",
            system_prompt=None,
            cwd="/tmp",
            permission_mode=None,
            model=None,
            reasoning_effort=None,
        )
        # index_sessions defaults False on the raw transport → no write.
        assert not (tmp_path / "session_index.jsonl").exists()
