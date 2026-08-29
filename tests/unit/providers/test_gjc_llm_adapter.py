"""Contract tests for the GJC SDK-backed LLM adapter."""

from __future__ import annotations

import pytest

from ouroboros.gjc.sdk_client import GjcCoordinatorSession, GjcCoordinatorTurn
from ouroboros.providers.base import CompletionConfig, Message, MessageRole
from ouroboros.providers.gjc_llm_adapter import GjcLLMAdapter


class FakeClient:
    def __init__(self, turn: GjcCoordinatorTurn) -> None:
        self.turn = turn
        self.connected = False
        self.closed = False
        self.stopped: list[str] = []
        self.started: list[tuple[str, str | None]] = []

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.closed = True

    async def start_session(
        self, prompt: str, *, model: str | None = None, mpreset: str | None = None
    ) -> GjcCoordinatorSession:
        del mpreset
        self.started.append((prompt, model))
        return GjcCoordinatorSession("session-1", "turn-1")

    async def await_turn(self, session_id: str, turn_id: str) -> GjcCoordinatorTurn:
        assert (session_id, turn_id) == ("session-1", "turn-1")
        return self.turn

    async def read_last_assistant(self, session_id: str, *, lines: int = 400) -> str:
        del session_id, lines
        return self.turn.text

    async def stop_session(self, session_id: str) -> None:
        self.stopped.append(session_id)


def _factory(client: FakeClient):
    return lambda **_kwargs: client


def _turn(text: str, status: str = "completed") -> GjcCoordinatorTurn:
    return GjcCoordinatorTurn(
        session_id="session-1",
        turn_id="turn-1",
        status=status,
        text=text,
        error=None if status == "completed" else "failed",
        question=None,
        raw={},
    )


@pytest.mark.asyncio
async def test_completion_uses_coordinator_sdk_and_cleans_up() -> None:
    client = FakeClient(_turn("Hello"))
    adapter = GjcLLMAdapter(
        cli_path="/opt/gjc",
        cwd="/tmp/project",
        coordinator_client_factory=_factory(client),
    )

    result = await adapter.complete(
        [Message(role=MessageRole.USER, content="Say hello")],
        CompletionConfig(model="default"),
    )

    assert result.is_ok
    assert result.value.content == "Hello"
    assert client.connected and client.closed
    assert client.stopped == ["session-1"]
    assert client.started == [("user: Say hello", None)]


@pytest.mark.asyncio
async def test_structured_completion_reuses_existing_validation() -> None:
    client = FakeClient(_turn('{"approved": true}'))
    adapter = GjcLLMAdapter(
        cli_path="gjc",
        cwd="/tmp/project",
        coordinator_client_factory=_factory(client),
    )

    result = await adapter.complete(
        [Message(role=MessageRole.USER, content="Return JSON")],
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
    assert result.value.content == '{"approved": true}'
    assert "ONLY a valid JSON object" in client.started[0][0]


@pytest.mark.asyncio
async def test_failed_turn_maps_to_gjc_provider_error() -> None:
    client = FakeClient(_turn("", status="failed"))
    adapter = GjcLLMAdapter(cwd="/tmp/project", coordinator_client_factory=_factory(client))

    result = await adapter.complete(
        [Message(role=MessageRole.USER, content="Hello")],
        CompletionConfig(model="default"),
    )

    assert result.is_err
    assert result.error.provider == "gjc"
    assert result.error.message == "failed"
    assert client.stopped == ["session-1"]
