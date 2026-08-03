"""Unit tests for the update command."""

from __future__ import annotations

import json
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
    _resolve_runtime,
    _upgrade_command,
    app,
)

runner = CliRunner()


def _plain(output: str) -> str:
    """Strip ANSI escapes — rich highlighting splits version strings mid-token."""
    return re.sub(r"\x1b\[[0-9;]*m", "", output)


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
            "0.50.8b1": [{"filename": "beta.whl"}],
            "0.99.0": [],  # yanked/empty release must be ignored
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
            assert _latest_pypi_version(include_prereleases=True) == "0.50.8b1"

    def test_network_error_returns_none(self) -> None:
        with patch(
            "ouroboros.cli.commands.update.urllib.request.urlopen",
            MagicMock(side_effect=OSError("offline")),
        ):
            assert _latest_pypi_version(include_prereleases=False) is None


# ── Installer detection and upgrade commands ─────────────────────


class TestDetectInstaller:
    """Tests for _detect_installer."""

    def test_uv_owns_the_install(self) -> None:
        result = MagicMock(returncode=0, stdout="ouroboros-ai v0.50.7\n- ooo\n- ouroboros\n")
        with (
            patch("ouroboros.cli.commands.update.shutil.which", return_value="/usr/bin/tool"),
            patch("ouroboros.cli.commands.update.subprocess.run", return_value=result) as run,
        ):
            assert _detect_installer() == "uv"
        assert run.call_args_list[0].args[0] == ["uv", "tool", "list"]

    def test_pipx_owns_when_uv_does_not(self) -> None:
        uv_result = MagicMock(returncode=0, stdout="other-tool v1.0.0\n")
        pipx_result = MagicMock(returncode=0, stdout="package ouroboros-ai 0.50.7\n")
        with (
            patch("ouroboros.cli.commands.update.shutil.which", return_value="/usr/bin/tool"),
            patch(
                "ouroboros.cli.commands.update.subprocess.run",
                side_effect=[uv_result, pipx_result],
            ),
        ):
            assert _detect_installer() == "pipx"

    def test_falls_back_to_pip_when_no_tool_manager_found(self) -> None:
        with patch("ouroboros.cli.commands.update.shutil.which", return_value=None):
            assert _detect_installer() == "pip"

    def test_probe_failure_falls_through(self) -> None:
        with (
            patch("ouroboros.cli.commands.update.shutil.which", return_value="/usr/bin/tool"),
            patch(
                "ouroboros.cli.commands.update.subprocess.run",
                side_effect=OSError("cannot exec"),
            ),
        ):
            assert _detect_installer() == "pip"


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
        assert "uv tool install --upgrade" in result.output
        assert "claude plugin install" in result.output
        assert "setup --runtime claude --non-interactive" in result.output
        assert "Dry run" in result.output
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
        assert "claude CLI not found" in result.output
        assert "setup --runtime claude --non-interactive" in result.output
        run.assert_not_called()

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
