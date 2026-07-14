"""Reusable host-stage dispatch helpers for Full workflow handlers."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from ouroboros.mcp.tools.host_bridge import (
    HostCompletionReceipt,
    HostDispatchContext,
    HostStageBridge,
)
from ouroboros.persistence.event_store import EventStore


@pytest.fixture
async def event_store(tmp_path: Path):
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'events.db'}")
    await store.initialize()
    yield store
    await store.close()


@pytest.fixture
def host_context(tmp_path: Path) -> HostDispatchContext:
    return HostDispatchContext(
        workspace_id="workspace-01",
        workspace_root=tmp_path,
        sandbox_mode="workspace-write",
        approval_policy="on-request",
        authority_source="fixture",
    )


@pytest.mark.asyncio
async def test_pending_stage_work_is_deterministic_and_contains_continuation(
    event_store: EventStore,
    host_context: HostDispatchContext,
) -> None:
    bridge = HostStageBridge(event_store, host_context)
    arguments = {"session_id": "session-01"}
    payload = {
        "prompt": "Ask exactly one Socratic question.",
        "context": {"action": "resume", "transcript": "Question one"},
    }

    first = await bridge.dispatch_pending(
        stage="interview",
        session_id="session-01",
        lineage_id="session-01",
        payload=payload,
        continuation_tool="ouroboros_interview",
        continuation_arguments=arguments,
        acceptance_criteria=("Return exactly one question",),
        evidence_requirements=("interview_question",),
        pending_text="Complete this work in ChatGPT.",
    )
    second = await bridge.dispatch_pending(
        stage="interview",
        session_id="session-01",
        lineage_id="session-01",
        payload=payload,
        continuation_tool="ouroboros_interview",
        continuation_arguments=arguments,
        acceptance_criteria=("Return exactly one question",),
        evidence_requirements=("interview_question",),
        pending_text="Complete this work in ChatGPT.",
    )

    first_order = first.meta["work_order"]
    second_order = second.meta["work_order"]
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=True).encode()
    ).hexdigest()
    expected_dispatch_id = str(
        uuid5(NAMESPACE_URL, f"ouroboros-host-interview:session-01:{fingerprint}")
    )
    assert first_order["dispatch_id"] == expected_dispatch_id
    assert first_order["dispatch_id"] == second_order["dispatch_id"]
    assert first_order["context"]["continuation"] == {
        "tool_name": "ouroboros_interview",
        "arguments": {
            "session_id": "session-01",
            "_host_dispatch_id": first_order["dispatch_id"],
        },
    }
    assert first.meta["status"] == "host_work_pending"
    assert first.structured_content == {
        "status": "host_work_pending",
        "work_order": first_order,
    }
    events = await event_store.replay("host_dispatch", "session-01")
    assert sum(event.type == "host.dispatch.requested" for event in events) == 1
    assert sum(event.type == "host.dispatch.accepted" for event in events) == 1


@pytest.mark.asyncio
async def test_completed_stage_receipt_is_bound_to_same_session(
    event_store: EventStore,
    host_context: HostDispatchContext,
) -> None:
    bridge = HostStageBridge(event_store, host_context)
    pending = await bridge.dispatch_pending(
        stage="interview",
        session_id="session-01",
        lineage_id="session-01",
        payload={"prompt": "Ask one question.", "context": {}},
        continuation_tool="ouroboros_interview",
        continuation_arguments={"session_id": "session-01"},
        acceptance_criteria=("Return exactly one question",),
        evidence_requirements=("interview_question",),
        pending_text="Complete this work in ChatGPT.",
    )
    order = pending.meta["work_order"]
    receipt = HostCompletionReceipt(
        dispatch_id=order["dispatch_id"],
        session_id="session-01",
        lineage_id="session-01",
        workspace_id=host_context.workspace_id,
        workspace_root=host_context.workspace_root,
        sandbox_mode=host_context.sandbox_mode,
        approval_policy=host_context.approval_policy,
        terminal_status="completed",
        criterion_results=(
            {
                "criterion": "Return exactly one question",
                "passed": True,
                "evidence_refs": ("interview_question:next",),
            },
        ),
        evidence=({"kind": "interview_question", "value": "Who is the user?"},),
        changed_paths=(),
        completed_at=datetime(2026, 7, 14, 8, 0, tzinfo=UTC),
        receipt_sha256="a" * 64,
    )
    await bridge.complete(receipt)

    stored = await bridge.require_completed_receipt(
        dispatch_id=order["dispatch_id"], session_id="session-01"
    )

    assert stored == receipt
    with pytest.raises(ValueError, match="session mismatch"):
        await bridge.require_completed_receipt(
            dispatch_id=order["dispatch_id"], session_id="session-other"
        )
