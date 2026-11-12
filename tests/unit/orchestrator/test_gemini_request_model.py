"""Gemini request models belong to each individual runtime invocation."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import patch

from ouroboros.orchestrator.adapter import ParamSupport
from ouroboros.orchestrator.gemini_cli_runtime import GeminiCLIRuntime
from tests.unit.orchestrator.test_antigravity_cli_runtime import _FakeProcess


async def test_explicit_request_models_are_forwarded_without_mutating_shared_runtime() -> None:
    cli = str(Path(os.environ["OUROBOROS_TEST_CLI_DIR"]) / "gemini")
    runtime = GeminiCLIRuntime(cli_path=cli, model="constructor-model")
    calls: list[tuple[str, ...]] = []

    async def fake_exec(*command: str, **kwargs: object) -> _FakeProcess:
        del kwargs
        calls.append(command)
        await asyncio.sleep(0)
        return _FakeProcess([json.dumps({"type": "result", "response": "ok"})])

    async def run(label: str, model: str | None):
        return [message async for message in runtime.execute_task(label, model=model)]

    with (
        patch(
            "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
            side_effect=fake_exec,
        ),
        patch.object(runtime, "_process_group_id", return_value=None),
    ):
        results = await asyncio.gather(
            run("first", "gemini-first"),
            run("second", "--model with spaces"),
            run("fallback", None),
            run("default", "default"),
            run("empty", ""),
            run("whitespace", "  "),
        )
    assert len(calls) == 6
    by_prompt = {call[call.index("--prompt") + 1]: call for call in calls}
    for prompt, expected in {
        "first": "gemini-first",
        "second": "--model with spaces",
        "fallback": "constructor-model",
    }.items():
        command = by_prompt[prompt]
        assert command[command.index("--model") + 1] == expected
    for prompt in ("default", "empty", "whitespace"):
        assert "--model" not in by_prompt[prompt]
    assert runtime._model == "constructor-model"
    assert runtime.capabilities.model_override_support is ParamSupport.IGNORED
    assert all(not any(message.is_error for message in messages) for messages in results)
