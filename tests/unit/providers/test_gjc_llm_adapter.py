"""Unit tests for the GJC LLM adapter."""

import json
from typing import Any
from unittest.mock import patch

import pytest

from ouroboros.providers.base import CompletionConfig, Message, MessageRole
from ouroboros.providers.gjc_llm_adapter import GjcLLMAdapter


class _FakeStream:
    def __init__(self, text: str = "") -> None:
        self._buffer = text.encode("utf-8")
        self._cursor = 0

    async def read(self, chunk_size: int = 16384) -> bytes:
        if self._cursor >= len(self._buffer):
            return b""
        next_cursor = min(self._cursor + chunk_size, len(self._buffer))
        chunk = self._buffer[self._cursor : next_cursor]
        self._cursor = next_cursor
        return chunk


class _FakeStdin:
    def __init__(self) -> None:
        self.writes: list[str] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(data.decode("utf-8"))

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    def __init__(self, *, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdin = _FakeStdin()
        self.stdout = _FakeStream(stdout)
        self.stderr = _FakeStream(stderr)
        self._returncode = returncode
        self.returncode = None
        self.terminated = False

    async def wait(self) -> int:
        self.returncode = self._returncode
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.terminated = True


class _ProcessFactory:
    def __init__(self, *processes: _FakeProcess) -> None:
        self.processes = list(processes)
        self.created: list[_FakeProcess] = []
        self.commands: list[tuple[str, ...]] = []

    async def __call__(self, *command: str, **_kwargs: Any) -> _FakeProcess:
        self.commands.append(command)
        process = self.processes.pop(0)
        self.created.append(process)
        return process


def _gjc_jsonl(*events: dict[str, object]) -> str:
    return "".join(f"{json.dumps(event)}\n" for event in events)


def _ready() -> dict[str, object]:
    return {"type": "ready"}


def _ack(command_id: str, *, success: bool = True, command: str = "prompt") -> dict[str, object]:
    return {"id": command_id, "type": "response", "command": command, "success": success}


def _agent_end(prompt_id: str, content: str) -> dict[str, object]:
    return {
        "id": prompt_id,
        "type": "agent_end",
        "messages": [{"role": "assistant", "content": content}],
    }


@pytest.mark.asyncio
async def test_normal_completion_uses_gjc_rpc_and_closes_stdin() -> None:
    process = _FakeProcess(
        stdout=_gjc_jsonl(
            _ready(),
            _ack("prompt-1"),
            _ack("ignored"),
            {"id": "unrelated", "type": "message_update", "delta": "ignore"},
            {"id": "prompt-1", "type": "message_update", "delta": "Hel"},
            {"id": "prompt-1", "type": "message_update", "delta": "lo"},
            _agent_end("prompt-1", "Hello"),
        )
    )
    factory = _ProcessFactory(process)
    adapter = GjcLLMAdapter(cli_path="/tmp/gjc", cwd="/tmp/project")

    with (
        patch("ouroboros.providers.gjc_llm_adapter.asyncio.create_subprocess_exec", factory),
        patch(
            "ouroboros.providers.gjc_llm_adapter.uuid4", return_value=type("U", (), {"hex": "1"})()
        ),
    ):
        result = await adapter.complete(
            [Message(role=MessageRole.USER, content="Say hello")],
            CompletionConfig(model="default"),
        )

    assert result.is_ok
    assert result.value.content == "Hello"
    assert factory.commands == [("/tmp/gjc", "--mode", "rpc")]
    assert json.loads(process.stdin.writes[0]) == {
        "id": "prompt-1",
        "type": "prompt",
        "message": "user: Say hello",
    }
    assert process.stdin.closed is True


@pytest.mark.asyncio
async def test_json_object_extraction_injects_directive() -> None:
    process = _FakeProcess(
        stdout=_gjc_jsonl(
            _ready(),
            _ack("prompt-1"),
            _agent_end("prompt-1", 'Sure:\n```json\n{"approved": true}\n```'),
        )
    )
    factory = _ProcessFactory(process)
    adapter = GjcLLMAdapter(cli_path="/tmp/gjc", cwd="/tmp/project")

    with (
        patch("ouroboros.providers.gjc_llm_adapter.asyncio.create_subprocess_exec", factory),
        patch(
            "ouroboros.providers.gjc_llm_adapter.uuid4", return_value=type("U", (), {"hex": "1"})()
        ),
    ):
        result = await adapter.complete(
            [Message(role=MessageRole.USER, content="Return JSON")],
            CompletionConfig(model="default", response_format={"type": "json_object"}),
        )

    assert result.is_ok
    assert result.value.content == '{"approved": true}'
    prompt = json.loads(process.stdin.writes[0])["message"]
    assert "ONLY a valid JSON object" in prompt


@pytest.mark.asyncio
async def test_json_schema_invalid_retries_then_success() -> None:
    first = _FakeProcess(
        stdout=_gjc_jsonl(_ready(), _ack("prompt-1"), _agent_end("prompt-1", '{"approved": "yes"}'))
    )
    second = _FakeProcess(
        stdout=_gjc_jsonl(_ready(), _ack("prompt-2"), _agent_end("prompt-2", '{"approved": true}'))
    )
    factory = _ProcessFactory(first, second)
    uuids = [type("U", (), {"hex": "1"})(), type("U", (), {"hex": "2"})()]
    adapter = GjcLLMAdapter(cli_path="/tmp/gjc", cwd="/tmp/project", max_retries=2)

    with (
        patch("ouroboros.providers.gjc_llm_adapter.asyncio.create_subprocess_exec", factory),
        patch("ouroboros.providers.gjc_llm_adapter.uuid4", side_effect=uuids),
    ):
        result = await adapter.complete(
            [Message(role=MessageRole.USER, content="Return verdict")],
            CompletionConfig(
                model="default",
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "type": "object",
                        "properties": {"approved": {"type": "boolean"}},
                        "required": ["approved"],
                    },
                },
            ),
        )

    assert result.is_ok
    assert json.loads(result.value.content) == {"approved": True}
    assert len(factory.created) == 2


