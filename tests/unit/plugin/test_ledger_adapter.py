"""Tests for the firewall-to-core-ledger adapter (Q00/ouroboros#737)."""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator
import pytest

from ouroboros.plugin.ledger_adapter import (
    AUDIT_EVENT_TYPES,
    PLUGIN_AGGREGATE_TYPE,
    make_event_sink,
    unwrap_plugin_event,
    wrap_plugin_event,
)

# Audit-event schema is vendored in CR-3 at this path.
SCHEMA_PATH = Path(__file__).resolve().parents[3] / (
    "src/ouroboros/plugin/schemas/0.1/audit-event.schema.json"
)
AUDIT_SCHEMA = json.loads(SCHEMA_PATH.read_text())
AUDIT_VALIDATOR = Draft202012Validator(AUDIT_SCHEMA)


def _audit_event(event_type: str, **overrides) -> dict:
    """Build an audit event matching schemas/0.1/audit-event.schema.json."""
    base = {
        "schema_version": "0.1",
        "event_type": event_type,
        "occurred_at": "2026-05-07T12:00:00Z",
        "plugin": {
            "name": "github-pr-ops",
            "version": "0.1.0",
            "source_type": "plugin_home",
        },
        "command": {"namespace": "github-pr", "name": "review", "argv": ["url"]},
        "trust_state": "trusted",
        "capabilities_used": [],
        "permissions_used": ["github:read"],
        "result": {"status": "success"},
    }
    base.update(overrides)
    # Confirm the test fixture itself validates against the schema.
    errs = list(AUDIT_VALIDATOR.iter_errors(base))
    assert not errs, f"test fixture invalid: {errs}"
    return base


def test_wrap_basic_envelope() -> None:
    """Wrapping produces the documented envelope shape."""
    ev = _audit_event("plugin.invoked")
    env = wrap_plugin_event(ev, correlation_id="corr-1")

    assert env["aggregate_type"] == PLUGIN_AGGREGATE_TYPE
    assert env["aggregate_id"] == "corr-1"
    assert env["event_type"] == "plugin.invoked"
    assert env["timestamp"] == "2026-05-07T12:00:00Z"
    assert isinstance(env["id"], str) and len(env["id"]) > 0
    # payload contains the full audit event
    assert env["payload"]["schema_version"] == "0.1"
    assert env["payload"]["plugin"]["name"] == "github-pr-ops"


def test_wrap_does_not_mutate_input() -> None:
    """The audit event passed in must not be mutated by wrap()."""
    ev = _audit_event("plugin.invoked")
    snapshot = json.dumps(ev, sort_keys=True)
    wrap_plugin_event(ev, correlation_id="x")
    assert json.dumps(ev, sort_keys=True) == snapshot


def test_wrap_does_not_inject_fields_into_audit_event() -> None:
    """Envelope fields stay above the audit event boundary.

    The audit-event schema declares additionalProperties:false. The
    payload must remain schema-valid after wrapping.
    """
    ev = _audit_event("plugin.invoked")
    env = wrap_plugin_event(ev, correlation_id="x")
    # payload alone must validate as an audit event.
    errs = list(AUDIT_VALIDATOR.iter_errors(env["payload"]))
    assert not errs, f"payload no longer schema-valid: {errs}"


def test_unwrap_returns_audit_event() -> None:
    """unwrap recovers the audit event from an envelope."""
    ev = _audit_event("plugin.completed")
    env = wrap_plugin_event(ev, correlation_id="x")
    unwrapped = unwrap_plugin_event(env)
    # validate as audit event
    errs = list(AUDIT_VALIDATOR.iter_errors(unwrapped))
    assert not errs
    # equality with original
    assert unwrapped == ev


def test_round_trip_for_all_seven_event_types() -> None:
    """Round-trip every event type defined in the schema."""
    for event_type in AUDIT_EVENT_TYPES:
        ev = _audit_event(event_type)
        env = wrap_plugin_event(ev, correlation_id=f"corr-{event_type}")
        recovered = unwrap_plugin_event(env)
        assert recovered == ev, f"{event_type} did not round-trip"
        # And the envelope's event_type matches.
        assert env["event_type"] == event_type


