"""Claude 5.5 arguments cross the serialized, isolated sealed-worker boundary."""

import asyncio
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

pytest.importorskip("httpx")
pytest.importorskip("litellm")
pytest.importorskip("openai")

from ouroboros.providers import litellm_adapter
from ouroboros.providers import litellm_proof_worker as worker
from ouroboros.providers.anthropic_models import is_claude_5_5
from ouroboros.providers.base import CompletionConfig, Message, MessageRole


def _payload(model: str, effort: str | None, api_base: str) -> dict:
    adapter = litellm_adapter.LiteLLMAdapter(api_key="test-only", api_base=api_base)
    config = CompletionConfig(
        model=model,
        max_tokens=64,
        temperature=0.2,
        top_p=0.8,
        reasoning_effort=effort,
        response_format={"type": "json_object"} if is_claude_5_5(model) else None,
    )
    dispatch = SimpleNamespace(api_key="test-only", api_base=api_base, timeout=30.0)
    return {
        "protocol": worker._PROOF_WORKER_PROTOCOL,
        "client_type": adapter._proof_client_type(adapter._extract_provider(model)),
        "completion_kwargs": adapter._build_completion_kwargs(
            [Message(role=MessageRole.USER, content="hello")], config, _sealed_dispatch=dispatch
        ),
        "config": asdict(config),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model",
    [
        "claude-haiku-5-5",
        "claude-sonnet-5-5",
        "claude-opus-5-5",
        "anthropic/claude-sonnet-5-5",
        "openrouter/anthropic/claude-opus-5.5",
        "claude-sonnet-4-6",
    ],
)
@pytest.mark.parametrize("effort", [None, "high"])
async def test_real_isolated_worker_dispatches_built_anthropic_arguments(
    model: str, effort: str | None
) -> None:
    requests: list[dict] = []
    openrouter = model.startswith("openrouter/")
    wire_model = (
        model.removeprefix("openrouter/") if openrouter else model.removeprefix("anthropic/")
    )

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        header = await reader.readuntil(b"\r\n\r\n")
        length = next(
            int(line.split(b":", 1)[1])
            for line in header.split(b"\r\n")
            if line.lower().startswith(b"content-length:")
        )
        requests.append(json.loads(await reader.readexactly(length)))
        if openrouter:
            response = {
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 1,
                "model": wire_model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": '{"ok":true}'},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        else:
            response = {
                "id": "msg-test",
                "type": "message",
                "role": "assistant",
                "model": wire_model,
                "content": [{"type": "text", "text": '{"ok":true}'}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        body = json.dumps(response).encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    address = server.sockets[0].getsockname()
    payload = _payload(model, effort, f"http://127.0.0.1:{address[1]}/v1")
    worker_path = Path(worker.__file__).resolve()
    # -I ignores PYTHONPATH and an existing editable install can target another
    # checkout. Load the tested source explicitly, then verify its child hashes.
    bootstrap = (
        "import os,sys; from pathlib import Path; "
        f"sys.path.insert(0, {str(worker_path.parents[2])!r}); "
        "os.environ['LITELLM_LOCAL_MODEL_COST_MAP']='True'; "
        "from ouroboros.providers import litellm_proof_worker as worker; "
        f"assert Path(worker.__file__).resolve() == Path({str(worker_path)!r}); "
        "raise SystemExit(worker.main())"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-c",
        bootstrap,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(json.dumps(payload).encode()), timeout=40
        )
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        server.close()
        await server.wait_closed()
    assert process.returncode == 0, stderr.decode()
    report = json.loads(stdout)
    assert report["ok"] is True
    assert report["response"]["content"] == '{"ok":true}'
    proof = report["proof"]
    assert proof["authority_verified"] is True
    assert proof["client_cleanup_succeeded"] is True
    assert (
        proof["worker_implementation_sha256"]
        == hashlib.sha256(worker_path.read_bytes()).hexdigest()
    )
    assert (
        proof["adapter_implementation_sha256"]
        == hashlib.sha256(Path(litellm_adapter.__file__).read_bytes()).hexdigest()
    )
    assert proof["ouroboros_implementation_sha256"] == (
        litellm_adapter._ouroboros_implementation_sha256()
    )
    assert len(requests) == 1
    request = requests[0]
    assert request["model"] == wire_model
    if is_claude_5_5(model):
        assert not {"temperature", "top_p", "top_k"} & request.keys()
        if effort:
            if openrouter:
                assert request["reasoning"] == {"effort": effort}
            else:
                assert request["thinking"] == {"type": "adaptive"}
                assert request["output_config"] == {"effort": effort}
                assert "budget_tokens" not in json.dumps(request)
        if openrouter:
            assert request["response_format"] == {"type": "json_object"}
        else:
            assert "JSON" in json.dumps(request["system"])
            assert "tools" not in request
    else:
        assert request["temperature"] == 0.2
        assert "top_p" not in request
        if effort:
            assert request["thinking"] == {"type": "adaptive"}
            assert request["output_config"] == {"effort": effort}


@pytest.mark.parametrize(
    ("model", "effort", "field", "value"),
    [
        ("claude-sonnet-5-5", None, "temperature", 0.2),
        ("claude-sonnet-5-5", None, "top_p", 0.8),
        ("claude-sonnet-5-5", None, "thinking", {"type": "adaptive"}),
        ("claude-sonnet-5-5", "high", "thinking", {"type": "enabled", "budget_tokens": 1024}),
        ("claude-sonnet-5-5", "high", "output_config", {"effort": "low"}),
        ("claude-sonnet-5-5", "high", "output_config", {"effort": "high", "extra": True}),
        ("claude-sonnet-5-5", "high", "allowed_openai_params", ["thinking", "api_key"]),
        ("claude-sonnet-5-5", "high", "allowed_openai_params", "thinking"),
        ("claude-sonnet-5-5", "high", "extra_body", {"reasoning": {"effort": "high"}}),
        ("claude-sonnet-5-5", "high", "reasoning_effort", "high"),
        ("openrouter/anthropic/claude-opus-5.5", None, "extra_body", {}),
        (
            "openrouter/anthropic/claude-opus-5.5",
            "high",
            "extra_body",
            {"reasoning": {"effort": "low"}},
        ),
        (
            "openrouter/anthropic/claude-opus-5.5",
            "high",
            "extra_body",
            {"reasoning": {"effort": "high", "budget": 10}},
        ),
        ("openrouter/anthropic/claude-opus-5.5", "high", "thinking", {"type": "adaptive"}),
        ("claude-sonnet-4-6", "high", "thinking", {"type": "adaptive"}),
        ("claude-sonnet-4-6", "high", "extra_body", {}),
        ("claude-custom", "high", "allowed_openai_params", ["thinking"]),
    ],
)
def test_worker_rejects_out_of_contract_native_shapes(
    model: str, effort: str | None, field: str, value: object
) -> None:
    payload = _payload(model, effort, "https://provider.example/v1")
    payload["completion_kwargs"][field] = value
    assert worker._validated_payload(json.loads(json.dumps(payload))) is None


@pytest.mark.parametrize("field", ["thinking", "output_config", "allowed_openai_params"])
def test_worker_requires_the_complete_direct_native_effort_trio(field: str) -> None:
    payload = _payload("claude-sonnet-5-5", "high", "https://provider.example/v1")
    del payload["completion_kwargs"][field]
    assert worker._validated_payload(payload) is None


def test_worker_keeps_legacy_temperature_required() -> None:
    payload = _payload("claude-sonnet-4-6", None, "https://provider.example/v1")
    del payload["completion_kwargs"]["temperature"]
    assert worker._validated_payload(payload) is None
