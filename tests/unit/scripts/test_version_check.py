"""Tests for version-check script."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from ouroboros.mcp import update_notice

# Load the script as a module
_SCRIPT_PATH = Path(__file__).parent.parent.parent.parent / "scripts" / "version-check.py"
_spec = importlib.util.spec_from_file_location("version_check", str(_SCRIPT_PATH))
version_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(version_check)


def _block_packaging_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "packaging" or name.startswith("packaging."):
        raise ModuleNotFoundError("No module named 'packaging'", name=name)
    return _ORIGINAL_IMPORT(name, globals, locals, fromlist, level)


_ORIGINAL_IMPORT = __import__


class TestGetInstalledVersion:
    """Test get_installed_version."""

    def test_reads_from_plugin_json(self, tmp_path: Path) -> None:
        """Falls back to importlib.metadata when plugin.json not at expected path."""
        # Since plugin.json path is relative to script location,
        # just verify the function doesn't crash
        result = version_check.get_installed_version()
        # Should return a string or None
        assert result is None or isinstance(result, str)


class TestGetLatestVersion:
    """Test get_latest_version with caching."""

    def test_returns_cached_version(self, tmp_path: Path) -> None:
        """Returns cached version within TTL."""
        cache_file = tmp_path / "version-check-cache.json"
        cache_data = {
            "latest_version": "1.2.3",
            "timestamp": time.time(),  # fresh cache
        }
        cache_file.write_text(json.dumps(cache_data))

        with patch.object(version_check, "_CACHE_FILE", cache_file):
            result = version_check.get_latest_version()

        assert result == "1.2.3"

    def test_expired_cache_fetches_from_pypi(self, tmp_path: Path) -> None:
        """Expired cache triggers PyPI fetch."""
        cache_file = tmp_path / "version-check-cache.json"
        cache_data = {
            "latest_version": "0.1.0",
            "timestamp": time.time() - 100000,  # expired
        }
        cache_file.write_text(json.dumps(cache_data))

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"info": {"version": "2.0.0"}}).encode()

        with (
            patch.object(version_check, "_CACHE_FILE", cache_file),
            patch.object(version_check, "_CACHE_DIR", tmp_path),
            patch("urllib.request.urlopen", return_value=mock_response),
        ):
            result = version_check.get_latest_version()

        assert result == "2.0.0"

        # Verify cache was updated
        new_cache = json.loads(cache_file.read_text())
        assert new_cache["latest_version"] == "2.0.0"

    def test_network_failure_returns_none(self, tmp_path: Path) -> None:
        """Returns None when PyPI is unreachable and no cache."""
        cache_file = tmp_path / "nonexistent-cache.json"

        with (
            patch.object(version_check, "_CACHE_FILE", cache_file),
            patch("urllib.request.urlopen", side_effect=TimeoutError),
        ):
            result = version_check.get_latest_version()

        assert result is None

    def test_stale_prerelease_channel_refetches_after_stable_stamp(self, tmp_path: Path) -> None:
        """#2066: a stamped stable refresh cannot vouch for the prerelease
        channel — the unstamped leftover value is stale and refetched."""
        cache_file = tmp_path / "version-check-cache.json"
        cache_file.write_text(
            json.dumps(
                {
                    "latest_version": "2.0.0",
                    "latest_version_checked_at": time.time(),
                    "latest_version_pre": "2.0.0b1",
                    "timestamp": time.time(),
                }
            )
        )
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {"info": {"version": "2.0.0"}, "releases": {"2.1.0b2": [{}]}}
        ).encode()

        with (
            patch.object(version_check, "_CACHE_FILE", cache_file),
            patch.object(version_check, "_CACHE_DIR", tmp_path),
            patch("urllib.request.urlopen", return_value=mock_response),
        ):
            result = version_check.get_latest_version(current="2.0.0b1")

        assert result == "2.1.0b2"
        new_cache = json.loads(cache_file.read_text())
        assert new_cache["latest_version_pre"] == "2.1.0b2"
        assert "latest_version_pre_checked_at" in new_cache

    def test_future_timestamp_is_not_fresh(self, tmp_path: Path) -> None:
        """A clock-rollback or malformed future stamp must not be trusted —
        matches the MCP reader's `0 <= age < TTL` rule (src/ouroboros/mcp/update_notice.py)."""
        cache_file = tmp_path / "version-check-cache.json"
        cache_file.write_text(
            json.dumps(
                {
                    "latest_version": "9.9.9",
                    "latest_version_checked_at": time.time() + 10**9,
                    "timestamp": time.time() + 10**9,
                }
            )
        )
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"info": {"version": "2.0.0"}}).encode()

        with (
            patch.object(version_check, "_CACHE_FILE", cache_file),
            patch.object(version_check, "_CACHE_DIR", tmp_path),
            patch("urllib.request.urlopen", return_value=mock_response),
        ):
            result = version_check.get_latest_version()

        assert result == "2.0.0"

    def test_future_shared_timestamp_is_not_fresh(self, tmp_path: Path) -> None:
        """Same rejection for the legacy shared `timestamp` fallback used by
        caches written before per-channel stamps existed."""
        cache_file = tmp_path / "version-check-cache.json"
        cache_file.write_text(
            json.dumps(
                {
                    "latest_version": "9.9.9",
                    "timestamp": time.time() + 10**9,
                }
            )
        )
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"info": {"version": "2.0.0"}}).encode()

        with (
            patch.object(version_check, "_CACHE_FILE", cache_file),
            patch.object(version_check, "_CACHE_DIR", tmp_path),
            patch("urllib.request.urlopen", return_value=mock_response),
        ):
            result = version_check.get_latest_version()

        assert result == "2.0.0"

    def test_hook_and_mcp_concurrent_channel_writers_preserve_both_updates(
        self, tmp_path: Path
    ) -> None:
        """The shared lock covers read/merge/replace, not only replacement."""
        cache_file = tmp_path / "version-check-cache.json"
        fetch_barrier = threading.Barrier(2)

        def _hook_fetch(*, include_prerelease: bool = False) -> str:
            assert include_prerelease is False
            fetch_barrier.wait()
            return "2.0.0"

        def _mcp_fetch(*, include_prerelease: bool) -> str:
            assert include_prerelease is True
            fetch_barrier.wait()
            return "2.1.0b2"

        with (
            patch.object(version_check, "_CACHE_FILE", cache_file),
            patch.object(version_check, "_CACHE_DIR", tmp_path),
            patch.object(version_check, "_get_latest_from_pypi", _hook_fetch),
            patch.object(update_notice, "_CACHE_FILE", cache_file),
            patch.object(update_notice.metadata, "version", return_value="2.1.0b1"),
            patch.object(update_notice, "_fetch_latest_from_pypi", _mcp_fetch),
        ):
            hook = threading.Thread(target=version_check.get_latest_version)
            mcp = threading.Thread(target=update_notice._refresh_cache_now)
            hook.start()
            mcp.start()
            hook.join(timeout=5)
            mcp.join(timeout=5)

        assert not hook.is_alive()
        assert not mcp.is_alive()
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
        assert payload["latest_version"] == "2.0.0"
        assert payload["latest_version_pre"] == "2.1.0b2"
        assert "latest_version_checked_at" in payload
        assert "latest_version_pre_checked_at" in payload

    def test_non_object_json_cache_is_replaced(self, tmp_path: Path) -> None:
        """Valid JSON with the wrong shape must not disable cache refreshes."""
        for malformed_payload in ([], "invalid"):
            cache_file = tmp_path / "version-check-cache.json"
            cache_file.write_text(json.dumps(malformed_payload))
            with (
                patch.object(version_check, "_CACHE_FILE", cache_file),
                patch.object(version_check, "_CACHE_DIR", tmp_path),
                patch.object(version_check, "_get_latest_from_pypi", return_value="2.0.0"),
            ):
                assert version_check.get_latest_version() == "2.0.0"

            payload = json.loads(cache_file.read_text())
            assert payload["latest_version"] == "2.0.0"
            assert "latest_version_checked_at" in payload


