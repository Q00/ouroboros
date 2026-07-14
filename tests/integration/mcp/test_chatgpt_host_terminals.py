"""Integration coverage for persisted host terminals and MCP registration."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ouroboros.mcp.tools.definitions import get_ouroboros_tools
from ouroboros.mcp.tools.host_bridge import (
    CancelHostDispatchHandler,
    CompleteHostDispatchHandler,
    HostBridgeHandler,
    HostCompletionReceipt,
    HostWorkOrder,
)
from ouroboros.orchestrator.capabilities import ouroboros_tool_capability_registry
from ouroboros.persistence.event_store import EventStore


@pytest.fixture
async def event_store(tmp_path: Path):
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'integration.db'}")
    await store.initialize()
    yield store
    await store.close()


def _order(tmp_path: Path, dispatch_id: str = "dispatch-terminal") -> HostWorkOrder:
    return HostWorkOrder(
        dispatch_id=dispatch_id,
        session_id="session-terminal",
        lineage_id="lineage-terminal",
        workspace_id="workspace-terminal",
        workspace_root=tmp_path,
        sandbox_mode="workspace-write",
        approval_policy="on-request",
        prompt="Finish Full host work.",
        acceptance_criteria=("host work closes",),
        evidence_requirements=("event_lineage",),
        created_at=datetime(2026, 7, 14, 5, 0, tzinfo=UTC),
    )


def _receipt(
    tmp_path: Path,
    *,
    status: str,
    dispatch_id: str = "dispatch-terminal",
) -> HostCompletionReceipt:
    terminal_fields: dict[str, object] = {}
    if status == "failed":
        terminal_fields["failure"] = {"code": "HOST_FAILED", "message": "task failed"}
    if status == "cancelled":
        terminal_fields["cancelled_at"] = datetime(2026, 7, 14, 5, 1, tzinfo=UTC)
    return HostCompletionReceipt(
        dispatch_id=dispatch_id,
        session_id="session-terminal",
        lineage_id="lineage-terminal",
        workspace_id="workspace-terminal",
        workspace_root=tmp_path,
        sandbox_mode="workspace-write",
        approval_policy="on-request",
        terminal_status=status,
        criterion_results=(),
        evidence=(),
        changed_paths=(),
        completed_at=datetime(2026, 7, 14, 5, 1, tzinfo=UTC),
        receipt_sha256={"completed": "a", "failed": "b", "cancelled": "c"}[status] * 64,
        **terminal_fields,
    )


@pytest.mark.parametrize(
    ("status", "event_type"),
    [
        ("completed", "execution.completed"),
        ("failed", "host.dispatch.failed"),
        ("cancelled", "host.dispatch.cancelled"),
    ],
)
@pytest.mark.asyncio
async def test_full_event_store_persists_each_host_terminal(
    event_store: EventStore,
    tmp_path: Path,
    status: str,
    event_type: str,
) -> None:
    dispatch_id = f"dispatch-{status}"
    handler = HostBridgeHandler(event_store)
    await handler.dispatch(_order(tmp_path, dispatch_id))

    receipt = await handler.complete(_receipt(tmp_path, status=status, dispatch_id=dispatch_id))
    events, cursor = await event_store.get_events_after(
        "host_dispatch", "lineage-terminal", 0
    )

    assert receipt.terminal_status.value == status
    assert [event.type for event in events] == ["host.dispatch.requested", event_type]
    assert events[-1].data["status"] == status
    assert cursor > 0


def test_host_completion_tools_are_registered_with_full_wording(
    event_store: EventStore,
) -> None:
    tools = get_ouroboros_tools(include_auto=False, event_store=event_store)
    definitions = {handler.definition.name: handler.definition for handler in tools}

    assert isinstance(
        next(handler for handler in tools if isinstance(handler, CompleteHostDispatchHandler)),
        CompleteHostDispatchHandler,
    )
    assert isinstance(
        next(handler for handler in tools if isinstance(handler, CancelHostDispatchHandler)),
        CancelHostDispatchHandler,
    )
    assert "closing an Ouroboros Full dispatch" in definitions[
        "ouroboros_complete_host_dispatch"
    ].description
    assert "closing an Ouroboros Full dispatch" in definitions[
        "ouroboros_cancel_host_dispatch"
    ].description


def test_host_completion_tools_have_explicit_capability_metadata() -> None:
    registry = ouroboros_tool_capability_registry()

    for tool_name in (
        "ouroboros_complete_host_dispatch",
        "ouroboros_cancel_host_dispatch",
    ):
        metadata = registry[tool_name]
        assert metadata.fallback_used is False
        assert metadata.mutation_targets == ("event_store",)


@pytest.mark.asyncio
async def test_complete_tool_rejects_cancellation_receipt(
    event_store: EventStore, tmp_path: Path
) -> None:
    bridge = HostBridgeHandler(event_store)
    await bridge.dispatch(_order(tmp_path))
    handler = CompleteHostDispatchHandler(bridge)

    result = await handler.handle(
        {"receipt": _receipt(tmp_path, status="cancelled").model_dump(mode="json")}
    )

    assert result.is_err
    assert "requires terminal_status=completed or failed" in str(result.error)
