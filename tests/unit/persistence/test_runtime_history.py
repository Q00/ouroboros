from datetime import UTC, datetime, timedelta

import pytest

from ouroboros.events.base import BaseEvent
from ouroboros.mcp.tools.ac_tree_hud_handler import ACTreeHUDHandler
from ouroboros.mcp.tools.job_handlers import JobWaitHandler
from ouroboros.persistence.event_store import EventStore
from ouroboros.persistence.runtime_history import read_runtime_history


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["history", "hud", "job_wait", "legacy"])
async def test_later_rows_before_start_timestamp_reach_public_readers(surface):
    store = EventStore("sqlite+aiosqlite:///:memory:")
    await store.initialize()
    try:
        now = datetime.now(UTC)
        progress = BaseEvent(
            type="workflow.progress.updated",
            aggregate_type="execution",
            aggregate_id="e1",
            timestamp=now - timedelta(seconds=1),
            data={
                "session_id": "s1",
                "execution_id": "e1",
                "acceptance_criteria": [
                    {
                        "node_id": "ac_1",
                        "index": 1,
                        "content": "Late delivery",
                        "status": "completed",
                    }
                ],
            },
        )
        before_start = progress.model_copy(update={"id": "before-start", "timestamp": now})
        await store.append(before_start)
        await store.append(
            BaseEvent(
                type="orchestrator.session.started",
                aggregate_type="session",
                aggregate_id="s1",
                timestamp=now,
                data={"execution_id": "e1"},
            )
        )
        await store.append(progress)
        route = BaseEvent(
            type="execution.ac.route_observed",
            aggregate_type="execution",
            aggregate_id="e1",
            timestamp=now - timedelta(seconds=1),
            data={
                "session_id": "s1",
                "execution_id": "e1",
                "root_ac_index": 0,
                "observation": {"route_id": "delayed"},
                "decision": {"action": "blocked", "reason": "human_handoff_required"},
                "human_handoff_required": True,
            },
        )
        await store.append(route)
        foreign = route.model_copy(
            update={
                "id": "foreign",
                "data": {
                    **route.data,
                    "session_id": "foreign",
                    "observation": {"route_id": "foreign"},
                },
            }
        )
        await store.append(foreign)
        if surface == "legacy":
            events, _ = await store.query_session_related_events_after(
                session_id="s1", execution_id="e1", limit=1000
            )
            ids = {event.id for event in events}
            assert progress.id not in ids and route.id not in ids
            assert before_start.id in ids
        elif surface == "history":
            events, _ = await read_runtime_history(store, execution_id="e1", session_id="s1")
            ids = {event.id for event in events}
            assert progress.id in ids and route.id in ids
            assert before_start.id not in ids and foreign.id not in ids
        elif surface == "hud":
            result = await ACTreeHUDHandler(event_store=store).handle({"session_id": "s1"})
            assert result.is_ok
            assert "Late delivery" in result.value.text_content
            assert "route: delayed" in result.value.text_content
        else:
            for kind, data in [
                (
                    "mcp.job.created",
                    {
                        "job_type": "execute_seed",
                        "links": {"session_id": "s1", "execution_id": "e1"},
                    },
                ),
                (
                    "mcp.job.completed",
                    {"status": "completed", "session_id": "s1", "execution_id": "e1"},
                ),
            ]:
                await store.append(
                    BaseEvent(type=kind, aggregate_type="job", aggregate_id="j1", data=data)
                )
            result = await JobWaitHandler(event_store=store).handle(
                {
                    "job_id": "j1",
                    "cursor": 0,
                    "timeout_seconds": 0,
                    "stream": "linked",
                }
            )
            assert result.is_ok
            relays = result.value.meta["relay_events"]
            assert any(
                r.get("source_event_id") == route.id and r.get("trigger") == "route_blocked"
                for r in relays
            )
            assert all(r.get("source_event_id") != foreign.id for r in relays)
    finally:
        await store.close()
