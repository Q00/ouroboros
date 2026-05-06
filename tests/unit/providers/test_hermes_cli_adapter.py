"""Unit tests for the Hermes CLI-backed LLM adapter."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from ouroboros.providers.base import CompletionConfig, Message, MessageRole
from ouroboros.providers.hermes_cli_adapter import HermesCliLLMAdapter


class _FakeProcess:
    """Minimal asyncio subprocess substitute for communicate-based adapters."""

    def __init__(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
    ) -> None:
        self._stdout = stdout.encode()
        self._stderr = stderr.encode()
        self.returncode = returncode
        self.terminated = False
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self.returncode


@pytest.mark.asyncio
async def test_complete_invokes_hermes_quiet_chat_and_parses_session() -> None:
    commands: list[tuple[str, ...]] = []
    process = _FakeProcess(stdout="Hello from Hermes\nsession_id: 20260507_120000_abcdef\n")

    async def _fake_exec(*cmd: str, **kwargs: object) -> _FakeProcess:
        commands.append(cmd)
        return process

    adapter = HermesCliLLMAdapter(cli_path="/usr/bin/hermes", cwd="/tmp/project")
    with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
        result = await adapter.complete(
            [Message(role=MessageRole.USER, content="Answer this")],
            CompletionConfig(model="default"),
        )

    assert result.is_ok
    assert result.value.content == "Hello from Hermes"
    assert result.value.raw_response["session_id"] == "20260507_120000_abcdef"
    assert commands
    assert commands[0][:7] == (
        "/usr/bin/hermes",
        "chat",
        "-Q",
        "--source",
        "tool",
        "--max-turns",
        "1",
    )


@pytest.mark.asyncio
async def test_complete_returns_provider_error_on_nonzero_exit() -> None:
    process = _FakeProcess(stderr="auth failed", returncode=2)

    async def _fake_exec(*cmd: str, **kwargs: object) -> _FakeProcess:
        return process

    adapter = HermesCliLLMAdapter(cli_path="/usr/bin/hermes", max_retries=1)
    with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
        result = await adapter.complete(
            [Message(role=MessageRole.USER, content="Answer this")],
            CompletionConfig(model="default"),
        )

    assert result.is_err
    assert result.error.provider == "hermes_cli"
    assert "exited with code 2" in result.error.message


def test_build_prompt_includes_tool_envelope() -> None:
    adapter = HermesCliLLMAdapter(
        cli_path=Path("/usr/bin/hermes"),
        allowed_tools=["Read", "Grep"],
    )

    prompt = adapter._build_prompt(
        [
            Message(role=MessageRole.SYSTEM, content="Be concise."),
            Message(role=MessageRole.USER, content="Question?"),
        ]
    )

    assert "<tool_envelope>" in prompt
    assert "Read, Grep" in prompt
    assert "Be concise." in prompt


@pytest.mark.asyncio
async def test_allowed_tools_returns_error_without_spawning() -> None:
    adapter = HermesCliLLMAdapter(cli_path="/usr/bin/hermes", allowed_tools=[])

    with patch("asyncio.create_subprocess_exec") as create_process:
        result = await adapter.complete(
            [Message(role=MessageRole.USER, content="Answer this")],
            CompletionConfig(model="default"),
        )

    assert result.is_err
    assert "does not support allowed_tools envelopes" in result.error.message
    create_process.assert_not_called()


@pytest.mark.asyncio
async def test_complete_forwards_configured_max_turns() -> None:
    commands: list[tuple[str, ...]] = []
    process = _FakeProcess(stdout="Done\n")

    async def _fake_exec(*cmd: str, **kwargs: object) -> _FakeProcess:
        commands.append(cmd)
        return process

    adapter = HermesCliLLMAdapter(cli_path="/usr/bin/hermes", max_turns=5)
    with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
        result = await adapter.complete(
            [Message(role=MessageRole.USER, content="Answer this")],
            CompletionConfig(model="default", max_turns=2),
        )

    assert result.is_ok
    assert "--max-turns" in commands[0]
    assert commands[0][commands[0].index("--max-turns") + 1] == "2"


def test_bare_cli_path_preserves_path_lookup() -> None:
    adapter = HermesCliLLMAdapter(cli_path="hermes")

    assert adapter._cli_path == Path("hermes")


def test_configured_bare_cli_path_preserves_path_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ouroboros.providers.hermes_cli_adapter.get_hermes_cli_path",
        lambda: "hermes",
    )

    adapter = HermesCliLLMAdapter()

    assert adapter._cli_path == Path("hermes")