class TestNoticeConsumption:
    def test_notice_is_consumed_once_per_day(self, tmp_path: Path) -> None:
        cache_file = tmp_path / "version-check-cache.json"
        with (
            patch.object(version_check, "_CACHE_FILE", cache_file),
            patch.object(version_check, "_CACHE_DIR", tmp_path),
        ):
            assert version_check.consume_update_notice(current="0.20.0") is True
            assert version_check.consume_update_notice(current="0.20.0") is False


class TestCheckUpdate:
    """Test check_update logic."""

    def test_update_available(self) -> None:
        """Detects when newer version is available."""
        with (
            patch.object(version_check, "get_installed_version", return_value="0.19.0"),
            patch.object(version_check, "get_latest_version", return_value="0.20.0"),
        ):
            result = version_check.check_update()

        assert result["update_available"] is True
        assert result["current"] == "0.19.0"
        assert result["latest"] == "0.20.0"
        assert "ooo update" in result["message"]

    def test_update_available_without_packaging(self) -> None:
        """SessionStart's host Python need not contain the packaging module."""
        with (
            patch("builtins.__import__", side_effect=_block_packaging_import),
            patch.object(version_check, "get_installed_version", return_value="0.51.13"),
            patch.object(version_check, "get_latest_version", return_value="0.51.14"),
        ):
            result = version_check.check_update()

        assert result["update_available"] is True
        assert result["message"] == (
            "Ouroboros update available: v0.51.13 → v0.51.14. Run `ooo update` to upgrade."
        )

    def test_up_to_date(self) -> None:
        """No update when versions match."""
        with (
            patch.object(version_check, "get_installed_version", return_value="0.20.0"),
            patch.object(version_check, "get_latest_version", return_value="0.20.0"),
        ):
            result = version_check.check_update()

        assert result["update_available"] is False
        assert result["message"] is None

    def test_no_installed_version(self) -> None:
        """Handles missing installation gracefully."""
        with (
            patch.object(version_check, "get_installed_version", return_value=None),
            patch.object(version_check, "get_latest_version", return_value="0.20.0"),
        ):
            result = version_check.check_update()

        assert result["update_available"] is False

    def test_no_latest_version(self) -> None:
        """Handles PyPI unreachable gracefully."""
        with (
            patch.object(version_check, "get_installed_version", return_value="0.20.0"),
            patch.object(version_check, "get_latest_version", return_value=None),
        ):
            result = version_check.check_update()

        assert result["update_available"] is False

    def test_version_parse_failure_returns_false(self) -> None:
        """Version parsing failure does not falsely report update."""
        with (
            patch.object(version_check, "get_installed_version", return_value="invalid"),
            patch.object(version_check, "get_latest_version", return_value="also-invalid"),
        ):
            result = version_check.check_update()

        assert result["update_available"] is False

    @pytest.mark.parametrize(
        ("current", "latest"),
        (
            ("0.51.14rc1.dev1", "0.51.14b2"),
            ("0.51.14.post1.dev1", "0.51.14.post0"),
        ),
    )
    def test_composed_dev_versions_fail_closed(self, current: str, latest: str) -> None:
        with (
            patch.object(version_check, "get_installed_version", return_value=current),
            patch.object(version_check, "get_latest_version", return_value=latest),
        ):
            result = version_check.check_update()

        assert result["update_available"] is False
        assert result["message"] is None


