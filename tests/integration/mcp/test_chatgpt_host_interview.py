"""Host-driven Interview turns reuse Full state without nested model execution."""

from datetime import UTC, datetime
import json
from pathlib import Path

import pytest

from ouroboros.mcp.server.adapter import create_ouroboros_server
from ouroboros.mcp.tools.host_bridge import HostDispatchContext
from ouroboros.persistence.event_store import EventStore


def _receipt(order: dict[str, object], question: str) -> dict[str, object]:
    criterion = "Return exactly one next Socratic interview question"
    return {
        "dispatch_id": order["dispatch_id"],
        "session_id": order["session_id"],
        "lineage_id": order["lineage_id"],
        "workspace_id": order["workspace_id"],
        "workspace_root": order["workspace_root"],
        "sandbox_mode": order["sandbox_mode"],
        "approval_policy": order["approval_policy"],
        "terminal_status": "completed",
        "criterion_results": (
            {
                "criterion": criterion,
                "passed": True,
                "evidence_refs": ("interview_question:next",),
            },
        ),
        "evidence": ({"kind": "interview_question", "value": question},),
        "changed_paths": (),
        "completed_at": datetime.now(UTC).isoformat(),
        "receipt_sha256": "d" * 64,
    }


async def _complete_and_continue(server, order: dict[str, object], question: str):
    receipt = _receipt(order, question)
    completed = await server.call_tool("ouroboros_complete_host_dispatch", {"receipt": receipt})
    assert completed.is_ok
    continuation = order["context"]["continuation"]
    turn = await server.call_tool(continuation["tool_name"], continuation["arguments"])
    return turn, receipt


@pytest.mark.asyncio
async def test_interview_start_answer_and_resume_use_host_work_orders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("ChatGPT host interview must not construct a CLI/model runtime")

    monkeypatch.setattr("ouroboros.orchestrator.create_agent_runtime", forbidden)
    monkeypatch.setattr("ouroboros.providers.create_llm_adapter", forbidden)
    monkeypatch.setattr("ouroboros.mcp.tools.authoring_handlers.create_llm_adapter", forbidden)
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'interview.db'}")
    context = HostDispatchContext(
        workspace_id="fixture-workspace",
        workspace_root=tmp_path,
        sandbox_mode="workspace-write",
        approval_policy="on-request",
        authority_source="fixture",
    )
    server = create_ouroboros_server(
        runtime_backend="gemini",
        event_store=store,
        state_dir=tmp_path / "state",
        host_dispatch_context=context,
    )

    started = await server.call_tool(
        "ouroboros_interview",
        {
            "initial_context": "Build a safe release assistant",
            "interview_id": "interview_0123456789abcdef",
            "cwd": str(tmp_path),
        },
    )
    assert started.is_ok
    assert started.value.meta["status"] == "host_work_pending"
    start_order = started.value.meta["work_order"]
    assert start_order["session_id"] == "interview_0123456789abcdef"

    first_question = "Who is the primary user of this release assistant?"
    first_turn, first_receipt = await _complete_and_continue(server, start_order, first_question)
    assert first_turn.is_ok
    assert first_turn.value.meta["session_id"] == start_order["session_id"]
    assert first_question in first_turn.value.text_content

    completed_again = await server.call_tool(
        "ouroboros_complete_host_dispatch", {"receipt": first_receipt}
    )
    assert completed_again.is_ok
    continuation = start_order["context"]["continuation"]
    continued_again = await server.call_tool(continuation["tool_name"], continuation["arguments"])
    assert continued_again.is_ok
    state_path = tmp_path / "state" / "interview_interview_0123456789abcdef.json"
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert len(persisted["rounds"]) == 1

    answered = await server.call_tool(
        "ouroboros_interview",
        {
            "session_id": start_order["session_id"],
            "answer": "A non-technical workshop participant.",
            "last_question": first_question,
        },
    )
    assert answered.is_ok
    assert answered.value.meta["status"] == "host_work_pending"
    answer_order = answered.value.meta["work_order"]
    assert answer_order["session_id"] == start_order["session_id"]

    second_question = "What must the participant finish without administrator access?"
    second_turn, _ = await _complete_and_continue(server, answer_order, second_question)
    assert second_turn.is_ok
    assert second_question in second_turn.value.text_content

    resumed = await server.call_tool(
        "ouroboros_interview", {"session_id": start_order["session_id"]}
    )
    assert resumed.is_ok
    assert resumed.value.meta["status"] == "host_work_pending"
    resume_order = resumed.value.meta["work_order"]
    assert resume_order["session_id"] == start_order["session_id"]

    events = await store.replay("host_dispatch", start_order["lineage_id"])
    assert [event.type for event in events].count("host.dispatch.requested") == 3
    await store.close()
