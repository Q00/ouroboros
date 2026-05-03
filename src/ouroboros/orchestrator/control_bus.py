"""In-process ControlBus for ``control.directive.emitted`` events.

The bus is the *reactive* surface paired with the *observational* event
factory landed in #492. Subscribers register a predicate and a handler;
when callsites publish a directive event onto the bus, every matching
handler fires concurrently on its own task. A slow or failing handler
never blocks other handlers or the publisher.

Per the maintainer alignment in #476 Q1, the bus is exposed as
``AgentRuntimeContext.control`` (see :mod:`agent_runtime_context`). The
context describes *what the runtime offers*; the bus describes *where
directives flow*. Two different concerns, two different names.

Scope choices baked in here:

* The bus does *not* know about persistence. EventStore stays simple;
  callsites call :meth:`ControlBus.publish` themselves after
  :meth:`EventStore.append`. This honours the "no service locator"
  guardrail from #476 Q1 (narrow membership for the runtime context) and
  keeps the bus a pure in-process primitive that future callers — for
  example the #474 ``AgentRuntimeContext`` migration and #475
  ``evolve_step`` / ``unstuck`` / ``ralph`` wiring — can adopt
  incrementally.

* Cross-runtime delivery is a *separate* concern; that is the Mesh
  (sub-RFC :doc:`../../docs/rfc/mesh`). The Mesh, when it lands, can
  reuse this surface or wrap it.

* Forging resistance is *not* a goal. The cooperative-trust model from
  #476 explicitly accepts that any in-process actor can publish; the bus
  does not police identity.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ouroboros.events.base import BaseEvent


logger = logging.getLogger(__name__)


Predicate = Callable[["BaseEvent"], bool]
Handler = Callable[["BaseEvent"], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class SubscriptionHandle:
    """Opaque token returned by :meth:`ControlBus.subscribe`.

    Callers pass it back to :meth:`ControlBus.unsubscribe` to detach.
    Handles are scoped to the bus that created them so identical local
    integer IDs from different buses cannot detach each other.
    """

    _id: int
    _owner: Any


@dataclass(slots=True)
class _Subscription:
    """One predicate/handler pair tracked by the bus."""

    handle: SubscriptionHandle
    predicate: Predicate
    handler: Handler


@dataclass(slots=True)
class ControlBus:
    """Concurrent in-process publish/subscribe surface for control events.

    The bus is intended to live on :class:`AgentRuntimeContext` for the
    lifetime of an orchestrator session. It is **not** thread-safe in the
    classic shared-memory sense; it expects a single asyncio event loop.
    """

    _subscriptions: list[_Subscription] = field(default_factory=list)
    _next_id: int = 0
    _owner: object = field(default_factory=object)
    _tasks: set[asyncio.Task[None]] = field(default_factory=set)
    _spawn: Callable[[Awaitable[None]], asyncio.Task[None]] | None = None

    def subscribe(self, predicate: Predicate, handler: Handler) -> SubscriptionHandle:
        """Register *handler* to be invoked when *predicate* returns ``True``.

        Args:
            predicate: Pure synchronous filter on the event. Predicate
                exceptions are treated like ``False`` and logged so a
                broken predicate cannot starve other subscribers.
            handler: Async callable invoked once per matching event. The
                handler is awaited on its own task so a slow handler does
                not block fast ones.

        Returns:
            A :class:`SubscriptionHandle` accepted by
            :meth:`unsubscribe`.
        """
        handle = SubscriptionHandle(_id=self._next_id, _owner=self._owner)
        self._next_id += 1
        self._subscriptions.append(
            _Subscription(handle=handle, predicate=predicate, handler=handler)
        )
        return handle

    def unsubscribe(self, handle: SubscriptionHandle) -> None:
        """Remove the subscription identified by *handle*.

        Idempotent: detaching an unknown or already-removed handle is a
        no-op.
        """
        if handle._owner is not self._owner:
            return
        self._subscriptions = [sub for sub in self._subscriptions if sub.handle != handle]

    def publish(self, event: BaseEvent) -> tuple[asyncio.Task[None], ...]:
        """Spawn a delivery task for every matching subscription.

        Returns a tuple of the spawned tasks so tests can ``await`` them.
        Production callers can ignore the return value: handler errors
        are logged on the task so they cannot poison the publisher.

        A subscription whose predicate raises an exception is treated as
        non-matching for that event; the predicate is not unsubscribed.
        """
        spawned: list[asyncio.Task[None]] = []
        for sub in tuple(self._subscriptions):
            try:
                if not sub.predicate(event):
                    continue
            except Exception as exc:  # noqa: BLE001 — predicate isolation
                logger.warning(
                    "control_bus.predicate_raised",
                    extra={"handle_id": sub.handle._id, "error": repr(exc)},
                )
                continue

            spawned.append(self._spawn_handler(sub.handler, event))
        return tuple(spawned)

    def _spawn_handler(self, handler: Handler, event: BaseEvent) -> asyncio.Task[None]:
        """Run *handler* on its own task with structured error isolation."""
        spawn = self._spawn or asyncio.ensure_future
        task = spawn(self._invoke_handler(handler, event))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    @staticmethod
    async def _invoke_handler(handler: Handler, event: BaseEvent) -> None:
        try:
            await handler(event)
        except Exception as exc:  # noqa: BLE001 — handler isolation
            logger.warning(
                "control_bus.handler_raised",
                extra={"event_type": event.type, "error": repr(exc)},
            )