def test_unwrap_returned_value_is_isolated_from_envelope() -> None:
    """Mutating the unwrapped audit event must not corrupt the envelope.

    `wrap_plugin_event` deep-copies on the way in (line 125 of
    ``ledger_adapter.py``); the inverse must also deep-copy on the way out.
    If ``unwrap_plugin_event`` returned only ``dict(payload)`` (shallow),
    a caller writing ``result["plugin"]["name"] = "X"`` would mutate the
    live envelope still referenced by the event store, corrupting the
    audit log without any visible API surface to detect it.
    """
    ev = _audit_event("plugin.completed")
    env = wrap_plugin_event(ev, correlation_id="x")
    unwrapped = unwrap_plugin_event(env)

    # Mutate every nested container the audit event has.
    unwrapped["plugin"]["name"] = "MUTATED-PLUGIN-NAME"
    unwrapped["command"]["argv"].append("--injected")
    unwrapped["capabilities_used"].append("fs.write")
    unwrapped["result"]["status"] = "MUTATED-STATUS"

    # The envelope's payload must be unchanged.
    payload = env["payload"]
    assert payload["plugin"]["name"] != "MUTATED-PLUGIN-NAME"
    assert "--injected" not in payload["command"].get("argv", [])
    assert "fs.write" not in payload["capabilities_used"]
    assert payload["result"]["status"] != "MUTATED-STATUS"


def test_unwrap_rejects_non_plugin_envelope() -> None:
    """Envelope with the wrong aggregate_type is rejected."""
    fake = {
        "id": "x",
        "aggregate_type": "execution",
        "aggregate_id": "y",
        "event_type": "execution.something",
        "payload": {},
        "timestamp": "2026-05-07T12:00:00Z",
    }
    with pytest.raises(ValueError, match="not a plugin envelope"):
        unwrap_plugin_event(fake)


def test_wrap_requires_event_type() -> None:
    """Wrapping fails fast if the audit event is missing event_type."""
    with pytest.raises(ValueError, match="event_type"):
        wrap_plugin_event({"occurred_at": "x"}, correlation_id="c")


def test_wrap_requires_occurred_at() -> None:
    """Wrapping fails fast if the audit event is missing occurred_at."""
    with pytest.raises(ValueError, match="occurred_at"):
        wrap_plugin_event({"event_type": "plugin.invoked"}, correlation_id="c")


def test_aggregate_id_override() -> None:
    """aggregate_id parameter overrides the correlation_id default."""
    ev = _audit_event("plugin.invoked")
    env = wrap_plugin_event(ev, correlation_id="default", aggregate_id="custom")
    assert env["aggregate_id"] == "custom"
    assert env["aggregate_id"] != "default"


def test_make_event_sink_appends_envelopes() -> None:
    """The sink wraps each audit event and forwards to append_fn."""
    rows: list[dict] = []
    sink = make_event_sink(rows.append, correlation_id="corr-x")
    sink(_audit_event("plugin.invoked"))
    sink(_audit_event("plugin.completed"))
    assert len(rows) == 2
    for row in rows:
        assert row["aggregate_type"] == PLUGIN_AGGREGATE_TYPE
        assert row["aggregate_id"] == "corr-x"


def test_make_event_sink_with_id_factory() -> None:
    """envelope_id_factory provides deterministic ids for tests."""
    rows: list[dict] = []
    counter = iter(["env-1", "env-2"])
    sink = make_event_sink(
        rows.append,
        correlation_id="x",
        envelope_id_factory=lambda: next(counter),
    )
    sink(_audit_event("plugin.invoked"))
    sink(_audit_event("plugin.completed"))
    assert [r["id"] for r in rows] == ["env-1", "env-2"]


def test_envelope_event_type_matches_payload_event_type() -> None:
    """The envelope's event_type string mirrors the payload's event_type
    (so the events_table.event_type column is queryable without parsing
    the JSON payload)."""
    for event_type in AUDIT_EVENT_TYPES:
        ev = _audit_event(event_type)
        env = wrap_plugin_event(ev, correlation_id="x")
        assert env["event_type"] == env["payload"]["event_type"] == event_type


def test_wrap_rejects_non_dict_input() -> None:
    """Wrapping a non-dict raises TypeError."""
    with pytest.raises(TypeError, match="must be dict"):
        wrap_plugin_event("not a dict", correlation_id="x")  # type: ignore[arg-type]


def test_no_raw_token_fields_in_envelope() -> None:
    """Sanity: the envelope contains no token-shaped keys.

    Plugin events go through the firewall's bounded-payload guard;
    this test confirms the adapter doesn't accidentally introduce one.
    """
    ev = _audit_event("plugin.completed")
    env = wrap_plugin_event(ev, correlation_id="x")
    serialized = json.dumps(env).lower()
    for forbidden in ("ghp_", "bearer ", "x-api-key"):
        assert forbidden.lower() not in serialized


