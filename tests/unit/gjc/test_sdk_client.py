"""Contract tests for the shared GJC Coordinator MCP client."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
from typing import Any

import pytest

from ouroboros.core.types import Result
from ouroboros.gjc.sdk_client import (
    GjcCoordinatorClient,
    GjcCoordinatorError,
    GjcCoordinatorQuestion,
)
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPServerInfo, MCPToolResult


class _FakeAdapter:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.config = None
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.disconnected = False

    async def connect(self, config: Any) -> Any:
        self.config = config
        return Result.ok(MCPServerInfo(name="gjc-coordinator", version="1"))

    async def disconnect(self) -> Any:
        self.disconnected = True
        return Result.ok(None)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        payload = self.responses.pop(0)
        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=json.dumps(payload),
                    ),
                )
            )
        )


def _factory(adapter: _FakeAdapter):
    return lambda: adapter


@pytest.mark.asyncio
async def test_start_await_tail_and_stop_use_coordinator_contract(tmp_path: Path) -> None:
    adapter = _FakeAdapter(
        [
            {"ok": True, "session": {"session_id": "session-1"}, "turn_id": "turn-1"},
            {
                "ok": True,
                "turn": {
                    "status": "completed",
                    "final_response": {"text": "done"},
                    "error": None,
                },
            },
            {"ok": True, "source": "sdk", "lines": ["one", "two"]},
            {"ok": True, "session_id": "session-1", "closed": True},
        ]
    )
    client = GjcCoordinatorClient(
        cli_path="/opt/gjc",
        cwd=tmp_path,
        adapter_factory=_factory(adapter),
    )

    async with client:
        session = await client.start_session("work", model="openai-codex/gpt-5.6-sol")
        turn = await client.await_turn(session.session_id, session.turn_id)
        tail = await client.read_last_assistant(session.session_id)
        await client.stop_session(session.session_id)

    assert session.session_id == "session-1"
    assert turn.succeeded and turn.text == "done"
    assert tail == "one\ntwo"
    assert [name for name, _args in adapter.calls] == [
        "gjc_coordinator_start_session",
        "gjc_coordinator_await_turn",
        "gjc_coordinator_read_tail",
        "gjc_coordinator_stop_session",
    ]
    start_args = adapter.calls[0][1]
    assert start_args["allow_mutation"] is True
    assert start_args["model"] == "openai-codex/gpt-5.6-sol"
    assert adapter.config.command == "/opt/gjc"
    assert adapter.config.args == ("mcp-serve", "coordinator")
    assert adapter.config.env["GJC_COORDINATOR_MCP_WORKDIR_ROOTS"] == str(tmp_path.resolve())
    assert adapter.config.env["GJC_COORDINATOR_MCP_SESSION_COMMAND"] == "/opt/gjc"
    assert adapter.disconnected


@pytest.mark.asyncio
async def test_waiting_turn_projects_question_and_accepts_custom_answer(tmp_path: Path) -> None:
    adapter = _FakeAdapter(
        [
            {
                "ok": True,
                "turn": {
                    "status": "waiting_for_answer",
                    "final_response": {"text": None},
                    "error": None,
                },
            },
            {
                "ok": True,
                "questions": [
                    {
                        "question_id": "q-1",
                        "answer_binding": "binding-1",
                        "prompt": "Choose",
                        "options": [{"label": "A"}, {"label": "B"}],
                        "multi": False,
                    }
                ],
            },
            {"ok": True, "status": "accepted"},
        ]
    )
    client = GjcCoordinatorClient(
        cli_path="gjc",
        cwd=tmp_path,
        adapter_factory=_factory(adapter),
    )

    async with client:
        turn = await client.await_turn("session-1", "turn-1")
        assert turn.question is not None
        assert turn.question.options == ("A", "B")
        await client.submit_question_answer(turn.question, "custom")

    answer = adapter.calls[-1][1]
    assert answer["answer"] == {"selected": [], "other": True, "custom": "custom"}
    assert answer["answer_binding"] == "binding-1"


@pytest.mark.asyncio
async def test_public_coordinator_failure_maps_to_typed_error(tmp_path: Path) -> None:
    adapter = _FakeAdapter(
        [{"ok": False, "error": {"code": "unknown_model", "message": "No such model"}}]
    )
    client = GjcCoordinatorClient(
        cli_path="gjc",
        cwd=tmp_path,
        adapter_factory=_factory(adapter),
    )

    async with client:
        with pytest.raises(GjcCoordinatorError) as raised:
            await client.start_session("work", model="missing/model")

    assert raised.value.code == "unknown_model"
    assert str(raised.value) == "No such model"


@pytest.mark.asyncio
async def test_mutation_methods_reuse_caller_owned_idempotency_keys(tmp_path: Path) -> None:
    adapter = _FakeAdapter(
        [
            {"ok": True, "session": {"session_id": "session-1"}, "turn_id": "turn-1"},
            {"ok": True, "turn_id": "turn-2"},
            {"ok": True, "status": "accepted"},
        ]
    )
    client = GjcCoordinatorClient(cli_path="gjc", cwd=tmp_path, adapter_factory=_factory(adapter))

    async with client:
        await client.start_session("work", idempotency_key="start-key")
        await client.send_prompt("session-1", "next", idempotency_key="prompt-key")
        question = GjcCoordinatorQuestion(
            session_id="session-1",
            turn_id="turn-1",
            question_id="q-1",
            answer_binding="binding-1",
            prompt="Choose",
            options=(),
            multi=False,
        )
        await client.submit_question_answer(question, "answer", idempotency_key="answer-key")

    assert adapter.calls[0][1]["idempotency_key"] == "start-key"
    assert adapter.calls[1][1]["idempotency_key"] == "prompt-key"
    assert adapter.calls[2][1]["idempotency_key"] == "answer-key"


def test_current_gjc_exposes_coordinator_mcp_when_installed() -> None:
    gjc = shutil.which("gjc")
    if gjc is None:
        pytest.skip("gjc is not installed")

    completed = subprocess.run(
        [gjc, "mcp-serve", "coordinator", "--check", "--json"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["server"]["name"] == "gjc-coordinator-mcp"
    assert "gjc_coordinator_start_session" in payload["tools"]
    assert "gjc_coordinator_await_turn" in payload["tools"]