class TestPrerelease:
    """Test pre-release version handling."""

    def test_is_prerelease_beta(self) -> None:
        assert version_check._is_prerelease("0.26.0b4") is True

    def test_is_prerelease_alpha(self) -> None:
        assert version_check._is_prerelease("0.26.0a1") is True

    def test_is_prerelease_rc(self) -> None:
        assert version_check._is_prerelease("0.26.0rc1") is True

    def test_is_prerelease_dev(self) -> None:
        assert version_check._is_prerelease("0.26.0.dev3") is True

    def test_is_not_prerelease_stable(self) -> None:
        assert version_check._is_prerelease("0.26.0") is False

    def test_is_not_prerelease_stable_three_part(self) -> None:
        assert version_check._is_prerelease("1.0.0") is False

    def test_beta_user_gets_prerelease_scan(self, tmp_path: Path) -> None:
        """Beta user triggers include_prerelease=True, scans all releases."""
        cache_file = tmp_path / "nonexistent-cache.json"

        pypi_data = {
            "info": {"version": "0.25.0"},  # stable latest
            "releases": {
                "0.25.0": [{"filename": "x"}],
                "0.26.0b3": [{"filename": "x"}],
                "0.26.0b4": [{"filename": "x"}],
            },
        }
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(pypi_data).encode()

        with (
            patch.object(version_check, "_CACHE_FILE", cache_file),
            patch.object(version_check, "_CACHE_DIR", tmp_path),
            patch("urllib.request.urlopen", return_value=mock_response),
        ):
            result = version_check.get_latest_version(current="0.26.0b3")

        assert result == "0.26.0b4"

    def test_prerelease_scan_without_packaging(self, tmp_path: Path) -> None:
        pypi_data = {
            "info": {"version": "0.51.13"},
            "releases": {
                "invalid": [{"filename": "ignored"}],
                "0.51.14.dev2": [{"filename": "x"}],
                "0.51.14b1": [{"filename": "x"}],
                "0.51.14rc1": [{"filename": "x"}],
            },
        }
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(pypi_data).encode()

        with (
            patch("builtins.__import__", side_effect=_block_packaging_import),
            patch.object(version_check, "_CACHE_FILE", tmp_path / "cache.json"),
            patch.object(version_check, "_CACHE_DIR", tmp_path),
            patch("urllib.request.urlopen", return_value=mock_response),
        ):
            result = version_check.get_latest_version(current="0.51.14b1")

        assert result == "0.51.14rc1"

    def test_stable_user_gets_stable_only(self, tmp_path: Path) -> None:
        """Stable user does NOT see beta releases."""
        cache_file = tmp_path / "nonexistent-cache.json"

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"info": {"version": "0.25.0"}}).encode()

        with (
            patch.object(version_check, "_CACHE_FILE", cache_file),
            patch.object(version_check, "_CACHE_DIR", tmp_path),
            patch("urllib.request.urlopen", return_value=mock_response),
        ):
            result = version_check.get_latest_version(current="0.25.0")

        assert result == "0.25.0"

    def test_beta_to_stable_upgrade_detected(self) -> None:
        """Beta user is offered stable upgrade (0.26.0b4 → 0.26.0)."""
        with (
            patch.object(version_check, "get_installed_version", return_value="0.26.0b4"),
            patch.object(version_check, "get_latest_version", return_value="0.26.0"),
        ):
            result = version_check.check_update()

        assert result["update_available"] is True
        assert result["latest"] == "0.26.0"

    def test_stable_user_not_offered_beta(self) -> None:
        """Stable user is NOT offered beta upgrade."""
        with (
            patch.object(version_check, "get_installed_version", return_value="0.26.0"),
            patch.object(version_check, "get_latest_version", return_value="0.26.0"),
        ):
            result = version_check.check_update()

        assert result["update_available"] is False


