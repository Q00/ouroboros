"""Tests for the probe outcome record and its canonical bytes."""

from __future__ import annotations

import hashlib

import pytest

from ouroboros.examples.record import (
    OUTCOME_FIELDS,
    OutcomeRecord,
    ProbeStatus,
    canonical_json_bytes,
)


def _record(**fields: object) -> OutcomeRecord:
    return OutcomeRecord("p1", "c1", 0, ProbeStatus.RETURNED, fields)


def test_canonical_bytes_ignore_field_insertion_order() -> None:
    one = _record(value={"b": 1, "a": [1, 2]}, stdout="x")
    two = _record(stdout="x", value={"a": [1, 2], "b": 1})
    assert one.canonical_bytes() == two.canonical_bytes()
    assert one.digest() == two.digest()
    assert one.canonical_bytes() == b'{"status":"returned","stdout":"x","value":{"a":[1,2],"b":1}}'


def test_digest_is_sha256_of_canonical_bytes() -> None:
    record = _record(value=5400)
    assert record.digest() == hashlib.sha256(record.canonical_bytes()).hexdigest()


def test_int_float_and_bool_have_distinct_bytes() -> None:
    assert _record(value=1).canonical_bytes() != _record(value=1.0).canonical_bytes()
    assert _record(value=1).canonical_bytes() != _record(value=True).canonical_bytes()


def test_non_ascii_text_is_escaped_deterministically() -> None:
    assert canonical_json_bytes("é") == b'"\\u00e9"'


def test_status_string_is_coerced_to_enum() -> None:
    record = OutcomeRecord("p1", "c1", 1, "not_run")  # type: ignore[arg-type]
    assert record.status is ProbeStatus.NOT_RUN
    assert record.payload() == {"status": "not_run"}


def test_fields_are_detached_from_caller_containers() -> None:
    value = [1, 2]
    record = _record(value=value)
    value.append(3)
    assert record.fields["value"] == (1, 2)
    with pytest.raises(TypeError):
        record.fields["value"] = 0  # type: ignore[index]


def test_nested_fields_are_read_only_and_digest_is_fixed() -> None:
    record = _record(value={"items": [1, {"k": "v"}]}, files={"a.txt": {"sha256": "x"}})
    digest = record.digest()
    with pytest.raises(AttributeError):
        record.fields["value"]["items"].append(2)
    with pytest.raises(TypeError):
        record.fields["value"]["items"][1]["k"] = "w"
    with pytest.raises(TypeError):
        record.fields["files"]["b.txt"] = {}
    assert record.digest() == digest
    assert record.payload() == {
        "status": "returned",
        "value": {"items": [1, {"k": "v"}]},
        "files": {"a.txt": {"sha256": "x"}},
    }


def test_payload_is_a_fresh_copy() -> None:
    record = _record(value=[1])
    record.payload()["value"].append(2)
    assert record.payload() == {"status": "returned", "value": [1]}


def test_records_hash_and_compare() -> None:
    assert _record(value=1) == _record(value=1)
    assert len({_record(value=1), _record(value=1)}) == 1


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"probe_id": ""}, "probe_id"),
        ({"tree_id": ""}, "tree_id"),
        ({"repeat": 2}, "repeat"),
        ({"repeat": True}, "repeat"),
        ({"fields": {"verdict": 1}}, "unknown outcome fields"),
        ({"fields": {"value": float("nan")}}, "non-finite"),
        ({"fields": {"value": float("inf")}}, "non-finite"),
        ({"fields": {"value": (1, 2)}}, "tuple"),
        ({"fields": {"value": {1: "a"}}}, "not a string"),
        ({"fields": {"value": {"a": {1, 2}}}}, "set"),
    ],
)
def test_invalid_records_are_refused(kwargs: dict[str, object], message: str) -> None:
    base: dict[str, object] = {
        "probe_id": "p1",
        "tree_id": "c1",
        "repeat": 0,
        "status": ProbeStatus.RETURNED,
        "fields": {},
    }
    base.update(kwargs)
    with pytest.raises(ValueError, match=message):
        OutcomeRecord(**base)  # type: ignore[arg-type]


def test_unknown_status_is_refused() -> None:
    with pytest.raises(ValueError):
        OutcomeRecord("p1", "c1", 0, "passed")  # type: ignore[arg-type]


def test_every_documented_field_is_accepted() -> None:
    fields = dict.fromkeys(OUTCOME_FIELDS, "x")
    assert set(_record(**fields).fields) == OUTCOME_FIELDS
