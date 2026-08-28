"""Tests for the offline MCP update nudge (#2066)."""

from __future__ import annotations

from importlib import metadata
import json
from pathlib import Path
import time

import pytest

from ouroboros.mcp import update_notice


def _write_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: object,
) -> Path:
    cache_file = tmp_path / "version-check-cache.json"
    cache_file.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(update_notice, "_CACHE_FILE", cache_file)
    return cache_file


def _pin_installed(monkeypatch: pytest.MonkeyPatch, version: str) -> None:
    monkeypatch.setattr(update_notice.metadata, "version", lambda _name: version)


class TestCachedUpdateNotice:
    def test_fresh_cache_with_newer_version_yields_notice(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _pin_installed(monkeypatch, "0.51.0")
        _write_cache(
            tmp_path,
            monkeypatch,
            {"latest_version": "0.52.0", "timestamp": time.time()},
        )

        notice = update_notice.cached_update_notice()

        assert notice == (
            'Ouroboros 0.52.0 is available (installed: 0.51.0). Suggest running "ooo update".'
        )

    def test_missing_cache_yields_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _pin_installed(monkeypatch, "0.51.0")
        monkeypatch.setattr(update_notice, "_CACHE_FILE", tmp_path / "absent.json")

        assert update_notice.cached_update_notice() is None

    def test_stale_cache_yields_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _pin_installed(monkeypatch, "0.51.0")
        _write_cache(
            tmp_path,
            monkeypatch,
            {"latest_version": "0.52.0", "timestamp": time.time() - 90000},
        )

        assert update_notice.cached_update_notice() is None

    def test_future_timestamp_yields_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A timestamp from the future is untrusted, not eternally fresh."""
        _pin_installed(monkeypatch, "0.51.0")
        _write_cache(
            tmp_path,
            monkeypatch,
            {"latest_version": "0.52.0", "timestamp": time.time() + 3600},
        )

        assert update_notice.cached_update_notice() is None

    @pytest.mark.parametrize("latest", ["0.51.0", "0.50.0"])
    def test_equal_or_older_latest_yields_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, latest: str
    ) -> None:
        _pin_installed(monkeypatch, "0.51.0")
        _write_cache(
            tmp_path,
            monkeypatch,
            {"latest_version": latest, "timestamp": time.time()},
        )

        assert update_notice.cached_update_notice() is None

    @pytest.mark.parametrize(
        "content",
        ["not json {", '"a bare string"', '{"latest_version": "0.52.0"}'],
    )
    def test_malformed_cache_yields_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str
    ) -> None:
        _pin_installed(monkeypatch, "0.51.0")
        cache_file = tmp_path / "version-check-cache.json"
        cache_file.write_text(content, encoding="utf-8")
        monkeypatch.setattr(update_notice, "_CACHE_FILE", cache_file)

        assert update_notice.cached_update_notice() is None

    def test_prerelease_install_reads_prerelease_channel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A prerelease install compares against latest_version_pre, matching
        the writer's channel split in scripts/version-check.py."""
        _pin_installed(monkeypatch, "0.52.0b1")
        _write_cache(
            tmp_path,
            monkeypatch,
            {
                "latest_version": "0.51.0",
                "latest_version_pre": "0.52.0b2",
                "timestamp": time.time(),
            },
        )

        notice = update_notice.cached_update_notice()

        assert notice is not None
        assert "0.52.0b2" in notice

    def test_uninstalled_package_yields_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise(_name: str) -> str:
            raise metadata.PackageNotFoundError("ouroboros-ai")

        monkeypatch.setattr(update_notice.metadata, "version", _raise)
        _write_cache(
            tmp_path,
            monkeypatch,
            {"latest_version": "0.52.0", "timestamp": time.time()},
        )

        assert update_notice.cached_update_notice() is None

    def test_unparseable_installed_version_yields_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _pin_installed(monkeypatch, "not-a-version")
        _write_cache(
            tmp_path,
            monkeypatch,
            {"latest_version": "0.52.0", "timestamp": time.time()},
        )

        assert update_notice.cached_update_notice() is None

    def test_stale_prerelease_value_does_not_survive_stable_refresh(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#2066 R3: a stable-channel refresh stamps only its own channel,
        so an older prerelease value is not resurrected by the refreshed
        shared timestamp — the prerelease reader sees it as stale."""
        _pin_installed(monkeypatch, "0.52.0b1")
        _write_cache(
            tmp_path,
            monkeypatch,
            {
                "latest_version": "0.52.0",
                "latest_version_checked_at": time.time(),
                "latest_version_pre": "0.52.0b3",
                "timestamp": time.time(),
            },
        )

        assert update_notice.cached_update_notice() is None

    def test_stamped_channel_serves_its_fresh_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A per-channel stamp attests its own channel even when the
        legacy shared timestamp is old."""
        _pin_installed(monkeypatch, "0.51.0")
        _write_cache(
            tmp_path,
            monkeypatch,
            {
                "latest_version": "0.52.0",
                "latest_version_checked_at": time.time(),
                "timestamp": 5,
            },
        )

        notice = update_notice.cached_update_notice()

        assert notice is not None
        assert "0.52.0" in notice


class _RecordingThread:
    started: list[str] = []

    def __init__(self, *, target, name, daemon):
        assert daemon is True
        type(self).started.append(name)

    def start(self) -> None:
        pass


class TestBackgroundRefresh:
    def _fresh_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import threading

        monkeypatch.setattr(update_notice, "_REFRESH_STARTED", threading.Event())
        monkeypatch.setattr(_RecordingThread, "started", [])
        monkeypatch.setattr(update_notice.threading, "Thread", _RecordingThread)

    def test_stale_cache_schedules_one_refresh(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing or stale cache schedules exactly one background
        refresh per process (#2066 R1): standalone hosts get a producer."""
        self._fresh_gate(monkeypatch)
        _pin_installed(monkeypatch, "0.51.0")
        monkeypatch.setattr(update_notice, "_CACHE_FILE", tmp_path / "absent.json")

        update_notice.maybe_schedule_cache_refresh()
        update_notice.maybe_schedule_cache_refresh()

        assert _RecordingThread.started == ["ouroboros-update-notice-refresh"]

    def test_fresh_cache_schedules_no_refresh(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._fresh_gate(monkeypatch)
        _pin_installed(monkeypatch, "0.51.0")
        _write_cache(
            tmp_path,
            monkeypatch,
            {"latest_version": "0.51.0", "timestamp": time.time()},
        )

        update_notice.maybe_schedule_cache_refresh()

        assert _RecordingThread.started == []

    @pytest.mark.parametrize(
        ("installed", "payload"),
        [
            ("0.52.0b1", {"latest_version": "0.52.0"}),
            ("0.51.0", {"latest_version_pre": "0.52.0b2"}),
            ("0.51.0", {"latest_version": "not-a-version"}),
        ],
    )
    def test_missing_channel_value_counts_as_stale(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        installed: str,
        payload: dict,
    ) -> None:
        """#2066 R2: freshness is channel-specific — a fresh payload
        without a usable value for the INSTALLED channel still refreshes,
        covering stable-to-prerelease and prerelease-to-stable
        transitions and unparseable cached values."""
        self._fresh_gate(monkeypatch)
        _pin_installed(monkeypatch, installed)
        _write_cache(
            tmp_path,
            monkeypatch,
            {**payload, "timestamp": time.time()},
        )

        update_notice.maybe_schedule_cache_refresh()

        assert _RecordingThread.started == ["ouroboros-update-notice-refresh"]

    def test_unstamped_channel_in_stamped_cache_refreshes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#2066 R3: after a stable-channel refresh, a prerelease process
        must not trust the leftover prerelease value — it refreshes its
        own channel instead."""
        self._fresh_gate(monkeypatch)
        _pin_installed(monkeypatch, "0.52.0b1")
        _write_cache(
            tmp_path,
            monkeypatch,
            {
                "latest_version": "0.52.0",
                "latest_version_checked_at": time.time(),
                "latest_version_pre": "0.52.0b3",
                "timestamp": time.time(),
            },
        )

        update_notice.maybe_schedule_cache_refresh()

        assert _RecordingThread.started == ["ouroboros-update-notice-refresh"]

    def test_concurrent_callers_start_exactly_one_refresh(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#2066 R2: the once gate is atomic — 20 barrier-synchronized
        callers start exactly one refresh thread."""
        import threading

        monkeypatch.setattr(update_notice, "_REFRESH_STARTED", threading.Event())
        _pin_installed(monkeypatch, "0.51.0")
        monkeypatch.setattr(update_notice, "_CACHE_FILE", tmp_path / "absent.json")
        started: list[str] = []
        started_lock = threading.Lock()

        def _record_refresh() -> None:
            with started_lock:
                started.append("refresh")

        monkeypatch.setattr(update_notice, "_refresh_cache_now", _record_refresh)
        caller_count = 20
        barrier = threading.Barrier(caller_count)

        def _call() -> None:
            barrier.wait()
            update_notice.maybe_schedule_cache_refresh()

        callers = [threading.Thread(target=_call) for _ in range(caller_count)]
        for caller in callers:
            caller.start()
        for caller in callers:
            caller.join()
        deadline = time.time() + 5
        while time.time() < deadline:
            with started_lock:
                if started:
                    break
            time.sleep(0.01)

        time.sleep(0.05)
        assert started == ["refresh"]

    def test_append_is_side_effect_free(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#2066 R2: instruction formatting never schedules the producer —
        non-serving create_ouroboros_server() callers stay network-free."""
        self._fresh_gate(monkeypatch)
        _pin_installed(monkeypatch, "0.51.0")
        monkeypatch.setattr(update_notice, "_CACHE_FILE", tmp_path / "absent.json")

        update_notice.append_cached_update_notice("Base.\n")

        assert _RecordingThread.started == []

    def test_refresh_stamps_only_its_own_channel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#2066 R3: the producer stamps the refreshed channel and leaves
        the shared legacy timestamp alone, so its write can never make the
        other channel's older value look current."""
        _pin_installed(monkeypatch, "0.51.0")
        cache_file = tmp_path / "version-check-cache.json"
        cache_file.write_text(json.dumps({"latest_version_pre": "0.52.0b1", "timestamp": 5}))
        monkeypatch.setattr(update_notice, "_CACHE_FILE", cache_file)
        monkeypatch.setattr(
            update_notice,
            "_fetch_latest_from_pypi",
            lambda *, include_prerelease: "0.52.0",  # noqa: ARG005
        )

        update_notice._refresh_cache_now()

        payload = json.loads(cache_file.read_text())
        assert payload["latest_version"] == "0.52.0"
        assert payload["latest_version_checked_at"] > 5
        assert payload["latest_version_pre"] == "0.52.0b1"
        assert "latest_version_pre_checked_at" not in payload
        assert payload["timestamp"] == 5

    def test_refresh_failure_is_swallowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _pin_installed(monkeypatch, "0.51.0")
        monkeypatch.setattr(update_notice, "_CACHE_FILE", tmp_path / "cache.json")

        def _explode(*, include_prerelease):
            raise OSError("network down")

        monkeypatch.setattr(update_notice, "_fetch_latest_from_pypi", _explode)

        update_notice._refresh_cache_now()

        assert not (tmp_path / "cache.json").exists()


class TestAppendCachedUpdateNotice:
    def test_appends_notice_after_blank_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _pin_installed(monkeypatch, "0.51.0")
        _write_cache(
            tmp_path,
            monkeypatch,
            {"latest_version": "0.52.0", "timestamp": time.time()},
        )

        combined = update_notice.append_cached_update_notice("Base instructions.\n")

        assert combined.startswith("Base instructions.\n\nOuroboros 0.52.0")
        assert combined.endswith('"ooo update".\n')

    def test_without_notice_returns_instructions_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _pin_installed(monkeypatch, "0.51.0")
        monkeypatch.setattr(update_notice, "_CACHE_FILE", tmp_path / "absent.json")

        assert update_notice.append_cached_update_notice("Base.\n") == "Base.\n"

    @pytest.mark.parametrize(
        "error",
        [OSError("corrupt dist-info"), RuntimeError("metadata backend failure")],
    )
    def test_unexpected_metadata_errors_fail_open(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
    ) -> None:
        """#2066 R1: the seam is the complete advisory boundary — errors
        beyond PackageNotFoundError must not escape toward server
        construction."""

        def _raise(_name: str) -> str:
            raise error

        monkeypatch.setattr(update_notice.metadata, "version", _raise)
        _write_cache(
            tmp_path,
            monkeypatch,
            {"latest_version": "0.52.0", "timestamp": time.time()},
        )

        assert update_notice.append_cached_update_notice("Base.\n") == "Base.\n"
