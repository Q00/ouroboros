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
    given append function (signature `append(envelope: dict) -> None`).
    Production wires it to `EventStore.append`; tests can wire it to a
    list.

This module deliberately does NOT import `persistence/event_store.py`
or its async machinery. It speaks the envelope shape, not the store.
The CLI (#731) is the integration point that takes a real EventStore
async session and bridges to `append_fn`.
"""

from __future__ import annotations

from collections.abc import Callable
import copy
import uuid

PLUGIN_AGGREGATE_TYPE = "plugin"

# Audit event types the firewall may emit. Used by tests + by
# downstream consumers that want to filter ledger queries.
PLUGIN_LIFECYCLE_AUDIT_EVENT_TYPES: tuple[str, ...] = (
    "plugin.discovered",
    "plugin.installed",
    "plugin.trusted",
    "plugin.invoked",
    "plugin.permission_used",
    "plugin.completed",
    "plugin.failed",
)

PLUGIN_HOOK_AUDIT_EVENT_TYPES: tuple[str, ...] = (
    "plugin.hook_started",
    "plugin.hook_completed",
    "plugin.hook_failed",
    "plugin.permission_denied",
)

AUDIT_EVENT_TYPES: tuple[str, ...] = (
    *PLUGIN_LIFECYCLE_AUDIT_EVENT_TYPES,
    *PLUGIN_HOOK_AUDIT_EVENT_TYPES,
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
        # Deep copy: plugin audit events have nested dicts (`plugin`,
        # `command`, `result`, `provenance`). A shallow `dict(...)`
        # would let a caller's later mutation of any nested dict bleed
        # into the wrapped envelope and corrupt the audit log.
        "payload": copy.deepcopy(audit_event),
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
    # Deep copy to mirror ``wrap_plugin_event``'s defensive copy on the way
    # in. ``dict(payload)`` is a shallow copy: mutating any nested dict on
    # the returned value (e.g. ``result["plugin"]["name"] = "X"``) would
    # write through to the live envelope still referenced by the event
    # store, silently corrupting the audit log.
    return copy.deepcopy(payload)


def make_event_sink(
    append_fn: Callable[[dict], None],
    *,
    correlation_id: str,
    aggregate_id: str | None = None,
    envelope_id_factory: Callable[[], str] | None = None,
) -> Callable[[dict], None]:
    """Build a firewall-compatible `EventSink` that wraps and appends.

    Args:
        append_fn: Where to append the wrapped envelope. In production,
            wire to an EventStore.append wrapper. In tests, pass
            `list.append`.
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


__all__ = [
    "AUDIT_EVENT_TYPES",
    "PLUGIN_AGGREGATE_TYPE",
    "PLUGIN_HOOK_AUDIT_EVENT_TYPES",
    "PLUGIN_LIFECYCLE_AUDIT_EVENT_TYPES",
    "make_event_sink",
    "unwrap_plugin_event",
    "wrap_plugin_event",
]
