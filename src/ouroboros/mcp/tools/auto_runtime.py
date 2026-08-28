"""Shared runtime dependencies for MCP auto construction."""

from ouroboros.persistence.event_store import EventStore


async def initialized_runtime_event_store(event_store: EventStore | None) -> EventStore:
    """Return the injected or default store after one initialization boundary."""
    runtime_event_store = event_store or EventStore()
    await runtime_event_store.initialize()
    return runtime_event_store
