"""Cancellation cleanup for evaluation-time CLI subprocesses."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ouroboros.providers.base import CompletionConfig, Message, MessageRole
from ouroboros.providers.gemini_cli_adapter import GeminiCLIAdapter
from ouroboros.providers.hermes_cli_adapter import HermesCliLLMAdapter
from ouroboros.providers.kiro_adapter import KiroCodeAdapter


class _HangingProcess:
    returncode: int | None = None

    def __init__(self) -> None:
        self.terminated = False
        self.killed = False
        self.waited = False

    async def communicate(self):
        await asyncio.Event().wait()

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        self.waited = True
        return self.returncode or 0


async def _cancel_and_assert_cleaned(task: asyncio.Task[Any], process: _HangingProcess) -> None:
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.waited is True
    assert process.terminated or process.killed


@pytest.mark.asyncio
async def test_gemini_cli_cancellation_terminates_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _HangingProcess()

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        return process

    async def fake_collect_response(_process):
        await asyncio.Event().wait()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    adapter = GeminiCLIAdapter(cli_path="/usr/bin/gemini")
    adapter._collect_response = fake_collect_response  # type: ignore[method-assign]

    task = asyncio.create_task(adapter._execute_request("prompt", "gemini-test", None))

    await _cancel_and_assert_cleaned(task, process)


@pytest.mark.asyncio
async def test_hermes_cli_cancellation_terminates_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _HangingProcess()

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    adapter = HermesCliLLMAdapter(cli_path="/usr/bin/hermes", max_retries=1)

    task = asyncio.create_task(
        adapter._execute_request("prompt", "default", max_turns=1),
    )

    await _cancel_and_assert_cleaned(task, process)


@pytest.mark.asyncio
async def test_kiro_cli_cancellation_kills_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _HangingProcess()

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    adapter = KiroCodeAdapter(cli_path="/usr/bin/kiro-cli", max_retries=1)

    task = asyncio.create_task(
        adapter.complete(
            [Message(role=MessageRole.USER, content="prompt")],
            CompletionConfig(model="default"),
        ),
    )

    await _cancel_and_assert_cleaned(task, process)
