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
    assert adapter.config.env["GJC_COORDINATOR_MCP_SESSION_COMMAND"] == "gjc"
    assert adapter.config.env["PATH"].split(":")[0] == "/opt"
    assert adapter.disconnected


@pytest.mark.asyncio
async def test_server_env_applies_child_recursion_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OUROBOROS_AGENT_RUNTIME", "gjc")
    monkeypatch.setenv("OUROBOROS_LLM_BACKEND", "gjc")
    monkeypatch.setenv("OUROBOROS_RUNTIME", "gjc")
    monkeypatch.setenv("_OUROBOROS_DEPTH", "1")
    adapter = _FakeAdapter([])
    client = GjcCoordinatorClient(
        cli_path="/opt/gjc",
        cwd=tmp_path,
        adapter_factory=_factory(adapter),
    )

    await client.connect()

    assert "OUROBOROS_AGENT_RUNTIME" not in adapter.config.env
    assert "OUROBOROS_LLM_BACKEND" not in adapter.config.env
    # The public legacy selector routes BOTH runtime and LLM choice; leaving it
    # set would send a discovered nested Ouroboros straight back into GJC.
    assert "OUROBOROS_RUNTIME" not in adapter.config.env
    assert adapter.config.env["_OUROBOROS_DEPTH"] == "2"


@pytest.mark.asyncio
async def test_server_env_depth_ceiling_raises_coordinator_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("_OUROBOROS_DEPTH", "5")
    adapter = _FakeAdapter([])
    client = GjcCoordinatorClient(
        cli_path="/opt/gjc",
        cwd=tmp_path,
        adapter_factory=_factory(adapter),
    )

    with pytest.raises(GjcCoordinatorError) as excinfo:
        await client.connect()
    assert excinfo.value.code == "recursion_depth_exceeded"


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


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["not_found", "resource_gone"])
async def test_stop_session_does_not_claim_missing_session_was_reaped(
    reason: str, tmp_path: Path
) -> None:
    adapter = _FakeAdapter([{"ok": False, "error": {"code": reason, "message": "gone"}}])
    client = GjcCoordinatorClient(cli_path="gjc", cwd=tmp_path, adapter_factory=_factory(adapter))
    async with client:
        with pytest.raises(GjcCoordinatorError, match="gone") as error:
            await client.stop_session("session-1")
        assert error.value.code == reason
    assert len(adapter.calls) == 1
    assert adapter.calls[0][1]["session_id"] == "session-1"


@pytest.mark.asyncio
async def test_runtime_retry_reuses_exact_session_after_pending_stop(tmp_path: Path) -> None:
    from ouroboros.orchestrator.gjc_runtime import GjcRuntime

    adapter = _FakeAdapter(
        [
            {"ok": False, "reason": "delivery_pending"},
            {"ok": True, "session_id": "session-1", "closed": True},
        ]
    )
    client = GjcCoordinatorClient(cli_path="gjc", cwd=tmp_path, adapter_factory=_factory(adapter))
    runtime = GjcRuntime(cwd=tmp_path)
    async with client:
        assert await runtime._stop_session_bounded(client, "session-1")
    assert adapter.calls == [
        (
            "gjc_coordinator_stop_session",
            {"session_id": "session-1", "force": True, "allow_mutation": True},
        ),
        (
            "gjc_coordinator_stop_session",
            {"session_id": "session-1", "force": True, "allow_mutation": True},
        ),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"ok": True, "session_id": "session-1", "closed": False},
        {"ok": True, "session_id": "session-1"},
        {"ok": True, "session_id": "different", "closed": True},
        {"ok": True, "closed": True},
    ],
)
async def test_runtime_does_not_confirm_unproven_stop_response(
    payload: dict[str, Any], tmp_path: Path
) -> None:
    from ouroboros.orchestrator.gjc_runtime import GjcRuntime

    adapter = _FakeAdapter([payload])
    client = GjcCoordinatorClient(cli_path="gjc", cwd=tmp_path, adapter_factory=_factory(adapter))
    runtime = GjcRuntime(cwd=tmp_path)
    async with client:
        assert not await runtime._stop_session_bounded(client, "session-1")
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        {"ok": True, "session_id": "session-1", "closed": False},
        {"ok": True, "session_id": "different", "closed": True},
        {"ok": False, "reason": "resource_gone"},
    ],
)
async def test_runtime_surfaces_unconfirmed_stop_through_actual_client(
    response: dict[str, Any], tmp_path: Path
) -> None:
    from ouroboros.orchestrator.gjc_runtime import GjcRuntime

    adapter = _FakeAdapter(
        [
            {"ok": True, "session": {"session_id": "session-1"}, "turn_id": "turn-1"},
            {"ok": True, "turn": {"status": "completed", "final_response": {"text": "done"}}},
            response,
        ]
    )

    def factory(**kwargs: Any) -> GjcCoordinatorClient:
        return GjcCoordinatorClient(**kwargs, adapter_factory=_factory(adapter))

    runtime = GjcRuntime(cwd=tmp_path, coordinator_client_factory=factory)
    final = [message async for message in runtime.execute_task("Do it")][-1]
    assert final.data["subtype"] == "success"
    assert final.data["cleanup_unconfirmed"] is True
    assert adapter.disconnected
    assert [name for name, _ in adapter.calls].count("gjc_coordinator_stop_session") == 1


@pytest.mark.asyncio
async def test_terminate_handle_reports_false_when_stop_is_not_proven(tmp_path: Path) -> None:
    from ouroboros.orchestrator.adapter import RuntimeHandle
    from ouroboros.orchestrator.gjc_runtime import GjcRuntime

    adapter = _FakeAdapter([{"ok": True, "session_id": "session-1", "closed": False}])

    def factory(**kwargs: Any) -> GjcCoordinatorClient:
        return GjcCoordinatorClient(**kwargs, adapter_factory=_factory(adapter))

    runtime = GjcRuntime(cwd=tmp_path, coordinator_client_factory=factory)
    handle = RuntimeHandle(backend="gjc", kind="agent_runtime", native_session_id="session-1")
    assert not await runtime._terminate_session(handle)
    assert adapter.disconnected
