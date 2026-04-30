"""Unit tests for :class:`ControlBus`.

Issue: #515. The bus is the reactive surface paired with the
observational event factory in #492 / ``events/control.py``.

Coverage:
- ``subscribe`` returns a handle and ``unsubscribe(handle)`` detaches it.
- A predicate filter scopes delivery: subscribers only see matching
  events.
- Multiple subscribers receive the same event independently.
- A failing handler does not affect other handlers' delivery for the
  same event.
- A predicate that raises is treated as ``False`` for that event without
  unsubscribing.
- Slow and fast handlers do not block one another (per-handler task).
- Re-entrant ``subscribe`` from inside a publish loop does not produce a
  ``RuntimeError``.
- ``unsubscribe`` is idempotent.
"""

from __future__ import annotations

import asyncio

import pytest

from ouroboros.events.base import BaseEvent
from ouroboros.orchestrator.control_bus import ControlBus, SubscriptionHandle


def _directive_event(directive: str = "retry") -> BaseEvent:
    """Build a minimal directive event for delivery tests."""
    return BaseEvent(
        type="control.directive.emitted",
        aggregate_type="lineage",
        aggregate_id="lin_bus_test",
        data={
            "directive": directive,
            "reason": "Bus delivery probe.",
            "emitted_by": "test",
        },
    )


def _state_event() -> BaseEvent:
    """An unrelated state event used for predicate filtering."""
    return BaseEvent(
        type="lineage.created",
        aggregate_type="lineage",
        aggregate_id="lin_bus_test",
        data={"goal": "filter probe"},
    )


def _is_directive(event: BaseEvent) -> bool:
    return event.type == "control.directive.emitted"


@pytest.mark.asyncio
async def test_subscribe_and_publish_delivers_to_handler() -> None:
    bus = ControlBus()
    received: list[BaseEvent] = []

    async def handler(event: BaseEvent) -> None:
        received.append(event)

    handle = bus.subscribe(_is_directive, handler)
    assert isinstance(handle, SubscriptionHandle)

    tasks = bus.publish(_directive_event())
    await asyncio.gather(*tasks)

    assert [e.type for e in received] == ["control.directive.emitted"]


@pytest.mark.asyncio
async def test_predicate_filters_unrelated_events() -> None:
    bus = ControlBus()
    seen: list[BaseEvent] = []

    async def handler(event: BaseEvent) -> None:
        seen.append(event)

    bus.subscribe(_is_directive, handler)

    # A non-directive event is published; predicate rejects it, no task spawned.
    tasks = bus.publish(_state_event())
    assert tasks == ()
    assert seen == []

    # A directive event is delivered.
    tasks = bus.publish(_directive_event())
    await asyncio.gather(*tasks)
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_multiple_subscribers_each_receive_event() -> None:
    bus = ControlBus()
    a_seen: list[BaseEvent] = []
    b_seen: list[BaseEvent] = []

    async def handler_a(event: BaseEvent) -> None:
        a_seen.append(event)

    async def handler_b(event: BaseEvent) -> None:
        b_seen.append(event)

    bus.subscribe(_is_directive, handler_a)
    bus.subscribe(_is_directive, handler_b)

    tasks = bus.publish(_directive_event())
    await asyncio.gather(*tasks)

    assert len(a_seen) == 1
    assert len(b_seen) == 1


@pytest.mark.asyncio
async def test_handler_error_isolated_from_siblings() -> None:
    bus = ControlBus()
    healthy_seen: list[BaseEvent] = []

    async def broken(event: BaseEvent) -> None:
        raise RuntimeError("intentional handler failure")

    async def healthy(event: BaseEvent) -> None:
        healthy_seen.append(event)

    bus.subscribe(_is_directive, broken)
    bus.subscribe(_is_directive, healthy)

    tasks = bus.publish(_directive_event())
    # gather() with return_exceptions=False would raise; the handler's
    # exception is *swallowed inside the per-handler wrapper*, so the
    # broken task completes successfully (with the warning logged).
    await asyncio.gather(*tasks)

    assert len(healthy_seen) == 1


