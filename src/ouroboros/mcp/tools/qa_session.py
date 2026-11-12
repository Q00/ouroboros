"""Durable, content-minimal records of completed in-process QA evaluations."""

from __future__ import annotations

from typing import Any

from ouroboros.events.base import BaseEvent
from ouroboros.persistence.event_store import EventStore
from ouroboros.persistence.qa_session_store import (
    QA_EVALUATED,
    append_qa_evaluation,
)


async def record_qa_evaluation(
    event_store: EventStore | None, session_id: str, meta: dict[str, Any]
) -> None:
    """Append one completed evaluation before the tool reports success.

    Only the public score summary is retained, not artifacts, prompts or model
    text.  Session status checks the same stream for collisions, including a
    concurrent claim by another session type. Embedded callers must inject
    their owner's store. None deliberately opens and closes the configured
    default store for standalone QA; a failed write cannot report success.
    """
    owns_store = event_store is None
    store = event_store or EventStore()
    try:
        await store.initialize()
        await append_qa_evaluation(
            store,
            BaseEvent(
                type=QA_EVALUATED,
                aggregate_type="session",
                aggregate_id=session_id,
                data={
                    "iteration": meta["iteration"],
                    "score": meta["score"],
                    "verdict": meta["verdict"],
                    "passed": meta["passed"],
                    "pass_threshold": meta["pass_threshold"],
                },
            ),
        )
    finally:
        if owns_store:
            await store.close()
