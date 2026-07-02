"""Minimal per-backend outcome flywheel over the EventStore (PR-X X4).

The meta-harness accumulates evidence no single vendor can: how each *runtime
backend* actually performs across runs. This module reads that history from the
append-only event log (read-only SQLite) and distills it into a per-backend
weight the runtime picker (X0) uses as a **tie-break only**.

Design constraints (all load-bearing):

* **Read-only + bounded.** We open the SQLite DB in ``mode=ro`` and cap the scan
  to a recent window (row limit + optional day cutoff). No index gymnastics, no
  full-table replay.
* **Never blocks dispatch.** Every failure path (missing DB, locked DB, corrupt
  payload, unexpected schema) collapses to *no weights*, so the picker simply
  falls back to alphabetical selection. Recovery must never be made worse by the
  flywheel that is supposed to improve it.
* **Uses the events the executor actually emits.** ``execution.session.completed``
  and ``execution.session.failed`` are the per-AC runtime-session lifecycle
  events (see ``parallel_executor._emit_ac_runtime_event``); both carry
  ``runtime_backend`` and a ``success`` flag in their JSON payload. We count
  completions vs failures per backend from exactly those.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sqlite3

from ouroboros.observability.logging import get_logger

log = get_logger(__name__)

# Per-AC runtime-session lifecycle events that carry runtime_backend + success.
_COMPLETED_EVENT = "execution.session.completed"
_FAILED_EVENT = "execution.session.failed"

# Bounded-scan defaults: cheap query, recent history only.
_DEFAULT_ROW_LIMIT = 5000
_DEFAULT_WINDOW_DAYS = 30


def _default_db_path() -> Path:
    """Standard Ouroboros event-store location (mirrors EventStore default)."""
    return Path.home() / ".ouroboros" / "ouroboros.db"


@dataclass(frozen=True, slots=True)
class BackendOutcome:
    """Completion/failure tallies for one runtime backend."""

    backend: str
    completed: int
    failed: int

    @property
    def total(self) -> int:
        """Total observed terminal runtime sessions for this backend."""
        return self.completed + self.failed

    @property
    def success_rate(self) -> float:
        """Fraction of sessions that completed (0.0 when nothing observed)."""
        return self.completed / self.total if self.total else 0.0


def _parse_timestamp(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def aggregate_backend_outcomes(
    *,
    db_path: Path | str | None = None,
    row_limit: int = _DEFAULT_ROW_LIMIT,
    window_days: int | None = _DEFAULT_WINDOW_DAYS,
) -> dict[str, BackendOutcome]:
    """Tally completion vs failure per runtime backend from the event log.

    Args:
        db_path: Event-store SQLite file; defaults to ``~/.ouroboros/ouroboros.db``.
        row_limit: Maximum recent lifecycle rows to scan (cost bound).
        window_days: Only count events within this many days; ``None`` disables
            the day cutoff and relies on ``row_limit`` alone.

    Returns:
        Mapping of canonical backend name to :class:`BackendOutcome`. Empty on
        any error or when the store has no relevant events — callers treat an
        empty result as "no signal", never as a hard failure.
    """
    path = Path(db_path) if db_path is not None else _default_db_path()
    if not path.exists():
        return {}

    cutoff = (
        datetime.now(UTC) - timedelta(days=window_days)
        if window_days is not None and window_days > 0
        else None
    )

    tallies: dict[str, list[int]] = {}
    try:
        # True read-only handle: mode=ro fails fast on any accidental write and
        # never creates the DB file.
        uri = f"file:{path}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=1.0) as conn:
            rows = conn.execute(
                "SELECT event_type, payload, timestamp FROM events "
                "WHERE event_type IN (?, ?) "
                "ORDER BY timestamp DESC LIMIT ?",
                (_COMPLETED_EVENT, _FAILED_EVENT, int(row_limit)),
            ).fetchall()
    except (sqlite3.Error, ValueError, OSError) as exc:
        log.debug("backend_outcomes.query_failed", error=str(exc))
        return {}

    for event_type, payload_raw, timestamp_raw in rows:
        if cutoff is not None:
            ts = _parse_timestamp(timestamp_raw)
            if ts is not None and ts < cutoff:
                continue
        try:
            payload = json.loads(payload_raw) if isinstance(payload_raw, str) else payload_raw
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, Mapping):
            continue
        backend = payload.get("runtime_backend")
        if not isinstance(backend, str) or not backend.strip():
            continue
        backend = backend.strip()
        tally = tallies.setdefault(backend, [0, 0])
        if event_type == _COMPLETED_EVENT:
            tally[0] += 1
        else:
            tally[1] += 1

    return {
        backend: BackendOutcome(backend=backend, completed=counts[0], failed=counts[1])
        for backend, counts in tallies.items()
    }


def outcome_weights(
    *,
    db_path: Path | str | None = None,
    row_limit: int = _DEFAULT_ROW_LIMIT,
    window_days: int | None = _DEFAULT_WINDOW_DAYS,
) -> dict[str, float]:
    """Return per-backend success-rate weights for the runtime picker (X0).

    The weight is each backend's observed completion rate in the recent window.
    This is consumed only as a *tie-break* by ``pick_alternative_runtime`` — it
    reorders eligible candidates but never admits or excludes one. Returns an
    empty mapping on any failure, so a broken/empty flywheel is a silent no-op.
    """
    try:
        outcomes = aggregate_backend_outcomes(
            db_path=db_path,
            row_limit=row_limit,
            window_days=window_days,
        )
    except Exception as exc:  # never let the flywheel block dispatch
        log.debug("backend_outcomes.weights_failed", error=str(exc))
        return {}
    return {
        backend: outcome.success_rate for backend, outcome in outcomes.items() if outcome.total > 0
    }


__all__ = [
    "BackendOutcome",
    "aggregate_backend_outcomes",
    "outcome_weights",
]
