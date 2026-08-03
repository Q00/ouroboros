"""Unit tests for the update command."""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from ouroboros.cli.commands.update import (
    PACKAGE_SPEC,
    _compare_versions,
    _detect_installer,
    _fallback_version_key,
    _is_prerelease,
    _latest_pypi_version,
    _manager_env_root,
    _resolve_runtime,
    _upgrade_command,
    app,
)

runner = CliRunner()


def _plain(output: str) -> str:
    """Flatten CLI output for substring assertions.

    Strips ANSI escapes and rich panel borders, and collapses the wrapping
    whitespace that splits long commands and messages across lines.
    """
    text = re.sub(r"\x1b\[[0-9;]*m", "", output)
    text = re.sub(r"[│╭╮╰╯─]", " ", text)
    return re.sub(r"\s+", " ", text)


# ── Version helpers ──────────────────────────────────────────────


class TestCompareVersions:
    """Tests for _compare_versions."""

    def test_orders_stable_releases(self) -> None:
        assert _compare_versions("0.50.7", "0.50.8") == -1
        assert _compare_versions("0.50.8", "0.50.7") == 1
        assert _compare_versions("0.50.7", "0.50.7") == 0

    def test_prerelease_sorts_below_final(self) -> None:
        assert _compare_versions("0.50.8b1", "0.50.8") == -1
        assert _compare_versions("0.50.8rc1", "0.50.8") == -1

    def test_prerelease_kinds_are_ordered(self) -> None:
        assert _compare_versions("0.50.8a2", "0.50.8b1") == -1
        assert _compare_versions("0.50.8b2", "0.50.8rc1") == -1

    def test_dev_sorts_below_final_but_above_previous(self) -> None:
        assert _compare_versions("0.50.8.dev0", "0.50.8") == -1
        assert _compare_versions("0.50.8.dev0", "0.50.7") == 1


class TestFallbackVersionKey:
    """Tests for the packaging-free fallback ordering."""

    def test_orders_project_version_shapes(self) -> None:
        ordered = ["0.50.7", "0.50.8.dev0", "0.50.8a1", "0.50.8b1", "0.50.8rc1", "0.50.8"]
        keys = [_fallback_version_key(v) for v in ordered]
        assert keys == sorted(keys)

    def test_unparseable_sorts_lowest(self) -> None:
        assert _fallback_version_key("garbage") < _fallback_version_key("0.0.1")


class TestIsPrerelease:
    """Tests for _is_prerelease."""

    def test_stable_is_not_prerelease(self) -> None:
        assert _is_prerelease("0.50.7") is False

    def test_beta_and_dev_are_prereleases(self) -> None:
        assert _is_prerelease("0.50.8b1") is True
        assert _is_prerelease("0.50.8.dev0") is True


# ── PyPI query ───────────────────────────────────────────────────


def _urlopen_returning(payload: dict) -> MagicMock:
    response = MagicMock()
    response.read.return_value = json.dumps(payload).encode()
    context_manager = MagicMock()
    context_manager.__enter__.return_value = response
    context_manager.__exit__.return_value = False
    return MagicMock(return_value=context_manager)


class TestLatestPypiVersion:
    """Tests for _latest_pypi_version."""

    PAYLOAD = {
        "info": {"version": "0.50.7"},
        "releases": {
            "0.40.0": [{"filename": "old.whl"}],
            "0.50.7": [{"filename": "stable.whl"}],
            "0.50.8b1": [{"filename": "beta.whl", "yanked": False}],
            "0.60.0": [{"filename": "pulled.whl", "yanked": True}],  # fully yanked
            "0.99.0": [],  # empty release must be ignored
        },
    }

    def test_stable_channel_uses_info_version(self) -> None:
        with patch(
            "ouroboros.cli.commands.update.urllib.request.urlopen",
            _urlopen_returning(self.PAYLOAD),
        ):
            assert _latest_pypi_version(include_prereleases=False) == "0.50.7"

    def test_prerelease_channel_scans_all_releases(self) -> None:
        with patch(
            "ouroboros.cli.commands.update.urllib.request.urlopen",
            _urlopen_returning(self.PAYLOAD),
        ):
            # 0.60.0 is newer but fully yanked — pip/uv would skip it, so we must too.
            assert _latest_pypi_version(include_prereleases=True) == "0.50.8b1"

    def test_network_error_returns_none(self) -> None:
        with patch(
            "ouroboros.cli.commands.update.urllib.request.urlopen",
            MagicMock(side_effect=OSError("offline")),
        ):
            assert _latest_pypi_version(include_prereleases=False) is None


