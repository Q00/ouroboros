from datetime import UTC, datetime, timedelta

import pytest

from ouroboros.events.base import BaseEvent
from ouroboros.mcp.tools.job_handlers import JobWaitHandler
from ouroboros.persistence.event_store import EventStore


@pytest.mark.asyncio
async def test_active_job_wait_relays_delayed_worker_route():
    store = EventStore("sqlite+aiosqlite:///:memory:")
    await store.initialize()
    try:
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
                type="mcp.job.created",
                aggregate_type="job",
                aggregate_id="j1",
                data={
                    "job_type": "execute_seed",
                    "links": {"session_id": "s1", "execution_id": "e1"},
                },
            )
        )
        route = BaseEvent(
            type="execution.ac.route_observed",
            aggregate_type="execution",
            aggregate_id="e1_worker",
            timestamp=now - timedelta(seconds=1),
            data={
                "execution_id": "e1",
                "session_id": "s1",
                "root_ac_index": 0,
                "observation": {"route_id": "worker-delayed"},
                "decision": {"action": "blocked", "reason": "human_handoff_required"},
                "human_handoff_required": True,
            },
        )
        await store.append(route)
        result = await JobWaitHandler(event_store=store).handle(
            {
                "job_id": "j1",
                "cursor": 0,
                "timeout_seconds": 0,
                "stream": "linked",
            }
        )
        assert result.is_ok
        assert any(
            relay.get("source_event_id") == route.id and relay.get("trigger") == "route_blocked"
            for relay in result.value.meta["relay_events"]
        )
    finally:
        await store.close()
