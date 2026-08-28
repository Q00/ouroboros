"""ClaudeWorkerTransport — deterministic parsing/mapping (no live claude)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ouroboros.orchestrator.adapter import (
    WORKER_CWD_UNAVAILABLE_MESSAGE,
    ParamSupport,
    ResolvedWorkerCwd,
    SubagentOrchestration,
    is_leader_driven_worker,
)
from ouroboros.orchestrator.claude_worker_runtime import (
    ClaudeWorkerTransport,
    build_claude_worker_runtime,
)
from ouroboros.orchestrator.frugality_evidence import harvest_token_spend
from ouroboros.orchestrator.worker_runtime import WorkerTurn


class TestParseTurn:
    def test_parses_session_and_result(self) -> None:
        payload = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "PONG",
                "session_id": "abc-123",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 2,
                    "cache_read_input_tokens": 40,
                },
            }
        )
        turn = ClaudeWorkerTransport._parse_turn(payload, "", 0)
        assert turn.text == "PONG"
        assert turn.session_id == "abc-123"
        assert turn.is_error is False
        assert turn.usage == {
            "input_tokens": 10,
            "output_tokens": 2,
            "cache_read_input_tokens": 40,
            "total_tokens": 52,
        }

    def test_is_error_flag_propagates(self) -> None:
        payload = json.dumps({"is_error": True, "result": "", "session_id": "s"})
        turn = ClaudeWorkerTransport._parse_turn(payload, "stderr text", 0)
        assert turn.is_error is True

    def test_nonzero_returncode_is_error(self) -> None:
        payload = json.dumps({"is_error": False, "result": "x", "session_id": "s"})
        turn = ClaudeWorkerTransport._parse_turn(payload, "", 1)
        assert turn.is_error is True

    def test_non_json_output_is_error(self) -> None:
        # "No conversation found with session ID" is plain text, not JSON.
        turn = ClaudeWorkerTransport._parse_turn("No conversation found", "", 0)
        assert turn.is_error is True
        assert turn.session_id is None
        assert "No conversation found" in (turn.error or "")

    def test_takes_last_json_line(self) -> None:
        out = "warning: something\n" + json.dumps({"result": "ok", "session_id": "z"})
        turn = ClaudeWorkerTransport._parse_turn(out, "", 0)
        assert turn.text == "ok"
        assert turn.session_id == "z"

    def test_parses_top_level_stream_event_array(self) -> None:
        out = json.dumps(
            [
                {"type": "system", "subtype": "init", "session_id": "array-session"},
                {
                    "type": "assistant",
                    "session_id": "array-session",
                    "message": {"content": [{"type": "text", "text": "draft"}]},
                },
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": "final",
                    "session_id": "array-session",
                    "usage": {"input_tokens": 4, "output_tokens": 2},
                },
            ]
        )

        turn = ClaudeWorkerTransport._parse_turn(out, "", 0)

        assert turn == WorkerTurn(
            text="final",
            session_id="array-session",
            usage={"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
        )

    def test_structured_error_uses_result_when_stderr_is_empty(self) -> None:
        out = json.dumps(
            {
                "type": "result",
                "is_error": True,
                "result": "maximum turns reached",
            }
        )

        turn = ClaudeWorkerTransport._parse_turn(out, "", 0)

        assert turn.is_error is True
        assert turn.error == "maximum turns reached"

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param(
                {
                    "type": "result",
                    "is_error": True,
                    "result": None,
                    "subtype": "error_max_turns",
                    "session_id": "error-session",
                    "usage": {"input_tokens": 9, "output_tokens": 2},
                },
                id="typed-null",
            ),
            pytest.param(
                {
                    "is_error": True,
                    "subtype": "error_max_turns",
                    "session_id": "error-session",
                    "usage": {"input_tokens": 9, "output_tokens": 2},
                },
                id="untyped-omitted",
            ),
        ],
    )
    def test_empty_structured_error_preserves_metadata(self, payload: dict[str, object]) -> None:
        turn = ClaudeWorkerTransport._parse_turn(json.dumps(payload), "", 0)

        assert turn == WorkerTurn(
            text="",
            session_id="error-session",
            is_error=True,
            error="error_max_turns",
            usage={"input_tokens": 9, "output_tokens": 2, "total_tokens": 11},
        )

    @pytest.mark.parametrize(
        ("stdout", "message"),
        [
            pytest.param(
                '{"type":"result","is_error":false,"result":"done","trace":' + "9" * 4301 + "}",
                "integer exceeds",
                id="oversized-integer",
            ),
            pytest.param(
                '{"type":"result","is_error":false,"result":"done",'
                '"usage":{"cache_read_input_tokens":1e9999}}',
                "non-finite JSON number",
                id="non-finite-secondary-usage",
            ),
            pytest.param(
                '{"type":"result","is_error":false,"result":"done",'
                '"usage":{"cache_read_input_tokens":' + "9" * 1000 + "}}",
                "bounded non-negative integer",
                id="thousand-digit-secondary-usage",
            ),
            pytest.param(
                '{"type":"result","is_error":false,"result":"done",'
                '"usage":{"prompt_tokens":' + "9" * 1000 + "}}",
                "bounded non-negative integer",
                id="thousand-digit-primary-alias",
            ),
            pytest.param(
                '{"type":"result","is_error":false,"result":"done",'
                '"usage":{"prompt_tokens":true,"completion_tokens":2}}',
                "bounded non-negative integer",
                id="malformed-primary-alias",
            ),
        ],
    )
    def test_hostile_number_becomes_structured_worker_error(
        self, stdout: str, message: str
    ) -> None:
        turn = ClaudeWorkerTransport._parse_turn(stdout, "", 0)

        assert turn.is_error is True
        assert turn.session_id is None
        assert message in (turn.error or "")

    def test_nonzero_exit_cannot_be_overridden_by_success_payload(self) -> None:
        out = json.dumps({"type": "result", "is_error": False, "result": "looks okay"})

        turn = ClaudeWorkerTransport._parse_turn(out, "process failed", 7)

        assert turn.is_error is True
        assert turn.text == ""
        assert turn.error == "process failed"

    def test_nonzero_exit_without_stderr_rejects_stale_success_diagnostic(self) -> None:
        out = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "stale success",
                "session_id": "stale-session",
            }
        )

        turn = ClaudeWorkerTransport._parse_turn(out, "", 7)

        assert turn.is_error is True
        assert turn.text == ""
        assert turn.error == "claude exited with status 7"


class TestPermissionArgs:
    def test_bypass_maps_to_skip_permissions(self) -> None:
        assert ClaudeWorkerTransport._permission_args("bypassPermissions") == [
            "--dangerously-skip-permissions"
        ]

    def test_accept_edits_maps_to_permission_mode(self) -> None:
        assert ClaudeWorkerTransport._permission_args("acceptEdits") == [
            "--permission-mode",
            "acceptEdits",
        ]

    def test_none_yields_no_args(self) -> None:
        assert ClaudeWorkerTransport._permission_args(None) == []
        assert ClaudeWorkerTransport._permission_args("") == []


class TestRuntimeWiring:
    def test_builds_leader_driven_runtime(self) -> None:
        rt = build_claude_worker_runtime(cwd="/tmp")
        assert rt.runtime_backend == "claude_mcp"
        caps = rt.capabilities
        assert caps.subagent_orchestration is SubagentOrchestration.EXTERNAL_LEADER_DRIVEN
        assert is_leader_driven_worker(caps) is True
        assert caps.targeted_resume is False

    def test_persisted_runtime_declares_targeted_resume(self) -> None:
        rt = build_claude_worker_runtime(cwd="/tmp", persist_sessions=True)
        assert rt.capabilities.targeted_resume is True

    @pytest.mark.asyncio
    async def test_nonzero_exit_stderr_is_public_provider_error_message(
        self, tmp_path: Path
    ) -> None:
        runtime = build_claude_worker_runtime(cli_path="claude", cwd=tmp_path)
        process = AsyncMock()
        process.returncode = 7
        process.communicate.return_value = (
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": "stale success",
                    "session_id": "stale-session",
                }
            ).encode(),
            b"error: authentication required",
        )

        with patch("asyncio.create_subprocess_exec", return_value=process):
            result = await runtime.execute_task_to_result("do the work")

        assert result.is_err
        assert result.error.message == "error: authentication required"

    def test_normalizes_path_cwd(self, tmp_path: Path) -> None:
        rt = build_claude_worker_runtime(cwd=tmp_path, persist_sessions=True)

        assert rt.working_directory == str(tmp_path)

    def test_omitted_cwd_survives_unavailable_process_cwd(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[int] = []

        def unavailable_cwd() -> str:
            calls.append(len(calls))
            if len(calls) == 1:
                raise FileNotFoundError
            return str(tmp_path / f"moving-cwd-{len(calls)}")

        monkeypatch.setattr("ouroboros.orchestrator.adapter.os.getcwd", unavailable_cwd)

        runtime = build_claude_worker_runtime(persist_sessions=True)
        assert runtime.working_directory is None
        assert runtime._transport._cwd is None
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_unresolved_cwd_fails_before_later_process_cwd_or_transport(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[int] = []

        def moving_cwd() -> str:
            calls.append(len(calls))
            if len(calls) == 1:
                raise FileNotFoundError
            return str(tmp_path / "unselected-later-cwd")

        monkeypatch.setattr("ouroboros.orchestrator.adapter.os.getcwd", moving_cwd)
        runtime = build_claude_worker_runtime(persist_sessions=True)

        async def unexpected_spawn(**_kwargs) -> WorkerTurn:
            raise AssertionError("transport must not run without a resolved cwd")

        runtime._transport.spawn = unexpected_spawn  # type: ignore[method-assign]
        messages = [message async for message in runtime.execute_task("must not run")]

        assert len(messages) == 1
        assert messages[0].data["error_type"] == "WorkerCwdUnavailable"
        assert calls == [0]

    @pytest.mark.asyncio
    async def test_transport_rejects_none_cwd_before_subprocess(self) -> None:
        transport = ClaudeWorkerTransport(cli_path="claude", cwd=ResolvedWorkerCwd(None))

        turn = await transport.spawn(
            prompt="must not run",
            system_prompt=None,
            cwd=None,
            permission_mode=None,
            model=None,
            reasoning_effort=None,
        )

        assert turn.is_error
        assert turn.error == WORKER_CWD_UNAVAILABLE_MESSAGE

    @pytest.mark.parametrize("cwd", [None, "workspace"], ids=["omitted", "relative"])
    @pytest.mark.asyncio
    async def test_cwd_is_resolved_once_and_shared_by_spawn_and_persistent_resume(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        cwd: str | None,
    ) -> None:
        launch_cwd = tmp_path / "launch"
        later_cwd = tmp_path / "later"
        launch_cwd.mkdir()
        later_cwd.mkdir()
        resolved_cwd = launch_cwd if cwd is None else launch_cwd / cwd
        resolved_cwd.mkdir(exist_ok=True)
        monkeypatch.chdir(launch_cwd)
        runtime = build_claude_worker_runtime(cwd=cwd, persist_sessions=True)
        transport = runtime._transport
        observed_cwds: list[str | None] = []

        async def fake_run(command, prompt, cwd):
            observed_cwds.append(cwd)
            return WorkerTurn(text="ok", session_id="session-1")

        transport._run = fake_run  # type: ignore[method-assign]
        first = [message async for message in runtime.execute_task("first")]
        resume_handle = first[-1].resume_handle
        assert resume_handle is not None
        monkeypatch.chdir(later_cwd)
        _ = [
            message
            async for message in runtime.execute_task(
                "resume",
                resume_handle=resume_handle,
            )
        ]

        assert runtime.working_directory == str(resolved_cwd)
        assert observed_cwds == [str(resolved_cwd), str(resolved_cwd)]

    def test_exposes_effective_cli_path(self) -> None:
        rt = build_claude_worker_runtime(cli_path="/tmp/claude", cwd="/tmp")

        assert rt.cli_path == "/tmp/claude"

    def test_declares_native_model_override(self) -> None:
        # The transport routes a per-call model to ``claude --model``, so the
        # worker enforces a model-tier override natively (RFC #1405 sibling).
        rt = build_claude_worker_runtime(cwd="/tmp")
        assert rt.capabilities.model_override_support is ParamSupport.NATIVE

    @pytest.mark.asyncio
    async def test_default_runtime_does_not_emit_resumable_handle(self) -> None:
        rt = build_claude_worker_runtime(cwd="/tmp")
        transport = rt._transport

        async def _fake_spawn(**_kwargs) -> WorkerTurn:
            return WorkerTurn(text="ok", session_id="nonpersisted-id")

        transport.spawn = _fake_spawn  # type: ignore[method-assign]
        messages = [message async for message in rt.execute_task("hi")]
        assert [message.type for message in messages] == ["result"]
        assert messages[0].resume_handle is None
        assert messages[0].data["session_id"] == "nonpersisted-id"

    @pytest.mark.asyncio
    async def test_runtime_result_propagates_cli_usage(self) -> None:
        rt = build_claude_worker_runtime(cwd="/tmp")
        transport = rt._transport

        async def _fake_spawn(**_kwargs) -> WorkerTurn:
            return WorkerTurn(
                text="ok",
                session_id="worker-id",
                usage={
                    "input_tokens": 5,
                    "output_tokens": 1,
                    "cache_creation_input_tokens": 20,
                },
            )

        transport.spawn = _fake_spawn  # type: ignore[method-assign]

        messages = [message async for message in rt.execute_task("hi")]

        assert messages[-1].data["usage"] == {
            "input_tokens": 5,
            "output_tokens": 1,
            "cache_creation_input_tokens": 20,
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "stdout",
        [
            pytest.param(
                '{"type":"result","is_error":false,"result":"done",'
                '"usage":{"cache_read_input_tokens":' + "9" * 1000 + "}}",
                id="huge-secondary",
            ),
            pytest.param(
                '{"type":"result","is_error":false,"result":"done",'
                '"usage":{"prompt_tokens":' + "9" * 1000 + "}}",
                id="huge-alias",
            ),
            pytest.param(
                '{"type":"result","is_error":false,"result":"done",'
                '"usage":{"prompt_tokens":true,"completion_tokens":2}}',
                id="malformed-alias",
            ),
        ],
    )
    async def test_hostile_usage_never_reaches_frugality_consumer(self, stdout: str) -> None:
        turn = ClaudeWorkerTransport._parse_turn(stdout, "", 0)
        assert turn.is_error is True
        assert turn.usage is None

        rt = build_claude_worker_runtime(cwd="/tmp")
        transport = rt._transport

        async def _fake_spawn(**_kwargs) -> WorkerTurn:
            return turn

        transport.spawn = _fake_spawn  # type: ignore[method-assign]
        messages = [message async for message in rt.execute_task("hi")]

        assert messages[-1].is_error is True
        assert "usage" not in messages[-1].data
        assert harvest_token_spend(messages) is None

    @pytest.mark.asyncio
    async def test_multi_turn_cli_usage_reaches_frugality_as_request_total(self) -> None:
        turn = ClaudeWorkerTransport._parse_turn(
            json.dumps(
                [
                    {
                        "type": "assistant",
                        "message": {"usage": {"input_tokens": 100, "output_tokens": 20}},
                    },
                    {
                        "type": "assistant",
                        "message": {"usage": {"input_tokens": 10, "output_tokens": 2}},
                    },
                    {
                        "type": "result",
                        "is_error": False,
                        "result": "ok",
                    },
                ]
            ),
            "",
            0,
        )
        assert turn.is_error is False
        assert turn.usage == {
            "input_tokens": 110,
            "output_tokens": 22,
            "total_tokens": 132,
        }

        rt = build_claude_worker_runtime(cwd="/tmp")
        transport = rt._transport

        async def _fake_spawn(**_kwargs) -> WorkerTurn:
            return turn

        transport.spawn = _fake_spawn  # type: ignore[method-assign]
        messages = [message async for message in rt.execute_task("hi")]

        assert messages[-1].data["usage"] == {
            "input_tokens": 110,
            "output_tokens": 22,
            "total_tokens": 132,
        }
        assert harvest_token_spend(messages) == (
            132.0,
            {"input_tokens": 110.0, "output_tokens": 22.0, "total_tokens": 132.0},
        )

    @pytest.mark.asyncio
    async def test_total_only_cli_usage_reaches_final_receipt_without_allocation(self) -> None:
        turn = ClaudeWorkerTransport._parse_turn(
            json.dumps(
                {
                    "type": "result",
                    "is_error": False,
                    "result": "ok",
                    "usage": {"total_tokens": 132},
                }
            ),
            "",
            0,
        )
        assert turn.is_error is False
        assert turn.usage == {"total_tokens": 132}

        rt = build_claude_worker_runtime(cwd="/tmp")
        transport = rt._transport

        async def _fake_spawn(**_kwargs) -> WorkerTurn:
            return turn

        transport.spawn = _fake_spawn  # type: ignore[method-assign]
        messages = [message async for message in rt.execute_task("hi")]

        assert messages[-1].data["usage"] == {"total_tokens": 132}
        assert harvest_token_spend(messages) == (
            132.0,
            {"total_tokens": 132.0},
        )

    @pytest.mark.asyncio
    async def test_bounded_all_counter_usage_preserves_spend_semantics(self) -> None:
        usage = {
            "input_tokens": 5,
            "output_tokens": 1,
            "cache_creation_input_tokens": 20,
            "cache_read_input_tokens": 40,
            "cached_input_tokens": 7,
            "total_tokens": 66,
        }
        turn = ClaudeWorkerTransport._parse_turn(
            json.dumps(
                {
                    "type": "result",
                    "is_error": False,
                    "result": "ok",
                    "usage": usage,
                }
            ),
            "",
            0,
        )
        assert turn.is_error is False
        assert turn.usage == usage

        rt = build_claude_worker_runtime(cwd="/tmp")
        transport = rt._transport

        async def _fake_spawn(**_kwargs) -> WorkerTurn:
            return turn

        transport.spawn = _fake_spawn  # type: ignore[method-assign]
        messages = [message async for message in rt.execute_task("hi")]

        assert messages[-1].data["usage"] == usage
        assert harvest_token_spend(messages) == (
            66.0,
            {
                "input_tokens": 5.0,
                "output_tokens": 1.0,
                "cache_creation_input_tokens": 20.0,
                "cache_read_input_tokens": 40.0,
                "cached_input_tokens": 7.0,
                "total_tokens": 66.0,
            },
        )

    def test_resume_is_cwd_pinned(self) -> None:
        # The transport must pin cwd so --resume targets the session's store.
        transport = ClaudeWorkerTransport(cli_path="claude", cwd="/project/x")
        assert transport._cwd == "/project/x"


class TestNameArgs:
    def test_label_becomes_name_flag(self) -> None:
        assert ClaudeWorkerTransport._name_args("ooo: build x") == ["--name", "ooo: build x"]

    def test_blank_label_yields_no_flag(self) -> None:
        assert ClaudeWorkerTransport._name_args(None) == []
        assert ClaudeWorkerTransport._name_args("  ") == []


async def _capture_spawn(transport: ClaudeWorkerTransport, **kwargs) -> list[str]:
    captured: dict[str, list[str]] = {}

    async def _fake_run(command: list[str], prompt: str, cwd: str | None) -> WorkerTurn:
        captured["command"] = command
        return WorkerTurn(text="ok", session_id="child-1")

    transport._run = _fake_run  # type: ignore[method-assign]
    await transport.spawn(
        prompt=kwargs.get("prompt", "hi"),
        system_prompt=kwargs.get("system_prompt"),
        cwd=kwargs.get("cwd"),
        permission_mode=kwargs.get("permission_mode"),
        model=kwargs.get("model"),
        reasoning_effort=kwargs.get("reasoning_effort"),
        fork_from_session_id=kwargs.get("fork_from_session_id"),
        label=kwargs.get("label"),
    )
    return captured["command"]


async def _capture_resume(transport: ClaudeWorkerTransport, **kwargs) -> list[str]:
    captured: dict[str, list[str]] = {}

    async def _fake_run(command: list[str], prompt: str, cwd: str | None) -> WorkerTurn:
        captured["command"] = command
        return WorkerTurn(text="ok", session_id="s1")

    transport._run = _fake_run  # type: ignore[method-assign]
    await transport.resume(
        session_id=kwargs.get("session_id", "s1"),
        prompt=kwargs.get("prompt", "again"),
        permission_mode=kwargs.get("permission_mode"),
        model=kwargs.get("model"),
        reasoning_effort=kwargs.get("reasoning_effort"),
    )
    return captured["command"]


class TestDashboardCentricDefault:
    """Default (persist_sessions=False): no /resume flooding. Workers run with
    --no-session-persistence and never fork/--name; the dashboard is the view."""

    def test_base_command_disables_persistence_by_default(self) -> None:
        transport = ClaudeWorkerTransport(cli_path="claude")
        assert "--no-session-persistence" in transport._base_command(cwd="/tmp")

    @pytest.mark.asyncio
    async def test_default_spawn_does_not_fork_or_name(self) -> None:
        transport = ClaudeWorkerTransport(cli_path="claude")
        command = await _capture_spawn(
            transport, fork_from_session_id="parent-live", label="ooo: build x"
        )
        assert "--no-session-persistence" in command
        assert "--fork-session" not in command
        assert "--resume" not in command
        assert "--name" not in command

    @pytest.mark.asyncio
    async def test_default_resume_fails_clearly(self) -> None:
        transport = ClaudeWorkerTransport(cli_path="claude")
        turn = await transport.resume(session_id="s1", prompt="again")
        assert turn.is_error
        assert "non-persisted" in (turn.error or "")


class TestForkAndLabelSpawnOptIn:
    """Opt-in (persist_sessions=True): persist + fork host + --name → visible and
    resumable in /resume."""

    def test_base_command_persists_when_opted_in(self) -> None:
        transport = ClaudeWorkerTransport(cli_path="claude", persist_sessions=True)
        assert "--no-session-persistence" not in transport._base_command(cwd="/tmp")

    @pytest.mark.asyncio
    async def test_fork_from_host_session_builds_fork_command(self) -> None:
        transport = ClaudeWorkerTransport(cli_path="claude", persist_sessions=True)
        command = await _capture_spawn(
            transport, fork_from_session_id="parent-live", label="ooo: build x"
        )
        assert "--resume" in command
        assert command[command.index("--resume") + 1] == "parent-live"
        assert "--fork-session" in command
        assert command[command.index("--name") + 1] == "ooo: build x"

    @pytest.mark.asyncio
    async def test_fresh_spawn_labels_without_forking(self) -> None:
        transport = ClaudeWorkerTransport(cli_path="claude", persist_sessions=True)
        command = await _capture_spawn(transport, label="ooo: ship it")
        assert "--fork-session" not in command
        assert "--resume" not in command
        assert command[command.index("--name") + 1] == "ooo: ship it"


class TestPerCallModelArg:
    """The per-call model (routed by frugality tiering) reaches ``claude --model``.
    ``LeaderDrivenWorkerRuntime`` resolves ``model or self._model`` before spawn, so
    the transport only needs to forward whatever value it is handed."""

    @pytest.mark.asyncio
    async def test_model_becomes_model_flag(self) -> None:
        transport = ClaudeWorkerTransport(cli_path="claude")
        command = await _capture_spawn(transport, model="claude-haiku-4-5")
        assert command[command.index("--model") + 1] == "claude-haiku-4-5"

    @pytest.mark.asyncio
    async def test_default_sentinel_emits_no_model_flag(self) -> None:
        transport = ClaudeWorkerTransport(cli_path="claude")
        command = await _capture_spawn(transport, model="default")
        assert "--model" not in command

    @pytest.mark.asyncio
    async def test_no_model_emits_no_model_flag(self) -> None:
        transport = ClaudeWorkerTransport(cli_path="claude")
        command = await _capture_spawn(transport)
        assert "--model" not in command


class TestPersistedResumeControls:
    @pytest.mark.asyncio
    async def test_resume_enforces_bypass_permission_flag(self) -> None:
        transport = ClaudeWorkerTransport(cli_path="claude", persist_sessions=True)
        command = await _capture_resume(
            transport,
            permission_mode="bypassPermissions",
        )

        assert "--dangerously-skip-permissions" in command

    @pytest.mark.asyncio
    async def test_resume_enforces_model_and_effort_flags(self) -> None:
        transport = ClaudeWorkerTransport(cli_path="claude", persist_sessions=True)
        command = await _capture_resume(
            transport,
            model="claude-haiku-4-5",
            reasoning_effort="high",
        )

        assert command[command.index("--resume") + 1] == "s1"
        assert command[command.index("--model") + 1] == "claude-haiku-4-5"
        assert command[command.index("--effort") + 1] == "high"

    @pytest.mark.asyncio
    async def test_resume_omits_default_model_and_unsupported_effort(self) -> None:
        transport = ClaudeWorkerTransport(cli_path="claude", persist_sessions=True)
        command = await _capture_resume(
            transport,
            model="default",
            reasoning_effort="unbounded",
        )

        assert "--model" not in command
        assert "--effort" not in command


class TestContextReferenceAddDirs:
    """C4 native context channel: existing context_references dirs become
    ``--add-dir`` grants (deduped, cap 8). Absent ⇒ byte-identical to pre-C4."""

    def test_no_add_dir_flag_when_no_references(self) -> None:
        transport = ClaudeWorkerTransport(cli_path="claude", cwd="/tmp")
        assert "--add-dir" not in transport._base_command(cwd="/tmp")

    def test_existing_dirs_become_add_dir_grants(self, tmp_path) -> None:
        d1 = tmp_path / "pkg-a"
        d2 = tmp_path / "pkg-b"
        d1.mkdir()
        d2.mkdir()
        transport = ClaudeWorkerTransport(
            cli_path="claude",
            cwd=str(tmp_path),
            context_reference_dirs=[str(d1), str(d2)],
        )
        command = transport._base_command(cwd=str(tmp_path))
        add_dir_values = [command[i + 1] for i, tok in enumerate(command) if tok == "--add-dir"]
        assert add_dir_values == [str(d1.resolve()), str(d2.resolve())]

    def test_nonexistent_dirs_and_files_are_skipped(self, tmp_path) -> None:
        real = tmp_path / "real"
        real.mkdir()
        a_file = tmp_path / "notes.md"
        a_file.write_text("x", encoding="utf-8")
        transport = ClaudeWorkerTransport(
            cli_path="claude",
            cwd=str(tmp_path),
            context_reference_dirs=[str(real), str(tmp_path / "missing"), str(a_file)],
        )
        command = transport._base_command(cwd=str(tmp_path))
        add_dir_values = [command[i + 1] for i, tok in enumerate(command) if tok == "--add-dir"]
        assert add_dir_values == [str(real.resolve())]

    def test_relative_reference_resolves_against_cwd(self, tmp_path) -> None:
        (tmp_path / "sub").mkdir()
        transport = ClaudeWorkerTransport(
            cli_path="claude",
            cwd=str(tmp_path),
            context_reference_dirs=["sub"],
        )
        command = transport._base_command(cwd=str(tmp_path))
        assert command[command.index("--add-dir") + 1] == str((tmp_path / "sub").resolve())

    def test_duplicates_deduped_and_capped(self, tmp_path) -> None:
        dirs: list[str] = []
        for i in range(12):
            d = tmp_path / f"d{i}"
            d.mkdir()
            dirs.append(str(d))
        # Feed a duplicate of the first plus 12 uniques; expect the 8-cap to hold.
        transport = ClaudeWorkerTransport(
            cli_path="claude",
            cwd=str(tmp_path),
            context_reference_dirs=[dirs[0], *dirs],
        )
        command = transport._base_command(cwd=str(tmp_path))
        add_dir_values = [command[i + 1] for i, tok in enumerate(command) if tok == "--add-dir"]
        assert len(add_dir_values) == 8
        assert add_dir_values == [str((tmp_path / f"d{i}").resolve()) for i in range(8)]

    def test_factory_threads_context_reference_dirs(self, tmp_path) -> None:
        d = tmp_path / "ref"
        d.mkdir()
        rt = build_claude_worker_runtime(cwd=str(tmp_path), context_reference_dirs=[str(d)])
        command = rt._transport._base_command(cwd=str(tmp_path))
        assert command[command.index("--add-dir") + 1] == str(d.resolve())


class TestRecursionHardening:
    """The claude worker must deny ouroboros tools + set the depth guard, while
    preserving native passthrough of every other MCP server."""

    def test_base_command_denies_ouroboros_tools(self) -> None:
        transport = ClaudeWorkerTransport(cli_path="claude")
        command = transport._base_command(cwd="/tmp")
        assert "--disallowedTools" in command
        denied = command[command.index("--disallowedTools") + 1]
        # Both plain and plugin-namespaced registrations are denied.
        assert "mcp__ouroboros" in denied
        assert "mcp__plugin_ouroboros_ouroboros" in denied

    def test_empty_disallow_list_sends_no_flag(self) -> None:
        transport = ClaudeWorkerTransport(cli_path="claude", disallowed_tools=())
        assert "--disallowedTools" not in transport._base_command(cwd="/tmp")

    def test_child_env_strips_markers_and_sets_depth_guard(self, monkeypatch) -> None:
        monkeypatch.setenv("OUROBOROS_AGENT_RUNTIME", "claude")
        monkeypatch.setenv("OUROBOROS_LLM_BACKEND", "claude")
        monkeypatch.setenv("CLAUDECODE", "1")
        monkeypatch.delenv("_OUROBOROS_DEPTH", raising=False)
        env = ClaudeWorkerTransport._child_env()
        # Discovery markers + nested-session marker stripped.
        assert "OUROBOROS_AGENT_RUNTIME" not in env
        assert "OUROBOROS_LLM_BACKEND" not in env
        assert "CLAUDECODE" not in env
        # Depth guard incremented (0 → 1).
        assert env["_OUROBOROS_DEPTH"] == "1"
