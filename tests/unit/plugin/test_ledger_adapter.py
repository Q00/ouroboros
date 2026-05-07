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
