import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from textual.widgets import DataTable

from ouroboros.dashboard.board import reduce_board
from ouroboros.dashboard_web.reader import EventTail
from ouroboros.events.base import BaseEvent
from ouroboros.mcp.tools.ac_tree_hud_handler import ACTreeHUDHandler
from ouroboros.persistence.event_store import EventStore
from ouroboros.tui.app import OuroborosTUI
from ouroboros.tui.screens.dashboard_v3 import NodeDetailPanel


@pytest.mark.asyncio
async def test_execution_only_subscription_keeps_worker_events(tmp_path):
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'worker.db'}")
    await store.initialize()
    await store.append(
        BaseEvent(
            type="execution.ac.model_routed",
            aggregate_type="execution",
            aggregate_id="e1_worker",
            data={"execution_id": "e1", "node_id": "ac_1", "model": "worker-model"},
        )
    )
    app = OuroborosTUI(event_store=store)
    app._poll_interval_seconds = 0.01
    app.set_execution("e1")
    try:
        for _ in range(100):
            if app._ac_runtime.for_node("ac_1").route == "worker-model":
                break
            await asyncio.sleep(0.01)
        assert app._ac_runtime.for_node("ac_1").route == "worker-model"
    finally:
        app._subscription_task.cancel()
        await app._subscription_task
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("completion_offset", [-1, 1])
async def test_subscription_preserves_delivered_completion_in_visible_detail(
    tmp_path, completion_offset
):
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'events.db'}")
    await store.initialize()
    now = datetime.now(UTC)
    await store.append(
        BaseEvent(
            type="orchestrator.session.started",
            aggregate_type="session",
            aggregate_id="s1",
            timestamp=now,
            data={"execution_id": "e1"},
        )
    )
    await store.append(
        BaseEvent(
            type="workflow.progress.updated",
            aggregate_type="execution",
            aggregate_id="e1",
            timestamp=now + timedelta(milliseconds=500),
            data={
                "execution_id": "e1",
                "session_id": "s1",
                "acceptance_criteria": [
                    {
                        "node_id": "ac_1",
                        "index": 1,
                        "content": "Delivered completion",
                        "status": "pending",
                    }
                ],
            },
        )
    )
    await store.append(
        BaseEvent(
            type="execution.node.updated",
            aggregate_type="execution",
            aggregate_id="e1",
            timestamp=now + timedelta(seconds=2),
            data={
                "execution_id": "e1",
                "session_id": "s1",
                "node_id": "ac_1",
                "status": "executing",
            },
        )
    )
    await store.append(
        BaseEvent(
            type="execution.node.updated",
            aggregate_type="execution",
            aggregate_id="e1",
            timestamp=now + timedelta(seconds=completion_offset),
            data={
                "execution_id": "e1",
                "session_id": "s1",
                "node_id": "ac_1",
                "status": "completed",
            },
        )
    )
    await store.append(
        BaseEvent(
            type="execution.ac.route_observed",
            aggregate_type="execution",
            aggregate_id="e1",
            timestamp=now + timedelta(seconds=3),
            data={
                "execution_id": "e1",
                "session_id": "s1",
                "root_ac_index": None,
                "observation": {"route_id": "poll-drained"},
                "decision": {"action": "continue"},
            },
        )
    )
    board = reduce_board(EventTail(tmp_path / "events.db", "e1").fetch_all(), execution_id="e1")
    assert board["columns"]["completed"][0]["runtime"]["lifecycle"] == "completed"
    hud = await ACTreeHUDHandler(event_store=store).handle({"session_id": "s1"})
    assert hud.is_ok and "lifecycle: completed" in hud.value.text_content
    app = OuroborosTUI(event_store=store)
    app._poll_interval_seconds = 0.01
    async with app.run_test(size=(180, 40)) as pilot:
        await pilot.pause()
        app.screen.query_one(DataTable).focus()
        await pilot.press("enter")
        for _ in range(100):
            await pilot.pause(0.02)
            if (
                getattr(app, "_ac_runtime", None) is not None
                and app._ac_runtime.run.route == "poll-drained"
            ):
                break
        assert app._ac_runtime.run.route == "poll-drained"
        app.screen.query_one("#ac-tree").focus()
        await pilot.press("down", "enter")
        await pilot.pause()
        panel = app.screen.query_one(NodeDetailPanel)
        app.save_screenshot(filename="subscription.svg", path=str(tmp_path))
        assert panel.selected_node["runtime"]["lifecycle"] == "completed"
        assert panel.selected_node["runtime"]["acceptance"] == "unfinalized"
        assert app.state.ac_tree["nodes"]["ac_1"]["runtime"]["lifecycle"] == "completed"
        await store.append(
            BaseEvent(
                type="execution.node.updated",
                aggregate_type="execution",
                aggregate_id="e1",
                timestamp=now - timedelta(seconds=2),
                data={
                    "execution_id": "e1",
                    "session_id": "s1",
                    "node_id": "ac_1",
                    "status": "failed",
                },
            )
        )
        for _ in range(100):
            await pilot.pause(0.02)
            if panel.selected_node["runtime"]["lifecycle"] == "failed":
                break
        assert panel.selected_node["runtime"]["lifecycle"] == "failed"
        app.save_screenshot(filename="subscription-live.svg", path=str(tmp_path))
