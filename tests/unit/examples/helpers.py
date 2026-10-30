"""Builders for outcome records in the examples tests."""

from __future__ import annotations

from typing import Any

from ouroboros.examples.record import BASE_TREE_ID, OutcomeRecord, ProbeStatus


def runs(
    probe_id: str,
    tree_id: str,
    status: ProbeStatus = ProbeStatus.RETURNED,
    second: dict[str, Any] | None = None,
    second_status: ProbeStatus | None = None,
    **fields: Any,
) -> list[OutcomeRecord]:
    """Two repeats of one probe on one tree; ``second`` overrides repeat 1 fields."""
    return [
        OutcomeRecord(probe_id, tree_id, 0, status, fields),
        OutcomeRecord(
            probe_id,
            tree_id,
            1,
            second_status or status,
            fields if second is None else second,
        ),
    ]


def probe(
    probe_id: str,
    base: dict[str, Any],
    candidates: dict[str, dict[str, Any]],
    status: dict[str, ProbeStatus] | None = None,
) -> list[OutcomeRecord]:
    """Stable records for one probe: ``{tree: fields}`` with optional statuses."""
    status = status or {}
    records = runs(probe_id, BASE_TREE_ID, status.get(BASE_TREE_ID, ProbeStatus.RETURNED), **base)
    for tree_id, fields in candidates.items():
        records += runs(probe_id, tree_id, status.get(tree_id, ProbeStatus.RETURNED), **fields)
    return records