@pytest.mark.asyncio
async def test_retry_exhaustion_returns_provider_error() -> None:
    process = _FakeProcess(
        stdout=_gjc_jsonl(_ready(), _ack("prompt-1"), _agent_end("prompt-1", '{"approved": "yes"}'))
    )
    factory = _ProcessFactory(process)
    adapter = GjcLLMAdapter(cli_path="/tmp/gjc", cwd="/tmp/project", max_retries=1)

    with (
        patch("ouroboros.providers.gjc_llm_adapter.asyncio.create_subprocess_exec", factory),
        patch(
            "ouroboros.providers.gjc_llm_adapter.uuid4", return_value=type("U", (), {"hex": "1"})()
        ),
    ):
        result = await adapter.complete(
            [Message(role=MessageRole.USER, content="Return verdict")],
            CompletionConfig(
                model="default",
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "type": "object",
                        "properties": {"approved": {"type": "boolean"}},
                        "required": ["approved"],
                    },
                },
            ),
        )

    assert result.is_err
    assert result.error.provider == "gjc"
    assert "non-conforming output" in result.error.message


@pytest.mark.asyncio
async def test_assistant_error_returns_provider_error() -> None:
    process = _FakeProcess(
        stdout=_gjc_jsonl(
            _ready(),
            _ack("prompt-1"),
            {
                "id": "prompt-1",
                "type": "agent_end",
                "messages": [
                    {
                        "role": "assistant",
                        "stopReason": "error",
                        "errorMessage": "Model not found",
                    }
                ],
            },
        ),
        returncode=0,
    )
    factory = _ProcessFactory(process)
    adapter = GjcLLMAdapter(cli_path="/tmp/gjc", cwd="/tmp/project")

    with (
        patch("ouroboros.providers.gjc_llm_adapter.asyncio.create_subprocess_exec", factory),
        patch(
            "ouroboros.providers.gjc_llm_adapter.uuid4", return_value=type("U", (), {"hex": "1"})()
        ),
    ):
        result = await adapter.complete(
            [Message(role=MessageRole.USER, content="Hello")],
            CompletionConfig(model="default"),
        )

    assert result.is_err
    assert result.error.provider == "gjc"
    assert result.error.message == "Model not found"
    assert result.error.details["event_type"] == "agent_end"


