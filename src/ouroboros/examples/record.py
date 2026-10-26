"""The probe outcome record: what one probe did on one tree in one repeat.

A probe is one input (a small program or a command). The controller runs
each probe on the base snapshot and on every candidate tree, twice per tree.
Each run yields one ``OutcomeRecord``. The record holds only what execution
produced; nobody writes an expected value into it.

Shape:

- ``probe_id``: the probe's digest (sha256 of its bytes).
- ``tree_id``: ``BASE_TREE_ID`` for the base snapshot, otherwise the
  candidate's id.
- ``repeat``: 0 or 1.
- ``status``: a ``ProbeStatus``. ``not_run`` means the probe could not
  import or start on that tree.
- ``fields``: the outcome fields, a subset of ``OUTCOME_FIELDS``: ``value``
  (the return value as JSON data), ``exception_type`` and ``message`` (for
  ``raised``), ``exit_code``, ``stdout`` and ``stderr`` (for commands),
  ``files`` (declared path to digest data, for a later probe kind).

Every field value must be JSON data: ``None``, ``bool``, ``int``, a finite
``float``, ``str``, ``list``, or ``dict`` with ``str`` keys. The producer
encodes anything else (a tuple, a set, a non-finite float, an arbitrary
object) before it builds the record. A record that breaks these rules is
refused with ``ValueError``; it is never coerced.

The canonical bytes of a record are the JSON text of its payload (``status``
plus the fields) with sorted keys, no whitespace, and ASCII escapes, encoded
as UTF-8. Two records with the same payload have the same bytes and the same
digest, whatever the insertion order of their fields.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
import hashlib
import json
import math
from types import MappingProxyType
from typing import Any

BASE_TREE_ID = "base"
REPEATS: tuple[int, ...] = (0, 1)
STATUS_KEY = "status"
VALUE_KEY = "value"
OUTCOME_FIELDS = frozenset(
    {"value", "exception_type", "message", "exit_code", "stdout", "stderr", "files"}
)


class ProbeStatus(StrEnum):
    """How one probe run ended."""

    RETURNED = "returned"
    RAISED = "raised"
    EXITED = "exited"
    TIMED_OUT = "timed_out"
    NOT_RUN = "not_run"


def canonical_json_bytes(data: Any) -> bytes:
    """Return the canonical bytes of JSON data (sorted keys, no whitespace)."""
    return json.dumps(
        data, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    """Return the lowercase hex sha256 of ``data``."""
    return hashlib.sha256(data).hexdigest()


def _check_json(value: Any, where: str) -> None:
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{where}: non-finite float is not JSON data")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _check_json(item, f"{where}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{where}: dict key {key!r} is not a string")
            _check_json(item, f"{where}.{key}")
        return
    raise ValueError(f"{where}: {type(value).__name__} is not JSON data")


@dataclass(frozen=True, slots=True)
class OutcomeRecord:
    """One run of one probe on one tree. Immutable; validated on creation."""

    probe_id: str
    tree_id: str
    repeat: int
    status: ProbeStatus
    fields: Mapping[str, Any] = field(default_factory=dict, hash=False)

    def __post_init__(self) -> None:
        if not isinstance(self.probe_id, str) or not self.probe_id:
            raise ValueError("probe_id must be a non-empty string")
        if not isinstance(self.tree_id, str) or not self.tree_id:
            raise ValueError("tree_id must be a non-empty string")
        if isinstance(self.repeat, bool) or self.repeat not in REPEATS:
            raise ValueError(f"repeat must be one of {REPEATS}, got {self.repeat!r}")
        object.__setattr__(self, "status", ProbeStatus(self.status))
        if not isinstance(self.fields, Mapping):
            raise ValueError("fields must be a mapping")
        unknown = sorted(str(key) for key in self.fields if key not in OUTCOME_FIELDS)
        if unknown:
            raise ValueError(f"unknown outcome fields: {unknown}")
        plain = dict(self.fields)
        _check_json(plain, "fields")
        # A deep, detached copy: later changes to the caller's containers
        # cannot change this record.
        frozen = json.loads(canonical_json_bytes(plain))
        object.__setattr__(self, "fields", MappingProxyType(frozen))

    def payload(self) -> dict[str, Any]:
        """Return the outcome as one JSON object: ``status`` plus the fields."""
        return {
            STATUS_KEY: self.status.value,
            **json.loads(canonical_json_bytes(dict(self.fields))),
        }

    def canonical_bytes(self) -> bytes:
        """Return the canonical bytes of the payload."""
        return canonical_json_bytes(self.payload())

    def digest(self) -> str:
        """Return the sha256 hex digest of the canonical bytes."""
        return sha256_hex(self.canonical_bytes())
