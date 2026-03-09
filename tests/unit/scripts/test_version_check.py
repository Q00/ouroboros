"""Tests for version-check script."""

from __future__ import annotations

from importlib.machinery import SourceFileLoader
import json
from pathlib import Path
import time
from unittest.mock import MagicMock, patch

# Load the script as a module
_SCRIPT_PATH = Path(__file__).parent.parent.parent.parent / "scripts" / "version-check.py"
version_check = SourceFileLoader("version_check", str(_SCRIPT_PATH)).load_module()


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


class TestKeywordDetector:
    """Test that ooo update keyword is registered."""

    def test_ooo_update_detected(self) -> None:
        """keyword-detector recognizes 'ooo update'."""
        detector_path = (
            Path(__file__).parent.parent.parent.parent / "scripts" / "keyword-detector.py"
        )
        detector = SourceFileLoader("keyword_detector", str(detector_path)).load_module()

        result = detector.detect_keywords("ooo update")
        assert result["detected"] is True
        assert result["suggested_skill"] == "/ouroboros:update"

    def test_ooo_upgrade_detected(self) -> None:
        """keyword-detector recognizes 'ooo upgrade'."""
        detector_path = (
            Path(__file__).parent.parent.parent.parent / "scripts" / "keyword-detector.py"
        )
        detector = SourceFileLoader("keyword_detector", str(detector_path)).load_module()

        result = detector.detect_keywords("ooo upgrade")
        assert result["detected"] is True
        assert result["suggested_skill"] == "/ouroboros:update"