@pytest.mark.asyncio
async def test_explicit_set_model_success() -> None:
    process = _FakeProcess(
        stdout=_gjc_jsonl(
            _ready(),
            _ack("set-model-1", command="set_model"),
            _ack("prompt-1"),
            _agent_end("prompt-1", "Done"),
        )
    )
    factory = _ProcessFactory(process)
    adapter = GjcLLMAdapter(cli_path="/tmp/gjc", cwd="/tmp/project")

    with (
        patch("ouroboros.providers.gjc_llm_adapter.asyncio.create_subprocess_exec", factory),
        patch(
            "ouroboros.providers.gjc_llm_adapter.uuid4", return_value=type("U", (), {"hex": "1"})()
        ),
    ):
        result = await adapter.complete(
            [Message(role=MessageRole.USER, content="Hello")],
            CompletionConfig(model="openai/gpt-4.1", model_is_explicit=True),
        )

    assert result.is_ok
    assert json.loads(process.stdin.writes[0]) == {
        "id": "set-model-1",
        "type": "set_model",
        "provider": "openai",
        "modelId": "gpt-4.1",
    }
    assert json.loads(process.stdin.writes[1])["type"] == "prompt"


@pytest.mark.asyncio
async def test_explicit_set_model_failure_returns_provider_error() -> None:
    process = _FakeProcess(
        stdout=_gjc_jsonl(_ready(), _ack("set-model-1", success=False, command="set_model"))
    )
    factory = _ProcessFactory(process)
    adapter = GjcLLMAdapter(cli_path="/tmp/gjc", cwd="/tmp/project")

    with (
        patch("ouroboros.providers.gjc_llm_adapter.asyncio.create_subprocess_exec", factory),
        patch(
            "ouroboros.providers.gjc_llm_adapter.uuid4", return_value=type("U", (), {"hex": "1"})()
        ),
    ):
        result = await adapter.complete(
            [Message(role=MessageRole.USER, content="Hello")],
            CompletionConfig(model="openai/gpt-4.1", model_is_explicit=True),
        )

    assert result.is_err
    assert result.error.provider == "gjc"
    assert "set_model command failed" in result.error.message


@pytest.mark.asyncio
async def test_malformed_jsonl_returns_provider_error() -> None:
    process = _FakeProcess(stdout='{"type":"ready"}\nnot-json\n')
    factory = _ProcessFactory(process)
    adapter = GjcLLMAdapter(cli_path="/tmp/gjc", cwd="/tmp/project")

    with (
        patch("ouroboros.providers.gjc_llm_adapter.asyncio.create_subprocess_exec", factory),
        patch(
            "ouroboros.providers.gjc_llm_adapter.uuid4", return_value=type("U", (), {"hex": "1"})()
        ),
    ):
        result = await adapter.complete(
            [Message(role=MessageRole.USER, content="Hello")],
            CompletionConfig(model="default"),
        )

    assert result.is_err
    assert result.error.provider == "gjc"
    assert "Malformed JSONL" in result.error.message


