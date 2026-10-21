"""The constructor's model call keeps no session on disk (held-out values at rest).

The constructor's reply holds every held-out case. Codex, Claude and OMP
must use their no-persistence modes; unsupported runtimes are refused
before a model call.
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import patch

import pytest

from ouroboros.boundary.constructor import CheckConstructor
from ouroboros.boundary.constructor_session import (
    NOT_EPHEMERAL_PREFIX,
    disable_session_persistence,
)
from ouroboros.orchestrator.adapter import ClaudeAgentAdapter
from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime
from ouroboros.orchestrator.copilot_cli_runtime import CopilotCliRuntime
from tests.unit.boundary.test_constructor import FakeRuntime, _reply, _seed


def _codex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CodexCliRuntime:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    cli_path = tmp_path / "codex"
    cli_path.write_text("#!/bin/sh\necho codex 1.0\n", encoding="utf-8")
    cli_path.chmod(0o755)
    return CodexCliRuntime(cli_path=cli_path, cwd=str(tmp_path), model="gpt-6-luna")


def test_the_codex_constructor_call_runs_codex_exec_ephemeral(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _codex(tmp_path, monkeypatch)
    assert "--ephemeral" not in runtime._build_command(str(tmp_path / "last"))
    assert disable_session_persistence(runtime) is None
    command = runtime._build_command(str(tmp_path / "last"))
    assert command[1:3] == ["exec", "--ephemeral"]
    # Other Codex runtimes (workers) are unaffected.
    assert "--ephemeral" not in _codex(tmp_path / "w", monkeypatch)._build_command("/tmp/x")


def _mock_claude_sdk(options_sink: list[dict[str, Any]]) -> dict[str, ModuleType]:
    module = ModuleType("claude_agent_sdk")

    class _Options:
        def __init__(self, **kwargs: Any) -> None:
            options_sink.append(kwargs)

    class _HookMatcher:
        def __init__(self, **_kwargs: Any) -> None:
            pass

    async def query(*, prompt: str, options: Any):
        result = type("ResultMessage", (), {})()
        result.result, result.subtype = "ok", "success"
        yield result

    types_module = ModuleType("claude_agent_sdk.types")
    types_module.HookMatcher = _HookMatcher  # type: ignore[attr-defined]
    module.ClaudeAgentOptions = _Options  # type: ignore[attr-defined]
    module.query = query  # type: ignore[attr-defined]
    module.types = types_module  # type: ignore[attr-defined]
    return {"claude_agent_sdk": module, "claude_agent_sdk.types": types_module}


async def test_the_claude_constructor_call_disables_session_persistence() -> None:
    options: list[dict[str, Any]] = []
    plain = ClaudeAgentAdapter(api_key="test", cwd="/tmp/project")
    switched = ClaudeAgentAdapter(api_key="test", cwd="/tmp/project")
    assert disable_session_persistence(switched) is None
    with patch.dict("sys.modules", _mock_claude_sdk(options)):
        _ = [message async for message in plain.execute_task("hi")]
        _ = [message async for message in switched.execute_task("hi")]
    assert "extra_args" not in options[0]
    assert options[1]["extra_args"] == {"no-session-persistence": None}


@pytest.mark.parametrize("persist_sessions", [False, True])
def test_the_claude_cli_worker_constructor_call_disables_session_persistence(
    tmp_path: Path, persist_sessions: bool
) -> None:
    # The ``[mcp]`` profile runs Claude through ``--runtime claude-cli``
    # (backend ``claude_mcp``); its constructor call must not be refused, and it
    # must keep no session even when worker sessions are persisted.
    from ouroboros.orchestrator.claude_worker_runtime import build_claude_worker_runtime

    runtime = build_claude_worker_runtime(
        cli_path="claude", cwd=tmp_path, persist_sessions=persist_sessions
    )
    assert disable_session_persistence(runtime) is None
    command = runtime._transport._base_command(cwd=str(tmp_path))
    assert "--no-session-persistence" in command


def test_a_runtime_without_a_no_persistence_mode_is_refused(tmp_path: Path) -> None:
    copilot = object.__new__(CopilotCliRuntime)  # a Codex-family CLI without --ephemeral
    assert disable_session_persistence(copilot) == f"{NOT_EPHEMERAL_PREFIX}:copilot"
    assert disable_session_persistence(object()) == f"{NOT_EPHEMERAL_PREFIX}:object"


def test_a_plugin_runtime_is_refused_under_its_configured_backend() -> None:
    # the reason names the backend the person configured, not the
    # plugin runtime's class.
    from ouroboros.orchestrator.worker_runtime import LeaderDrivenWorkerRuntime

    # The warm Codex MCP session pool keeps its sessions under CODEX_HOME and
    # has no ephemeral mode.
    plugin = object.__new__(LeaderDrivenWorkerRuntime)
    plugin._runtime_backend = "codex_mcp"
    assert disable_session_persistence(plugin) == f"{NOT_EPHEMERAL_PREFIX}:codex_mcp"


class _PersistingRuntime(FakeRuntime):
    """A runtime with no way to keep its session off disk."""

    _runtime_backend = "gemini"


@pytest.mark.parametrize("per_criterion", [False, True])
async def test_the_constructor_switches_the_runtime_or_makes_no_call(
    tmp_path: Path, per_criterion: bool
) -> None:
    base = tmp_path / "base"
    base.mkdir()
    (base / "calc.py").write_text("def add(a, b):\n    return a - b\n")

    def constructor(runtime: FakeRuntime) -> CheckConstructor:
        return CheckConstructor(
            runtime_backend="codex",
            model="gpt-test",
            runtime_factory=lambda **_: runtime,
            system_prompt="SYSTEM",
            per_criterion=per_criterion,
        )

    codex_like = FakeRuntime(_reply())
    outcome = await constructor(codex_like).construct(_seed(), base)
    assert codex_like.calls and codex_like._exec_session_flags == ("--ephemeral",)
    assert outcome.package is not None or per_criterion  # per-criterion replies differ

    persisting = _PersistingRuntime(_reply())
    refused = await constructor(persisting).construct(_seed(), base)
    assert persisting.calls == []  # no model call at all
    assert refused.package is None
    assert f"{NOT_EPHEMERAL_PREFIX}:gemini" in (refused.failure_reason or "")


SECRET_REPLY = '{"oracles": [{"cases": [{"held_out": true, "args": {"value": 6173}}]}]}'


def _logged_text(events: list[dict[str, Any]]) -> str:
    return "\n".join(repr(event) for event in events)


async def test_the_claude_constructor_call_logs_the_reply_by_length_and_digest_only() -> None:
    import hashlib

    from structlog.testing import capture_logs

    def sdk() -> dict[str, ModuleType]:
        modules = _mock_claude_sdk([])

        async def query(*, prompt: str, options: Any):
            result = type("ResultMessage", (), {})()
            result.result, result.subtype = SECRET_REPLY, "success"
            yield result

        modules["claude_agent_sdk"].query = query  # type: ignore[attr-defined]
        return modules

    plain = ClaudeAgentAdapter(api_key="test", cwd="/tmp/project")
    switched = ClaudeAgentAdapter(api_key="test", cwd="/tmp/project")
    assert disable_session_persistence(switched) is None
    with patch.dict("sys.modules", sdk()), capture_logs() as worker_logs:
        _ = [message async for message in plain.execute_task("hi")]
    with patch.dict("sys.modules", sdk()), capture_logs() as constructor_logs:
        _ = [message async for message in switched.execute_task("hi")]
    # Control: the worker path logs a prefix of the reply on the same log line.
    assert "6173" in _logged_text(worker_logs)
    assert "6173" not in _logged_text(constructor_logs)
    result_lines = [
        e for e in constructor_logs if e["event"] == "orchestrator.adapter.result_message"
    ]
    assert result_lines and result_lines[0]["result_content"] == {
        "chars": len(SECRET_REPLY),
        "sha256": hashlib.sha256(SECRET_REPLY.encode()).hexdigest(),
    }


async def test_the_codex_constructor_call_logs_no_reply_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from structlog.testing import capture_logs

    from tests.unit.orchestrator.test_codex_cli_runtime import _FakeProcess

    runtime = _codex(tmp_path, monkeypatch)
    assert disable_session_persistence(runtime) is None

    async def fake_exec(*command: str, **_kwargs: object) -> _FakeProcess:
        output = command[command.index("--output-last-message") + 1]
        Path(output).write_text(SECRET_REPLY, encoding="utf-8")
        item = {"type": "agent_message", "content": [{"text": SECRET_REPLY}]}
        return _FakeProcess(
            stdout_lines=[
                json.dumps({"type": "thread.started", "thread_id": "t-1"}),
                json.dumps({"type": "item.completed", "item": item}),
            ],
            stderr_lines=[],
        )

    with (
        patch(
            "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
            side_effect=fake_exec,
        ),
        capture_logs() as logs,
    ):
        messages = [message async for message in runtime.execute_task("construct")]
    assert messages[-1].content == SECRET_REPLY  # the reply reached the caller
    assert logs and "6173" not in _logged_text(logs)


@pytest.mark.parametrize("per_criterion", [False, True])
async def test_omp_constructor_builds_package_without_persisting_or_logging_reply(
    tmp_path: Path, per_criterion: bool
) -> None:
    import json

    from structlog.testing import capture_logs

    from ouroboros.orchestrator.omp_runtime import OmpRuntime
    from tests.unit.orchestrator.test_omp_runtime import _FakeProcess

    base = tmp_path / "base"
    base.mkdir()
    (base / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    marker = "constructor-held-out-6173"
    reply = _reply().replace("readability is not mechanical", marker)
    commands: list[tuple[str, ...]] = []

    async def fake_exec(*command: str, **_kwargs: Any) -> _FakeProcess:
        commands.append(command)
        return _FakeProcess(
            stdout_lines=[
                json.dumps(
                    {
                        "type": "agent_end",
                        "messages": [{"role": "assistant", "content": reply}],
                    }
                )
            ],
            stderr_lines=[],
        )

    constructor = CheckConstructor(
        runtime_backend="omp",
        model=None,
        runtime_factory=lambda **kwargs: OmpRuntime(cli_path="omp", **kwargs),
        system_prompt="SYSTEM",
        per_criterion=per_criterion,
    )
    with (
        patch(
            "ouroboros.orchestrator.omp_runtime.asyncio.create_subprocess_exec",
            side_effect=fake_exec,
        ),
        capture_logs() as logs,
    ):
        outcome = await constructor.construct(_seed(), base)

    assert outcome.failure_reason is None
    assert outcome.package is not None
    assert commands and all("--no-session" in command for command in commands)
    assert marker not in _logged_text(logs)
    # The constructor switch is instance-local; workers still support resume.
    worker_command = OmpRuntime(cwd=base)._build_command(
        prompt="work", resume_session_id="worker-1"
    )
    assert "--no-session" not in worker_command
    assert worker_command[worker_command.index("--resume") + 1] == "worker-1"
