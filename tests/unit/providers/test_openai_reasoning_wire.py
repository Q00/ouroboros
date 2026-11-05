"""Exercise LiteLLM's direct OpenAI mapping against a local HTTP endpoint."""

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from threading import Thread

import pytest

pytest.importorskip("litellm")

from ouroboros.providers.base import CompletionConfig, Message, MessageRole
from ouroboros.providers.litellm_adapter import LiteLLMAdapter


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "effort", "sampling"),
    [
        ("openai/o3", None, False),
        ("openai/o4-mini", None, False),
        ("openai/gpt-5-mini", None, False),
        ("openai/gpt-5.4", "high", False),
        ("openai/gpt-5.4", None, True),
        ("openai/gpt-4o", None, True),
    ],
)
async def test_reasoning_sampling_on_wire(model: str, effort: str | None, sampling: bool) -> None:
    bodies: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:  # noqa: N802
            bodies.append(json.loads(self.rfile.read(int(self.headers["content-length"]))))
            response = json.dumps(
                {
                    "id": "test",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "fake",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
                }
            ).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        adapter = LiteLLMAdapter(
            api_key="test", api_base=f"http://127.0.0.1:{server.server_port}/v1"
        )
        result = await adapter.complete(
            [Message(role=MessageRole.USER, content="ok")],
            CompletionConfig(model=model, reasoning_effort=effort),
        )
        assert result.is_ok
        assert len(bodies) == 1
        assert ("temperature" in bodies[0]) is sampling
        assert ("top_p" in bodies[0]) is sampling
        assert bodies[0].get("reasoning_effort") == effort
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
