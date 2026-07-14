"""Unit contract for idempotent Full host-dispatch completion."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import ValidationError
import pytest

from ouroboros.mcp.tools.host_bridge import (
    HostBridgeHandler,
    HostCompletionReceipt,
    HostDispatchIdentityError,
    HostReceiptConflict,
    HostWorkOrder,
    terminal_event_from_receipt,
)
from ouroboros.persistence.event_store import EventStore


@pytest.fixture
async def event_store(tmp_path: Path):
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'events.db'}")
    await store.initialize()
    yield store
    await store.close()


@pytest.fixture
def work_order(tmp_path: Path) -> HostWorkOrder:
    return HostWorkOrder(
        dispatch_id="dispatch-01",
        session_id="session-01",
        lineage_id="lineage-01",
        workspace_id="workspace-01",
        workspace_root=tmp_path,
        sandbox_mode="workspace-write",
        approval_policy="on-request",
        prompt="Create result.txt.",
        context={"seed_content": "goal: verify"},
        acceptance_criteria=("result.txt exists",),
        evidence_requirements=("sha256",),
        created_at=datetime(2026, 7, 14, 5, 0, tzinfo=UTC),
    )


@pytest.fixture
def completed_receipt(tmp_path: Path) -> HostCompletionReceipt:
    return HostCompletionReceipt(
        dispatch_id="dispatch-01",
        session_id="session-01",
        lineage_id="lineage-01",
        workspace_id="workspace-01",
        workspace_root=tmp_path,
        sandbox_mode="workspace-write",
        approval_policy="on-request",
        terminal_status="completed",
        criterion_results=(
            {
                "criterion": "result.txt exists",
                "passed": True,
                "evidence_refs": ("sha256:result.txt",),
            },
        ),
        evidence=({"kind": "sha256", "value": "a" * 64},),
        changed_paths=(Path("result.txt"),),
        completed_at=datetime(2026, 7, 14, 5, 1, tzinfo=UTC),
        receipt_sha256="b" * 64,
    )


@pytest.fixture
async def host_bridge_handler(
    event_store: EventStore, work_order: HostWorkOrder
) -> HostBridgeHandler:
    handler = HostBridgeHandler(event_store)
    await handler.dispatch(work_order)
    return handler


@pytest.mark.asyncio
async def test_duplicate_receipt_returns_original_hash_without_new_terminal_event(
    host_bridge_handler: HostBridgeHandler,
    event_store: EventStore,
    completed_receipt: HostCompletionReceipt,
) -> None:
    first = await host_bridge_handler.complete(completed_receipt)
    second = await host_bridge_handler.complete(completed_receipt)
    events = await event_store.replay("host_dispatch", completed_receipt.lineage_id)
    terminals = [event for event in events if event.type == "execution.completed"]

    assert first.receipt_sha256 == second.receipt_sha256
    assert len(terminals) == 1


@pytest.mark.asyncio
async def test_two_concurrent_completions_leave_one_terminal(
    host_bridge_handler: HostBridgeHandler,
    event_store: EventStore,
    completed_receipt: HostCompletionReceipt,
) -> None:
    first, second = await asyncio.gather(
        host_bridge_handler.complete(completed_receipt),
        host_bridge_handler.complete(completed_receipt),
    )

    assert first == second
    events = await event_store.replay("host_dispatch", completed_receipt.lineage_id)
    assert sum(event.type == "execution.completed" for event in events) == 1


@pytest.mark.asyncio
async def test_conflicting_concurrent_receipt_rejects_loser(
    host_bridge_handler: HostBridgeHandler,
    event_store: EventStore,
    completed_receipt: HostCompletionReceipt,
) -> None:
    conflicting = completed_receipt.model_copy(update={"receipt_sha256": "c" * 64})
    results = await asyncio.gather(
        host_bridge_handler.complete(completed_receipt),
        host_bridge_handler.complete(conflicting),
        return_exceptions=True,
    )

    assert sum(isinstance(result, HostReceiptConflict) for result in results) == 1
    events = await event_store.replay("host_dispatch", completed_receipt.lineage_id)
    assert sum(event.type == "execution.completed" for event in events) == 1


@pytest.mark.asyncio
async def test_retry_after_terminal_insert_returns_persisted_receipt(
    event_store: EventStore,
    work_order: HostWorkOrder,
    completed_receipt: HostCompletionReceipt,
) -> None:
    first_process = HostBridgeHandler(event_store)
    await first_process.dispatch(work_order)
    await event_store.append_idempotent(terminal_event_from_receipt(completed_receipt))

    resumed_process = HostBridgeHandler(event_store)
    resumed = await resumed_process.complete(completed_receipt)

    assert resumed == completed_receipt
    events = await event_store.replay("host_dispatch", completed_receipt.lineage_id)
    assert sum(event.type == "execution.completed" for event in events) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("workspace_id", "workspace-other"),
        ("workspace_root", Path("/")),
        ("lineage_id", "lineage-other"),
        ("session_id", "session-other"),
        ("sandbox_mode", "danger-full-access"),
        ("approval_policy", "never"),
    ],
)
@pytest.mark.asyncio
async def test_receipt_identity_mismatch_is_rejected(
    host_bridge_handler: HostBridgeHandler,
    completed_receipt: HostCompletionReceipt,
    field: str,
    value: object,
) -> None:
    mismatched = completed_receipt.model_copy(update={field: value})

    with pytest.raises(HostDispatchIdentityError, match=field):
        await host_bridge_handler.complete(mismatched)


def test_failed_receipt_requires_failure_fields(
    completed_receipt: HostCompletionReceipt,
) -> None:
    data = completed_receipt.model_dump(mode="python")
    data.update(terminal_status="failed", receipt_sha256="d" * 64)

    with pytest.raises(ValidationError, match="failure"):
        HostCompletionReceipt.model_validate(data)


def test_cancelled_receipt_requires_cancellation_timestamp(
    completed_receipt: HostCompletionReceipt,
) -> None:
    data = completed_receipt.model_dump(mode="python")
    data.update(terminal_status="cancelled", receipt_sha256="e" * 64)

    with pytest.raises(ValidationError, match="cancelled_at"):
        HostCompletionReceipt.model_validate(data)


def test_cancelled_receipt_rejects_timestamp_after_completion(
    completed_receipt: HostCompletionReceipt,
) -> None:
    data = completed_receipt.model_dump(mode="python")
    data.update(
        terminal_status="cancelled",
        cancelled_at=completed_receipt.completed_at + timedelta(seconds=1),
        receipt_sha256="e" * 64,
    )

    with pytest.raises(ValidationError, match="cancelled_at"):
        HostCompletionReceipt.model_validate(data)


def test_cancelled_receipt_rejects_naive_cancellation_timestamp(
    completed_receipt: HostCompletionReceipt,
) -> None:
    data = completed_receipt.model_dump(mode="python")
    data.update(
        terminal_status="cancelled",
        cancelled_at="2026-07-14T05:01:00",
        receipt_sha256="e" * 64,
    )

    with pytest.raises(ValidationError, match="cancelled_at"):
        HostCompletionReceipt.model_validate(data)
