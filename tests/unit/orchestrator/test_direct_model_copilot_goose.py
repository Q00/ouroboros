"""Direct request-local model forwarding on Codex-derived CLI adapters."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from ouroboros.orchestrator.copilot_cli_runtime import CopilotCliRuntime
from ouroboros.orchestrator.goose_runtime import GooseCliRuntime
from tests.unit.orchestrator.test_goose_runtime import _FakeProcess


def _cli(name: str) -> str:
    return str(Path(os.environ["OUROBOROS_TEST_CLI_DIR"]) / name)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["copilot", "goose"])
async def test_direct_model_reaches_cli_without_mutating_default(tmp_path: Path, kind: str) -> None:
    runtime = (
        CopilotCliRuntime(cli_path=_cli("copilot"), cwd=str(tmp_path), model="base.1")
        if kind == "copilot"
        else GooseCliRuntime(cli_path=_cli("goose"), cwd=str(tmp_path), model="base.1")
    )
    commands: list[tuple[str, ...]] = []

    async def fake_exec(*command: str, **kwargs: object) -> _FakeProcess:
        del kwargs
        commands.append(command)
        return _FakeProcess([json.dumps({"type": "completed", "text": "done"})])

    with patch(
        "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
        side_effect=fake_exec,
    ):
        messages = [message async for message in runtime.execute_task("test", model="choice.1")]

    assert commands, [message.content for message in messages]
    flag = "--model"
    assert commands[0][commands[0].index(flag) + 1] == "choice.1"
    assert runtime._model == "base.1"


@pytest.mark.parametrize("kind", ["copilot", "goose"])
@pytest.mark.parametrize(
    ("requested", "expected"),
    [(None, "base.1"), ("choice.1", "choice.1"), ("default", None), ("", None)],
)
def test_request_model_precedence(kind: str, requested: str | None, expected: str | None) -> None:
    runtime = (
        CopilotCliRuntime(cli_path=_cli("copilot"), model="base.1")
        if kind == "copilot"
        else GooseCliRuntime(cli_path=_cli("goose"), model="base.1")
    )
    command = runtime._build_command("/tmp/unused", prompt="test", model=requested)
    assert (command[command.index("--model") + 1] if "--model" in command else None) == expected
    assert runtime._model == "base.1"


def test_goose_refuses_model_override_for_native_resume() -> None:
    runtime = GooseCliRuntime(cli_path=_cli("goose"))
    with pytest.raises(ValueError, match="model.*resume"):
        runtime._build_command("/tmp/unused", resume_session_id="existing", model="choice.1")


def test_copilot_refuses_model_override_with_custom_agent() -> None:
    runtime = CopilotCliRuntime(cli_path=_cli("copilot"), runtime_profile="worker")
    with pytest.raises(ValueError, match="model.*agent"):
        runtime._build_command("/tmp/unused", model="choice.1")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["copilot", "goose"])
async def test_concurrent_requests_do_not_share_model_state(tmp_path: Path, kind: str) -> None:
    import asyncio

    runtime = (
        CopilotCliRuntime(cli_path=_cli("copilot"), cwd=str(tmp_path), model="base.1")
        if kind == "copilot"
        else GooseCliRuntime(cli_path=_cli("goose"), cwd=str(tmp_path), model="base.1")
    )
    commands: list[tuple[str, ...]] = []

    async def fake_exec(*command: str, **kwargs: object) -> _FakeProcess:
        del kwargs
        await asyncio.sleep(0)
        commands.append(command)
        return _FakeProcess([json.dumps({"type": "completed", "text": "done"})])

    async def invoke(model: str) -> None:
        _ = [message async for message in runtime.execute_task("test", model=model)]

    with patch(
        "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
        side_effect=fake_exec,
    ):
        await asyncio.gather(invoke("first.1"), invoke("second.1"))
    assert len(commands) == 2
    assert {c[c.index("--model") + 1] for c in commands} == {"first.1", "second.1"}
    assert runtime._model == "base.1"


def test_copilot_refuses_model_override_with_handle_profile_agent() -> None:
    from dataclasses import replace

    runtime = CopilotCliRuntime(cli_path=_cli("copilot"))
    handle = runtime._build_runtime_handle("sess-1")
    assert handle is not None
    handle = replace(handle, metadata={"agent_runtime_profile": "worker"})
    with pytest.raises(ValueError, match="model.*agent"):
        runtime._build_command("/tmp/unused", model="choice.1", runtime_handle=handle)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["copilot", "goose"])
async def test_unsupported_override_does_not_spawn_cli(tmp_path: Path, kind: str) -> None:
    if kind == "copilot":
        runtime = CopilotCliRuntime(
            cli_path=_cli("copilot"), cwd=str(tmp_path), runtime_profile="worker"
        )
        kwargs = {"model": "choice.1"}
    else:
        runtime = GooseCliRuntime(cli_path=_cli("goose"), cwd=str(tmp_path))
        kwargs = {"model": "choice.1", "resume_session_id": "existing"}
    with patch("ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec") as spawn:
        messages = [message async for message in runtime.execute_task("test", **kwargs)]
    spawn.assert_not_called()
    assert messages and "unsupported" in messages[-1].content.lower()
    if kind == "goose":
        assert messages[-1].data.get("recoverable") is not True
        assert "recovery" not in messages[-1].data


@pytest.mark.parametrize("sentinel", ["default", ""])
def test_copilot_refuses_even_sentinel_override_with_custom_agent(sentinel: str) -> None:
    runtime = CopilotCliRuntime(cli_path=_cli("copilot"), runtime_profile="worker")
    with pytest.raises(ValueError, match="model.*agent"):
        runtime._build_command("/tmp/unused", model=sentinel)


@pytest.mark.asyncio
async def test_goose_resumed_handle_with_model_is_not_reported_as_retryable(tmp_path: Path) -> None:
    runtime = GooseCliRuntime(cli_path=_cli("goose"), cwd=str(tmp_path))
    handle = runtime._build_runtime_handle("existing")
    assert handle is not None
    with patch("ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec") as spawn:
        messages = [
            message
            async for message in runtime.execute_task(
                "test", model="choice.1", resume_handle=handle
            )
        ]
    spawn.assert_not_called()
    assert messages[-1].is_error
    assert messages[-1].data.get("recoverable") is not True
    assert "recovery" not in messages[-1].data


@pytest.mark.asyncio
async def test_copilot_agent_conflict_with_handle_is_not_retryable(tmp_path: Path) -> None:
    runtime = CopilotCliRuntime(
        cli_path=_cli("copilot"), cwd=str(tmp_path), runtime_profile="worker"
    )
    handle = runtime._build_runtime_handle("existing")
    assert handle is not None
    with patch("ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec") as spawn:
        messages = [
            message
            async for message in runtime.execute_task(
                "test", model="choice.1", resume_handle=handle
            )
        ]
    spawn.assert_not_called()
    assert messages[-1].is_error
    assert messages[-1].data.get("error_type") == "UnsupportedModelOverrideError"
    assert messages[-1].data.get("recoverable") is not True
    assert "recovery" not in messages[-1].data


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["copilot", "goose"])
async def test_other_bootstrap_failures_remain_retryable(tmp_path: Path, kind: str) -> None:
    runtime = (
        CopilotCliRuntime(cli_path=_cli("copilot"), cwd=str(tmp_path))
        if kind == "copilot"
        else GooseCliRuntime(cli_path=_cli("goose"), cwd=str(tmp_path))
    )
    handle = runtime._build_runtime_handle("existing")
    assert handle is not None
    with patch.object(runtime, "_build_command", side_effect=OSError("synthetic failure")):
        messages = [message async for message in runtime.execute_task("test", resume_handle=handle)]
    assert messages[-1].is_error
    assert messages[-1].data.get("recoverable") is True
    assert messages[-1].data["recovery"]["reason"] == "resume_bootstrap_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["initial", "retired"])
async def test_goose_new_or_retired_handle_allows_model(tmp_path: Path, mode: str) -> None:
    runtime = GooseCliRuntime(cli_path=_cli("goose"), cwd=str(tmp_path))
    handle = runtime._build_runtime_handle(None if mode == "initial" else "existing")
    assert handle is not None
    if mode == "retired":
        runtime._drift.epoch = 1
    commands: list[tuple[str, ...]] = []

    async def fake_exec(*command: str, **kwargs: object) -> _FakeProcess:
        del kwargs
        commands.append(command)
        return _FakeProcess([json.dumps({"type": "completed", "text": "done"})])

    with patch(
        "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
        side_effect=fake_exec,
    ):
        messages = [
            message
            async for message in runtime.execute_task(
                "test", model="choice.1", resume_handle=handle
            )
        ]
    assert commands, [message.content for message in messages]
    assert "--resume" not in commands[0]
    assert commands[0][commands[0].index("--model") + 1] == "choice.1"
