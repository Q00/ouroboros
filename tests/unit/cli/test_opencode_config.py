"""Unit tests for the shared opencode_config helper.

All tests patch ``opencode_config_dir`` directly so they are
platform-agnostic — no reliance on Linux-specific XDG paths.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from ouroboros.cli.opencode_config import find_opencode_config

_OCD = "ouroboros.cli.opencode_config.opencode_config_dir"


class TestFindOpencodeConfig:
    """Tests for find_opencode_config()."""

    def test_prefers_jsonc_over_json(self, tmp_path: Path) -> None:
        """opencode.jsonc takes priority over opencode.json when both exist."""
        config_dir = tmp_path / "opencode"
        config_dir.mkdir()
        jsonc = config_dir / "opencode.jsonc"
        json_ = config_dir / "opencode.json"
        jsonc.write_text("{}")
        json_.write_text("{}")

        with patch(_OCD, return_value=config_dir):
            result = find_opencode_config(allow_default=True)

        assert result == jsonc

    def test_falls_back_to_json_when_no_jsonc(self, tmp_path: Path) -> None:
        """Returns opencode.json when only that file exists."""
        config_dir = tmp_path / "opencode"
        config_dir.mkdir()
        json_ = config_dir / "opencode.json"
        json_.write_text("{}")

        with patch(_OCD, return_value=config_dir):
            result = find_opencode_config(allow_default=True)

        assert result == json_

    def test_allow_default_true_returns_default_when_missing(self, tmp_path: Path) -> None:
        """Returns default opencode.json path when no config exists and allow_default=True."""
        config_dir = tmp_path / "opencode"
        config_dir.mkdir()

        with patch(_OCD, return_value=config_dir):
            result = find_opencode_config(allow_default=True)

        assert result == config_dir / "opencode.json"
        assert result is not None

    def test_allow_default_false_returns_none_when_missing(self, tmp_path: Path) -> None:
        """Returns None when no config exists and allow_default=False."""
        config_dir = tmp_path / "opencode"
        config_dir.mkdir()

        with patch(_OCD, return_value=config_dir):
            result = find_opencode_config(allow_default=False)

        assert result is None

    def test_allow_default_false_returns_existing_file(self, tmp_path: Path) -> None:
        """Returns existing file even when allow_default=False."""
        config_dir = tmp_path / "opencode"
        config_dir.mkdir()
        jsonc = config_dir / "opencode.jsonc"
        jsonc.write_text("{}")

        with patch(_OCD, return_value=config_dir):
            result = find_opencode_config(allow_default=False)

        assert result == jsonc

    def test_oserror_on_candidate_is_skipped(self, tmp_path: Path) -> None:
        """OSError on exists() check is silently skipped — fallback continues."""
        config_dir = tmp_path / "opencode"
        config_dir.mkdir()
        json_ = config_dir / "opencode.json"
        json_.write_text("{}")

        original_exists = Path.exists

        def patched_exists(self: Path) -> bool:
            if self.name == "opencode.jsonc":
                raise OSError("permission denied")
            return original_exists(self)

        with (
            patch(_OCD, return_value=config_dir),
            patch.object(Path, "exists", patched_exists),
        ):
            result = find_opencode_config(allow_default=True)

        # jsonc errored, falls through to json which exists
        assert result == json_

    def test_returns_jsonc_path_type(self, tmp_path: Path) -> None:
        """Return value is always a Path instance (not str)."""
        config_dir = tmp_path / "opencode"
        config_dir.mkdir()
        (config_dir / "opencode.jsonc").write_text("{}")

        with patch(_OCD, return_value=config_dir):
            result = find_opencode_config(allow_default=True)

        assert isinstance(result, Path)