class TestKeywordDetector:
    """Test that ooo update keyword is registered."""

    def test_ooo_update_detected(self) -> None:
        """keyword-detector recognizes 'ooo update'."""
        detector_path = (
            Path(__file__).parent.parent.parent.parent / "scripts" / "keyword-detector.py"
        )
        spec = importlib.util.spec_from_file_location("keyword_detector", str(detector_path))
        detector = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(detector)

        result = detector.detect_keywords("ooo update")
        assert result["detected"] is True
        assert result["suggested_skill"] == "/ouroboros:update"

    def test_ooo_upgrade_detected(self) -> None:
        """keyword-detector recognizes 'ooo upgrade'."""
        detector_path = (
            Path(__file__).parent.parent.parent.parent / "scripts" / "keyword-detector.py"
        )
        spec = importlib.util.spec_from_file_location("keyword_detector", str(detector_path))
        detector = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(detector)

        result = detector.detect_keywords("ooo upgrade")
        assert result["detected"] is True
        assert result["suggested_skill"] == "/ouroboros:update"


_HOOKS_PATH = _SCRIPT_PATH.parent.parent / "hooks" / "hooks.json"
_TIME_KEYS = ("_checked_at", "_failed_at", "_notified_at", "timestamp")


def _reference_get_latest_version(*, current: str | None = None) -> str | None:
    """``get_latest_version`` as it was before the failure backoff (#2928 E24).

    Copied from ``main`` at 592c667f with comments dropped and module globals read
    through ``vc``, to compare the new implementation against.
    """
    vc = version_check
    include_pre = False
    if current:
        include_pre = vc._is_prerelease(current)
    cache_key = "latest_version_pre" if include_pre else "latest_version"
    try:
        if vc._CACHE_FILE.exists():
            cache = json.loads(vc._CACHE_FILE.read_text())
            stamp = cache.get(f"{cache_key}_checked_at")
            if stamp is None:
                stamped_keys = ("latest_version", "latest_version_pre")
                if any(cache.get(f"{key}_checked_at") is not None for key in stamped_keys):
                    stamp = 0
                else:
                    stamp = cache.get("timestamp", 0)
            age = time.time() - stamp
            if 0 <= age < vc._CACHE_TTL:
                cached = cache.get(cache_key)
                if cached:
                    return cached
    except Exception:
        pass
    try:
        latest = vc._get_latest_from_pypi(include_prerelease=include_pre)
        try:
            vc._CACHE_DIR.mkdir(parents=True, exist_ok=True)
            with vc._cache_write_lock():
                existing_cache: dict = {}
                try:
                    if vc._CACHE_FILE.exists():
                        loaded_cache = json.loads(vc._CACHE_FILE.read_text())
                        if isinstance(loaded_cache, dict):
                            existing_cache = loaded_cache
                except Exception:
                    pass
                now = time.time()
                existing_cache[cache_key] = latest
                existing_cache[f"{cache_key}_checked_at"] = now
                existing_cache["timestamp"] = now
                cache_content = json.dumps(existing_cache)
                fd, tmp_path = tempfile.mkstemp(dir=vc._CACHE_DIR, suffix=".tmp")
                try:
                    with open(fd, "w") as f:
                        f.write(cache_content)
                    Path(tmp_path).replace(vc._CACHE_FILE)
                except Exception:
                    Path(tmp_path).unlink(missing_ok=True)
                    raise
        except Exception:
            print("ouroboros: failed to write version cache", file=sys.stderr)
        return latest
    except Exception:
        return None


def _pypi_response(payload: dict) -> MagicMock:
    response = MagicMock()
    response.read.return_value = json.dumps(payload).encode()
    return response


def _normalized_cache(cache_file: Path) -> object:
    """The persisted cache with time stamps reduced to their presence."""
    if not cache_file.exists():
        return None
    try:
        payload = json.loads(cache_file.read_text())
    except ValueError:
        return cache_file.read_text()
    if not isinstance(payload, dict):
        return payload
    return {
        key: "<time>" if key.endswith(_TIME_KEYS) and isinstance(value, float) else value
        for key, value in payload.items()
    }


def _run(implementation, tmp_path: Path, initial: str | None, current: str | None, urlopen):
    cache_dir = tmp_path / ".ouroboros"
    cache_dir.mkdir(parents=True)
    cache_file = cache_dir / "version-check-cache.json"
    if initial is not None:
        cache_file.write_text(initial)
    with (
        patch.object(version_check, "_CACHE_FILE", cache_file),
        patch.object(version_check, "_CACHE_DIR", cache_dir),
        patch("urllib.request.urlopen", urlopen),
    ):
        result = implementation(current=current)
    return result, urlopen.call_count, _normalized_cache(cache_file)


