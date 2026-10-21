from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from ouroboros.core.ac_runtime_projection import (
    ACRuntimeProjection,
    fold_ac_runtime,
    project_ac_runtime,
)
from ouroboros.dashboard.board import reduce_board
from ouroboros.events.base import BaseEvent
from ouroboros.mcp.tools.ac_tree_hud_handler import (
    _compose_progress_data,
    _extract_tree_snapshot,
    _has_tree_change_event,
)
from ouroboros.mcp.tools.attention_relay import classify_relay_events
from ouroboros.persistence.event_store import EventStore, acceptance_generation_id_for_session
from ouroboros.persistence.runtime_history import read_runtime_history
from ouroboros.tui.app import OuroborosTUI


def event(kind: str, **data) -> BaseEvent:
    return BaseEvent(
        type=kind,
        aggregate_type="execution",
        aggregate_id="e1",
        data={"session_id": "s1", "execution_id": "e1", **data},
    )


def receipt(accepted: bool = False, /, **changes) -> BaseEvent:
    return event(
        "execution.ac.acceptance_finalized",
        **{
            "acceptance_generation_id": acceptance_generation_id_for_session("s1", "e1"),
            "root_ac_index": 0,
            "final_retry_attempt": 0,
            "accepted": accepted,
            "disposition": "accepted" if accepted else "rejected",
            "outcome": "succeeded",
            "terminal_status": "completed" if accepted else "failed",
            **changes,
        },
    )


def history() -> list[BaseEvent]:
    return [
        event(
            "workflow.progress.updated",
            acceptance_criteria=[
                {"node_id": "ac_1", "index": 1, "status": "executing", "content": "Ship"},
                {
                    "node_id": "child",
                    "index": 1,
                    "parent_node_id": "ac_1",
                    "status": "completed",
                    "content": "Part",
                },
            ],
        ),
        event("execution.ac.completed", node_id="ac_1", success=True),
        event("execution.ac.attempt_judged", root_ac_index=0, outcome="succeeded"),
    ]


def project(events):
    return project_ac_runtime(
        ((e.type, e.data, e.aggregate_id) for e in events), execution_id="e1", session_id="s1"
    )


def test_lifecycle_success_is_unfinalized_without_final_gate():
    state = project(history()).for_node("ac_1")
    assert (state.lifecycle, state.acceptance, state.decomposition_trust) == (
        "completed",
        "unfinalized",
        "unknown",
    )


def test_rejected_root_does_not_relabel_completed_child():
    state = project([*history(), receipt()])
    assert (state.for_node("ac_1").lifecycle, state.for_node("ac_1").acceptance) == (
        "completed",
        "rejected",
    )
    assert state.for_node("child").acceptance == "unfinalized"


@pytest.mark.parametrize(
    "changes",
    [
        {"accepted": "true"},
        {"accepted": 1},
        {"root_ac_index": True},
        {"acceptance_generation_id": "old"},
        {"session_id": "foreign"},
        {"execution_id": "foreign"},
        {"terminal_status": "failed", "accepted": True},
    ],
)
def test_invalid_or_foreign_receipts_cannot_accept(changes):
    state = project([*history(), receipt(True, **changes)])
    assert state.for_node("ac_1").acceptance == "unfinalized"


def test_conflicting_receipts_remain_conflicted_after_duplicate():
    accepted = receipt(True)
    state = project([*history(), accepted, receipt(), accepted])
    assert state.for_node("ac_1").acceptance == "conflict"


def test_incremental_and_cold_replay_agree_at_every_prefix():
    events = [
        *history(),
        event(
            "execution.ac.route_observed",
            root_ac_index=0,
            observation={"route_id": "route-a"},
            decision={"action": "blocked"},
            human_handoff_required=True,
        ),
        receipt(),
    ]
    state = ACRuntimeProjection(execution_id="e1", session_id="s1")
    for index, item in enumerate(events):
        state = fold_ac_runtime(state, item.type, item.data, aggregate_id=item.aggregate_id)
        assert state == project(events[: index + 1])
    assert (state.for_node("ac_1").route, state.for_node("ac_1").blocked) == ("route-a", True)


@pytest.mark.parametrize("action,blocked", [("blocked", True), ("escalate_route", False)])
def test_direct_runner_route_has_run_scope_and_ignores_foreign_session(action, blocked):
    observed = event(
        "execution.ac.route_observed",
        root_ac_index=None,
        call_site="runner",
        observation={"route_id": "direct"},
        decision={"action": action},
        human_handoff_required=blocked,
    )
    foreign = observed.model_copy(
        update={
            "data": {
                **observed.data,
                "session_id": "foreign",
                "human_handoff_required": not blocked,
            }
        }
    )
    state = project([observed, foreign])
    assert state.run.route == "direct"
    assert state.run.blocked is blocked
    assert state.run.escalation == action
    assert state.roots == ()
    relays = classify_relay_events([observed, foreign], session_id="s1")
    assert len(relays) == 1
    assert relays[0]["runtime"] == state.run.to_data()