# ── Installer detection and upgrade commands ─────────────────────


class TestManagerEnvRoot:
    """Tests for _manager_env_root."""

    def test_returns_reported_root(self) -> None:
        result = MagicMock(returncode=0, stdout="/home/u/.local/share/uv/tools\n")
        with (
            patch("ouroboros.cli.commands.update.shutil.which", return_value="/usr/bin/uv"),
            patch("ouroboros.cli.commands.update.subprocess.run", return_value=result),
        ):
            root = _manager_env_root(["uv", "tool", "dir"])
        assert root == Path("/home/u/.local/share/uv/tools").resolve()

    def test_missing_binary_returns_none(self) -> None:
        with patch("ouroboros.cli.commands.update.shutil.which", return_value=None):
            assert _manager_env_root(["uv", "tool", "dir"]) is None

    def test_probe_failure_returns_none(self) -> None:
        with (
            patch("ouroboros.cli.commands.update.shutil.which", return_value="/usr/bin/uv"),
            patch(
                "ouroboros.cli.commands.update.subprocess.run",
                side_effect=OSError("cannot exec"),
            ),
        ):
            assert _manager_env_root(["uv", "tool", "dir"]) is None

    def test_relative_output_returns_none(self) -> None:
        result = MagicMock(returncode=0, stdout="not/an/absolute/path\n")
        with (
            patch("ouroboros.cli.commands.update.shutil.which", return_value="/usr/bin/uv"),
            patch("ouroboros.cli.commands.update.subprocess.run", return_value=result),
        ):
            assert _manager_env_root(["uv", "tool", "dir"]) is None


class TestDetectInstaller:
    """Tests for _detect_installer — ownership anchored to the running env.

    Roots are real tmp_path directories: _detect_installer resolves the
    prefix, and fabricated paths (e.g. /home/... on macOS) resolve through
    automount symlinks to something else entirely.
    """

    @staticmethod
    def _roots(tmp_path: Path) -> tuple[Path, Path]:
        uv_root = tmp_path / "uv" / "tools"
        pipx_root = tmp_path / "pipx" / "venvs"
        (uv_root / "ouroboros-ai").mkdir(parents=True)
        (pipx_root / "ouroboros-ai").mkdir(parents=True)
        return uv_root, pipx_root

    def test_uv_owns_the_running_env(self, tmp_path: Path) -> None:
        uv_root, _ = self._roots(tmp_path)
        with patch(
            "ouroboros.cli.commands.update._manager_env_root",
            side_effect=[uv_root],
        ):
            assert _detect_installer(prefix=uv_root / "ouroboros-ai") == "uv"

    def test_pipx_env_wins_over_stale_uv_install(self, tmp_path: Path) -> None:
        # Regression: a stale uv-tool installation elsewhere must not hijack
        # the upgrade target when the running CLI lives in a pipx venv.
        uv_root, pipx_root = self._roots(tmp_path)
        with patch(
            "ouroboros.cli.commands.update._manager_env_root",
            side_effect=[uv_root, pipx_root],
        ):
            assert _detect_installer(prefix=pipx_root / "ouroboros-ai") == "pipx"

    def test_unmanaged_env_falls_back_to_pip(self, tmp_path: Path) -> None:
        uv_root, pipx_root = self._roots(tmp_path)
        with patch(
            "ouroboros.cli.commands.update._manager_env_root",
            side_effect=[uv_root, pipx_root],
        ):
            assert _detect_installer(prefix=tmp_path / "project" / ".venv") == "pip"

    def test_heuristic_fallback_when_managers_report_nothing(self, tmp_path: Path) -> None:
        uv_root, pipx_root = self._roots(tmp_path)
        with patch(
            "ouroboros.cli.commands.update._manager_env_root",
            return_value=None,
        ):
            assert _detect_installer(prefix=pipx_root / "ouroboros-ai") == "pipx"
            assert _detect_installer(prefix=uv_root / "ouroboros-ai") == "uv"
            assert _detect_installer(prefix=tmp_path / "project" / ".venv") == "pip"