_NOW = time.time()
_STABLE = {"info": {"version": "2.0.0"}}
_WITH_PRERELEASES = {
    "info": {"version": "2.0.0"},
    "releases": {"2.0.0": [{}], "2.1.0b2": [{}], "2.1.0b3": []},
}
# (initial cache text, installed version, PyPI payload) for fetches that succeed
# or are answered from the cache. A recent failure stamp appears only next to a
# fresh value, where it is never consulted: beside a stale value it is the new
# behavior, covered by TestFetchFailureBackoff.
_SUCCESS_CASES = {
    "no cache": (None, "1.0.0", _STABLE),
    "fresh shared timestamp": (
        json.dumps({"latest_version": "1.2.3", "timestamp": _NOW}),
        None,
        _STABLE,
    ),
    "fresh channel stamp": (
        json.dumps(
            {"latest_version": "1.2.3", "latest_version_checked_at": _NOW, "timestamp": _NOW}
        ),
        "1.0.0",
        _STABLE,
    ),
    "expired": (
        json.dumps({"latest_version": "0.1.0", "timestamp": _NOW - 100000}),
        "1.0.0",
        _STABLE,
    ),
    "future stamp": (
        json.dumps({"latest_version": "9.9.9", "latest_version_checked_at": _NOW + 10**9}),
        "1.0.0",
        _STABLE,
    ),
    "prerelease channel unstamped": (
        json.dumps(
            {
                "latest_version": "2.0.0",
                "latest_version_checked_at": _NOW,
                "latest_version_pre": "2.0.0b1",
            }
        ),
        "2.0.0b1",
        _WITH_PRERELEASES,
    ),
    "fresh value without content": (
        json.dumps({"latest_version": None, "latest_version_checked_at": _NOW}),
        "1.0.0",
        _STABLE,
    ),
    "list json": ("[]", "1.0.0", _STABLE),
    "corrupt": ("{not json", "1.0.0", _STABLE),
    "stale failure stamp kept": (
        json.dumps(
            {
                "latest_version": "0.1.0",
                "latest_version_checked_at": _NOW - 100000,
                "latest_version_failed_at": _NOW - 7200,
                "latest_version_notified_at": _NOW - 100000,
            }
        ),
        "1.0.0",
        _STABLE,
    ),
    "fresh value beats a recent failure stamp": (
        json.dumps(
            {
                "latest_version": "1.2.3",
                "latest_version_checked_at": _NOW,
                "latest_version_failed_at": _NOW,
            }
        ),
        "1.0.0",
        _STABLE,
    ),
    "malformed channel stamp": (
        json.dumps({"latest_version": "1.2.3", "latest_version_checked_at": "invalid"}),
        "1.0.0",
        _STABLE,
    ),
    "oversized channel stamp": (
        json.dumps({"latest_version": "1.2.3", "latest_version_checked_at": 10**400}),
        "1.0.0",
        _STABLE,
    ),
    "malformed shared timestamp": (
        json.dumps({"latest_version": "1.2.3", "timestamp": [1]}),
        "1.0.0",
        _STABLE,
    ),
}

# Every other key get_latest_version reads, with values that make the freshness
# calculation raise or come out stale. Beside a recent, valid failure stamp none
# of them may cause a fetch (#2948 review round 1).
_MALFORMED_COMPANIONS = [
    ("1.0.0", "latest_version_checked_at", "invalid"),
    ("1.0.0", "latest_version_checked_at", [1]),
    ("1.0.0", "latest_version_checked_at", {"at": 1}),
    ("1.0.0", "latest_version_checked_at", True),
    ("1.0.0", "latest_version_checked_at", float("nan")),
    ("1.0.0", "latest_version_checked_at", 10**400),
    ("1.0.0", "latest_version_pre_checked_at", "invalid"),
    ("1.0.0", "timestamp", "invalid"),
    ("1.0.0", "timestamp", 10**400),
    ("1.0.0", "latest_version", ["2.0.0"]),
    ("2.1.0b1", "latest_version_pre_checked_at", "invalid"),
    ("2.1.0b1", "latest_version_pre_checked_at", 10**400),
    ("2.1.0b1", "latest_version_checked_at", "invalid"),
    ("2.1.0b1", "timestamp", [1]),
]


