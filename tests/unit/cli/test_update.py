"""Unit tests for the update command."""

from __future__ import annotations

import json
from pathlib import Path
import re
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from ouroboros.cli.commands.update import (
    PACKAGE_NAME,
    InstallationIdentity,
    InstallationIdentityError,
    _compare_versions,
    _detect_installation_identity,
    _fallback_version_key,
    _is_prerelease,
    _latest_pypi_version,
    _resolve_environment_console,
    _resolve_runtime,
    _upgrade_command,
    _upgrade_environment,
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


def _write_console(environment: Path, *, windows: bool = False, suffix: str = "") -> Path:
    console = environment / ("Scripts" if windows else "bin") / f"ouroboros{suffix}"
    console.parent.mkdir(parents=True, exist_ok=True)
    console.write_text("launcher", encoding="utf-8")
    return console


def _identity(manager: str, tmp_path: Path) -> InstallationIdentity:
    environment = tmp_path / manager / ("venvs" if manager == "pipx" else "tools") / PACKAGE_NAME
    console = _write_console(environment)
    manager_home = environment.parent.parent if manager == "pipx" else environment.parent
    return InstallationIdentity(
        manager=manager,  # type: ignore[arg-type]
        environment=environment,
        profile="ouroboros-ai[mcp,tui]",
        console_path=console,
        manager_binary=f"/usr/bin/{manager}",
        manager_home=manager_home,
    )


def _mock_identity(manager: str = "uv") -> InstallationIdentity:
    environment = Path(f"/managed/{manager}/venvs/ouroboros-ai")
    return InstallationIdentity(
        manager=manager,  # type: ignore[arg-type]
        environment=environment,
        profile="ouroboros-ai[tui]",
        console_path=environment / "bin" / "ouroboros",
        manager_binary=f"/usr/bin/{manager}",
        manager_home=environment.parent.parent,
    )


class TestEnvironmentConsole:
    def test_resolves_posix_console_inside_environment(self, tmp_path: Path) -> None:
        expected = _write_console(tmp_path)
        assert _resolve_environment_console(tmp_path) == expected.resolve()

    def test_resolves_windows_exe_from_pathext(self, tmp_path: Path) -> None:
        expected = _write_console(tmp_path, windows=True, suffix=".exe")
        assert (
            _resolve_environment_console(tmp_path, windows=True, pathext=".CMD;.EXE;.BAT")
            == expected.resolve()
        )

    def test_never_falls_back_to_path(self, tmp_path: Path) -> None:
        with patch("ouroboros.cli.commands.update.shutil.which") as which:
            assert _resolve_environment_console(tmp_path) is None
        which.assert_not_called()

    def test_rejects_console_symlink_escaping_environment(self, tmp_path: Path) -> None:
        outside = tmp_path / "stale-ouroboros"
        outside.write_text("launcher", encoding="utf-8")
        candidate = tmp_path / "environment" / "bin" / "ouroboros"
        candidate.parent.mkdir(parents=True)
        try:
            candidate.symlink_to(outside)
        except OSError:
            pytest.skip("symlink creation is unavailable")
        assert _resolve_environment_console(tmp_path / "environment") is None


class TestInstallationIdentity:
    @pytest.mark.parametrize(
        ("extras", "expected"),
        [
            ([], "ouroboros-ai"),
            (["tui"], "ouroboros-ai[tui]"),
            (["mcp", "tui"], "ouroboros-ai[mcp,tui]"),
            (["claude", "tui"], "ouroboros-ai[claude,tui]"),
            (["all"], "ouroboros-ai[all]"),
        ],
    )
    def test_uv_profile_matrix(self, tmp_path: Path, extras: list[str], expected: str) -> None:
        environment = tmp_path / "tools" / "ouroboros-ai"
        _write_console(environment)
        extras_toml = f", extras = {json.dumps(extras)}" if extras else ""
        (environment / "uv-receipt.toml").write_text(
            f'[tool]\nrequirements = [{{ name = "ouroboros-ai"{extras_toml} }}]\n',
            encoding="utf-8",
        )
        with patch("ouroboros.cli.commands.update.shutil.which", return_value="/opt/bin/uv"):
            assert _detect_installation_identity(environment).profile == expected

    def test_uv_receipt_preserves_extras_and_with_requirements(self, tmp_path: Path) -> None:
        environment = tmp_path / "custom-uv-root" / "ouroboros-ai"
        console = _write_console(environment)
        (environment / "uv-receipt.toml").write_text(
            """[tool]
requirements = [
  { name = "ouroboros-ai", extras = ["mcp", "tui"] },
  { name = "click", specifier = ">=8.1,<9" },
]
""",
            encoding="utf-8",
        )
        with patch("ouroboros.cli.commands.update.shutil.which", return_value="/opt/bin/uv"):
            identity = _detect_installation_identity(environment)

        assert identity.manager == "uv"
        assert identity.environment == environment.resolve()
        assert identity.console_path == console.resolve()
        assert identity.profile == "ouroboros-ai[mcp,tui] (with click>=8.1,<9)"
        assert identity.manager_home == environment.parent.resolve()

    def test_pipx_receipt_preserves_original_profile(self, tmp_path: Path) -> None:
        environment = tmp_path / "custom-pipx-home" / "venvs" / "ouroboros-ai"
        console = _write_console(environment)
        (environment / "pipx_metadata.json").write_text(
            json.dumps(
                {
                    "main_package": {
                        "package": "ouroboros-ai",
                        "package_or_url": "ouroboros-ai[claude,tui]",
                    }
                }
            ),
            encoding="utf-8",
        )
        with patch("ouroboros.cli.commands.update.shutil.which", return_value="/opt/bin/pipx"):
            identity = _detect_installation_identity(environment)

        assert identity.manager == "pipx"
        assert identity.console_path == console.resolve()
        assert identity.profile == "ouroboros-ai[claude,tui]"
        assert identity.manager_home == (tmp_path / "custom-pipx-home").resolve()

    def test_pipx_receipt_preserves_injected_requirements(self, tmp_path: Path) -> None:
        environment = tmp_path / "pipx" / "venvs" / "ouroboros-ai"
        _write_console(environment)
        (environment / "pipx_metadata.json").write_text(
            json.dumps(
                {
                    "main_package": {
                        "package": "ouroboros-ai",
                        "package_or_url": "ouroboros-ai[tui]",
                    },
                    "injected_packages": {
                        "click": {"package_or_url": "click>=8.1,<9"},
                    },
                }
            ),
            encoding="utf-8",
        )
        with patch("ouroboros.cli.commands.update.shutil.which", return_value="/opt/bin/pipx"):
            identity = _detect_installation_identity(environment)
        assert identity.profile == "ouroboros-ai[tui] (with injected click>=8.1,<9)"

    @pytest.mark.parametrize(
        "profile",
        [
            "ouroboros-ai",
            "ouroboros-ai[tui]",
            "ouroboros-ai[mcp,tui]",
            "ouroboros-ai[claude,tui]",
            "ouroboros-ai[all]",
        ],
    )
    def test_pipx_profile_matrix(self, tmp_path: Path, profile: str) -> None:
        environment = tmp_path / "pipx" / "venvs" / "ouroboros-ai"
        _write_console(environment)
        (environment / "pipx_metadata.json").write_text(
            json.dumps(
                {
                    "main_package": {
                        "package": "ouroboros_ai",
                        "package_or_url": profile,
                    }
                }
            ),
            encoding="utf-8",
        )
        with patch("ouroboros.cli.commands.update.shutil.which", return_value="/opt/bin/pipx"):
            assert _detect_installation_identity(environment).profile == profile

    def test_unmanaged_or_direct_pip_environment_fails_closed(self, tmp_path: Path) -> None:
        _write_console(tmp_path)
        with pytest.raises(InstallationIdentityError, match="cannot be preserved"):
            _detect_installation_identity(tmp_path)

    def test_manager_receipt_without_manager_binary_fails_closed(self, tmp_path: Path) -> None:
        _write_console(tmp_path)
        (tmp_path / "uv-receipt.toml").write_text(
            '[tool]\nrequirements = [{ name = "ouroboros-ai" }]\n', encoding="utf-8"
        )
        with (
            patch("ouroboros.cli.commands.update.shutil.which", return_value=None),
            pytest.raises(InstallationIdentityError, match="uv executable is unavailable"),
        ):
            _detect_installation_identity(tmp_path)

    def test_ambiguous_manager_receipts_fail_closed(self, tmp_path: Path) -> None:
        _write_console(tmp_path)
        (tmp_path / "uv-receipt.toml").write_text("[tool]\nrequirements = []\n", encoding="utf-8")
        (tmp_path / "pipx_metadata.json").write_text("{}", encoding="utf-8")
        with pytest.raises(InstallationIdentityError, match="ownership is ambiguous"):
            _detect_installation_identity(tmp_path)


class TestUpgradeCommand:
    """Tests for receipt-replaying upgrade commands."""

    def test_uv_upgrade_replays_receipt(self, tmp_path: Path) -> None:
        identity = _identity("uv", tmp_path)
        assert _upgrade_command(identity, prerelease=False) == [
            "/usr/bin/uv",
            "tool",
            "upgrade",
            PACKAGE_NAME,
        ]
        assert _upgrade_environment(identity) == {"UV_TOOL_DIR": str(identity.manager_home)}

    def test_uv_prerelease(self, tmp_path: Path) -> None:
        command = _upgrade_command(_identity("uv", tmp_path), prerelease=True)
        assert command[-3:] == ["--prerelease", "allow", PACKAGE_NAME]

    def test_pipx_upgrade_replays_receipt(self, tmp_path: Path) -> None:
        identity = _identity("pipx", tmp_path)
        command = _upgrade_command(identity, prerelease=True)
        assert command == [
            "/usr/bin/pipx",
            "upgrade",
            "--include-injected",
            "--pip-args=--pre",
            PACKAGE_NAME,
        ]
        assert _upgrade_environment(identity) == {"PIPX_HOME": str(identity.manager_home)}


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

    def test_unproven_identity_exits_without_mutation(self) -> None:
        with (
            patch("ouroboros.cli.commands.update.__version__", "0.1.0"),
            patch(
                "ouroboros.cli.commands.update._latest_pypi_version",
                return_value="99.0.0",
            ),
            patch(
                "ouroboros.cli.commands.update._detect_installation_identity",
                side_effect=InstallationIdentityError("profile is unknown"),
            ),
            patch("ouroboros.cli.commands.update.subprocess.run") as run,
        ):
            result = runner.invoke(app, ["--yes"])

        assert result.exit_code == 1
        output = _plain(result.output)
        assert "Cannot update safely" in output
        assert "No changes were made" in output
        run.assert_not_called()

    def test_dry_run_previews_all_steps(self) -> None:
        with (
            patch("ouroboros.cli.commands.update.__version__", "0.1.0"),
            patch(
                "ouroboros.cli.commands.update._latest_pypi_version",
                return_value="99.0.0",
            ),
            patch(
                "ouroboros.cli.commands.update._detect_installation_identity",
                return_value=_mock_identity("uv"),
            ),
            patch(
                "ouroboros.cli.commands.update.shutil.which",
                return_value="/usr/bin/anything",
            ),
            patch("ouroboros.cli.commands.update.subprocess.run") as run,
        ):
            result = runner.invoke(app, ["--dry-run"])

        assert result.exit_code == 0
        output = _plain(result.output)
        assert "uv tool upgrade" in output
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
            patch(
                "ouroboros.cli.commands.update._detect_installation_identity",
                return_value=_mock_identity("pipx"),
            ),
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
            patch(
                "ouroboros.cli.commands.update._detect_installation_identity",
                return_value=_mock_identity("uv"),
            ),
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
            patch(
                "ouroboros.cli.commands.update._detect_installation_identity",
                return_value=_mock_identity("pipx"),
            ),
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
        upgrade_call, version_call = run.call_args_list
        assert upgrade_call.kwargs["env"]["PIPX_HOME"] == "/managed/pipx"
        assert version_call.args[0][0] == "/managed/pipx/venvs/ouroboros-ai/bin/ouroboros"

    def test_failed_package_upgrade_aborts_with_exit_code_one(self) -> None:
        failed_step = MagicMock(returncode=1)
        with (
            patch("ouroboros.cli.commands.update.__version__", "0.1.0"),
            patch(
                "ouroboros.cli.commands.update._latest_pypi_version",
                return_value="99.0.0",
            ),
            patch(
                "ouroboros.cli.commands.update._detect_installation_identity",
                return_value=_mock_identity("uv"),
            ),
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
