"""Unit tests for bridge plugin install and uninstall lifecycle.

Tests _install_opencode_bridge_plugin (setup.py) and
_remove_opencode_bridge_plugin (uninstall.py).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from ouroboros.cli.commands.setup import _install_opencode_bridge_plugin
from ouroboros.cli.commands.uninstall import _remove_opencode_bridge_plugin

# Patch targets — both namespaces so internal calls through
# find_opencode_config() also resolve to the test directory.
_OCD_SETUP = "ouroboros.cli.commands.setup.opencode_config_dir"
_OCD_UNINSTALL = "ouroboros.cli.commands.uninstall.opencode_config_dir"
_OCD_CONFIG = "ouroboros.cli.opencode_config.opencode_config_dir"

# ── _install_opencode_bridge_plugin ──────────────────────────────


class TestInstallBridgePlugin:
    """Tests for _install_opencode_bridge_plugin in setup.py."""

    def test_creates_plugin_dir_and_writes_file(self, tmp_path: Path) -> None:
        """Plugin dir created, .ts file written from package resource."""
        fake_content = "// ouroboros bridge plugin v2\nexport default {}\n"
        oc_dir = tmp_path / "opencode"

        mock_source = MagicMock()
        mock_source.read_text.return_value = fake_content

        mock_package = MagicMock()
        mock_package.joinpath.return_value = mock_source

        with (
            patch(_OCD_SETUP, return_value=oc_dir),
            patch(_OCD_CONFIG, return_value=oc_dir),
            patch("importlib.resources.files", return_value=mock_package),
        ):
            _install_opencode_bridge_plugin()

        dest = oc_dir / "plugins" / "ouroboros-bridge" / "ouroboros-bridge.ts"
        assert dest.exists()
        assert dest.read_text() == fake_content

    def test_overwrites_existing_file(self, tmp_path: Path) -> None:
        """Existing plugin file overwritten with new version."""
        oc_dir = tmp_path / "opencode"
        plugin_dir = oc_dir / "plugins" / "ouroboros-bridge"
        plugin_dir.mkdir(parents=True)
        dest = plugin_dir / "ouroboros-bridge.ts"
        dest.write_text("// old version")

        new_content = "// new version"
        mock_source = MagicMock()
        mock_source.read_text.return_value = new_content
        mock_package = MagicMock()
        mock_package.joinpath.return_value = mock_source

        with (
            patch(_OCD_SETUP, return_value=oc_dir),
            patch(_OCD_CONFIG, return_value=oc_dir),
            patch("importlib.resources.files", return_value=mock_package),
        ):
            _install_opencode_bridge_plugin()

        assert dest.read_text() == new_content

    def test_fallback_to_dev_source(self, tmp_path: Path) -> None:
        """When importlib.resources fails, fallback to dev tree source."""
        oc_dir = tmp_path / "opencode"
        dev_content = "// dev bridge plugin"
        dev_root = tmp_path / "src" / "ouroboros"
        dev_plugin = dev_root / "opencode" / "plugin" / "ouroboros-bridge.ts"
        dev_plugin.parent.mkdir(parents=True)
        dev_plugin.write_text(dev_content)

        # Fake __file__ so parents[3] resolves to dev_root
        fake_setup_file = dev_root / "cli" / "commands" / "setup.py"
        fake_setup_file.parent.mkdir(parents=True)
        fake_setup_file.write_text("")

        with (
            patch(_OCD_SETUP, return_value=oc_dir),
            patch(_OCD_CONFIG, return_value=oc_dir),
            patch("importlib.resources.files", side_effect=FileNotFoundError),
            patch(
                "ouroboros.cli.commands.setup.__file__",
                str(fake_setup_file),
            ),
        ):
            _install_opencode_bridge_plugin()

        dest = oc_dir / "plugins" / "ouroboros-bridge" / "ouroboros-bridge.ts"
        assert dest.exists()
        assert dest.read_text() == dev_content

    def test_prints_warning_when_no_source(self, tmp_path: Path) -> None:
        """Warning printed when both importlib and dev source fail."""
        oc_dir = tmp_path / "opencode"
        fake_file = tmp_path / "nowhere" / "cli" / "commands" / "setup.py"
        fake_file.parent.mkdir(parents=True)
        fake_file.write_text("")

        with (
            patch(_OCD_SETUP, return_value=oc_dir),
            patch(_OCD_CONFIG, return_value=oc_dir),
            patch("importlib.resources.files", side_effect=ModuleNotFoundError),
            patch("ouroboros.cli.commands.setup.__file__", str(fake_file)),
            patch("ouroboros.cli.commands.setup.print_warning") as mock_warn,
        ):
            _install_opencode_bridge_plugin()

        mock_warn.assert_called_once()
        assert "not found" in mock_warn.call_args[0][0].lower()


# ── _remove_opencode_bridge_plugin ───────────────────────────────


class TestRemoveBridgePlugin:
    """Tests for _remove_opencode_bridge_plugin in uninstall.py."""

    def test_removes_existing_plugin_dir(self, tmp_path: Path) -> None:
        """Plugin dir removed successfully returns True."""
        oc_dir = tmp_path / "opencode"
        plugin_dir = oc_dir / "plugins" / "ouroboros-bridge"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "ouroboros-bridge.ts").write_text("// plugin")

        with (
            patch(_OCD_UNINSTALL, return_value=oc_dir),
            patch(_OCD_CONFIG, return_value=oc_dir),
        ):
            result = _remove_opencode_bridge_plugin(dry_run=False)

        assert result is True
        assert not plugin_dir.exists()

    def test_dry_run_preserves_dir(self, tmp_path: Path) -> None:
        """Dry run returns True but does not delete."""
        oc_dir = tmp_path / "opencode"
        plugin_dir = oc_dir / "plugins" / "ouroboros-bridge"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "ouroboros-bridge.ts").write_text("// plugin")

        with (
            patch(_OCD_UNINSTALL, return_value=oc_dir),
            patch(_OCD_CONFIG, return_value=oc_dir),
        ):
            result = _remove_opencode_bridge_plugin(dry_run=True)

        assert result is True
        assert plugin_dir.exists()

    def test_missing_dir_returns_false(self, tmp_path: Path) -> None:
        """No plugin dir returns False (nothing to remove)."""
        oc_dir = tmp_path / "opencode"

        with (
            patch(_OCD_UNINSTALL, return_value=oc_dir),
            patch(_OCD_CONFIG, return_value=oc_dir),
        ):
            result = _remove_opencode_bridge_plugin(dry_run=False)

        assert result is False

    def test_os_error_returns_false(self, tmp_path: Path) -> None:
        """OSError during rmtree returns False + prints warning."""
        oc_dir = tmp_path / "opencode"
        plugin_dir = oc_dir / "plugins" / "ouroboros-bridge"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "ouroboros-bridge.ts").write_text("// plugin")

        with (
            patch(_OCD_UNINSTALL, return_value=oc_dir),
            patch(_OCD_CONFIG, return_value=oc_dir),
            patch("shutil.rmtree", side_effect=OSError("permission denied")),
            patch("ouroboros.cli.commands.uninstall.print_warning") as mock_warn,
        ):
            result = _remove_opencode_bridge_plugin(dry_run=False)

        assert result is False
        mock_warn.assert_called_once()


# ── Uninstall CLI integration for bridge plugin ──────────────────


class TestUninstallBridgePluginIntegration:
    """Verify bridge plugin appears in uninstall preview and is removed."""

    def test_bridge_plugin_appears_in_targets(self, tmp_path: Path) -> None:
        """When bridge plugin dir exists, it appears in 'Will remove' list."""
        from typer.testing import CliRunner

        from ouroboros.cli.commands.uninstall import app

        # Create bridge plugin dir under the patched opencode config dir.
        oc_dir = tmp_path / "opencode"
        plugin_dir = oc_dir / "plugins" / "ouroboros-bridge"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "ouroboros-bridge.ts").write_text("// plugin")

        runner = CliRunner()
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("pathlib.Path.cwd", return_value=tmp_path),
            patch(_OCD_UNINSTALL, return_value=oc_dir),
            patch(_OCD_CONFIG, return_value=oc_dir),
        ):
            result = runner.invoke(app, ["--dry-run"])

        assert result.exit_code == 0
        assert "bridge plugin" in result.output.lower()

    def test_bridge_plugin_removed_on_uninstall(self, tmp_path: Path) -> None:
        """Bridge plugin dir removed during actual uninstall."""
        from typer.testing import CliRunner

        from ouroboros.cli.commands.uninstall import app

        home_dir = tmp_path / "home"
        home_dir.mkdir()
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        # Create bridge plugin dir under the patched opencode config dir.
        oc_dir = tmp_path / "opencode"
        plugin_dir = oc_dir / "plugins" / "ouroboros-bridge"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "ouroboros-bridge.ts").write_text("// plugin")

        runner = CliRunner()
        with (
            patch("pathlib.Path.home", return_value=home_dir),
            patch("pathlib.Path.cwd", return_value=project_dir),
            patch(_OCD_UNINSTALL, return_value=oc_dir),
            patch(_OCD_CONFIG, return_value=oc_dir),
        ):
            result = runner.invoke(app, ["-y"])

        assert result.exit_code == 0
        assert not plugin_dir.exists()