def test_envelope_to_base_event_round_trips_through_real_event_store() -> None:
    """Regression: the bridge from a wrapped envelope to a `BaseEvent`
    that `EventStore.append` accepts is real, not aspirational.

    The previous adapter docstring claimed `make_event_sink` could be
    wired to `EventStore.append` directly, but that method is async
    and rejects non-`BaseEvent` payloads. The new
    `envelope_to_base_event` + `append_envelope_to_event_store`
    helpers actually bridge the contract. This test exercises the
    full path against an in-memory EventStore and asserts the
    persisted row reproduces the original audit event.
    """
    pytest.importorskip("aiosqlite")  # safety on slim CI images

    import asyncio

    from ouroboros.persistence.event_store import EventStore
    from ouroboros.plugin.ledger_adapter import (
        append_envelope_to_event_store,
        envelope_to_base_event,
    )

    audit = _audit_event("plugin.completed")
    envelope = wrap_plugin_event(audit, correlation_id="corr-bridge", envelope_id="env-bridge")

    base_event = envelope_to_base_event(envelope)
    # The bridge speaks BaseEvent's contract: aggregate_type pinned,
    # event_type matches the audit payload, payload preserved verbatim.
    assert base_event.aggregate_type == "plugin"
    assert base_event.aggregate_id == "corr-bridge"
    assert base_event.type == "plugin.completed"
    assert base_event.data["event_type"] == "plugin.completed"
    assert base_event.data["plugin"]["name"] == "github-pr-ops"

    async def _persist_and_replay() -> list:
        store = EventStore("sqlite+aiosqlite:///:memory:")
        await store.initialize()
        # The async bridge is what production code is expected to use.
        await append_envelope_to_event_store(envelope, store=store)
        return await store.replay("plugin", "corr-bridge")

    rows = asyncio.run(_persist_and_replay())
    assert len(rows) == 1
    persisted = rows[0]
    assert persisted.type == "plugin.completed"
    assert persisted.aggregate_type == "plugin"
    assert persisted.aggregate_id == "corr-bridge"
    # The original audit event survives a full round-trip — `unwrap_plugin_event`
    # reconstructs the schema-valid form from the persisted row's data.
    rebuilt_envelope = {
        "id": persisted.id,
        "aggregate_type": persisted.aggregate_type,
        "aggregate_id": persisted.aggregate_id,
        "event_type": persisted.type,
        "payload": persisted.data,
        "timestamp": persisted.timestamp,
    }
    rebuilt_audit = unwrap_plugin_event(rebuilt_envelope)
    assert rebuilt_audit["event_type"] == audit["event_type"]
    assert rebuilt_audit["plugin"] == audit["plugin"]
    assert rebuilt_audit["command"] == audit["command"]


def test_wrap_payload_is_deep_copied() -> None:
    """Regression: `wrap_plugin_event` must defend against later
    mutation of nested dicts in the original audit event.

    The previous shallow `dict(audit_event)` copy still shared
    `audit_event["plugin"]`, `["command"]`, `["result"]`, etc., so a
    caller mutating `audit_event["plugin"]["name"]` after wrap would
    silently corrupt the persisted envelope. The contract says the
    payload is owned by the envelope; deep-copy enforces it.
    """
    audit = _audit_event("plugin.invoked")
    env = wrap_plugin_event(audit, correlation_id="x")

    # Mutate the nested dicts on the SOURCE — none of these mutations
    # may leak into the envelope payload.
    audit["plugin"]["name"] = "tampered"
    audit["command"]["argv"].append("evil")
    audit["result"]["status"] = "tampered"

    assert env["payload"]["plugin"]["name"] == "github-pr-ops"
    assert env["payload"]["command"]["argv"] == ["url"]
    assert env["payload"]["result"]["status"] == "success"


def test_envelope_to_base_event_data_is_deep_copied() -> None:
    """Regression: `BaseEvent.data` must not alias the envelope's
    payload. Mutating the envelope after the bridge runs must not
    reach into the persisted event."""
    from ouroboros.plugin.ledger_adapter import envelope_to_base_event

    audit = _audit_event("plugin.completed")
    env = wrap_plugin_event(audit, correlation_id="x")
    base = envelope_to_base_event(env)

    env["payload"]["plugin"]["name"] = "tampered"
    env["payload"]["command"]["argv"].append("evil")

    assert base.data["plugin"]["name"] == "github-pr-ops"
    assert base.data["command"]["argv"] == ["url"]