def test_recovery_without_node_identity_updates_only_bound_root():
    exhausted = event("execution.ac.recovery_exhausted", root_ac_index=0)
    state = project([*history(), exhausted])
    assert state.for_node("ac_1").escalation == "exhausted"
    assert state.for_node("ac_1").blocked is True
    assert state.for_node("child").blocked is None


def test_board_ambiguous_sessions_do_not_share_final_acceptance():
    events = [
        *history(),
        receipt(True),
        event(
            "workflow.progress.updated",
            session_id="s2",
            acceptance_criteria=[{"node_id": "ac_1", "index": 1, "status": "executing"}],
        ),
    ]
    board = reduce_board(
        [{"event_type": e.type, "payload": e.data} for e in events], execution_id="e1"
    )
    cards = [card for column in board["columns"].values() for card in column]
    assert all(card["runtime"]["acceptance"] == "unfinalized" for card in cards)


@pytest.mark.asyncio
@pytest.mark.parametrize("session_id", [None, "s1"])
async def test_history_freezes_high_water_across_pages(monkeypatch, session_id):
    store = EventStore("sqlite+aiosqlite:///:memory:")
    await store.initialize()
    try:
        now = datetime.now(UTC)
        events = [
            event(
                "execution.node.updated",
                node_id="ac_1",
                root_ac_number=1,
                status="completed" if i == 1000 else "executing",
            ).model_copy(update={"timestamp": now - timedelta(seconds=i)})
            for i in range(1001)
        ]
        await store.append_batch(events)
        appended = event("execution.node.updated", node_id="ac_1", status="failed")
        method = "query_session_related_events_after" if session_id else "get_events_after"
        read_page = getattr(store, method)
        calls = 0

        async def append_after_first_page(*args: Any, **kwargs: Any):
            nonlocal calls
            page = await read_page(*args, **kwargs)
            calls += 1
            if calls == 1:
                await store.append(appended)
            return page

        monkeypatch.setattr(store, method, append_after_first_page)
        cold, cursor = await read_runtime_history(store, execution_id="e1", session_id=session_id)
        assert [item.id for item in cold] == [item.id for item in events]
        assert calls == 2
        assert project(cold).for_node("ac_1").lifecycle == "completed"
        live, advanced = await read_runtime_history(
            store, execution_id="e1", session_id=session_id, after_cursor=cursor
        )
        assert [item.id for item in live] == [appended.id]
        assert advanced > cursor
        assert project([*cold, *live]).for_node("ac_1").lifecycle == "failed"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_cursor_history_preserves_late_delivery_and_foreign_filter():
    store = EventStore("sqlite+aiosqlite:///:memory:")
    await store.initialize()
    try:
        start = datetime.now(UTC)
        first = event(
            "execution.node.created", node_id="ac_1", root_ac_number=1, status="executing"
        ).model_copy(update={"timestamp": start})
        late = event("execution.node.updated", node_id="ac_1", status="completed").model_copy(
            update={"timestamp": start - timedelta(seconds=1)}
        )
        foreign = event(
            "execution.node.updated", node_id="ac_1", session_id="foreign", status="failed"
        )
        for item in (first, late, foreign):
            await store.append(item)
        cold, cursor = await read_runtime_history(store, execution_id="e1", session_id="s1")
        assert [item.id for item in cold] == [first.id, late.id]
        assert project(cold).for_node("ac_1").lifecycle == "completed"
        live, advanced = await read_runtime_history(
            store, execution_id="e1", session_id="s1", after_cursor=cursor
        )
        assert live == []
        assert advanced == cursor
    finally:
        await store.close()


def test_consumer_agreement_on_completed_but_rejected_root():
    events = [*history(), receipt()]
    expected = project(events).for_node("ac_1").to_data()
    board = reduce_board(
        [{"event_type": e.type, "payload": e.data} for e in events], execution_id="e1"
    )
    card = next(card for card in board["columns"]["failed"] if card["id"] == "ac_1")
    hud = _compose_progress_data(events[0].data, events)
    relay = classify_relay_events(events, session_id="s1")[-1]
    app = OuroborosTUI()
    app.set_execution("e1", "s1")
    app.update_ac_tree(
        {
            "root_id": "root",
            "nodes": {
                "root": {"children_ids": ["ac_1"]},
                "ac_1": {"index": 1, "status": "completed", "parent_id": "root"},
            },
        }
    )
    for item in events:
        app._ingest_board_event(item)
    assert card["runtime"] == expected
    assert _extract_tree_snapshot(hud)["nodes"]["ac_1"]["runtime"] == expected
    assert relay["evidence"]["runtime"] == expected
    assert app.state.ac_tree["nodes"]["ac_1"]["runtime"] == expected
    assert _has_tree_change_event([events[-1]])