@pytest.mark.asyncio
async def test_prompt_ack_required_before_streaming() -> None:
    process = _FakeProcess(
        stdout=_gjc_jsonl(_ready(), {"id": "prompt-1", "type": "message_update", "delta": "early"})
    )
    factory = _ProcessFactory(process)
    adapter = GjcLLMAdapter(cli_path="/tmp/gjc", cwd="/tmp/project")

    with (
        patch("ouroboros.providers.gjc_llm_adapter.asyncio.create_subprocess_exec", factory),
        patch(
            "ouroboros.providers.gjc_llm_adapter.uuid4", return_value=type("U", (), {"hex": "1"})()
        ),
    ):
        result = await adapter.complete(
            [Message(role=MessageRole.USER, content="Hello")], CompletionConfig(model="default")
        )

    assert result.is_err
    assert result.error.details["error_type"] == "GjcProtocolError"
    assert process.terminated


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "frame_type",
    [
        "workflow_gate",
        "host_tool_call",
        "host_uri_request",
        "extension_ui_request",
        "unknown_frame",
    ],
)
async def test_unsupported_first_frame_returns_provider_error_and_terminates(
    frame_type: str,
) -> None:
    process = _FakeProcess(stdout=_gjc_jsonl({"id": "frame-1", "type": frame_type}))
    factory = _ProcessFactory(process)
    adapter = GjcLLMAdapter(cli_path="/tmp/gjc", cwd="/tmp/project")

    with patch("ouroboros.providers.gjc_llm_adapter.asyncio.create_subprocess_exec", factory):
        result = await adapter.complete(
            [Message(role=MessageRole.USER, content="Hello")], CompletionConfig(model="default")
        )

    assert result.is_err
    assert result.error.details["error_type"] == "UnsupportedGjcRpcFrame"
    assert frame_type in result.error.message
    assert process.terminated


@pytest.mark.asyncio
async def test_empty_stdout_missing_ready_returns_provider_error() -> None:
    process = _FakeProcess(stdout=_gjc_jsonl())
    factory = _ProcessFactory(process)
    adapter = GjcLLMAdapter(cli_path="/tmp/gjc", cwd="/tmp/project")

    with patch("ouroboros.providers.gjc_llm_adapter.asyncio.create_subprocess_exec", factory):
        result = await adapter.complete(
            [Message(role=MessageRole.USER, content="Hello")], CompletionConfig(model="default")
        )

    assert result.is_err
    assert result.error.provider == "gjc"
    assert result.error.details["error_type"] != "UnsupportedGjcRpcFrame"


@pytest.mark.asyncio
async def test_supported_out_of_order_first_frame_is_generic_protocol_error() -> None:
    process = _FakeProcess(stdout=_gjc_jsonl({"id": "x", "type": "agent_start"}))
    factory = _ProcessFactory(process)
    adapter = GjcLLMAdapter(cli_path="/tmp/gjc", cwd="/tmp/project")

    with patch("ouroboros.providers.gjc_llm_adapter.asyncio.create_subprocess_exec", factory):
        result = await adapter.complete(
            [Message(role=MessageRole.USER, content="Hello")], CompletionConfig(model="default")
        )

    assert result.is_err
    assert result.error.provider == "gjc"
    assert result.error.details["error_type"] == "ProviderError"
    assert "did not emit a ready frame" in result.error.message
    assert process.terminated


@pytest.mark.asyncio
async def test_late_same_id_success_false_after_prompt_ack_fails() -> None:
    process = _FakeProcess(
        stdout=_gjc_jsonl(_ready(), _ack("prompt-1"), _ack("prompt-1", success=False))
    )
    factory = _ProcessFactory(process)
    adapter = GjcLLMAdapter(cli_path="/tmp/gjc", cwd="/tmp/project")

    with (
        patch("ouroboros.providers.gjc_llm_adapter.asyncio.create_subprocess_exec", factory),
        patch(
            "ouroboros.providers.gjc_llm_adapter.uuid4", return_value=type("U", (), {"hex": "1"})()
        ),
    ):
        result = await adapter.complete(
            [Message(role=MessageRole.USER, content="Hello")], CompletionConfig(model="default")
        )

    assert result.is_err
    assert result.error.details["error_type"] == "GjcCommandError"
    assert process.terminated


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "frame_type",
    [
        "workflow_gate",
        "extension_ui_request",
        "host_tool_call",
        "host_tool_cancel",
        "host_uri_request",
        "host_uri_cancel",
        "unknown_frame",
    ],
)
async def test_unsupported_frames_fail_and_terminate(frame_type: str) -> None:
    process = _FakeProcess(
        stdout=_gjc_jsonl(_ready(), _ack("prompt-1"), {"id": "frame-1", "type": frame_type})
    )
    factory = _ProcessFactory(process)
    adapter = GjcLLMAdapter(cli_path="/tmp/gjc", cwd="/tmp/project")

    with (
        patch("ouroboros.providers.gjc_llm_adapter.asyncio.create_subprocess_exec", factory),
        patch(
            "ouroboros.providers.gjc_llm_adapter.uuid4", return_value=type("U", (), {"hex": "1"})()
        ),
    ):
        result = await adapter.complete(
            [Message(role=MessageRole.USER, content="Hello")], CompletionConfig(model="default")
        )

    assert result.is_err
    assert result.error.details["error_type"] == "UnsupportedGjcRpcFrame"
    assert frame_type in result.error.message
    assert process.terminated


