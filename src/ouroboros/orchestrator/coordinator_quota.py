"""Durable publication and replay contract for coordinator quota pauses."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ouroboros.config import MAX_USAGE_LIMIT_PAUSE_SECONDS, get_usage_limit_pause_seconds
from ouroboros.events.base import BaseEvent
from ouroboros.orchestrator.coordinator import CoordinatorReview
from ouroboros.orchestrator.parallel_executor_models import CoordinatorQuotaPause

if TYPE_CHECKING:
    from ouroboros.orchestrator.level_context import LevelContext


def resolve_usage_limit_pause_seconds(value: int | None) -> int:
    """Resolve and validate the immutable default coordinator quota window."""

    resolved = get_usage_limit_pause_seconds() if value is None else value
    if type(resolved) is not int or not 1 <= resolved <= MAX_USAGE_LIMIT_PAUSE_SECONDS:
        raise ValueError("usage-limit pause seconds are outside the durable range")
    return resolved


def normalize_published_coordinator_pause_owner(
    value: object,
) -> dict[str, object] | None:
    """Copy the optional JSON owner before replay mutates local state."""

    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("published coordinator pause owner must be a mapping")
    return dict(value)


async def resolve_replayed_coordinator_quota_pause(
    *,
    event_store: Any,
    review: CoordinatorReview,
    execution_id: str,
    session_id: str,
    level_number: int,
    coordinator_aggregate_id: str,
    restored: bool,
    published_owner: Mapping[str, object] | None,
) -> tuple[CoordinatorQuotaPause | None, dict[str, object] | None]:
    """Return the unconsumed pause and remaining published owner.

    A completed coordinator artifact can exist before the runner publishes
    ``orchestrator.session.paused``. Such a crash replay returns the same pause.
    Only the exact owner embedded in that lifecycle event may append the
    consumption record that opens the next stage.
    """

    consequence = review.recoverable_quota_pause
    if consequence is None:
        return None, dict(published_owner) if published_owner is not None else None
    pause = CoordinatorQuotaPause(
        execution_id=execution_id,
        session_id=session_id,
        level_number=level_number,
        coordinator_aggregate_id=coordinator_aggregate_id,
        consequence=consequence,
    )
    if not restored:
        return pause, dict(published_owner) if published_owner is not None else None
    consumed = await consume_published_coordinator_pause(
        event_store=event_store,
        pause=pause,
        published_owner=published_owner,
    )
    if not consumed:
        return pause, dict(published_owner) if published_owner is not None else None
    remaining_owner = (
        None if published_owner == pause.owner_payload() else dict(published_owner or {}) or None
    )
    return None, remaining_owner


async def restore_checkpointed_coordinator_quota(
    *,
    event_store: Any,
    level_contexts: list[LevelContext],
    execution_id: str,
    session_id: str,
    published_owner: Mapping[str, object] | None,
) -> tuple[CoordinatorQuotaPause | None, dict[str, object] | None]:
    """Consume or surface every quota owner restored before the next stage."""

    from ouroboros.orchestrator.execution_event_emitter import ExecutionEventEmitter

    remaining_owner = dict(published_owner) if published_owner is not None else None
    for context in level_contexts:
        review = context.coordinator_review
        if review is None or review.recoverable_quota_pause is None:
            continue
        pause, remaining_owner = await resolve_replayed_coordinator_quota_pause(
            event_store=event_store,
            review=review,
            execution_id=execution_id,
            session_id=session_id,
            level_number=review.level_number,
            coordinator_aggregate_id=ExecutionEventEmitter.coordinator_aggregate_id(
                execution_id,
                review.level_number,
            ),
            restored=True,
            published_owner=remaining_owner,
        )
        if pause is not None:
            return pause, remaining_owner
    return None, remaining_owner


async def consume_published_coordinator_pause(
    *,
    event_store: Any,
    pause: CoordinatorQuotaPause,
    published_owner: Mapping[str, object] | None,
) -> bool:
    """Consume one exact published coordinator PAUSED owner at most once."""

    query_events = getattr(event_store, "query_events", None)
    if not callable(query_events):
        raise RuntimeError("coordinator pause consumption requires durable event queries")
    owner_payload = pause.owner_payload()
    try:
        consumed_events = await query_events(
            aggregate_id=pause.coordinator_aggregate_id,
            event_type="execution.coordinator.quota_pause_consumed",
            limit=2,
        )
    except Exception as exc:
        raise RuntimeError("coordinator pause consumption state is unreadable") from exc
    if not isinstance(consumed_events, list | tuple) or len(consumed_events) > 1:
        raise RuntimeError("coordinator pause consumption state is ambiguous")
    if consumed_events:
        consumed = consumed_events[0]
        if (
            getattr(consumed, "type", None) != "execution.coordinator.quota_pause_consumed"
            or getattr(consumed, "aggregate_type", None) != "execution"
            or getattr(consumed, "aggregate_id", None) != pause.coordinator_aggregate_id
            or getattr(consumed, "data", None) != owner_payload
        ):
            raise RuntimeError("coordinator pause consumption identity is invalid")
        return True
    if published_owner is None or dict(published_owner) != owner_payload:
        return False
    try:
        await event_store.append(
            BaseEvent(
                type="execution.coordinator.quota_pause_consumed",
                aggregate_type="execution",
                aggregate_id=pause.coordinator_aggregate_id,
                data=owner_payload,
            )
        )
    except Exception as exc:
        raise RuntimeError("coordinator pause consumption could not be persisted") from exc
    return True


__all__ = [
    "consume_published_coordinator_pause",
    "normalize_published_coordinator_pause_owner",
    "restore_checkpointed_coordinator_quota",
    "resolve_replayed_coordinator_quota_pause",
    "resolve_usage_limit_pause_seconds",
]
