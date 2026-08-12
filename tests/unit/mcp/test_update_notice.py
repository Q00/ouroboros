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


class TestBackgroundRefresh:
    def _fresh_event(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import threading

        monkeypatch.setattr(update_notice, "_REFRESH_STARTED", threading.Event())

    def test_stale_cache_schedules_one_refresh(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing or stale cache schedules exactly one background
        refresh per process (#2066 R1): standalone hosts get a producer."""
        self._fresh_event(monkeypatch)
        _pin_installed(monkeypatch, "0.51.0")
        monkeypatch.setattr(update_notice, "_CACHE_FILE", tmp_path / "absent.json")
        started: list[str] = []

        class _RecordingThread:
            def __init__(self, *, target, name, daemon):
                assert daemon is True
                started.append(name)

            def start(self) -> None:
                pass

        monkeypatch.setattr(update_notice.threading, "Thread", _RecordingThread)

        update_notice.append_cached_update_notice("Base.\n")
        update_notice.append_cached_update_notice("Base.\n")

        assert started == ["ouroboros-update-notice-refresh"]

    def test_fresh_cache_schedules_no_refresh(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._fresh_event(monkeypatch)
        _pin_installed(monkeypatch, "0.51.0")
        _write_cache(
            tmp_path,
            monkeypatch,
            {"latest_version": "0.51.0", "timestamp": time.time()},
        )

        def _explode(*_args, **_kwargs):
            raise AssertionError("no thread with a fresh cache")

        monkeypatch.setattr(update_notice.threading, "Thread", _explode)

        assert update_notice.append_cached_update_notice("Base.\n") == "Base.\n"

    def test_refresh_writes_channel_and_timestamp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The producer writes the writer's own cache shape, preserving
        unrelated keys."""
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
        assert payload["latest_version_pre"] == "0.52.0b1"
        assert payload["timestamp"] > 5

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