class TestFetchFailureBackoffEquivalence:
    """Every outcome but a failed fetch is identical to the previous implementation."""

    @pytest.mark.parametrize("case", sorted(_SUCCESS_CASES))
    def test_success_and_cache_paths_match_previous_implementation(
        self, case: str, tmp_path: Path
    ) -> None:
        initial, current, payload = _SUCCESS_CASES[case]
        old = _run(
            _reference_get_latest_version,
            tmp_path / "old",
            initial,
            current,
            MagicMock(return_value=_pypi_response(payload)),
        )
        new = _run(
            version_check.get_latest_version,
            tmp_path / "new",
            initial,
            current,
            MagicMock(return_value=_pypi_response(payload)),
        )
        assert new == old

    def test_missing_ssl_bundle_matches_previous_implementation(self, tmp_path: Path) -> None:
        with patch("ssl.create_default_context", side_effect=OSError("no bundle")):
            old = _run(_reference_get_latest_version, tmp_path / "old", None, "1.0.0", MagicMock())
            new = _run(
                version_check.get_latest_version, tmp_path / "new", None, "1.0.0", MagicMock()
            )
        assert (
            new
            == old
            == (
                None,
                0,
                {
                    "latest_version": None,
                    **dict.fromkeys(("latest_version_checked_at", "timestamp"), "<time>"),
                },
            )
        )

    @pytest.mark.parametrize(
        ("initial", "current", "failed_key"),
        [
            (None, "1.0.0", "latest_version_failed_at"),
            (None, "1.1.0rc1", "latest_version_pre_failed_at"),
            (
                json.dumps({"latest_version": "0.1.0", "timestamp": _NOW - 100000, "other": 1}),
                "1.0.0",
                "latest_version_failed_at",
            ),
        ],
        ids=["no cache", "no cache prerelease", "expired cache kept"],
    )
    def test_failed_fetch_differs_only_by_the_channel_stamp(
        self, initial: str | None, current: str, failed_key: str, tmp_path: Path
    ) -> None:
        old = _run(
            _reference_get_latest_version,
            tmp_path / "old",
            initial,
            current,
            MagicMock(side_effect=TimeoutError),
        )
        new = _run(
            version_check.get_latest_version,
            tmp_path / "new",
            initial,
            current,
            MagicMock(side_effect=TimeoutError),
        )
        assert new[:2] == old[:2] == (None, 1)
        assert new[2] == {**(old[2] or {}), failed_key: "<time>"}

    def test_base_exception_still_propagates_without_a_stamp(self, tmp_path: Path) -> None:
        """Only ``Exception`` was ever turned into None; the worker thread keeps that."""
        for implementation, name in (
            (_reference_get_latest_version, "old"),
            (version_check.get_latest_version, "new"),
        ):
            with pytest.raises(SystemExit):
                _run(
                    implementation,
                    tmp_path / name,
                    None,
                    "1.0.0",
                    MagicMock(side_effect=SystemExit),
                )
            assert not (tmp_path / name / ".ouroboros" / "version-check-cache.json").exists()