class TestUpgradeCommand:
    """Tests for _upgrade_command."""

    def test_uv_stable(self) -> None:
        assert _upgrade_command("uv", prerelease=False) == [
            "uv",
            "tool",
            "install",
            "--upgrade",
            PACKAGE_SPEC,
        ]

    def test_uv_prerelease(self) -> None:
        assert "--prerelease=allow" in _upgrade_command("uv", prerelease=True)

    def test_pipx_reinstalls_with_force(self) -> None:
        command = _upgrade_command("pipx", prerelease=True)
        assert command[:3] == ["pipx", "install", "--force"]
        assert "--pip-args=--pre" in command
        assert command[-1] == PACKAGE_SPEC

    def test_pip_targets_running_interpreter(self) -> None:
        command = _upgrade_command("pip", prerelease=False)
        assert command[0] == sys.executable
        assert command[1:5] == ["-m", "pip", "install", "--upgrade"]
        assert "--pre" in _upgrade_command("pip", prerelease=True)


class TestResolveRuntime:
    """Tests for _resolve_runtime."""

    def test_explicit_runtime_passes_through(self) -> None:
        assert _resolve_runtime("codex") == "codex"
        assert _resolve_runtime("none") == "none"

    def test_auto_prefers_claude(self) -> None:
        with patch(
            "ouroboros.cli.commands.update.shutil.which",
            side_effect=lambda name: "/usr/bin/claude" if name == "claude" else None,
        ):
            assert _resolve_runtime("auto") == "claude"

    def test_auto_falls_back_to_codex(self) -> None:
        with patch(
            "ouroboros.cli.commands.update.shutil.which",
            side_effect=lambda name: "/usr/bin/codex" if name == "codex" else None,
        ):
            assert _resolve_runtime("auto") == "codex"

    def test_auto_with_no_runtime_is_none(self) -> None:
        with patch("ouroboros.cli.commands.update.shutil.which", return_value=None):
            assert _resolve_runtime("auto") == "none"


# ── CLI flows ────────────────────────────────────────────────────


class TestCheckFlow:
    """Tests for `ouroboros update --check`."""

    def test_reports_available_update_without_changing_anything(self) -> None:
        with (
            patch("ouroboros.cli.commands.update.__version__", "0.1.0"),
            patch(
                "ouroboros.cli.commands.update._latest_pypi_version",
                return_value="99.0.0",
            ),
            patch("ouroboros.cli.commands.update.subprocess.run") as run,
        ):
            result = runner.invoke(app, ["--check"])

        assert result.exit_code == 0
        output = _plain(result.output)
        assert "Update available" in output
        assert "v0.1.0" in output
        assert "v99.0.0" in output
        run.assert_not_called()

    def test_up_to_date_exits_cleanly(self) -> None:
        with (
            patch("ouroboros.cli.commands.update.__version__", "0.50.7"),
            patch(
                "ouroboros.cli.commands.update._latest_pypi_version",
                return_value="0.50.7",
            ),
        ):
            result = runner.invoke(app, ["--check"])

        assert result.exit_code == 0
        assert "up to date" in result.output

    def test_unreachable_pypi_exits_nonzero(self) -> None:
        with patch(
            "ouroboros.cli.commands.update._latest_pypi_version",
            return_value=None,
        ):
            result = runner.invoke(app, ["--check"])

        assert result.exit_code == 1
        assert "Could not reach PyPI" in result.output