@pytest.mark.asyncio
async def test_predicate_exception_is_treated_as_non_match() -> None:
    bus = ControlBus()
    seen: list[BaseEvent] = []

    def broken_predicate(event: BaseEvent) -> bool:
        raise ValueError("intentional predicate failure")

    async def handler(event: BaseEvent) -> None:
        seen.append(event)

    handle = bus.subscribe(broken_predicate, handler)

    tasks = bus.publish(_directive_event())
    assert tasks == ()
    assert seen == []

    # The broken subscription is *not* auto-unsubscribed; idempotent
    # unsubscribe still removes it cleanly.
    bus.unsubscribe(handle)
    bus.unsubscribe(handle)  # second call is a no-op


@pytest.mark.asyncio
async def test_slow_handler_does_not_block_fast_handler() -> None:
    bus = ControlBus()
    fast_done = asyncio.Event()

    async def slow(event: BaseEvent) -> None:
        await asyncio.sleep(0.05)

    async def fast(event: BaseEvent) -> None:
        fast_done.set()

    bus.subscribe(_is_directive, slow)
    bus.subscribe(_is_directive, fast)

    tasks = bus.publish(_directive_event())
    # The fast handler should fire well before the slow one finishes.
    await asyncio.wait_for(fast_done.wait(), timeout=0.5)
    assert fast_done.is_set()
    await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_subscribe_inside_handler_does_not_raise() -> None:
    bus = ControlBus()
    delivery_count = 0

    async def adopter(event: BaseEvent) -> None:
        nonlocal delivery_count
        delivery_count += 1
        # Adding a subscriber while iterating the live publish loop must
        # not raise; ``publish`` snapshots the subscription list.

        async def latecomer(_event: BaseEvent) -> None:
            nonlocal delivery_count
            delivery_count += 1

        bus.subscribe(_is_directive, latecomer)

    bus.subscribe(_is_directive, adopter)

    tasks = bus.publish(_directive_event())
    await asyncio.gather(*tasks)

    # The latecomer is not invoked retroactively for the in-flight event;
    # it will receive the next publish.
    assert delivery_count == 1

    tasks = bus.publish(_directive_event())
    await asyncio.gather(*tasks)
    # Now both subscribers fired: adopter (1) + latecomer (1) for the
    # second event, plus adopter's first delivery.
    assert delivery_count == 3


@pytest.mark.asyncio
async def test_unsubscribe_stops_delivery() -> None:
    bus = ControlBus()
    seen: list[BaseEvent] = []

    async def handler(event: BaseEvent) -> None:
        seen.append(event)

    handle = bus.subscribe(_is_directive, handler)
    bus.unsubscribe(handle)

    tasks = bus.publish(_directive_event())
    assert tasks == ()
    assert seen == []


@pytest.mark.asyncio
async def test_unsubscribe_rejects_foreign_handle_with_same_local_id() -> None:
    bus_a = ControlBus()
    bus_b = ControlBus()
    seen: list[BaseEvent] = []

    async def handler(event: BaseEvent) -> None:
        seen.append(event)

    handle_a = bus_a.subscribe(_is_directive, handler)
    handle_b = bus_b.subscribe(_is_directive, handler)
    assert handle_a._id == handle_b._id

    bus_a.unsubscribe(handle_b)
    tasks = bus_a.publish(_directive_event())
    await asyncio.gather(*tasks)

    assert len(seen) == 1


@pytest.mark.asyncio
async def test_publish_retains_fire_and_forget_tasks_until_done() -> None:
    bus = ControlBus()
    release = asyncio.Event()

    async def slow(event: BaseEvent) -> None:
        await release.wait()

    bus.subscribe(_is_directive, slow)
    tasks = bus.publish(_directive_event())

    assert len(tasks) == 1
    assert len(bus._tasks) == 1
    release.set()
    await asyncio.gather(*tasks)
    await asyncio.sleep(0)
    assert bus._tasks == set()