@pytest.mark.asyncio
async def test_wrong_command_prompt_ack_fails() -> None:
    process = _FakeProcess(stdout=_gjc_jsonl(_ready(), _ack("prompt-1", command="set_model")))
    factory = _ProcessFactory(process)
    adapter = GjcLLMAdapter(cli_path="/tmp/gjc", cwd="/tmp/project")

    with (
        patch("ouroboros.providers.gjc_llm_adapter.asyncio.create_subprocess_exec", factory),
        patch(
            "ouroboros.providers.gjc_llm_adapter.uuid4", return_value=type("U", (), {"hex": "1"})()
        ),
    ):
        result = await adapter.complete(
            [Message(role=MessageRole.USER, content="Hello")], CompletionConfig(model="default")
        )

    assert result.is_err
    assert result.error.details["error_type"] == "GjcProtocolError"


@pytest.mark.asyncio
async def test_unsupported_frame_during_set_model_phase_is_unsupported() -> None:
    process = _FakeProcess(stdout=_gjc_jsonl(_ready(), {"type": "host_tool_call", "id": "tool-1"}))
    factory = _ProcessFactory(process)
    adapter = GjcLLMAdapter(cli_path="/tmp/gjc", cwd="/tmp/project")

    with (
        patch("ouroboros.providers.gjc_llm_adapter.asyncio.create_subprocess_exec", factory),
        patch(
            "ouroboros.providers.gjc_llm_adapter.uuid4", return_value=type("U", (), {"hex": "1"})()
        ),
    ):
        result = await adapter.complete(
            [Message(role=MessageRole.USER, content="Hello")],
            CompletionConfig(model="openai/gpt-4.1", model_is_explicit=True),
        )

    assert result.is_err
    assert result.error.details["error_type"] == "UnsupportedGjcRpcFrame"


@pytest.mark.asyncio
async def test_unsupported_frame_during_prompt_phase_is_unsupported() -> None:
    process = _FakeProcess(stdout=_gjc_jsonl(_ready(), {"type": "host_tool_call", "id": "tool-1"}))
    factory = _ProcessFactory(process)
    adapter = GjcLLMAdapter(cli_path="/tmp/gjc", cwd="/tmp/project")

    with (
        patch("ouroboros.providers.gjc_llm_adapter.asyncio.create_subprocess_exec", factory),
        patch(
            "ouroboros.providers.gjc_llm_adapter.uuid4", return_value=type("U", (), {"hex": "1"})()
        ),
    ):
        result = await adapter.complete(
            [Message(role=MessageRole.USER, content="Hello")], CompletionConfig(model="default")
        )

    assert result.is_err
    assert result.error.details["error_type"] == "UnsupportedGjcRpcFrame"


def _envelope(event: dict[str, object], *, seq: int = 1) -> dict[str, object]:
    return {
        "protocol_version": 2,
        "session_id": "session-1",
        "seq": seq,
        "frame_id": f"frame-{seq}",
        "type": "event",
        "payload": {"event_type": event.get("type"), "event": event},
    }


