"""Unit tests for the local daily usage rollup (src/ouroboros/telemetry_rollup.py)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from typing import Any
import uuid

import pytest

from ouroboros import telemetry_rollup as rollup

DAY1, DAY2, DAY3 = "2026-11-15", "2026-11-16", "2026-11-17"
DIMS = {"app_version": "1.0.0", "os": "linux"}


def _state(state_dir: Path) -> dict[str, Any]:
    return json.loads((state_dir / rollup.ROLLUP_FILENAME).read_text(encoding="utf-8"))


class TestRecord:
    def test_increments_accumulate_and_dims_keep_the_last_value(self, tmp_path: Path) -> None:
        rollup.record(tmp_path, DAY1, {"cmd_run": 1, "surface_cli": 1}, DIMS)
        rollup.record(
            tmp_path,
            DAY1,
            {"cmd_run": 2, "cmd_seed": 1},
            {"app_version": "1.1.0", "runtime_backend": "codex"},
        )
        rollup.record(tmp_path, DAY1, {"cmd_run": 1}, {"app_version": "1.1.0"})

        (bucket,) = _state(tmp_path)["days"].values()
        assert bucket["counts"] == {"cmd_run": 4, "cmd_seed": 1, "surface_cli": 1}
        # A dimension absent from a later call keeps its stored value.
        assert bucket["dims"] == {"app_version": "1.1.0", "os": "linux", "runtime_backend": "codex"}

    def test_invalid_keys_and_values_are_dropped(self, tmp_path: Path) -> None:
        rollup.record(
            tmp_path,
            DAY1,
            {"cmd_run": 1, "Bad-Key": 1, "cmd_seed": True, "cmd_qa": -2, "cmd_pm": 0},
            {"os": "linux", "app_version": "x" * 65, "nested": {"a": 1}},  # type: ignore[dict-item]
        )
        (bucket,) = _state(tmp_path)["days"].values()
        assert bucket["counts"] == {"cmd_run": 1}
        assert bucket["dims"] == {"os": "linux"}

    def test_nothing_is_written_for_no_valid_counts(self, tmp_path: Path) -> None:
        rollup.record(tmp_path, DAY1, {"Bad-Key": 1}, DIMS)
        assert not (tmp_path / rollup.ROLLUP_FILENAME).exists()

    @pytest.mark.parametrize("day", ["2026-13-01", "today", "2026-11-1", ""])
    def test_an_invalid_day_is_rejected(self, tmp_path: Path, day: str) -> None:
        with pytest.raises(ValueError):
            rollup.record(tmp_path, day, {"cmd_run": 1}, DIMS)
        with pytest.raises(ValueError):
            rollup.claim_due(tmp_path, day)


class TestBounds:
    def test_counts_are_clamped_and_keys_capped(self, tmp_path: Path) -> None:
        rollup.record(tmp_path, DAY1, {"cmd_run": rollup.MAX_COUNT}, DIMS)
        rollup.record(tmp_path, DAY1, {"cmd_run": 5}, DIMS)
        many = {f"cmd_k{index}": 1 for index in range(rollup.MAX_KEYS + 10)}
        rollup.record(tmp_path, DAY1, many, DIMS)

        (bucket,) = _state(tmp_path)["days"].values()
        assert bucket["counts"]["cmd_run"] == rollup.MAX_COUNT
        assert len(bucket["counts"]) == rollup.MAX_KEYS

    def test_buckets_far_from_today_are_discarded(self, tmp_path: Path) -> None:
        rollup.record(tmp_path, "2026-09-01", {"cmd_run": 1}, DIMS)
        rollup.record(tmp_path, DAY3, {"cmd_run": 1}, DIMS)
        assert list(_state(tmp_path)["days"]) == [DAY3]

    def test_a_clock_40_days_behind_never_discards_newer_buckets(self, tmp_path: Path) -> None:
        rollup.record(tmp_path, DAY1, {"cmd_run": 1}, DIMS)
        rollup.record(tmp_path, DAY2, {"cmd_seed": 1}, DIMS)
        now = time.time()
        (first,) = rollup.claim_due(
            tmp_path, DAY2, clock=lambda: now
        )  # DAY1: leased, not delivered
        # A process whose clock reads 40 days before DAY1 records and claims.
        behind = "2026-10-06"
        rollup.record(tmp_path, behind, {"cmd_run": 1}, DIMS)
        assert rollup.claim_due(tmp_path, behind, clock=lambda: now) == []
        state = _state(tmp_path)
        assert sorted(state["days"]) == [behind, DAY2]
        assert [b["day"] for b in state["sending"].values()] == [DAY1]

        # Back on the real clock (DAY1's claimer having died), each real day is
        # still sent exactly once; the bucket dated 42 days back is the one
        # discarded by age.
        ready = rollup.claim_due(tmp_path, DAY3, clock=lambda: now + rollup.LEASE_SECONDS + 1)
        assert sorted((b["day"], b["counts"]) for b in ready) == [
            (DAY1, {"cmd_run": 1}),
            (DAY2, {"cmd_seed": 1}),
        ]
        for bucket in ready:
            rollup.settle(tmp_path, bucket["id"], delivered=True)
        assert first["id"] in {bucket["id"] for bucket in ready}
        assert rollup.claim_due(tmp_path, DAY3, clock=lambda: now + rollup.LEASE_SECONDS * 3) == []
        assert _state(tmp_path)["sent"] == [DAY1, DAY2]

    def test_a_clock_behind_keeps_delivered_days_closed(self, tmp_path: Path) -> None:
        rollup.record(tmp_path, DAY1, {"cmd_run": 1}, DIMS)
        (claimed,) = rollup.claim_due(tmp_path, DAY2)
        rollup.settle(tmp_path, claimed["id"], delivered=True)
        rollup.record(tmp_path, "2026-10-06", {"cmd_run": 1}, DIMS)
        assert _state(tmp_path)["sent"] == [DAY1]
        with pytest.raises(rollup.DayClosed):
            rollup.record(tmp_path, DAY1, {"cmd_run": 1}, DIMS)

    def test_at_most_max_buckets_are_kept(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rollup, "MAX_BUCKETS", 3)
        for day in ("2026-11-12", "2026-11-13", "2026-11-14", DAY1, DAY2):
            rollup.record(tmp_path, day, {"cmd_run": 1}, DIMS)
        assert sorted(_state(tmp_path)["days"]) == ["2026-11-14", DAY1, DAY2]

    def test_an_oversized_or_malformed_file_reads_as_empty(self, tmp_path: Path) -> None:
        path = tmp_path / rollup.ROLLUP_FILENAME
        for garbage in (
            "{not json",
            json.dumps({"schema": 99, "days": {}}),
            json.dumps({"schema": 1, "days": {DAY1: {"id": "x", "day": DAY1}}}),
            " " * (rollup.MAX_FILE_BYTES + 1),
        ):
            path.write_text(garbage, encoding="utf-8")
            assert rollup.claim_due(tmp_path, DAY3) == []
            rollup.record(tmp_path, DAY1, {"cmd_run": 1}, DIMS)
            assert _state(tmp_path)["days"][DAY1]["counts"] == {"cmd_run": 1}
            path.unlink()


class TestClaimAndSettle:
    def test_today_is_never_claimed_and_a_past_day_once(self, tmp_path: Path) -> None:
        rollup.record(tmp_path, DAY1, {"cmd_run": 2}, DIMS)
        now = time.time()

        assert rollup.claim_due(tmp_path, DAY1, clock=lambda: now) == []
        rollup.record(tmp_path, DAY2, {"cmd_seed": 1}, DIMS)
        (claimed,) = rollup.claim_due(tmp_path, DAY2, clock=lambda: now)
        assert (claimed["day"], claimed["counts"], claimed["dims"]) == (DAY1, {"cmd_run": 2}, DIMS)
        # Leased: a second process finds nothing to send.
        assert rollup.claim_due(tmp_path, DAY2, clock=lambda: now + 1) == []

        rollup.settle(tmp_path, claimed["id"], delivered=True)
        state = _state(tmp_path)
        assert state["sending"] == {}
        assert list(state["days"]) == [DAY2]
        assert state["sent"] == [DAY1]
        assert rollup.claim_due(tmp_path, DAY2, clock=lambda: now + rollup.LEASE_SECONDS * 2) == []

    def test_an_undelivered_day_is_released_and_resent_under_the_same_id(
        self, tmp_path: Path
    ) -> None:
        rollup.record(tmp_path, DAY1, {"cmd_run": 1}, DIMS)
        now = time.time()
        (first,) = rollup.claim_due(tmp_path, DAY2, clock=lambda: now)
        rollup.settle(tmp_path, first["id"], delivered=False)
        (retry,) = rollup.claim_due(tmp_path, DAY2, clock=lambda: now + 1)
        assert retry["id"] == first["id"]

    def test_an_expired_lease_is_taken_over(self, tmp_path: Path) -> None:
        rollup.record(tmp_path, DAY1, {"cmd_run": 1}, DIMS)
        now = time.time()
        (first,) = rollup.claim_due(tmp_path, DAY2, clock=lambda: now)  # this process then dies
        assert rollup.claim_due(tmp_path, DAY2, clock=lambda: now + rollup.LEASE_SECONDS - 1) == []
        (takeover,) = rollup.claim_due(tmp_path, DAY2, clock=lambda: now + rollup.LEASE_SECONDS + 1)
        assert takeover["id"] == first["id"]

    def test_a_claimed_day_never_gets_a_second_bucket(self, tmp_path: Path) -> None:
        # A process on DAY3 claims DAY2 and holds the lease; a process whose
        # clock still reads DAY2 records again; another DAY3 process claims.
        # DAY2 must still produce exactly one row, so the late count is
        # refused (DayClosed) instead of starting a second bucket.
        rollup.record(tmp_path, DAY2, {"cmd_run": 1}, DIMS)
        now = time.time()
        (claimed,) = rollup.claim_due(tmp_path, DAY3, clock=lambda: now)
        with pytest.raises(rollup.DayClosed):
            rollup.record(tmp_path, DAY2, {"cmd_run": 5}, DIMS)
        assert rollup.claim_due(tmp_path, DAY3, clock=lambda: now + 1) == []
        assert list(_state(tmp_path)["sending"]) == [claimed["id"]]

        # The day stays closed after delivery, long after any lease.
        rollup.settle(tmp_path, claimed["id"], delivered=True)
        with pytest.raises(rollup.DayClosed):
            rollup.record(tmp_path, DAY2, {"cmd_run": 5}, DIMS)
        assert rollup.claim_due(tmp_path, DAY3, clock=lambda: now + rollup.LEASE_SECONDS * 2) == []
        state = _state(tmp_path)
        assert (state["days"], state["sending"], state["sent"]) == ({}, {}, [DAY2])
        assert claimed["counts"] == {"cmd_run": 1}

    def test_a_released_day_is_resent_unchanged_and_late_counts_stay_out(
        self, tmp_path: Path
    ) -> None:
        rollup.record(tmp_path, DAY2, {"cmd_run": 1}, DIMS)
        now = time.time()
        (first,) = rollup.claim_due(tmp_path, DAY3, clock=lambda: now)
        rollup.settle(tmp_path, first["id"], delivered=False)
        with pytest.raises(rollup.DayClosed):
            rollup.record(tmp_path, DAY2, {"cmd_seed": 1}, DIMS)
        # A retry carries exactly what the first attempt carried, so a copy
        # PostHog already stored cannot differ from it.
        (retry,) = rollup.claim_due(tmp_path, DAY3, clock=lambda: now + 1)
        assert retry == first

    def test_a_future_day_is_kept_until_it_is_over(self, tmp_path: Path) -> None:
        # A clock that moved back from DAY3 to DAY2 leaves a DAY3 bucket.
        rollup.record(tmp_path, DAY3, {"cmd_run": 1}, DIMS)
        now = time.time()
        assert rollup.claim_due(tmp_path, DAY2, clock=lambda: now) == []
        # Counts made on DAY3 itself join that bucket: one row for the day.
        rollup.record(tmp_path, DAY3, {"cmd_run": 2}, DIMS)
        (claimed,) = rollup.claim_due(tmp_path, "2026-11-18", clock=lambda: now)
        assert (claimed["day"], claimed["counts"]) == (DAY3, {"cmd_run": 3})

    def test_a_lease_from_a_clock_that_was_ahead_counts_as_expired(self, tmp_path: Path) -> None:
        rollup.record(tmp_path, DAY1, {"cmd_run": 1}, DIMS)
        now = time.time()
        (first,) = rollup.claim_due(tmp_path, DAY2, clock=lambda: now)
        # The clock then jumps back a day: a lease that far ahead cannot be
        # live, so the bucket is claimable again.
        (again,) = rollup.claim_due(tmp_path, DAY2, clock=lambda: now - 86_400)
        assert again["id"] == first["id"]

    def test_a_fresh_lease_is_kept_from_a_process_whose_reading_is_earlier(
        self, tmp_path: Path
    ) -> None:
        # The CI interleaving (#3031): B's clock reads t and A's t + 0.3, but A
        # gets the lock first and leases DAY1 until t + 0.3 + LEASE_SECONDS.
        # B must not take that lease for one set by a clock that was ahead.
        rollup.record(tmp_path, DAY1, {"cmd_run": 1}, DIMS)
        t = 1_800_000_000.0
        (first,) = rollup.claim_due(tmp_path, DAY2, clock=lambda: t + 0.3)
        assert rollup.claim_due(tmp_path, DAY2, clock=lambda: t) == []
        # Even a reading a full tolerance earlier leaves the lease alone.
        earliest = t + 0.3 - rollup.CLOCK_AHEAD_TOLERANCE_SECONDS
        assert rollup.claim_due(tmp_path, DAY2, clock=lambda: earliest) == []
        assert [b["id"] for b in _state(tmp_path)["sending"].values()] == [first["id"]]

    def test_a_lease_beyond_the_tolerance_counts_as_set_by_a_clock_ahead(
        self, tmp_path: Path
    ) -> None:
        rollup.record(tmp_path, DAY1, {"cmd_run": 1}, DIMS)
        t = 1_800_000_000.0
        (first,) = rollup.claim_due(tmp_path, DAY2, clock=lambda: t)
        edge = t - rollup.CLOCK_AHEAD_TOLERANCE_SECONDS  # lease ends a term + tolerance ahead
        assert rollup.claim_due(tmp_path, DAY2, clock=lambda: edge) == []
        (again,) = rollup.claim_due(tmp_path, DAY2, clock=lambda: edge - 1)
        assert again["id"] == first["id"]

    def test_the_clock_is_read_only_once_the_lock_is_held(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A reading taken before waiting for the lock is what let a contender
        # judge a lease written while it waited. The default clock (what
        # telemetry uses) must be read after the file lock is acquired.
        rollup.record(tmp_path, DAY1, {"cmd_run": 1}, DIMS)
        events: list[str] = []
        real_try_lock = rollup._try_lock

        def try_lock(fd: int) -> bool:
            acquired = real_try_lock(fd)
            if acquired:
                events.append("locked")
            return acquired

        def wall_clock() -> float:
            events.append("clock")
            return time.time()

        monkeypatch.setattr(rollup, "_try_lock", try_lock)
        monkeypatch.setattr(
            rollup,
            "time",
            SimpleNamespace(time=wall_clock, monotonic=time.monotonic, sleep=time.sleep),
        )
        (claimed,) = rollup.claim_due(tmp_path, DAY2)
        assert events == ["locked", "clock"]
        assert claimed["day"] == DAY1

    def test_a_file_with_a_second_bucket_for_a_closed_day_keeps_one(self, tmp_path: Path) -> None:
        rollup.record(tmp_path, DAY1, {"cmd_run": 1}, DIMS)
        rollup.record(tmp_path, DAY2, {"cmd_run": 1}, DIMS)
        now = time.time()
        (claimed,) = rollup.claim_due(tmp_path, DAY2, clock=lambda: now)
        rollup.record(tmp_path, DAY3, {"cmd_run": 1}, DIMS)
        # Hand-craft what this module never writes: open buckets for a day
        # that is being sent (DAY1) and for one already delivered (DAY3).
        path = tmp_path / rollup.ROLLUP_FILENAME
        state = _state(tmp_path)
        state["days"][DAY1] = {
            **state["days"][DAY2],
            "id": "00000000-0000-4000-8000-000000000001",
            "day": DAY1,
        }
        state["sent"] = [DAY3, DAY3, "not-a-day", 7]
        path.write_text(json.dumps(state), encoding="utf-8")

        rollup.record(tmp_path, DAY2, {"cmd_run": 1}, DIMS)
        state = _state(tmp_path)
        assert sorted(state["days"]) == [DAY2]
        assert state["sent"] == [DAY3]
        assert list(state["sending"]) == [claimed["id"]]
        # DAY1 is still leased, so only DAY2 is ready; nothing for DAY1 or DAY3.
        ready = rollup.claim_due(tmp_path, "2026-11-18", clock=lambda: now + 1)
        assert [(b["day"], b["counts"]) for b in ready] == [(DAY2, {"cmd_run": 2})]

    def test_delivered_days_are_forgotten_outside_the_age_window(self, tmp_path: Path) -> None:
        rollup.record(tmp_path, DAY1, {"cmd_run": 1}, DIMS)
        (claimed,) = rollup.claim_due(tmp_path, DAY2)
        rollup.settle(tmp_path, claimed["id"], delivered=True)
        assert _state(tmp_path)["sent"] == [DAY1]
        rollup.record(tmp_path, "2026-12-16", {"cmd_run": 1}, DIMS)  # 31 days later
        assert _state(tmp_path)["sent"] == []

    def test_settling_an_unknown_bucket_changes_nothing(self, tmp_path: Path) -> None:
        rollup.record(tmp_path, DAY1, {"cmd_run": 1}, DIMS)
        before = _state(tmp_path)
        rollup.settle(tmp_path, "00000000-0000-4000-8000-000000000000", delivered=True)
        assert _state(tmp_path) == before


class TestEventUuid:
    def test_the_uuid_depends_only_on_the_install_and_the_day(self) -> None:
        install, other = "11111111-1111-4111-8111-111111111111", str(uuid.uuid4())
        first = rollup.event_uuid(install, DAY1)
        assert first == rollup.event_uuid(install, DAY1)
        assert uuid.UUID(first).version == 5
        assert len({first, rollup.event_uuid(install, DAY2), rollup.event_uuid(other, DAY1)}) == 3

    @pytest.mark.parametrize("day", ["2026-13-01", "2026-11-1", "", "2026-11-16T00:00"])
    def test_an_invalid_day_is_rejected(self, day: str) -> None:
        with pytest.raises(ValueError):
            rollup.event_uuid("11111111-1111-4111-8111-111111111111", day)


class TestCrashSafety:
    def test_a_crash_mid_write_leaves_the_previous_file_intact(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rollup.record(tmp_path, DAY1, {"cmd_run": 1}, DIMS)
        before = (tmp_path / rollup.ROLLUP_FILENAME).read_bytes()

        def crash(*_args: Any) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(os, "replace", crash)
        with pytest.raises(OSError, match="disk full"):
            rollup.record(tmp_path, DAY1, {"cmd_run": 1}, DIMS)
        monkeypatch.undo()

        assert (tmp_path / rollup.ROLLUP_FILENAME).read_bytes() == before
        assert list(tmp_path.glob("*.tmp")) == []
        rollup.record(tmp_path, DAY1, {"cmd_run": 1}, DIMS)
        assert _state(tmp_path)["days"][DAY1]["counts"] == {"cmd_run": 2}

    def test_a_torn_file_from_a_killed_writer_is_recovered(self, tmp_path: Path) -> None:
        rollup.record(tmp_path, DAY1, {"cmd_run": 1}, DIMS)
        path = tmp_path / rollup.ROLLUP_FILENAME
        path.write_bytes(path.read_bytes()[:20])
        # Whatever the file held is lost, but the rollup keeps working.
        rollup.record(tmp_path, DAY1, {"cmd_run": 1}, DIMS)
        assert _state(tmp_path)["days"][DAY1]["counts"] == {"cmd_run": 1}


class TestLocking:
    def test_a_held_lock_makes_writers_give_up_within_the_bound(self, tmp_path: Path) -> None:
        holder_code = (
            "import sys, time\n"
            "from pathlib import Path\n"
            "from ouroboros import telemetry_rollup as rollup\n"
            "with rollup._locked(Path(sys.argv[1])):\n"
            "    print('held', flush=True)\n"
            "    sys.stdin.readline()\n"
        )
        holder = subprocess.Popen(
            [sys.executable, "-c", holder_code, str(tmp_path)],
            env=_child_env(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
            started = time.monotonic()
            with pytest.raises(rollup.LockUnavailable):
                rollup.record(tmp_path, DAY1, {"cmd_run": 1}, DIMS)
            elapsed = time.monotonic() - started
            assert rollup.LOCK_TIMEOUT_SECONDS <= elapsed < rollup.LOCK_TIMEOUT_SECONDS + 1.0
        finally:
            assert holder.stdin is not None
            holder.stdin.write("\n")
            holder.stdin.flush()
            holder.wait(timeout=10)
        rollup.record(tmp_path, DAY1, {"cmd_run": 1}, DIMS)
        assert _state(tmp_path)["days"][DAY1]["counts"] == {"cmd_run": 1}

    @pytest.mark.parametrize("operation", ["record", "claim_due", "settle"])
    @pytest.mark.parametrize("thread_wait", [0.0, 0.3, 0.5])
    def test_one_deadline_bounds_the_thread_and_file_lock_waits_together(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        operation: str,
        thread_wait: float,
    ) -> None:
        # Another thread holds the in-process lock for ``thread_wait`` seconds,
        # then every file-lock attempt fails. On a fake clock the whole wait
        # must fit one LOCK_TIMEOUT_SECONDS budget, not one budget per lock.
        clock = _FakeClock(start=1000.0)
        acquire_timeouts: list[float] = []
        attempts: list[int] = []

        class _BusyThreadLock:
            def acquire(self, timeout: float) -> bool:
                acquire_timeouts.append(timeout)
                clock.now += min(thread_wait, timeout)
                return thread_wait <= timeout

            def release(self) -> None:
                pass

        def _file_lock_busy(fd: int) -> bool:
            attempts.append(fd)
            return False

        monkeypatch.setattr(rollup, "time", clock)
        monkeypatch.setattr(rollup, "_thread_lock", _BusyThreadLock())
        monkeypatch.setattr(rollup, "_try_lock", _file_lock_busy)
        calls = {
            "record": lambda: rollup.record(tmp_path, DAY1, {"cmd_run": 1}, DIMS),
            "claim_due": lambda: rollup.claim_due(tmp_path, DAY2, clock=lambda: 0.0),
            "settle": lambda: rollup.settle(tmp_path, "id", delivered=True),
        }

        with pytest.raises(rollup.LockUnavailable):
            calls[operation]()

        assert acquire_timeouts == [pytest.approx(rollup.LOCK_TIMEOUT_SECONDS)]
        assert clock.now - 1000.0 == pytest.approx(rollup.LOCK_TIMEOUT_SECONDS)
        # The file lock is still tried with whatever budget is left, at least once.
        assert attempts
        assert not (tmp_path / rollup.ROLLUP_FILENAME).exists()

    def test_a_held_thread_lock_and_a_busy_file_lock_share_one_real_budget(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The same contention on the real clock and the real thread lock: the
        # caller waits about 0.3 s for the thread lock, then only the remaining
        # 0.2 s for the file lock (two separate budgets would take ~0.8 s).
        monkeypatch.setattr(rollup, "_try_lock", lambda _fd: False)
        held = threading.Event()

        def hold_thread_lock() -> None:
            with rollup._thread_lock:
                held.set()
                time.sleep(0.3)

        holder = threading.Thread(target=hold_thread_lock)
        holder.start()
        try:
            assert held.wait(timeout=5)
            started = time.monotonic()
            with pytest.raises(rollup.LockUnavailable):
                rollup.record(tmp_path, DAY1, {"cmd_run": 1}, DIMS)
            elapsed = time.monotonic() - started
        finally:
            holder.join(timeout=5)
        assert rollup.LOCK_TIMEOUT_SECONDS <= elapsed < rollup.LOCK_TIMEOUT_SECONDS + 0.15
        assert not (tmp_path / rollup.ROLLUP_FILENAME).exists()

    def test_threads_in_one_process_do_not_lose_counts(self, tmp_path: Path) -> None:
        def work() -> None:
            for _ in range(25):
                rollup.record(tmp_path, DAY1, {"cmd_run": 1}, DIMS)

        threads = [threading.Thread(target=work) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert _state(tmp_path)["days"][DAY1]["counts"] == {"cmd_run": 100}

    def test_concurrent_processes_neither_lose_counts_nor_claim_twice(self, tmp_path: Path) -> None:
        rollup.record(tmp_path, DAY1, {"cmd_auto": 1}, DIMS)
        sentinel = tmp_path / "go"
        child_code = (
            "import json, sys, time\n"
            "from pathlib import Path\n"
            "from ouroboros import telemetry_rollup as rollup\n"
            "state_dir, sentinel = Path(sys.argv[1]), Path(sys.argv[2])\n"
            "deadline = time.monotonic() + 10\n"
            "while not sentinel.exists() and time.monotonic() < deadline:\n"
            "    time.sleep(0.002)\n"
            "claimed = []\n"
            "for _ in range(20):\n"
            f"    rollup.record(state_dir, {DAY2!r}, {{'cmd_run': 1}}, {{}})\n"
            f"    claimed += rollup.claim_due(state_dir, {DAY2!r})\n"
            "print(json.dumps([bucket['id'] for bucket in claimed]))\n"
        )
        children = [
            subprocess.Popen(
                [sys.executable, "-c", child_code, str(tmp_path), str(sentinel)],
                env=_child_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(6)
        ]
        sentinel.write_text("go", encoding="utf-8")
        claimed: list[str] = []
        for child in children:
            stdout, stderr = child.communicate(timeout=60)
            assert child.returncode == 0, stderr
            claimed += json.loads(stdout)

        assert len(claimed) == 1  # the DAY1 bucket, by exactly one process
        state = _state(tmp_path)
        assert state["days"][DAY2]["counts"] == {"cmd_run": 120}
        assert list(state["sending"]) == claimed


class _FakeClock:
    """Stands in for the ``time`` module inside ``telemetry_rollup``."""

    def __init__(self, start: float) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        assert seconds >= 0
        self.now += seconds


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    return env
