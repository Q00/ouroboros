#!/usr/bin/env python3
"""Export privacy-safe interview phase timings from the local EventStore.

The command writes JSON Lines to stdout. It intentionally uses a strict
allowlist so benchmark artifacts cannot include prompt/answer text, errors,
paths, credentials, or environment values from persisted event payloads.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import sys
from typing import Any
from urllib.parse import quote

_DEFAULT_DB = Path.home() / ".ouroboros" / "ouroboros.db"
_TIMING_FIELDS = (
    "total",
    "ambiguity_scoring",
    "question_generation",
    "advisory_build",
)
_METADATA_FIELDS: dict[str, dict[str, type]] = {
    "interview.response.emitted": {
        "response_kind": str,
        "round_number": int,
        "payload_chars": int,
        "transcript_chars": int,
        "ambiguity_prefix_present": bool,
        "is_length_guard": bool,
    },
    "interview.completed": {"total_rounds": int},
    "interview.failed": {"phase": str},
    "interview.question_generation.parent_handoff": {
        "phase": str,
        "reason_code": str,
        "provider_error_type": str,
    },
}
_STRING_METADATA_VALUES: dict[tuple[str, str], frozenset[str]] = {
    ("interview.response.emitted", "response_kind"): frozenset(
        {"start", "answer", "resume_pending"}
    ),
    ("interview.failed", "phase"): frozenset(
        {"question_generation", "unexpected_error", "completion"}
    ),
    ("interview.question_generation.parent_handoff", "phase"): frozenset(
        {"start_question_generation", "next_question_generation"}
    ),
    ("interview.question_generation.parent_handoff", "reason_code"): frozenset(
        {"question_generation_envelope_violation"}
    ),
    ("interview.question_generation.parent_handoff", "provider_error_type"): frozenset(
        {"ToolUseBlockViolation"}
    ),
}


def _hash_interview_id(interview_id: str) -> str:
    value = f"ouroboros-interview:{interview_id}".encode()
    return hashlib.sha256(value).hexdigest()


def _read_payload(raw_payload: object) -> dict[str, Any] | None:
    if isinstance(raw_payload, bytes):
        raw_payload = raw_payload.decode("utf-8", errors="strict")
    if not isinstance(raw_payload, str):
        return None
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _read_timings(payload: dict[str, Any]) -> dict[str, float | int | None] | None:
    raw_timings = payload.get("timings_ms")
    if not isinstance(raw_timings, dict):
        return None

    timings: dict[str, float | int | None] = {}
    for field in _TIMING_FIELDS:
        value = raw_timings.get(field)
        if value is None:
            timings[field] = None
            continue
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        if not math.isfinite(value) or value < 0:
            return None
        timings[field] = value
    if timings["total"] is None:
        return None
    return timings


def _read_metadata(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for field, expected_type in _METADATA_FIELDS[event_type].items():
        value = payload.get(field)
        if expected_type is int and isinstance(value, bool):
            continue
        allowed_values = _STRING_METADATA_VALUES.get((event_type, field))
        if allowed_values is not None and value not in allowed_values:
            continue
        if isinstance(value, expected_type):
            metadata[field] = value
    return metadata


def _read_timestamp(raw_timestamp: object) -> str | None:
    if isinstance(raw_timestamp, datetime):
        return raw_timestamp.isoformat()
    if not isinstance(raw_timestamp, str) or len(raw_timestamp) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.isoformat()


def _record_from_row(row: sqlite3.Row) -> dict[str, Any] | None:
    event_type = row["event_type"]
    if event_type not in _METADATA_FIELDS:
        return None

    payload = _read_payload(row["payload"])
    if payload is None:
        return None
    timings = _read_timings(payload)
    if timings is None:
        return None
    timestamp = _read_timestamp(row["timestamp"])
    if timestamp is None:
        return None

    return {
        "interview_id_sha256": _hash_interview_id(row["aggregate_id"]),
        "timestamp": timestamp,
        "event_type": event_type,
        "metadata": _read_metadata(event_type, payload),
        "timings_ms": timings,
    }


def _open_read_only(db_path: Path) -> sqlite3.Connection:
    encoded_path = quote(db_path.resolve().as_posix(), safe="/:")
    connection = sqlite3.connect(f"file:{encoded_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _query_rows(
    connection: sqlite3.Connection,
    *,
    interview_id: str | None,
) -> sqlite3.Cursor:
    placeholders = ", ".join("?" for _ in _METADATA_FIELDS)
    query = (
        "SELECT aggregate_id, event_type, payload, timestamp "
        "FROM events WHERE aggregate_type = ? "
        f"AND event_type IN ({placeholders})"
    )
    parameters: list[str] = ["interview", *_METADATA_FIELDS]
    if interview_id is not None:
        query += " AND aggregate_id = ?"
        parameters.append(interview_id)
    query += " ORDER BY timestamp ASC, id ASC"
    return connection.execute(query, parameters)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=_DEFAULT_DB,
        help="EventStore SQLite database (default: ~/.ouroboros/ouroboros.db).",
    )
    parser.add_argument(
        "--interview-id",
        help="Limit export to one raw interview id; output still contains only its SHA-256 hash.",
    )
    args = parser.parse_args()

    db_path = args.db.expanduser()
    if not db_path.is_file():
        print("export_interview_latency: EventStore database not found", file=sys.stderr)
        return 2

    emitted = 0
    try:
        with _open_read_only(db_path) as connection:
            for row in _query_rows(connection, interview_id=args.interview_id):
                record = _record_from_row(row)
                if record is None:
                    continue
                print(json.dumps(record, sort_keys=True, separators=(",", ":")))
                emitted += 1
    except (OSError, sqlite3.Error, UnicodeError) as exc:
        print(
            f"export_interview_latency: unable to read timing events ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 2

    if emitted == 0:
        print("export_interview_latency: no timed interview events found", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