class TestUpdateFlow:
    """Tests for the full update flow (dry-run — no subprocess execution)."""

    def test_dry_run_previews_all_steps(self) -> None:
        with (
            patch("ouroboros.cli.commands.update.__version__", "0.1.0"),
            patch(
                "ouroboros.cli.commands.update._latest_pypi_version",
                return_value="99.0.0",
            ),
            patch("ouroboros.cli.commands.update._detect_installer", return_value="uv"),
            patch(
                "ouroboros.cli.commands.update.shutil.which",
                return_value="/usr/bin/anything",
            ),
            patch("ouroboros.cli.commands.update.subprocess.run") as run,
        ):
            result = runner.invoke(app, ["--dry-run"])

        assert result.exit_code == 0
        output = _plain(result.output)
        assert "uv tool install --upgrade" in output
        assert "claude plugin install" in output
        assert "setup --runtime claude --non-interactive" in output
        assert "Dry run" in output
        run.assert_not_called()

    def test_auto_runtime_without_claude_or_codex_skips_refresh(self) -> None:
        with (
            patch("ouroboros.cli.commands.update.__version__", "0.1.0"),
            patch(
                "ouroboros.cli.commands.update._latest_pypi_version",
                return_value="99.0.0",
            ),
            patch("ouroboros.cli.commands.update._detect_installer", return_value="pip"),
            patch("ouroboros.cli.commands.update.shutil.which", return_value=None),
            patch("ouroboros.cli.commands.update.subprocess.run") as run,
        ):
            result = runner.invoke(app, ["--dry-run"])

        assert result.exit_code == 0
        assert "Runtime refresh skipped" in result.output
        run.assert_not_called()

    def test_explicit_claude_runtime_without_claude_cli_is_a_notice_not_a_failure(
        self,
    ) -> None:
        def which(name: str) -> str | None:
            return None if name == "claude" else f"/usr/bin/{name}"

        with (
            patch("ouroboros.cli.commands.update.__version__", "0.1.0"),
            patch(
                "ouroboros.cli.commands.update._latest_pypi_version",
                return_value="99.0.0",
            ),
            patch("ouroboros.cli.commands.update._detect_installer", return_value="uv"),
            patch("ouroboros.cli.commands.update.shutil.which", side_effect=which),
            patch("ouroboros.cli.commands.update.subprocess.run") as run,
        ):
            result = runner.invoke(app, ["--dry-run", "--runtime", "claude"])

        assert result.exit_code == 0
        output = _plain(result.output)
        assert "claude CLI not found" in output
        # Setup requires claude on PATH, so the config refresh is skipped too
        # — a notice, not a failure.
        assert "Skipping claude runtime config refresh" in output
        # The skip notice mentions the setup command to run later, but no
        # setup step was previewed for execution.
        assert "setup --runtime claude --non-interactive" not in output
        run.assert_not_called()

    def test_non_dry_run_without_claude_cli_completes_successfully(self) -> None:
        upgrade = MagicMock(returncode=0)
        version_probe = MagicMock(returncode=0, stdout="Ouroboros version 99.0.0\n")

        def which(name: str) -> str | None:
            return None if name == "claude" else f"/usr/bin/{name}"

        with (
            patch("ouroboros.cli.commands.update.__version__", "0.1.0"),
            patch(
                "ouroboros.cli.commands.update._latest_pypi_version",
                return_value="99.0.0",
            ),
            patch("ouroboros.cli.commands.update._detect_installer", return_value="pip"),
            patch("ouroboros.cli.commands.update.shutil.which", side_effect=which),
            patch(
                "ouroboros.cli.commands.update.subprocess.run",
                side_effect=[upgrade, version_probe],
            ) as run,
        ):
            result = runner.invoke(app, ["--yes", "--runtime", "claude"])

        assert result.exit_code == 0
        output = _plain(result.output)
        assert "claude CLI not found" in output
        assert "Skipping claude runtime config refresh" in output
        assert "Updated to v99.0.0" in output
        # Only the package upgrade and the version probe ran — no plugin or
        # setup subprocesses were attempted without the claude CLI.
        assert run.call_count == 2

    def test_failed_package_upgrade_aborts_with_exit_code_one(self) -> None:
        failed_step = MagicMock(returncode=1)
        with (
            patch("ouroboros.cli.commands.update.__version__", "0.1.0"),
            patch(
                "ouroboros.cli.commands.update._latest_pypi_version",
                return_value="99.0.0",
            ),
            patch("ouroboros.cli.commands.update._detect_installer", return_value="uv"),
            patch(
                "ouroboros.cli.commands.update.subprocess.run",
                return_value=failed_step,
            ) as run,
        ):
            result = runner.invoke(app, ["--yes"])

        assert result.exit_code == 1
        assert "package upgrade did not complete" in result.output
        # Only the upgrade step ran — no plugin/setup calls after the abort.
        assert run.call_count == 1
