"""Adapter from firewall audit events to the core event store.

The firewall (`plugin/firewall.py`) emits events that conform to
`schemas/0.1/audit-event.schema.json`. Those events have
`additionalProperties: false`, so any wrapping fields the core ledger
needs (`id`, `aggregate_type`, `aggregate_id`, `timestamp`) MUST live
in a layer ABOVE the audit-event boundary, not inside it.

This adapter:

  - `wrap_plugin_event(event_dict, *, correlation_id, aggregate_id=None)`
    returns a row-shaped envelope ready for `persistence.event_store`.
    The full audit event becomes the `payload`; the envelope adds the
    fields the core ledger requires.

  - `unwrap_plugin_event(envelope) -> dict`
    returns the original audit event from a stored envelope. Used by
    consumers that need to round-trip events back into schema-valid
    form (e.g. `ooo plugin status` reading the audit trail).

  - `make_event_sink(append_fn, *, correlation_id, ...) -> EventSink`
    factory that produces a `firewall.EventSink` callable wired to a
    sync `append_fn` whose signature is `(envelope: dict) -> None`.
    Tests pass `list.append`. Production callers compose this with the
    bridge helpers below — `EventStore.append` is async and rejects
    anything that is not a `BaseEvent`, so it MUST NOT be passed to
    `make_event_sink` directly. The bridge from the dict-shaped
    envelope to a real EventStore lives in `envelope_to_base_event`
    and `append_envelope_to_event_store` (the async helper).

  - `envelope_to_base_event(envelope) -> BaseEvent`
    pure converter from the envelope dict produced by
    `wrap_plugin_event` to a `BaseEvent` instance the core ledger
    accepts. Lazy-imports `BaseEvent` so this module stays cheap to
    import in non-persistence contexts (tests, schema validation).

  - `async append_envelope_to_event_store(envelope, *, store) -> None`
    awaits `store.append(...)` after converting the envelope. This is
    the canonical async bridge; firewall integrations running on an
    asyncio loop should call it directly. For sync stacks (the
    firewall is sync) the integration layer must hop to the loop —
    e.g. `asyncio.run_coroutine_threadsafe(...)` — and pass the
    resulting `Callable[[dict], None]` to `make_event_sink`.

This module deliberately does NOT import `persistence/event_store.py`
or its async machinery at module scope. It speaks the envelope shape,
not the store. The bridge helpers above lazily pull in `BaseEvent` and
`EventStore` only when they are actually invoked, so callers that
never persist (tests, schema validators) keep the import surface small.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
import uuid

if TYPE_CHECKING:  # pragma: no cover — typing-only import to avoid cycles
    from ouroboros.events.base import BaseEvent
    from ouroboros.persistence.event_store import EventStore

PLUGIN_AGGREGATE_TYPE = "plugin"

# Audit event types the firewall may emit. Used by tests + by
# downstream consumers that want to filter ledger queries.
AUDIT_EVENT_TYPES: tuple[str, ...] = (
    "plugin.discovered",
    "plugin.installed",
    "plugin.trusted",
    "plugin.invoked",
    "plugin.permission_used",
    "plugin.completed",
    "plugin.failed",
)


def wrap_plugin_event(
    audit_event: dict,
    *,
    correlation_id: str,
    aggregate_id: str | None = None,
    envelope_id: str | None = None,
) -> dict:
    """Wrap a plugin audit event in a core-ledger row envelope.

    Args:
        audit_event: A dict matching schemas/0.1/audit-event.schema.json.
            This becomes the `payload` field verbatim — the schema's
            `additionalProperties: false` is preserved by NOT mutating
            the event in place.
        correlation_id: Cross-event correlation id (from the firewall).
            Used as the default `aggregate_id`.
        aggregate_id: Override for the aggregate id. Defaults to
            `correlation_id`. Must be a string; the events_table column
            is `String(36)` so callers should keep it short (UUID-shaped
            is conventional).
        envelope_id: Override for the row's UUID. Defaults to a fresh
            uuid4. Tests pin a specific id for determinism.

    Returns:
        A dict with the envelope fields (`id`, `aggregate_type`,
        `aggregate_id`, `event_type`, `payload`, `timestamp`).
    """
    if not isinstance(audit_event, dict):
        raise TypeError(f"audit_event must be dict, got {type(audit_event).__name__}")
    if "event_type" not in audit_event:
        raise ValueError("audit_event missing 'event_type'")
    if "occurred_at" not in audit_event:
        raise ValueError("audit_event missing 'occurred_at'")

    return {
        "id": envelope_id or str(uuid.uuid4()),
        "aggregate_type": PLUGIN_AGGREGATE_TYPE,
        "aggregate_id": aggregate_id or correlation_id,
        "event_type": audit_event["event_type"],
        "payload": dict(audit_event),  # shallow copy so callers can't mutate stored form
        "timestamp": audit_event["occurred_at"],
    }


def unwrap_plugin_event(envelope: dict) -> dict:
    """Extract the original audit event from a wrapped envelope.

    Args:
        envelope: A dict produced by `wrap_plugin_event`, or a row read
            back from the event store.

    Returns:
        The original audit event (a dict matching audit-event.schema.json).

    Raises:
        ValueError: if the envelope is not a plugin envelope or its
            payload is missing.
    """
    agg = envelope.get("aggregate_type")
    if agg != PLUGIN_AGGREGATE_TYPE:
        raise ValueError(
            f"envelope is not a plugin envelope (aggregate_type={agg!r}); "
            f"expected {PLUGIN_AGGREGATE_TYPE!r}"
        )
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("envelope payload is missing or not a dict")
    return dict(payload)


def make_event_sink(
    append_fn: Callable[[dict], None],
    *,
    correlation_id: str,
    aggregate_id: str | None = None,
    envelope_id_factory: Callable[[], str] | None = None,
) -> Callable[[dict], None]:
    """Build a firewall-compatible `EventSink` that wraps and appends.

    Args:
        append_fn: A SYNC callable accepting the wrapped envelope dict.
            Tests pass `list.append`. Production callers MUST NOT pass
            `EventStore.append` directly — that method is async and
            rejects non-`BaseEvent` payloads. Compose with the bridge
            helpers in this module: convert to a `BaseEvent` via
            `envelope_to_base_event` and persist via
            `append_envelope_to_event_store`, then schedule the
            coroutine onto the running loop (e.g. with
            `asyncio.run_coroutine_threadsafe`) to obtain the sync
            callable this parameter expects.
        correlation_id: Default aggregate id and forwarded to wrap.
        aggregate_id: Override.
        envelope_id_factory: Override for envelope id generation
            (tests pass a counter).

    Returns:
        A callable that wraps each audit event and forwards to append_fn.
    """

    def _sink(audit_event: dict) -> None:
        envelope = wrap_plugin_event(
            audit_event,
            correlation_id=correlation_id,
            aggregate_id=aggregate_id,
            envelope_id=envelope_id_factory() if envelope_id_factory else None,
        )
        append_fn(envelope)

    return _sink


# ---------------------------------------------------------------------------
# Bridge to the core EventStore. Lazy imports keep this module cheap.
# ---------------------------------------------------------------------------


# Audit-event types that are still authoritative as `BaseEvent.type` strings
# for plugin events when persisted in the core ledger. Mirrors the
# audit-event schema's `event_type` enum.
def envelope_to_base_event(
    envelope: dict,
    *,
    event_class: type[BaseEvent] | None = None,
) -> BaseEvent:
    """Convert a wrapped plugin envelope into a `BaseEvent`.

    Bridges the dict-shaped contract this module speaks into the
    `BaseEvent` shape `EventStore.append` requires. The audit event
    payload (the inner schema-validated dict) is preserved verbatim
    under `data` so consumers can still round-trip it back to
    schema-valid form via `unwrap_plugin_event`.

    Args:
        envelope: Output of `wrap_plugin_event`.
        event_class: Optional `BaseEvent` subclass override. Defaults
            to a minimal in-module subclass that pins
            `aggregate_type='plugin'` and uses the envelope's
            `event_type` as `BaseEvent.type`.

    Returns:
        A frozen `BaseEvent` instance ready for `EventStore.append`.

    Raises:
        ValueError: if the envelope is not a plugin envelope produced
            by `wrap_plugin_event`.
    """
    if envelope.get("aggregate_type") != PLUGIN_AGGREGATE_TYPE:
        raise ValueError(
            f"envelope is not a plugin envelope (aggregate_type="
            f"{envelope.get('aggregate_type')!r}); expected "
            f"{PLUGIN_AGGREGATE_TYPE!r}"
        )

    cls = event_class or _default_plugin_event_class()
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("envelope payload is missing or not a dict")

    # Carry the original audit time through to BaseEvent.timestamp so
    # the persisted row reflects when the firewall observed the event,
    # not when the bridge ran. Without this the EventStore's "now"
    # default silently rewrites event ordering and recency for plugin
    # rows. Prefer the envelope's `timestamp` (set by `wrap_plugin_event`
    # from `audit_event.occurred_at`); fall back to the payload's
    # `occurred_at` defensively in case a third-party builder skipped
    # the envelope helper.
    timestamp_str = envelope.get("timestamp") or payload.get("occurred_at")
    if not isinstance(timestamp_str, str):
        raise ValueError(
            "envelope is missing a string 'timestamp' / payload.occurred_at; "
            "cannot bridge to BaseEvent without preserving audit time"
        )
    timestamp = _parse_audit_timestamp(timestamp_str)

    return cls(
        id=envelope["id"],
        type=envelope["event_type"],
        timestamp=timestamp,
        aggregate_type=PLUGIN_AGGREGATE_TYPE,
        aggregate_id=envelope["aggregate_id"],
        data=dict(payload),  # frozen BaseEvent: defensive shallow copy
    )


def _parse_audit_timestamp(value: str) -> datetime:
    """Parse a firewall audit timestamp into a tz-aware UTC datetime.

    The audit-event schema fixes the wire format at
    `"%Y-%m-%dT%H:%M:%SZ"` (RFC 3339, second precision, explicit Z).
    `datetime.fromisoformat` accepts the trailing `Z` from Python 3.11
    onward, but we normalize defensively for older callers and to
    surface a clean ValueError with the offending string.
    """
    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError(
            f"audit timestamp {value!r} is not RFC 3339; expected 'YYYY-MM-DDTHH:MM:SSZ'"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    else:
        parsed = parsed.astimezone(UTC)
    return parsed


async def append_envelope_to_event_store(
    envelope: dict,
    *,
    store: EventStore,
    event_class: type[BaseEvent] | None = None,
) -> None:
    """Persist a wrapped plugin envelope to the core EventStore.

    The canonical async bridge from this module's dict-shaped contract
    to the live ledger. Convert with `envelope_to_base_event`, then
    `await store.append(...)`. Use directly from async code; for the
    sync firewall, schedule onto the running loop and wrap the result
    in `make_event_sink`'s `append_fn`.
    """
    event = envelope_to_base_event(envelope, event_class=event_class)
    await store.append(event)


def _default_plugin_event_class() -> type[BaseEvent]:
    """Lazy-construct the default `BaseEvent` subclass for plugin events.

    Defined as a function so importing this module never pulls in
    `events.base` (which transitively touches Pydantic) for callers
    that only need the dict-shaped helpers.
    """
    global _DEFAULT_PLUGIN_EVENT_CLASS
    if _DEFAULT_PLUGIN_EVENT_CLASS is not None:
        return _DEFAULT_PLUGIN_EVENT_CLASS

    from ouroboros.events.base import BaseEvent as _BaseEvent

    class PluginAuditEvent(_BaseEvent):
        """Generic plugin audit event for the core ledger.

        Concrete subclasses can override `event_version` / type if a
        firewall feature needs richer typing; the default is intended
        for v0 dict-shaped persistence.
        """

    _DEFAULT_PLUGIN_EVENT_CLASS = PluginAuditEvent
    return PluginAuditEvent


_DEFAULT_PLUGIN_EVENT_CLASS: type[Any] | None = None


__all__ = [
    "AUDIT_EVENT_TYPES",
    "PLUGIN_AGGREGATE_TYPE",
    "append_envelope_to_event_store",
    "envelope_to_base_event",
    "make_event_sink",
    "unwrap_plugin_event",
    "wrap_plugin_event",
]