class TestFetchFailureBackoff:
    """A failed fetch is stamped, and the stamp spares later sessions the wait."""

    @staticmethod
    def _paths(tmp_path: Path):
        cache_file = tmp_path / "version-check-cache.json"
        return (
            patch.object(version_check, "_CACHE_FILE", cache_file),
            patch.object(version_check, "_CACHE_DIR", tmp_path),
            cache_file,
        )

    def test_failure_writes_only_the_channel_stamp(self, tmp_path: Path) -> None:
        file_patch, dir_patch, cache_file = self._paths(tmp_path)
        before = time.time()
        with file_patch, dir_patch, patch("urllib.request.urlopen", side_effect=OSError("down")):
            assert version_check.get_latest_version(current="1.0.0") is None
        payload = json.loads(cache_file.read_text())
        assert list(payload) == ["latest_version_failed_at"]
        assert before <= payload["latest_version_failed_at"] <= time.time()

    def test_second_call_inside_the_window_does_not_call_urlopen(self, tmp_path: Path) -> None:
        file_patch, dir_patch, _cache_file = self._paths(tmp_path)
        urlopen = MagicMock(side_effect=TimeoutError)
        with file_patch, dir_patch, patch("urllib.request.urlopen", urlopen):
            assert version_check.get_latest_version(current="1.0.0") is None
            assert version_check.get_latest_version(current="1.0.0") is None
            assert version_check.check_update()["latest"] is None
        assert urlopen.call_count == 1

    @pytest.mark.parametrize(
        ("age", "fetches"),
        [
            (0, 0),
            (version_check._FAILURE_BACKOFF - 60, 0),
            (version_check._FAILURE_BACKOFF + 1, 1),
            (-60, 1),  # a future stamp (clock rollback) is not trusted
        ],
    )
    def test_window_bounds(self, age: float, fetches: int, tmp_path: Path) -> None:
        file_patch, dir_patch, cache_file = self._paths(tmp_path)
        cache_file.write_text(json.dumps({"latest_version_failed_at": time.time() - age}))
        urlopen = MagicMock(return_value=_pypi_response(_STABLE))
        with file_patch, dir_patch, patch("urllib.request.urlopen", urlopen):
            result = version_check.get_latest_version(current="1.0.0")
        assert urlopen.call_count == fetches
        assert result == ("2.0.0" if fetches else None)

    def test_after_the_window_a_success_caches_as_before(self, tmp_path: Path) -> None:
        file_patch, dir_patch, cache_file = self._paths(tmp_path)
        stale = time.time() - version_check._FAILURE_BACKOFF - 1
        cache_file.write_text(json.dumps({"latest_version_failed_at": stale}))
        with (
            file_patch,
            dir_patch,
            patch("urllib.request.urlopen", return_value=_pypi_response(_STABLE)),
        ):
            assert version_check.get_latest_version(current="1.0.0") == "2.0.0"
        payload = json.loads(cache_file.read_text())
        assert payload["latest_version"] == "2.0.0"
        assert payload["latest_version_failed_at"] == stale
        assert set(payload) == {
            "latest_version",
            "latest_version_checked_at",
            "latest_version_failed_at",
            "timestamp",
        }
        with (
            file_patch,
            dir_patch,
            patch("urllib.request.urlopen", side_effect=AssertionError("cached")),
        ):
            assert version_check.get_latest_version(current="1.0.0") == "2.0.0"

    @pytest.mark.parametrize(
        "stamp",
        ["recent", None, True, False, [1], float("nan"), float("inf"), 10**400, -(10**400)],
        ids=["str", "null", "true", "false", "list", "nan", "inf", "huge", "-huge"],
    )
    @pytest.mark.parametrize(
        "companion",
        [{}, {"latest_version_checked_at": "invalid"}],
        ids=["alone", "beside malformed freshness"],
    )
    def test_malformed_stamp_does_not_skip_the_fetch(
        self, stamp: object, companion: dict, tmp_path: Path
    ) -> None:
        file_patch, dir_patch, cache_file = self._paths(tmp_path)
        cache_file.write_text(json.dumps({**companion, "latest_version_failed_at": stamp}))
        urlopen = MagicMock(return_value=_pypi_response(_STABLE))
        with file_patch, dir_patch, patch("urllib.request.urlopen", urlopen):
            assert version_check.get_latest_version(current="1.0.0") == "2.0.0"
        assert urlopen.call_count == 1

    @pytest.mark.parametrize(
        ("current", "key", "value"),
        _MALFORMED_COMPANIONS,
        ids=[
            f"{current}-{key}-{type(value).__name__}"
            for current, key, value in _MALFORMED_COMPANIONS
        ],
    )
    def test_recent_stamp_skips_pypi_whatever_the_other_keys_hold(
        self, current: str, key: str, value: object, tmp_path: Path
    ) -> None:
        file_patch, dir_patch, cache_file = self._paths(tmp_path)
        channel = (
            "latest_version_pre" if version_check._is_prerelease(current) else "latest_version"
        )
        cache_file.write_text(json.dumps({key: value, f"{channel}_failed_at": time.time()}))
        urlopen = MagicMock(return_value=_pypi_response(_WITH_PRERELEASES))
        with file_patch, dir_patch, patch("urllib.request.urlopen", urlopen):
            assert version_check.get_latest_version(current=current) is None
        assert urlopen.call_count == 0

    def test_stamp_is_per_channel(self, tmp_path: Path) -> None:
        file_patch, dir_patch, cache_file = self._paths(tmp_path)
        cache_file.write_text(json.dumps({"latest_version_failed_at": time.time()}))
        urlopen = MagicMock(return_value=_pypi_response(_WITH_PRERELEASES))
        with file_patch, dir_patch, patch("urllib.request.urlopen", urlopen):
            assert version_check.get_latest_version(current="2.1.0b1") == "2.1.0b2"
            assert version_check.get_latest_version(current="2.0.0") is None
        assert urlopen.call_count == 1

    def test_failure_stamp_keeps_other_entries_and_replaces_malformed_caches(
        self, tmp_path: Path
    ) -> None:
        file_patch, dir_patch, cache_file = self._paths(tmp_path)
        others = {
            "latest_version_pre": "2.1.0b2",
            "latest_version_pre_checked_at": 1.0,
            "latest_version_notified_at": 2.0,
            "timestamp": 3.0,
        }
        for initial, expected_others in (
            (json.dumps(others), others),
            ("{not json", {}),
            ("[]", {}),
        ):
            cache_file.write_text(initial)
            with file_patch, dir_patch, patch("urllib.request.urlopen", side_effect=TimeoutError):
                assert version_check.get_latest_version(current="1.0.0") is None
            payload = json.loads(cache_file.read_text())
            assert payload.pop("latest_version_failed_at") > 0
            assert payload == expected_others

    def test_unwritable_stamp_is_reported_not_raised(self, tmp_path: Path, capsys) -> None:
        file_patch, dir_patch, cache_file = self._paths(tmp_path)
        with (
            file_patch,
            dir_patch,
            patch("urllib.request.urlopen", side_effect=TimeoutError),
            patch.object(version_check.tempfile, "mkstemp", side_effect=OSError("read-only")),
        ):
            assert version_check.get_latest_version(current="1.0.0") is None
        assert "ouroboros: failed to write version cache" in capsys.readouterr().err
        assert not cache_file.exists()
        assert list(tmp_path.glob("*.tmp")) == []

    def test_failure_stamp_is_written_under_the_shared_lock(self, tmp_path: Path) -> None:
        """A concurrent MCP channel refresh and a hook failure both survive."""
        file_patch, dir_patch, cache_file = self._paths(tmp_path)
        barrier = threading.Barrier(2)

        def _hook_fetch(*, include_prerelease: bool = False) -> str:
            barrier.wait()
            raise TimeoutError

        def _mcp_fetch(*, include_prerelease: bool) -> str:
            barrier.wait()
            return "2.1.0b2"

        with (
            file_patch,
            dir_patch,
            patch.object(version_check, "_get_latest_from_pypi", _hook_fetch),
            patch.object(update_notice, "_CACHE_FILE", cache_file),
            patch.object(update_notice.metadata, "version", return_value="2.1.0b1"),
            patch.object(update_notice, "_fetch_latest_from_pypi", _mcp_fetch),
        ):
            hook = threading.Thread(target=version_check.get_latest_version)
            mcp = threading.Thread(target=update_notice._refresh_cache_now)
            hook.start()
            mcp.start()
            hook.join(timeout=5)
            mcp.join(timeout=5)

        assert not hook.is_alive()
        assert not mcp.is_alive()
        payload = json.loads(cache_file.read_text())
        assert payload["latest_version_pre"] == "2.1.0b2"
        assert "latest_version_pre_checked_at" in payload
        assert "latest_version_failed_at" in payload

    def test_mcp_reader_ignores_the_failure_stamp(self, tmp_path: Path) -> None:
        cache_file = tmp_path / "version-check-cache.json"
        fresh = {"latest_version": "999.0.0", "latest_version_checked_at": time.time()}
        notices = []
        for payload in (
            fresh,
            {**fresh, "latest_version_failed_at": time.time()},
            {},
            {"latest_version_failed_at": time.time()},
        ):
            cache_file.write_text(json.dumps(payload))
            with (
                patch.object(update_notice, "_CACHE_FILE", cache_file),
                patch.object(update_notice.metadata, "version", return_value="1.0.0"),
            ):
                notices.append(update_notice.cached_update_notice())
        assert notices[0] is not None
        assert notices[1] == notices[0]
        assert notices[2] is None
        assert notices[3] is None


