"""Contract tests for the GJC SDK-backed agent runtime."""

from __future__ import annotations

import pytest

from ouroboros.gjc.sdk_client import (
    GjcCoordinatorQuestion,
    GjcCoordinatorSession,
    GjcCoordinatorTurn,
)
from ouroboros.orchestrator.adapter import ParamSupport, RuntimeHandle
from ouroboros.orchestrator.gjc_runtime import GjcRuntime
from ouroboros.orchestrator.runtime_param_negotiation import negotiate_execution_params


class FakeClient:
    def __init__(self, turns: list[GjcCoordinatorTurn]) -> None:
        self.turns = list(turns)
        self.connected = False
        self.closed = False
        self.started: list[tuple[str, str | None, str | None]] = []
        self.sent: list[tuple[str, str, str | None]] = []
        self.answers: list[tuple[GjcCoordinatorQuestion, str, str | None]] = []

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.closed = True

    async def start_session(
        self,
        prompt: str,
        *,
        model: str | None = None,
        mpreset: str | None = None,
        idempotency_key: str | None = None,
    ) -> GjcCoordinatorSession:
        del mpreset
        self.started.append((prompt, model, idempotency_key))
        return GjcCoordinatorSession("session-1", "turn-1")

    async def send_prompt(
        self,
        session_id: str,
        prompt: str,
        *,
        queue: bool = False,
        idempotency_key: str | None = None,
    ) -> str:
        del queue
        self.sent.append((session_id, prompt, idempotency_key))
        return "turn-2"

    async def submit_question_answer(
        self,
        question: GjcCoordinatorQuestion,
        answer: str,
        *,
        idempotency_key: str | None = None,
    ) -> None:
        self.answers.append((question, answer, idempotency_key))

    async def await_turn(self, session_id: str, turn_id: str) -> GjcCoordinatorTurn:
        del session_id, turn_id
        return self.turns.pop(0)

    async def read_last_assistant(self, session_id: str, *, lines: int = 400) -> str:
        del session_id, lines
        return "tail"


def _factory(client: FakeClient):
    return lambda **_kwargs: client


def _turn(
    text: str, *, status: str = "completed", question: GjcCoordinatorQuestion | None = None
) -> GjcCoordinatorTurn:
    return GjcCoordinatorTurn(
        session_id="session-1",
        turn_id="turn-1",
        status=status,
        text=text,
        error=None if status in {"completed", "waiting_for_answer"} else "failed",
        question=question,
        raw={},
    )


@pytest.mark.asyncio
async def test_execute_task_maps_sdk_turn_to_runtime_result() -> None:
    client = FakeClient([_turn("Done")])
    runtime = GjcRuntime(
        cli_path="/opt/gjc",
        cwd="/tmp/project",
        model="openai-codex/gpt-5.6-sol",
        coordinator_client_factory=_factory(client),
    )

    messages = [message async for message in runtime.execute_task("Do it", tools=["read"])]

    assert messages[-1].content == "Done"
    assert messages[-1].data == {"subtype": "success", "transport": "gjc-coordinator-mcp"}
    assert messages[-1].resume_handle is not None
    assert messages[-1].resume_handle.native_session_id == "session-1"
    assert len(client.started) == 1
    assert client.started[0][:2] == (
        "## Tooling Guidance\nPrefer these tools:\n- read\n\nDo it",
        "openai-codex/gpt-5.6-sol",
    )
    assert client.started[0][2] is not None
    assert client.connected and client.closed


@pytest.mark.asyncio
async def test_question_round_trip_uses_resume_handle_binding() -> None:
    question = GjcCoordinatorQuestion(
        session_id="session-1",
        turn_id="turn-1",
        question_id="q-1",
        answer_binding="binding-1",
        prompt="Choose one",
        options=("A", "B"),
        multi=False,
    )
    client = FakeClient(
        [
            _turn("", status="waiting_for_answer", question=question),
            _turn("Finished"),
        ]
    )
    runtime = GjcRuntime(cwd="/tmp/project", coordinator_client_factory=_factory(client))

    first = [message async for message in runtime.execute_task("Start")][-1]
    assert first.is_error
    assert first.data["error_type"] == "GjcQuestionRequired"
    assert first.resume_handle is not None

    second = [
        message
        async for message in runtime.execute_task(
            "My custom answer",
            resume_handle=first.resume_handle,
        )
    ][-1]
    assert second.content == "Finished"

    assert len(client.answers) == 1
    assert client.answers[0][:2] == (question, "My custom answer")
    assert client.answers[0][2] == first.resume_handle.metadata["gjc_idempotency_key"]
    assert client.sent == []


@pytest.mark.asyncio
async def test_resume_handle_awaits_existing_turn_without_new_prompt() -> None:
    client = FakeClient([_turn("Recovered")])
    runtime = GjcRuntime(cwd="/tmp/project", coordinator_client_factory=_factory(client))
    handle = RuntimeHandle(
        backend="gjc",
        native_session_id="session-1",
        cwd="/tmp/project",
        metadata={"turn_id": "turn-1", "gjc_operation_phase": "awaiting"},
    )

    message = [item async for item in runtime.execute_task("Do not resend", resume_handle=handle)][
        -1
    ]

    assert message.content == "Recovered"
    assert client.sent == []


@pytest.mark.asyncio
async def test_failed_sdk_turn_is_runtime_error() -> None:
    client = FakeClient([_turn("", status="failed")])
    runtime = GjcRuntime(cwd="/tmp/project", coordinator_client_factory=_factory(client))

    message = [item async for item in runtime.execute_task("Do it")][-1]

    assert message.is_error
    assert message.data["error_type"] == "GjcTurnError"
    assert message.content == "failed"


def test_capabilities_use_sdk_resume_and_translate_no_permission_or_tool_envelope() -> None:
    runtime = GjcRuntime(cwd="/tmp/project")
    assert runtime.capabilities.targeted_resume is True
    assert runtime.capabilities.structured_output is True
    assert runtime.capabilities.permission_mode_support is ParamSupport.IGNORED
    assert runtime.capabilities.empty_tool_restriction_support is ParamSupport.IGNORED

    negotiated = negotiate_execution_params(
        runtime.capabilities,
        system_prompt=None,
        tools=[],
        permission_mode="bypassPermissions",
    )
    assert {item.parameter for item in negotiated} == {"tools", "permission_mode"}