@pytest.mark.asyncio
async def test_protocol_v2_enveloped_stream_completes() -> None:
    streamed: list[str] = []
    process = _FakeProcess(
        stdout=_gjc_jsonl(
            _ready(),
            _ack("prompt-1"),
            _envelope({"type": "agent_start"}, seq=1),
            _envelope(
                {
                    "type": "message_update",
                    "assistantMessageEvent": {"type": "thinking_delta", "delta": "let me think"},
                },
                seq=2,
            ),
            _envelope(
                {
                    "type": "message_update",
                    "assistantMessageEvent": {"type": "text_delta", "delta": "Hel"},
                },
                seq=3,
            ),
            _envelope(
                {
                    "type": "message_update",
                    "assistantMessageEvent": {"type": "text_delta", "delta": "lo"},
                },
                seq=4,
            ),
            _envelope(
                {
                    "type": "message_update",
                    "assistantMessageEvent": {"type": "text_end", "content": "Hello"},
                },
                seq=5,
            ),
            _envelope({"type": "tool_execution_update", "toolCallId": "t1"}, seq=6),
            _envelope({"type": "some_future_event"}, seq=7),
            _envelope(
                {
                    "type": "agent_end",
                    "messages": [
                        {
                            "role": "assistant",
                            "content": [
                                {"type": "thinking", "thinking": "let me think"},
                                {"type": "text", "text": "Hello"},
                            ],
                        }
                    ],
                },
                seq=8,
            ),
        )
    )
    factory = _ProcessFactory(process)
    adapter = GjcLLMAdapter(
        cli_path="/tmp/gjc",
        cwd="/tmp/project",
        on_message=lambda _role, delta: streamed.append(delta),
    )

    with (
        patch("ouroboros.providers.gjc_llm_adapter.asyncio.create_subprocess_exec", factory),
        patch(
            "ouroboros.providers.gjc_llm_adapter.uuid4", return_value=type("U", (), {"hex": "1"})()
        ),
    ):
        result = await adapter.complete(
            [Message(role=MessageRole.USER, content="Say hello")],
            CompletionConfig(model="default"),
        )

    assert result.is_ok
    assert result.value.content == "Hello"
    # Thinking deltas and text_end snapshots must not pollute streamed content.
    assert streamed == ["Hel", "lo"]


@pytest.mark.asyncio
async def test_protocol_v2_malformed_envelope_is_protocol_error() -> None:
    process = _FakeProcess(
        stdout=_gjc_jsonl(
            _ready(),
            _ack("prompt-1"),
            {"type": "event", "payload": {"event_type": "agent_start"}},
        )
    )
    factory = _ProcessFactory(process)
    adapter = GjcLLMAdapter(cli_path="/tmp/gjc", cwd="/tmp/project")

    with (
        patch("ouroboros.providers.gjc_llm_adapter.asyncio.create_subprocess_exec", factory),
        patch(
            "ouroboros.providers.gjc_llm_adapter.uuid4", return_value=type("U", (), {"hex": "1"})()
        ),
    ):
        result = await adapter.complete(
            [Message(role=MessageRole.USER, content="Say hello")],
            CompletionConfig(model="default"),
        )

    assert result.is_err
    assert result.error.details["error_type"] == "GjcProtocolError"


@pytest.mark.asyncio
async def test_slash_qualified_model_id_sends_set_model() -> None:
    process = _FakeProcess(
        stdout=_gjc_jsonl(
            _ready(),
            _ack("set-model-1", command="set_model"),
            _ack("prompt-1"),
            _agent_end("prompt-1", "Done"),
        )
    )
    factory = _ProcessFactory(process)
    adapter = GjcLLMAdapter(cli_path="/tmp/gjc", cwd="/tmp/project")

    with (
        patch("ouroboros.providers.gjc_llm_adapter.asyncio.create_subprocess_exec", factory),
        patch(
            "ouroboros.providers.gjc_llm_adapter.uuid4", return_value=type("U", (), {"hex": "1"})()
        ),
    ):
        result = await adapter.complete(
            [Message(role=MessageRole.USER, content="Hello")],
            CompletionConfig(
                model="openrouter/google/gemini-3-flash-preview", model_is_explicit=True
            ),
        )

    assert result.is_ok
    assert json.loads(process.stdin.writes[0]) == {
        "id": "set-model-1",
        "type": "set_model",
        "provider": "openrouter",
        "modelId": "google/gemini-3-flash-preview",
    }