class TestFetchDeadline:
    """The fetch is bounded in wall-clock time, below the hook's host timeout."""

    def test_deadline_is_well_below_the_session_start_host_timeout(self) -> None:
        manifest = json.loads(_HOOKS_PATH.read_text(encoding="utf-8"))
        (entry,) = manifest["hooks"]["SessionStart"]
        (hook,) = entry["hooks"]
        host_timeout = hook["timeout"]
        assert host_timeout >= 2 * version_check._FETCH_DEADLINE

    def test_urlopen_gets_the_deadline_as_its_socket_timeout(self) -> None:
        urlopen = MagicMock(return_value=_pypi_response(_STABLE))
        with patch("urllib.request.urlopen", urlopen):
            assert version_check._get_latest_from_pypi() == "2.0.0"
        assert urlopen.call_args.kwargs["timeout"] == version_check._FETCH_DEADLINE

    def test_a_fetch_past_the_deadline_is_abandoned_and_stamped(self, tmp_path: Path) -> None:
        cache_file = tmp_path / "version-check-cache.json"
        started = threading.Event()
        release = threading.Event()
        calls = []

        def _hanging_fetch(*, include_prerelease: bool = False) -> str:
            calls.append(threading.current_thread())
            started.set()
            release.wait(timeout=30)
            return "2.0.0"

        try:
            with (
                patch.object(version_check, "_CACHE_FILE", cache_file),
                patch.object(version_check, "_CACHE_DIR", tmp_path),
                patch.object(version_check, "_FETCH_DEADLINE", 0.05),
                patch.object(version_check, "_get_latest_from_pypi", _hanging_fetch),
            ):
                assert version_check.get_latest_version(current="1.0.0") is None
        finally:
            release.set()
        assert started.wait(timeout=5)
        (worker,) = calls
        assert worker is not threading.main_thread()
        assert worker.daemon
        worker.join(timeout=5)
        assert not worker.is_alive()
        # The abandoned worker only fetched: its late answer never reaches the cache.
        assert list(json.loads(cache_file.read_text())) == ["latest_version_failed_at"]

    @pytest.mark.parametrize(
        "error", [TimeoutError("slow"), ValueError("bad json"), KeyError("info")]
    )
    def test_worker_errors_reach_the_caller_unchanged(self, error: Exception) -> None:
        with (
            patch.object(version_check, "_get_latest_from_pypi", side_effect=error),
            pytest.raises(type(error)) as raised,
        ):
            version_check._get_latest_within_deadline(include_prerelease=False)
        assert raised.value is error
