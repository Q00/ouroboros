"""Local daily usage rollup behind the ``usage_daily`` telemetry event (stdlib only).

Counters live in ``~/.ouroboros/telemetry-usage.json``, one bucket per UTC
day. A bucket is sent once, as a single ``usage_daily`` event, after its day
is over: the first process that records or captures on a later day claims it.

Invariants, each covered by ``tests/unit/test_telemetry_rollup.py``:

- Every read-modify-write holds an exclusive lock on
  ``telemetry-usage.lock`` (``fcntl.flock``, or ``msvcrt.locking`` on
  Windows) and the in-process thread lock. A caller waits at most
  ``LOCK_TIMEOUT_SECONDS`` for both together; one that cannot get them skips
  the update instead of blocking.
- Writes go to a same-directory temp file, are fsynced, then replace the
  file, so a crash leaves either the old or the new file, never a torn one.
  An unreadable, oversized or malformed file is treated as empty.
- A day has at most one bucket, ever. Claiming a day moves its bucket out of
  ``days`` into ``sending`` under the lock, so two processes can never claim
  the same bucket, and closes the day: a later ``record`` for it (a process
  still on the old day at UTC midnight, or a clock that moved backwards)
  raises ``DayClosed`` and its counts are dropped, never sent as a second
  row. The bucket is deleted only after delivery succeeds, leaving the day in
  ``sent`` so it stays closed. A failed delivery releases it, and a claim
  whose process died is taken over once its lease runs out; every send of a
  day carries ``event_uuid(install id, day)``, which PostHog deduplicates on.
  The clock behind a lease is read while the lock is held, so a process that
  waited for the lock never judges a lease by a reading taken before the
  lease was written.
- Only days before ``today`` are claimed. A bucket for a later day, left by
  a clock that moved backwards, is kept and sent once that day is over.
- Size is bounded: at most ``MAX_BUCKETS`` buckets, ``MAX_KEYS`` counters per
  bucket, each counter at most ``MAX_COUNT``, and buckets more than
  ``MAX_AGE_DAYS`` before today are discarded unsent. Age only counts
  backwards: a bucket for a later day is never discarded by age, so a process
  whose clock reads too early cannot delete the real days' buckets.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import date
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
from typing import Any
import uuid

ROLLUP_FILENAME = "telemetry-usage.json"
LOCK_FILENAME = "telemetry-usage.lock"
SCHEMA_VERSION = 1

LOCK_TIMEOUT_SECONDS = 0.5
_LOCK_POLL_SECONDS = 0.005
LEASE_SECONDS = 120.0
# How far past a full term a lease may end before it is taken to have been
# set by a clock that was ahead, and so treated as expired. It must exceed any
# honest gap between two readings of this machine's clock taken in lock order
# (a wall-clock step of a few seconds, the 0.5 s lock wait); a clock that was
# ahead by less only delays a takeover by up to this long.
CLOCK_AHEAD_TOLERANCE_SECONDS = 60.0
MAX_BUCKETS = 32
MAX_KEYS = 96
MAX_DIMS = 8
MAX_COUNT = 1_000_000
MAX_AGE_DAYS = 30
MAX_SENT_DAYS = 2 * MAX_AGE_DAYS + 1
MAX_FILE_BYTES = 256 * 1024
_MAX_DIM_LEN = 64

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

_thread_lock = threading.Lock()
_EVENT_UUID_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL, "https://github.com/Q00/ouroboros#telemetry-usage-daily"
)


class LockUnavailable(Exception):
    """The rollup lock was not acquired within ``LOCK_TIMEOUT_SECONDS``."""


class DayClosed(Exception):
    """The day was already claimed for sending, so counts for it are dropped."""


def _try_lock(fd: int) -> bool:
    """One non-blocking attempt at an exclusive lock on ``fd``."""
    if sys.platform == "win32":
        import msvcrt

        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def _locked(state_dir: Path) -> Iterator[None]:
    """Hold the cross-process rollup lock, or raise ``LockUnavailable``.

    One deadline, set on entry, bounds the wait for the in-process thread lock
    and the file lock together, so a caller waits at most
    ``LOCK_TIMEOUT_SECONDS`` in total, however the wait is split between them.
    """
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    if not _thread_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise LockUnavailable
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(state_dir / LOCK_FILENAME, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            while not _try_lock(fd):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LockUnavailable
                time.sleep(min(_LOCK_POLL_SECONDS, remaining))
            try:
                yield
            finally:
                _unlock(fd)
        finally:
            os.close(fd)
    finally:
        _thread_lock.release()


def _day_ordinal(day: str) -> int | None:
    if not _DAY_RE.fullmatch(day):
        return None
    try:
        return date.fromisoformat(day).toordinal()
    except ValueError:
        return None


def _clean_counts(raw: Any) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    counts: dict[str, int] = {}
    for key, value in raw.items():
        if len(counts) >= MAX_KEYS:
            break
        if (
            isinstance(key, str)
            and _KEY_RE.fullmatch(key)
            and isinstance(value, int)
            and not isinstance(value, bool)
            and value > 0
        ):
            counts[key] = min(value, MAX_COUNT)
    return counts


def _clean_dims(raw: Any) -> dict[str, str | bool]:
    if not isinstance(raw, dict):
        return {}
    dims: dict[str, str | bool] = {}
    for key, value in raw.items():
        if len(dims) >= MAX_DIMS:
            break
        if not (isinstance(key, str) and _KEY_RE.fullmatch(key)):
            continue
        if isinstance(value, bool) or (isinstance(value, str) and 0 < len(value) <= _MAX_DIM_LEN):
            dims[key] = value
    return dims


def _clean_bucket(raw: Any) -> dict[str, Any] | None:
    """A validated copy of one stored bucket, or ``None`` when it is malformed."""
    if not isinstance(raw, dict):
        return None
    bucket_id, day = raw.get("id"), raw.get("day")
    if not (isinstance(bucket_id, str) and _ID_RE.fullmatch(bucket_id)):
        return None
    if not (isinstance(day, str) and _day_ordinal(day) is not None):
        return None
    lease = raw.get("lease_until", 0.0)
    if isinstance(lease, bool) or not isinstance(lease, (int, float)):
        lease = 0.0
    return {
        "id": bucket_id,
        "day": day,
        "counts": _clean_counts(raw.get("counts")),
        "dims": _clean_dims(raw.get("dims")),
        "lease_until": float(lease),
    }


def event_uuid(install_id: str, day: str) -> str:
    """The ``usage_daily`` event uuid for one install's UTC day.

    It depends only on the anonymous install id and the day, so every send of
    a day (a retry after a lost acknowledgement, or a bucket rebuilt after the
    rollup file was lost) carries the same uuid and PostHog keeps one row.
    """
    if _day_ordinal(day) is None:
        raise ValueError(f"invalid day: {day!r}")
    return str(uuid.uuid5(_EVENT_UUID_NAMESPACE, f"{install_id}/{day}"))


def _closed_days(state: dict[str, Any]) -> set[str]:
    """Days whose bucket was claimed: being sent, or already delivered."""
    return {bucket["day"] for bucket in state["sending"].values()} | set(state["sent"])


def _load(path: Path) -> dict[str, Any]:
    """Read the rollup file; anything unreadable or malformed reads as empty."""
    empty: dict[str, Any] = {"schema": SCHEMA_VERSION, "days": {}, "sending": {}, "sent": []}
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return empty
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return empty
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA_VERSION:
        return empty
    days: dict[str, dict[str, Any]] = {}
    raw_days = raw.get("days")
    for day, bucket in raw_days.items() if isinstance(raw_days, dict) else ():
        cleaned = _clean_bucket(bucket)
        if cleaned is not None and cleaned["day"] == day:
            days[day] = cleaned
    sending: dict[str, dict[str, Any]] = {}
    raw_sending = raw.get("sending")
    for bucket_id, bucket in raw_sending.items() if isinstance(raw_sending, dict) else ():
        cleaned = _clean_bucket(bucket)
        if cleaned is not None and cleaned["id"] == bucket_id:
            sending[bucket_id] = cleaned
    raw_sent = raw.get("sent")
    sent = sorted(
        {d for d in raw_sent if isinstance(d, str) and _day_ordinal(d) is not None}
        if isinstance(raw_sent, list)
        else set()
    )[-MAX_SENT_DAYS:]
    state: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "days": days,
        "sending": sending,
        "sent": sent,
    }
    for day in _closed_days(state) & set(days):
        del days[day]  # a closed day never gets a second bucket
    return state


def _prune(state: dict[str, Any], today: str) -> None:
    """Drop buckets more than ``MAX_AGE_DAYS`` before ``today``; keep ``MAX_BUCKETS``.

    Only age before ``today`` counts. A process whose clock reads weeks early
    sees every real bucket as a later day and must not delete it.
    """
    today_ordinal = _day_ordinal(today)
    if today_ordinal is None:
        return
    entries: list[tuple[int, str, str]] = []
    for section in ("days", "sending"):
        for key, bucket in list(state[section].items()):
            ordinal = _day_ordinal(bucket["day"])
            if ordinal is None or ordinal < today_ordinal - MAX_AGE_DAYS:
                del state[section][key]
            else:
                entries.append((ordinal, section, key))
    entries.sort()
    for _ordinal, section, key in entries[: max(0, len(entries) - MAX_BUCKETS)]:
        del state[section][key]
    state["sent"] = [
        d
        for d in state["sent"]
        if (ordinal := _day_ordinal(d)) is not None and ordinal >= today_ordinal - MAX_AGE_DAYS
    ]


def _write(path: Path, state: dict[str, Any]) -> None:
    """Atomically replace ``path`` with ``state`` (temp file, fsync, replace)."""
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def record(
    state_dir: Path,
    day: str,
    counts: Mapping[str, int],
    dims: Mapping[str, str | bool],
) -> None:
    """Add ``counts`` to ``day``'s bucket and merge ``dims`` into it.

    ``dims`` overwrite earlier values of the same key, so the last process of
    the day names the version and backend; a key absent from ``dims`` keeps
    its stored value. Raises ``ValueError`` for an invalid day, and
    ``LockUnavailable`` or ``OSError`` when the update was not persisted.
    Raises ``DayClosed``, persisting nothing, when ``day`` was already claimed
    for sending: its one row is fixed, so these counts are dropped.
    """
    if _day_ordinal(day) is None:
        raise ValueError(f"invalid day: {day!r}")
    increments = _clean_counts(dict(counts))
    if not increments:
        return
    path = state_dir / ROLLUP_FILENAME
    with _locked(state_dir):
        state = _load(path)
        if day in _closed_days(state):
            raise DayClosed(day)
        bucket = state["days"].get(day)
        if bucket is None:
            bucket = {
                "id": str(uuid.uuid4()),
                "day": day,
                "counts": {},
                "dims": {},
                "lease_until": 0.0,
            }
            state["days"][day] = bucket
        stored = bucket["counts"]
        for key, value in increments.items():
            if key in stored or len(stored) < MAX_KEYS:
                stored[key] = min(stored.get(key, 0) + value, MAX_COUNT)
        bucket["dims"] = _clean_dims({**bucket["dims"], **_clean_dims(dict(dims))})
        _prune(state, day)
        _write(path, state)


def claim_due(
    state_dir: Path, today: str, *, clock: Callable[[], float] | None = None
) -> list[dict[str, Any]]:
    """Claim every bucket ready to send and return copies of them.

    Ready means a bucket for a day before ``today``, or a claimed bucket whose
    lease ran out. A bucket for a later day (left by a clock that moved
    backwards) waits until that day is over, so counts recorded on the day
    itself join it instead of becoming a second row. A lease that ends more
    than ``LEASE_SECONDS + CLOCK_AHEAD_TOLERANCE_SECONDS`` from now can only
    come from a clock that was ahead and counts as expired. ``clock``
    (``time.time`` by default) is read once the lock is held, never before,
    so a process that waited for the lock cannot mistake a lease another
    process just wrote for one from a clock that was ahead. Claiming closes
    the day (see ``record``). Raises ``LockUnavailable`` or ``OSError`` when
    nothing could be claimed.
    """
    if _day_ordinal(today) is None:
        raise ValueError(f"invalid day: {today!r}")
    path = state_dir / ROLLUP_FILENAME
    with _locked(state_dir):
        now = (clock or time.time)()
        state = _load(path)
        _prune(state, today)
        sending = state["sending"]
        # Validated ``YYYY-MM-DD`` strings sort chronologically.
        for day in [d for d in state["days"] if d < today]:
            bucket = state["days"].pop(day)
            bucket["lease_until"] = 0.0
            sending[bucket["id"]] = bucket
        claimed: list[dict[str, Any]] = []
        for bucket_id in list(sending):
            bucket = sending[bucket_id]
            if not bucket["counts"]:
                del sending[bucket_id]  # nothing to report
                continue
            lease = bucket["lease_until"]
            set_by_a_clock_ahead = lease - now > LEASE_SECONDS + CLOCK_AHEAD_TOLERANCE_SECONDS
            if lease <= now or set_by_a_clock_ahead:
                bucket["lease_until"] = now + LEASE_SECONDS
                claimed.append(bucket)
        if path.exists() or claimed:
            _write(path, state)
        return [
            {"id": b["id"], "day": b["day"], "counts": dict(b["counts"]), "dims": dict(b["dims"])}
            for b in claimed
        ]


def settle(state_dir: Path, bucket_id: str, *, delivered: bool) -> None:
    """Delete a delivered bucket (its day stays closed), or release it for retry."""
    path = state_dir / ROLLUP_FILENAME
    with _locked(state_dir):
        state = _load(path)
        bucket = state["sending"].get(bucket_id)
        if bucket is None:
            return
        if delivered:
            del state["sending"][bucket_id]
            state["sent"] = sorted({*state["sent"], bucket["day"]})[-MAX_SENT_DAYS:]
        else:
            bucket["lease_until"] = 0.0
        _write(path, state)


__all__ = [
    "CLOCK_AHEAD_TOLERANCE_SECONDS",
    "LEASE_SECONDS",
    "LOCK_FILENAME",
    "DayClosed",
    "LockUnavailable",
    "ROLLUP_FILENAME",
    "claim_due",
    "event_uuid",
    "record",
    "settle",
]
