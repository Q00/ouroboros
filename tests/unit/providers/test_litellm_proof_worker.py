"""The LiteLLM proof worker owns one sealed effect boundary."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from ouroboros.evolution.provider_usage import (
    capture_generation_provider_usage,
    tracked_complete,
)
from ouroboros.providers import litellm_adapter
from ouroboros.providers import litellm_proof_worker as worker
from ouroboros.providers.base import CompletionConfig


def _payload() -> dict[str, object]:
    return {
        "protocol": worker._PROOF_WORKER_PROTOCOL,
        "client_type": "openai.AsyncOpenAI",
        "completion_kwargs": {
            "api_base": "https://provider.example/v1",
            "api_key": "credential-a",
            "max_retries": 0,
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "hello"}],
            "model": "openai/gpt-test",
            "temperature": 0.0,
            "timeout": 30.0,
            "top_p": 1.0,
        },
        "config": {
            "model": "openai/gpt-test",
            "temperature": 0.0,
            "max_tokens": 64,
            "stop": None,
            "top_p": 1.0,
            "response_format": None,
            "role": None,
            "profile": None,
            "max_turns": None,
            "reasoning_effort": None,
            "model_is_explicit": True,
        },
    }


@pytest.mark.asyncio
async def test_worker_passes_the_sealed_client_to_litellm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    async def completion(**kwargs: object) -> object:
        observed.update(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content="done"),
                )
            ],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2, total_tokens=5),
            model="openai/gpt-test",
            model_dump=lambda: {},
        )

    monkeypatch.setattr(worker.litellm, "acompletion", completion)

    report = await worker._run(_payload())

    assert report["ok"] is True
    assert observed["client"] is not None
    assert observed["api_key"] == "credential-a"
    assert observed["api_base"] == "https://provider.example/v1"
    assert observed["max_retries"] == 0
    proof = report["proof"]
    assert isinstance(proof, dict)
    assert proof["authority_verified"] is True
    assert proof["client_cleanup_succeeded"] is True
    assert proof["client_type"] == "openai.AsyncOpenAI"
    assert proof["worker_executable_resolved_path"]
    assert len(proof["worker_executable_content_sha256"]) == 64
    assert proof["worker_python_version"]
    assert proof["worker_module"] == "ouroboros.providers.litellm_proof_worker"
    assert len(proof["worker_implementation_sha256"]) == 64
    assert proof["adapter_module"] == "ouroboros.providers.litellm_adapter"
    assert len(proof["adapter_implementation_sha256"]) == 64
    assert proof["ouroboros_implementation_contract"] == "ouroboros.python-sources.v1"
    assert len(proof["ouroboros_implementation_sha256"]) == 64


@pytest.mark.asyncio
async def test_worker_rejects_unregistered_payload_fields() -> None:
    payload = _payload()
    payload["ambient_authority"] = "parent"

    report = await worker._run(payload)

    assert report["ok"] is False
    assert report["response"] is None


@pytest.mark.asyncio
async def test_real_worker_isolates_sub_poll_parent_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_received: dict[str, object] = {}

    async def serve_completion(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        header = await reader.readuntil(b"\r\n\r\n")
        content_length = 0
        for line in header.decode("latin-1").split("\r\n"):
            if line.lower().startswith("content-length:"):
                content_length = int(line.split(":", 1)[1].strip())
        request_received["payload"] = json.loads(await reader.readexactly(content_length))
        body = json.dumps(
            {
                "id": "completion-1",
                "object": "chat.completion",
                "created": 1,
                "model": "gpt-test",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "done"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 2,
                    "total_tokens": 5,
                },
            }
        ).encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n".encode()
            + b"Connection: close\r\n\r\n"
            + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(serve_completion, "127.0.0.1", 0)
    address = server.sockets[0].getsockname()
    adapter = litellm_adapter.LiteLLMAdapter(
        api_key="credential-a",
        api_base=f"http://127.0.0.1:{address[1]}/v1",
        frugality_proof=True,
    )
    worker_started = asyncio.Event()
    create_process = litellm_adapter.asyncio.create_subprocess_exec

    async def observed_create_process(*args: object, **kwargs: object) -> object:
        process = await create_process(*args, **kwargs)
        worker_started.set()
        return process

    async def mutate_and_restore() -> None:
        await worker_started.wait()
        litellm_adapter.litellm.modify_params = True
        litellm_adapter.litellm.modify_params = False

    monkeypatch.setattr(
        litellm_adapter.asyncio,
        "create_subprocess_exec",
        observed_create_process,
    )
    mutation = asyncio.create_task(mutate_and_restore())
    try:
        with capture_generation_provider_usage() as capture:
            result = await tracked_complete(
                adapter,
                [],
                CompletionConfig(model="openai/gpt-test", role="reflect"),
            )
        await mutation
    finally:
        server.close()
        await server.wait_closed()

    assert result.is_ok
    assert result.value.content == "done"
    assert request_received["payload"] == {
        "messages": [],
        "model": "gpt-test",
        "temperature": 0.7,
        "max_tokens": 4096,
        "top_p": 1.0,
    }
    summary = capture.summary()
    assert summary.call_count == 1
    assert summary.token_spend is None
    assert summary.complete is False