def test_envelope_to_base_event_preserves_audit_timestamp() -> None:
    """Regression: the bridge MUST carry the original audit time over to
    `BaseEvent.timestamp`. Without that, persisted rows inherit
    `BaseEvent`'s "now" default and the events_table.timestamp column
    no longer reflects when the firewall observed the event — silently
    breaking ordering, recency, and replay for plugin events.
    """
    from datetime import UTC, datetime, timedelta

    from ouroboros.plugin.ledger_adapter import envelope_to_base_event

    # An audit event from clearly in the past: any "now"-default would
    # be wrong by a wide enough margin that the assert can't false-pass
    # on a slow test machine.
    audit = _audit_event("plugin.invoked", occurred_at="2020-01-15T08:30:45Z")
    envelope = wrap_plugin_event(audit, correlation_id="corr-time")

    before = datetime.now(UTC)
    base_event = envelope_to_base_event(envelope)
    after = datetime.now(UTC)

    expected = datetime(2020, 1, 15, 8, 30, 45, tzinfo=UTC)
    assert base_event.timestamp == expected
    # Sanity: the persisted timestamp is the audit time, not anything
    # close to "now". Any drift wider than a year proves we did not
    # accidentally fall through to the default factory.
    assert before - base_event.timestamp > timedelta(days=365)
    assert after - base_event.timestamp > timedelta(days=365)


def test_envelope_to_base_event_round_trips_timestamp_through_real_store() -> None:
    """End-to-end check: persist, replay, and confirm the timestamp on
    the row coming out of EventStore matches the audit's occurred_at
    rather than wall-clock-now."""
    pytest.importorskip("aiosqlite")

    import asyncio
    from datetime import UTC, datetime

    from ouroboros.persistence.event_store import EventStore
    from ouroboros.plugin.ledger_adapter import append_envelope_to_event_store

    audit = _audit_event("plugin.completed", occurred_at="2024-12-31T23:59:59Z")
    envelope = wrap_plugin_event(
        audit, correlation_id="corr-roundtrip", envelope_id="env-roundtrip"
    )
    expected = datetime(2024, 12, 31, 23, 59, 59, tzinfo=UTC)

    async def _persist_and_replay() -> list:
        store = EventStore("sqlite+aiosqlite:///:memory:")
        await store.initialize()
        await append_envelope_to_event_store(envelope, store=store)
        return await store.replay("plugin", "corr-roundtrip")

    rows = asyncio.run(_persist_and_replay())
    assert len(rows) == 1
    persisted = rows[0]
    persisted_ts = persisted.timestamp
    if persisted_ts.tzinfo is None:
        # Some SQLAlchemy/sqlite combinations strip tzinfo on read; the
        # timestamp value itself must still be UTC-equivalent.
        persisted_ts = persisted_ts.replace(tzinfo=UTC)
    assert persisted_ts == expected, (
        f"persisted timestamp {persisted_ts!r} drifted from audit occurred_at {expected!r}"
    )


def test_envelope_to_base_event_rejects_missing_timestamp() -> None:
    """If neither the envelope nor the payload exposes a timestamp the
    bridge MUST refuse rather than silently default to "now"."""
    from ouroboros.plugin.ledger_adapter import envelope_to_base_event

    bogus = {
        "id": "x",
        "aggregate_type": "plugin",
        "aggregate_id": "y",
        "event_type": "plugin.completed",
        "payload": {"event_type": "plugin.completed"},  # no occurred_at
        # no top-level timestamp
    }
    with pytest.raises(ValueError, match="timestamp"):
        envelope_to_base_event(bogus)


def test_envelope_to_base_event_rejects_non_plugin_envelope() -> None:
    """The bridge refuses envelopes for other aggregates, so a misrouted
    payload cannot end up persisted under aggregate_type='plugin'."""
    from ouroboros.plugin.ledger_adapter import envelope_to_base_event

    bogus = {
        "id": "x",
        "aggregate_type": "session",
        "aggregate_id": "y",
        "event_type": "plugin.completed",
        "payload": {"event_type": "plugin.completed", "occurred_at": "2026-05-07T12:00:00Z"},
        "timestamp": "2026-05-07T12:00:00Z",
    }
    with pytest.raises(ValueError, match="not a plugin envelope"):
        envelope_to_base_event(bogus)
