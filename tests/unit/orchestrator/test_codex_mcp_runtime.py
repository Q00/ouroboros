"""CodexMcpWorkerTransport — deterministic parsing/mapping (no live codex)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ouroboros.core.types import Result
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.orchestrator import codex_mcp_runtime as codex_mod
from ouroboros.orchestrator.adapter import (
    ParamSupport,
    SubagentOrchestration,
    is_leader_driven_worker,
)
from ouroboros.orchestrator.codex_mcp_runtime import (
    CodexMcpWorkerTransport,
    _map_permission_mode,
    build_codex_mcp_worker_runtime,
)
from ouroboros.orchestrator.worker_runtime import WorkerTurn


class TestPermissionMapping:
    @pytest.mark.parametrize(
        ("mode", "sandbox", "approval"),
        [
            (None, "workspace-write", "never"),
            ("acceptEdits", "workspace-write", "never"),
            ("bypassPermissions", "danger-full-access", "never"),
            ("read-only", "read-only", "on-request"),
            ("plan", "read-only", "on-request"),
        ],
    )
    def test_maps_to_codex_sandbox_approval(self, mode, sandbox, approval) -> None:
        assert _map_permission_mode(mode) == (sandbox, approval)

    def test_only_explicit_bypass_requests_danger_full_access(self) -> None:
        for mode in (None, "acceptEdits", "read-only", "plan", "weird"):
            assert _map_permission_mode(mode)[0] != "danger-full-access"
        assert _map_permission_mode("bypassPermissions") == ("danger-full-access", "never")


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

    def test_does_not_declare_targeted_resume(self) -> None:
        # codex mcp-server sessions are process-bound and the warm pool is closed
        # after each run → a persisted handle is always dead on reload. So this
        # runtime must NOT advertise resume (mirrors the dashboard-centric Claude
        # worker), unlike the disk-persisted codex exec / claude --resume backends.
        rt = build_codex_mcp_worker_runtime(cwd="/tmp")
        assert rt.capabilities.targeted_resume is False

    def test_normalizes_path_cwd(self, tmp_path: Path) -> None:
        rt = build_codex_mcp_worker_runtime(cwd=tmp_path)

        assert rt.working_directory == str(tmp_path)

    def test_resume_controls_are_declared_ignored(self) -> None:
        """codex-reply cannot retarget either model or reasoning effort."""
        rt = build_codex_mcp_worker_runtime(cwd="/tmp")
        assert rt.capabilities.model_override_support is ParamSupport.IGNORED
        assert rt.capabilities.reasoning_effort_support is ParamSupport.IGNORED

    @pytest.mark.asyncio
    async def test_does_not_emit_resumable_handle(self) -> None:
        # Even when a turn surfaces a live threadId, no RuntimeHandle is emitted:
        # ParallelExecutor must not persist a handle that resume() can only fail on.
        rt = build_codex_mcp_worker_runtime(cwd="/tmp")

        async def _fake_spawn(**_kwargs) -> WorkerTurn:
            return WorkerTurn(text="ok", session_id="process-bound-threadid")

        rt._transport.spawn = _fake_spawn  # type: ignore[method-assign]
        messages = [message async for message in rt.execute_task("hi")]
        assert [message.type for message in messages] == ["result"]
        assert messages[0].resume_handle is None
        assert messages[0].data["session_id"] == "process-bound-threadid"


class _RecordingActor:
    """Captures the codex tool arguments instead of spawning a real server."""

    last_args: dict = {}

    def __init__(self, server_config, *, idle_timeout) -> None:  # noqa: ARG002
        self.is_alive = True

    async def call(self, tool: str, arguments: dict):
        _RecordingActor.last_args = {"tool": tool, **arguments}
        return Result.ok(MCPToolResult(structured_content={"threadId": "t1", "content": "ok"}))

    async def aclose(self) -> None:
        self.is_alive = False


class TestResumeContract:
    @pytest.mark.asyncio
    async def test_resume_does_not_send_unsupported_model_or_effort(self, monkeypatch) -> None:
        monkeypatch.setattr(codex_mod, "MCPSessionActor", _RecordingActor)
        transport = CodexMcpWorkerTransport(cli_path="codex")
        turn = await transport.spawn(
            prompt="first",
            system_prompt=None,
            cwd="/tmp",
            permission_mode=None,
            model="gpt-5.5",
            reasoning_effort="high",
        )

        assert turn.session_id == "t1"
        _RecordingActor.last_args = {}
        await transport.resume(
            session_id="t1",
            prompt="again",
            model="gpt-5.6-sol",
            reasoning_effort="xhigh",
        )

        assert _RecordingActor.last_args == {
            "tool": "codex-reply",
            "threadId": "t1",
            "prompt": "again",
        }
        await transport.aclose()


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
