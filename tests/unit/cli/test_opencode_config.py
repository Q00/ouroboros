"""Unit tests for the shared opencode_config helper.

All tests patch ``opencode_config_dir`` directly so they are
platform-agnostic — no reliance on Linux-specific XDG paths.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ouroboros.cli.opencode_config import find_opencode_config, opencode_config_dir

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

    def test_honors_opencode_config_file_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OPENCODE_CONFIG points setup at an explicit config file."""
        explicit = tmp_path / "custom" / "opencode.json"
        monkeypatch.setenv("OPENCODE_CONFIG", str(explicit))

        result = find_opencode_config(allow_default=True)

        assert result == explicit

    def test_opencode_config_file_env_requires_existing_when_no_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Uninstall-style lookup ignores a missing OPENCODE_CONFIG file."""
        explicit = tmp_path / "missing.json"
        monkeypatch.setenv("OPENCODE_CONFIG", str(explicit))

        result = find_opencode_config(allow_default=False)

        assert result is None


class TestOpencodeConfigDir:
    """Tests for active OpenCode config-directory resolution."""

    def test_honors_opencode_config_dir_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OPENCODE_CONFIG_DIR is the strongest explicit directory override."""
        custom = tmp_path / "custom-opencode"
        monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(custom))

        result = opencode_config_dir()

        assert result == custom

    def test_uses_debug_paths_config_when_available(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The OpenCode CLI-reported config dir is authoritative."""
        reported = tmp_path / ".config" / "opencode"
        monkeypatch.delenv("OPENCODE_CONFIG_DIR", raising=False)
        monkeypatch.setenv("OUROBOROS_OPENCODE_CLI_PATH", "/bin/opencode")
        monkeypatch.delenv("OPENCODE_CLI_PATH", raising=False)

        completed = SimpleNamespace(
            returncode=0,
            stdout=f"home       {tmp_path}\nconfig     {reported}\nstate      {tmp_path / '.state'}\n",
        )
        with patch("ouroboros.cli.opencode_config.subprocess.run", return_value=completed) as run:
            result = opencode_config_dir()

        assert result == reported
        run.assert_called_once()
        assert run.call_args.args[0][:3] == ["/bin/opencode", "debug", "paths"]

    def test_uses_persisted_opencode_cli_path_for_debug_paths(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Post-setup cleanup/uninstall use the configured OpenCode binary."""
        reported = tmp_path / "active" / "opencode"
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text(
            "orchestrator:\n  opencode_cli_path: /configured/bin/opencode\n",
            encoding="utf-8",
        )
        monkeypatch.delenv("OPENCODE_CONFIG_DIR", raising=False)
        monkeypatch.delenv("OUROBOROS_OPENCODE_CLI_PATH", raising=False)
        monkeypatch.delenv("OPENCODE_CLI_PATH", raising=False)

        completed = SimpleNamespace(returncode=0, stdout=f"config     {reported}\n")
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.cli.opencode_config.subprocess.run", return_value=completed) as run,
        ):
            result = opencode_config_dir()

        assert result == reported
        assert run.call_args.args[0][:3] == ["/configured/bin/opencode", "debug", "paths"]

    def test_does_not_query_path_opencode_without_configured_cli(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Avoid targeting a different OpenCode install from PATH after setup."""
        monkeypatch.delenv("OPENCODE_CONFIG_DIR", raising=False)
        monkeypatch.delenv("OUROBOROS_OPENCODE_CLI_PATH", raising=False)
        monkeypatch.delenv("OPENCODE_CLI_PATH", raising=False)
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.cli.opencode_config.subprocess.run") as run,
        ):
            result = opencode_config_dir()

        assert result == tmp_path / ".config" / "opencode"
        run.assert_not_called()

    def test_darwin_defaults_to_xdg_config_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Modern OpenCode uses XDG config on macOS, not Application Support."""
        monkeypatch.delenv("OPENCODE_CONFIG_DIR", raising=False)
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

        with (
            patch("ouroboros.cli.opencode_config.sys.platform", "darwin"),
            patch("pathlib.Path.home", return_value=tmp_path),
        ):
            result = opencode_config_dir()

        assert result == tmp_path / ".config" / "opencode"

    def test_darwin_honors_xdg_config_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """XDG_CONFIG_HOME applies on macOS as it does on Linux."""
        xdg = tmp_path / "xdg"
        monkeypatch.delenv("OPENCODE_CONFIG_DIR", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))

        with (
            patch("ouroboros.cli.opencode_config.sys.platform", "darwin"),
        ):
            result = opencode_config_dir()

        assert result == xdg / "opencode"
