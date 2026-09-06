"""Unit tests for the update command."""

from __future__ import annotations

from collections.abc import Iterator
import json
import os
from pathlib import Path
import re
from typing import Literal
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from ouroboros.backends import resolve_runtime_backend_name, runtime_backend_choices
from ouroboros.cli.commands.update import (
    PACKAGE_NAME,
    InstallationIdentity,
    InstallationIdentityError,
    RuntimeRefreshTopology,
    _canonical_runtime_backend,
    _compare_versions,
    _configured_runtime_topology,
    _detect_installation_identity,
    _fallback_version_key,
    _is_prerelease,
    _latest_pypi_version,
    _refresh_all_runtime_artifacts,
    _refresh_claude_plugin,
    _refresh_codex_plugin,
    _refresh_runtime_config,
    _resolve_environment_console,
    _resolve_runtime,
    _run_step,
    _upgrade_command,
    _upgrade_environment,
    _validate_runtime_option,
    app,
)
from ouroboros.config.loader import _load_env_file
from ouroboros.core.errors import ConfigError

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolate_runtime_topology() -> Iterator[None]:
    """Keep CLI-flow tests independent of developer runtime configuration."""
    with (
        patch(
            "ouroboros.cli.commands.update._configured_runtime_topology",
            return_value=RuntimeRefreshTopology(
                runtime_executable="/usr/bin/claude",
                runtime_executable_env_key="OUROBOROS_CLI_PATH",
            ),
        ),
        patch("ouroboros.cli.commands.update.configure_omp_tool_call_timeout", return_value=True),
    ):
        yield


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

    @pytest.mark.parametrize("include_prereleases", [False, True])
    def test_fully_yanked_info_version_is_never_selected(
        self,
        include_prereleases: bool,
    ) -> None:
        payload = {
            "info": {"version": "2.0.0"},
            "releases": {
                "1.9.0": [{"filename": "available.whl", "yanked": False}],
                "2.0.0": [{"filename": "yanked.whl", "yanked": True}],
            },
        }
        with patch(
            "ouroboros.cli.commands.update.urllib.request.urlopen",
            _urlopen_returning(payload),
        ):
            assert _latest_pypi_version(include_prereleases) == "1.9.0"

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
        tool_name=environment.name,
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
        tool_name=environment.name,
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

    def test_uv_pypi_registry_receipt_is_accepted(self, tmp_path: Path) -> None:
        environment = tmp_path / "tools" / "ouroboros-ai"
        _write_console(environment)
        (environment / "uv-receipt.toml").write_text(
            """[tool]
requirements = [{ name = "ouroboros-ai", extras = ["tui"], specifier = ">=0.50" }]
entrypoints = [
  { name = "ouroboros", install-path = "/usr/local/bin/ouroboros", from = "ouroboros-ai" },
]
""",
            encoding="utf-8",
        )

        with patch("ouroboros.cli.commands.update.shutil.which", return_value="/opt/bin/uv"):
            identity = _detect_installation_identity(environment)

        assert identity.profile == "ouroboros-ai[tui]>=0.50"

    def test_real_shaped_uv_editable_receipt_fails_closed(self, tmp_path: Path) -> None:
        environment = tmp_path / "tools" / "ouroboros-ai"
        _write_console(environment)
        (environment / "uv-receipt.toml").write_text(
            f"""[tool]
requirements = [{{ name = "ouroboros-ai", editable = "{tmp_path.as_posix()}" }}]
entrypoints = [
  {{ name = "ouroboros", install-path = "{environment / "bin" / "ouroboros"}", from = "ouroboros-ai" }},
]
""",
            encoding="utf-8",
        )

        with (
            patch("ouroboros.cli.commands.update.shutil.which", return_value="/opt/bin/uv"),
            pytest.raises(InstallationIdentityError, match=r"not a PyPI registry source.*editable"),
        ):
            _detect_installation_identity(environment)

    @pytest.mark.parametrize(
        ("source_field", "source_value"),
        [
            ("git", "https://github.com/Q00/ouroboros"),
            ("path", "/tmp/ouroboros-ai.whl"),
            ("url", "https://example.invalid/ouroboros-ai.whl"),
            ("directory", "/tmp/ouroboros-ai"),
            ("virtual", "/tmp/ouroboros-ai"),
        ],
    )
    def test_uv_non_pypi_source_receipts_fail_closed(
        self,
        tmp_path: Path,
        source_field: str,
        source_value: str,
    ) -> None:
        environment = tmp_path / "tools" / "ouroboros-ai"
        _write_console(environment)
        (environment / "uv-receipt.toml").write_text(
            "[tool]\n"
            f'requirements = [{{ name = "ouroboros-ai", {source_field} = "{source_value}" }}]\n',
            encoding="utf-8",
        )

        with (
            patch("ouroboros.cli.commands.update.shutil.which", return_value="/opt/bin/uv"),
            pytest.raises(
                InstallationIdentityError,
                match=rf"not a PyPI registry source.*{source_field}",
            ),
        ):
            _detect_installation_identity(environment)

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
        assert identity.tool_name == "ouroboros-ai"
        assert identity.console_path == console.resolve()
        assert identity.profile == "ouroboros-ai[claude,tui]"
        assert identity.manager_home == (tmp_path / "custom-pipx-home").resolve()

    def test_pipx_suffixed_environment_preserves_exact_tool_target(self, tmp_path: Path) -> None:
        environment = tmp_path / "pipx" / "venvs" / "ouroboros-ai-dev"
        _write_console(environment)
        (environment / "pipx_metadata.json").write_text(
            json.dumps(
                {
                    "main_package": {
                        "package": "ouroboros-ai",
                        "package_or_url": "ouroboros-ai[tui]",
                    }
                }
            ),
            encoding="utf-8",
        )
        with patch("ouroboros.cli.commands.update.shutil.which", return_value="/opt/bin/pipx"):
            identity = _detect_installation_identity(environment)

        assert identity.tool_name == "ouroboros-ai-dev"
        assert _upgrade_command(identity, prerelease=False)[-1] == "ouroboros-ai-dev"

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
            "ouroboros-ai",
        ]
        assert _upgrade_environment(identity) == {"PIPX_HOME": str(identity.manager_home)}

    @pytest.mark.parametrize(
        ("manager", "project_controls", "root_key"),
        [
            (
                "uv",
                {
                    "UV_INDEX": "https://attacker.invalid/simple",
                    "UV_DEFAULT_INDEX": "https://attacker.invalid/default",
                    "UV_CONFIG_FILE": "./attacker-uv.toml",
                },
                "UV_TOOL_DIR",
            ),
            (
                "pipx",
                {
                    "PIP_INDEX_URL": "https://attacker.invalid/simple",
                    "PIP_EXTRA_INDEX_URL": "https://attacker.invalid/extra",
                    "PIP_CONFIG_FILE": "./attacker-pip.conf",
                },
                "PIPX_HOME",
            ),
        ],
    )
    def test_project_env_cannot_redirect_upgrade_source(
        self,
        tmp_path: Path,
        manager: Literal["uv", "pipx"],
        project_controls: dict[str, str],
        root_key: str,
    ) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text(
            "".join(f"{key}={value}\n" for key, value in project_controls.items()),
            encoding="utf-8",
        )
        identity = _identity(manager, tmp_path)
        completed = MagicMock(returncode=0)

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("ouroboros.cli.commands.update.subprocess.run", return_value=completed) as run,
        ):
            _load_env_file(env_file, trusted=False)
            assert _run_step(
                _upgrade_command(identity, prerelease=False),
                description="upgrade",
                dry_run=False,
                env_overrides=_upgrade_environment(identity),
            )

        command_env = run.call_args.kwargs["env"]
        assert project_controls.keys().isdisjoint(command_env)
        assert command_env[root_key] == str(identity.manager_home)

    @pytest.mark.parametrize(
        ("manager", "trusted_key"),
        [("uv", "UV_INDEX"), ("pipx", "PIP_INDEX_URL")],
    )
    def test_trusted_process_upgrade_source_is_preserved(
        self,
        tmp_path: Path,
        manager: Literal["uv", "pipx"],
        trusted_key: str,
    ) -> None:
        identity = _identity(manager, tmp_path)
        trusted_source = "https://packages.example/simple"
        completed = MagicMock(returncode=0)

        with (
            patch.dict(os.environ, {trusted_key: trusted_source}, clear=True),
            patch("ouroboros.cli.commands.update.subprocess.run", return_value=completed) as run,
        ):
            assert _run_step(
                _upgrade_command(identity, prerelease=False),
                description="upgrade",
                dry_run=False,
                env_overrides=_upgrade_environment(identity),
            )

        assert run.call_args.kwargs["env"][trusted_key] == trusted_source


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

    @pytest.mark.parametrize("configured_backend", ["hermes", "opencode", "codex", "claude"])
    def test_auto_preserves_configured_backend(self, configured_backend: str) -> None:
        with patch(
            "ouroboros.cli.commands.update.shutil.which",
            return_value="/usr/bin/claude",
        ) as which:
            assert (
                _resolve_runtime("auto", configured_backend=configured_backend)
                == configured_backend
            )
        which.assert_not_called()


class TestConfiguredRuntimeTopology:
    def test_missing_config_keeps_path_fallback_available(self, tmp_path: Path) -> None:
        with (
            patch("ouroboros.cli.commands.update.get_config_dir", return_value=tmp_path),
            patch("ouroboros.cli.commands.update.shutil.which", return_value=None),
        ):
            assert _configured_runtime_topology() == RuntimeRefreshTopology()

    @pytest.mark.parametrize("runtime", runtime_backend_choices(include_aliases=True))
    def test_registry_runtime_names_and_aliases_resolve_to_setup_integration(
        self,
        runtime: str,
    ) -> None:
        registry_backend = resolve_runtime_backend_name(runtime)
        expected = {"codex_mcp": "codex", "claude_mcp": "claude"}.get(
            registry_backend,
            registry_backend,
        )
        assert _canonical_runtime_backend(runtime) == expected
        assert _validate_runtime_option(runtime) == expected

    @pytest.mark.parametrize("runtime", runtime_backend_choices(include_aliases=True))
    def test_auto_environment_accepts_every_registry_runtime_name(
        self,
        tmp_path: Path,
        runtime: str,
    ) -> None:
        registry_backend = resolve_runtime_backend_name(runtime)
        expected = {"codex_mcp": "codex", "claude_mcp": "claude"}.get(
            registry_backend,
            registry_backend,
        )
        with (
            patch.dict(os.environ, {"OUROBOROS_AGENT_RUNTIME": runtime}, clear=True),
            patch("ouroboros.cli.commands.update.get_config_dir", return_value=tmp_path),
            patch(
                "ouroboros.cli.commands.update.load_config",
                side_effect=ConfigError("missing config"),
            ),
            patch(
                "ouroboros.cli.commands.update.shutil.which",
                side_effect=lambda command: f"/usr/bin/{command}",
            ),
        ):
            topology = _configured_runtime_topology()

        assert topology.runtime_backend == expected

    def test_existing_config_with_unrelated_validation_error_fails_closed(
        self,
        tmp_path: Path,
    ) -> None:
        (tmp_path / "config.yaml").write_text(
            """orchestrator:
  runtime_backend: opencode
  opencode_mode: subprocess
  default_max_turns: 0
""",
            encoding="utf-8",
        )

        with (
            patch("ouroboros.cli.commands.update.get_config_dir", return_value=tmp_path),
            pytest.raises(ConfigError) as exc_info,
        ):
            _configured_runtime_topology()

        assert exc_info.value.config_key == "orchestrator.default_max_turns"

    def test_existing_malformed_config_fails_closed(self, tmp_path: Path) -> None:
        (tmp_path / "config.yaml").write_text("orchestrator: [", encoding="utf-8")

        with (
            patch("ouroboros.cli.commands.update.get_config_dir", return_value=tmp_path),
            pytest.raises(ConfigError),
        ):
            _configured_runtime_topology()

    @pytest.mark.parametrize(
        ("backend", "env_key", "config_field", "command_name"),
        [
            ("claude", "OUROBOROS_CLI_PATH", "cli_path", "claude"),
            (
                "opencode",
                "OUROBOROS_OPENCODE_CLI_PATH",
                "opencode_cli_path",
                "opencode",
            ),
            ("hermes", "OUROBOROS_HERMES_CLI_PATH", "hermes_cli_path", "hermes"),
        ],
    )
    def test_configured_executable_outside_path_beats_stale_path(
        self,
        tmp_path: Path,
        backend: str,
        env_key: str,
        config_field: str,
        command_name: str,
    ) -> None:
        configured = tmp_path / "configured" / f"{command_name}-wrapper"
        configured.parent.mkdir()
        configured.write_text("#!/bin/sh\n", encoding="utf-8")
        configured.chmod(0o755)
        config = MagicMock()
        config.orchestrator.runtime_backend = backend
        config.orchestrator.opencode_mode = "subprocess" if backend == "opencode" else None
        setattr(config.orchestrator, config_field, str(configured))

        def which(candidate: str) -> str | None:
            if candidate == str(configured):
                return str(configured)
            if candidate == command_name:
                return f"/stale/path/{command_name}"
            return None

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("ouroboros.cli.commands.update.load_config", return_value=config),
            patch("ouroboros.cli.commands.update.shutil.which", side_effect=which),
        ):
            topology = _configured_runtime_topology()

        assert topology.runtime_backend == backend
        assert topology.runtime_executable == str(configured.resolve())
        assert topology.runtime_executable_env_key == env_key

    @pytest.mark.parametrize(
        ("backend", "env_key", "config_field"),
        [
            ("claude", "OUROBOROS_CLI_PATH", "cli_path"),
            ("opencode", "OUROBOROS_OPENCODE_CLI_PATH", "opencode_cli_path"),
            ("hermes", "OUROBOROS_HERMES_CLI_PATH", "hermes_cli_path"),
        ],
    )
    def test_executable_env_override_beats_config(
        self,
        tmp_path: Path,
        backend: str,
        env_key: str,
        config_field: str,
    ) -> None:
        env_executable = tmp_path / "env" / "runtime"
        config_executable = tmp_path / "config" / "runtime"
        env_executable.parent.mkdir()
        config_executable.parent.mkdir()
        for executable in (env_executable, config_executable):
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o755)
        config = MagicMock()
        config.orchestrator.runtime_backend = backend
        config.orchestrator.opencode_mode = None
        setattr(config.orchestrator, config_field, str(config_executable))

        with (
            patch.dict(os.environ, {env_key: str(env_executable)}, clear=True),
            patch("ouroboros.cli.commands.update.load_config", return_value=config),
            patch(
                "ouroboros.cli.commands.update.shutil.which",
                side_effect=lambda candidate: (
                    str(env_executable) if candidate == str(env_executable) else None
                ),
            ),
        ):
            topology = _configured_runtime_topology()

        assert topology.runtime_executable == str(env_executable.resolve())
        assert topology.runtime_executable_env_key == env_key

    @pytest.mark.parametrize(
        ("backend", "config_field", "command_name"),
        [
            ("claude", "cli_path", "claude"),
            ("opencode", "opencode_cli_path", "opencode"),
            ("hermes", "hermes_cli_path", "hermes"),
        ],
    )
    def test_invalid_configured_executable_does_not_fall_back_to_stale_path(
        self,
        tmp_path: Path,
        backend: str,
        config_field: str,
        command_name: str,
    ) -> None:
        missing = tmp_path / "missing" / f"{command_name}-wrapper"
        config = MagicMock()
        config.orchestrator.runtime_backend = backend
        config.orchestrator.opencode_mode = None
        setattr(config.orchestrator, config_field, str(missing))
        probes: list[str] = []

        def which(candidate: str) -> str | None:
            probes.append(candidate)
            return f"/stale/path/{command_name}" if candidate == command_name else None

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("ouroboros.cli.commands.update.load_config", return_value=config),
            patch("ouroboros.cli.commands.update.shutil.which", side_effect=which),
            pytest.raises(ConfigError) as exc_info,
        ):
            _configured_runtime_topology()

        assert exc_info.value.config_key == f"orchestrator.{config_field}"
        assert probes == [str(missing)]

    def test_reads_backend_and_opencode_mode_from_valid_config(self) -> None:
        config = MagicMock()
        config.orchestrator.runtime_backend = "opencode"
        config.orchestrator.opencode_mode = "subprocess"
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("ouroboros.cli.commands.update.load_config", return_value=config),
            patch("ouroboros.cli.commands.update.shutil.which", return_value=None),
        ):
            assert _configured_runtime_topology() == RuntimeRefreshTopology(
                runtime_backend="opencode",
                opencode_mode="subprocess",
            )

    def test_legacy_opencode_config_preserves_effective_subprocess_topology(self) -> None:
        config = MagicMock()
        config.orchestrator.runtime_backend = "opencode"
        config.orchestrator.opencode_mode = None
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("ouroboros.cli.commands.update.load_config", return_value=config),
            patch("ouroboros.cli.commands.update.shutil.which", return_value=None),
        ):
            assert _configured_runtime_topology() == RuntimeRefreshTopology(
                runtime_backend="opencode",
                opencode_mode="subprocess",
            )

    @pytest.mark.parametrize(
        ("opencode_mode", "plugin_dispatch"),
        [("plugin", True), ("subprocess", False)],
    )
    def test_opencode_refresh_forwards_existing_mcp_topology(
        self,
        opencode_mode: Literal["plugin", "subprocess"],
        plugin_dispatch: bool,
    ) -> None:
        from ouroboros.mcp.tools.subagent import should_dispatch_via_plugin

        with patch("ouroboros.cli.commands.update._run_step", return_value=True) as run:
            assert _refresh_runtime_config(
                "opencode",
                False,
                _mock_identity("uv"),
                opencode_mode=opencode_mode,
                runtime_executable="/configured/opencode",
                runtime_executable_env_key="OUROBOROS_OPENCODE_CLI_PATH",
            )

        assert run.call_args.args[0] == [
            "/managed/uv/venvs/ouroboros-ai/bin/ouroboros",
            "setup",
            "--runtime",
            "opencode",
            "--opencode-mode",
            opencode_mode,
            "--non-interactive",
        ]
        assert run.call_args.kwargs["env_overrides"] == {
            "OUROBOROS_OPENCODE_CLI_PATH": "/configured/opencode"
        }
        assert should_dispatch_via_plugin("opencode", opencode_mode) is plugin_dispatch


class TestClaudePluginRefresh:
    def test_existing_plugin_is_explicitly_updated_after_install(self) -> None:
        with (
            patch("ouroboros.cli.commands.update.shutil.which", return_value="/usr/bin/claude"),
            patch(
                "ouroboros.cli.commands.update._run_step",
                side_effect=[False, True, True],
            ) as run,
        ):
            assert _refresh_claude_plugin(False, "/configured/claude") is True

        commands = [call.args[0] for call in run.call_args_list]
        assert commands == [
            ["/configured/claude", "plugin", "marketplace", "update", "ouroboros"],
            ["/configured/claude", "plugin", "install", "ouroboros@ouroboros"],
            ["/configured/claude", "plugin", "update", "ouroboros@ouroboros"],
        ]

    def test_failed_install_does_not_attempt_plugin_update(self) -> None:
        with (
            patch("ouroboros.cli.commands.update.shutil.which", return_value="/usr/bin/claude"),
            patch(
                "ouroboros.cli.commands.update._run_step",
                side_effect=[True, False],
            ) as run,
        ):
            assert _refresh_claude_plugin(False, "/configured/claude") is False

        assert run.call_count == 2


class TestCodexPluginRefresh:
    def test_refreshes_only_the_ouroboros_marketplace(self) -> None:
        with patch("ouroboros.cli.commands.update._run_step", return_value=True) as run:
            assert _refresh_codex_plugin(False, "/configured/codex") is True

        assert run.call_args.args[0] == [
            "/configured/codex",
            "plugin",
            "marketplace",
            "upgrade",
            "ouroboros",
        ]

    def test_missing_codex_cli_skips_plugin_refresh(self) -> None:
        with patch("ouroboros.cli.commands.update._run_step") as run:
            assert _refresh_codex_plugin(False, None) is None

        run.assert_not_called()


def test_all_runtime_artifact_refresh_does_not_reconfigure_backend() -> None:
    with patch("ouroboros.cli.commands.update._run_step", return_value=True) as run:
        assert _refresh_all_runtime_artifacts(False, _mock_identity("uv"))

    assert run.call_args.args[0] == [
        "/managed/uv/venvs/ouroboros-ai/bin/ouroboros",
        "setup",
        "refresh",
    ]


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

    def test_up_to_date_command_repairs_omp_timeout(self) -> None:
        with (
            patch("ouroboros.cli.commands.update.__version__", "0.50.7"),
            patch(
                "ouroboros.cli.commands.update._latest_pypi_version",
                return_value="0.50.7",
            ),
            patch(
                "ouroboros.cli.commands.update.configure_omp_tool_call_timeout",
                return_value=True,
            ) as configure_omp,
        ):
            result = runner.invoke(app, [])

        assert result.exit_code == 0
        configure_omp.assert_called_once_with(dry_run=False)

    def test_up_to_date_omp_failure_warns_without_failing_update(self) -> None:
        with (
            patch("ouroboros.cli.commands.update.__version__", "0.50.7"),
            patch(
                "ouroboros.cli.commands.update._latest_pypi_version",
                return_value="0.50.7",
            ),
            patch(
                "ouroboros.cli.commands.update.configure_omp_tool_call_timeout",
                return_value=False,
            ),
        ):
            result = runner.invoke(app, [])

        assert result.exit_code == 0
        assert "Could not set OMP MCP tool timeout" in _plain(result.output)

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

    @pytest.mark.parametrize("extra_args", [[], ["--dry-run"]], ids=["normal", "dry-run"])
    def test_invalid_runtime_fails_before_any_io_or_mutation(
        self,
        extra_args: list[str],
    ) -> None:
        with (
            patch("ouroboros.cli.commands.update._latest_pypi_version") as latest,
            patch("ouroboros.cli.commands.update._detect_installation_identity") as detect,
            patch("ouroboros.cli.commands.update._run_step") as step,
            patch("ouroboros.cli.commands.update._configured_runtime_topology") as topology,
            patch("ouroboros.cli.commands.update._refresh_runtime_config") as refresh_config,
            patch("ouroboros.cli.commands.update._refresh_claude_plugin") as refresh_plugin,
            patch("ouroboros.cli.commands.update.subprocess.run") as run,
        ):
            result = runner.invoke(
                app,
                ["--yes", "--runtime", "not-a-runtime", *extra_args],
            )

        assert result.exit_code == 2
        assert "unsupported runtime 'not-a-runtime'" in _plain(result.output)
        latest.assert_not_called()
        detect.assert_not_called()
        step.assert_not_called()
        topology.assert_not_called()
        refresh_config.assert_not_called()
        refresh_plugin.assert_not_called()
        run.assert_not_called()

    @pytest.mark.parametrize(
        "runtime",
        [
            "auto",
            "none",
            "claude",
            "codex",
            "opencode",
            "hermes",
            "gemini",
            "kiro",
            "copilot",
            "goose",
            "pi",
            "omp",
            "gjc",
            "antigravity",
            "grok",
            "zcode",
        ],
    )
    def test_runtime_validation_accepts_canonical_backends(self, runtime: str) -> None:
        assert _validate_runtime_option(runtime) == runtime

    def test_runtime_validation_accepts_all_host_integrations(self) -> None:
        assert _validate_runtime_option("all") == "all"

    @pytest.mark.parametrize(
        ("runtime", "expected"),
        [
            (" CLAUDE_CODE ", "claude"),
            ("CODEX_CLI", "codex"),
            ("OpenCode_CLI", "opencode"),
            ("hermes_cli", "hermes"),
        ],
    )
    def test_runtime_validation_canonicalizes_supported_aliases(
        self,
        runtime: str,
        expected: str,
    ) -> None:
        assert _validate_runtime_option(runtime) == expected

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
        assert "claude plugin update" in output
        assert "setup --runtime claude --non-interactive" in output
        assert "Dry run" in output
        run.assert_not_called()

    def test_all_runtime_dry_run_refreshes_claude_and_codex_without_reselection(
        self,
    ) -> None:
        def topology(runtime: str) -> RuntimeRefreshTopology:
            env_key = "OUROBOROS_CLI_PATH" if runtime == "claude" else "OUROBOROS_CODEX_CLI_PATH"
            return RuntimeRefreshTopology(
                runtime_backend=runtime,
                runtime_executable=f"/configured/{runtime}",
                runtime_executable_env_key=env_key,
            )

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
                "ouroboros.cli.commands.update._configured_runtime_topology",
                side_effect=topology,
            ) as configured,
            patch("ouroboros.cli.commands.update.subprocess.run") as run,
        ):
            result = runner.invoke(app, ["--dry-run", "--runtime", "all"])

        assert result.exit_code == 0
        output = _plain(result.output)
        assert "claude plugin marketplace update ouroboros" in output
        assert "claude plugin update ouroboros@ouroboros" in output
        assert "codex plugin marketplace upgrade ouroboros" in output
        assert "ouroboros setup refresh" in output
        assert "setup --runtime claude" not in output
        assert "setup --runtime codex" not in output
        assert [call.args[0] for call in configured.call_args_list] == ["claude", "codex"]
        run.assert_not_called()

    def test_all_runtime_update_runs_both_host_refreshes_and_artifact_refresh(
        self,
    ) -> None:
        def topology(runtime: str) -> RuntimeRefreshTopology:
            env_key = "OUROBOROS_CLI_PATH" if runtime == "claude" else "OUROBOROS_CODEX_CLI_PATH"
            return RuntimeRefreshTopology(
                runtime_backend=runtime,
                runtime_executable=f"/configured/{runtime}",
                runtime_executable_env_key=env_key,
            )

        def run_command(command: list[str], **_kwargs: object) -> MagicMock:
            if command == ["/managed/uv/venvs/ouroboros-ai/bin/ouroboros", "--version"]:
                return MagicMock(returncode=0, stdout="Ouroboros version 99.0.0\n")
            return MagicMock(returncode=0, stdout="")

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
                "ouroboros.cli.commands.update._configured_runtime_topology",
                side_effect=topology,
            ),
            patch(
                "ouroboros.cli.commands.update.subprocess.run",
                side_effect=run_command,
            ) as run,
        ):
            result = runner.invoke(app, ["--yes", "--runtime", "all"])

        assert result.exit_code == 0
        commands = [call.args[0] for call in run.call_args_list]
        assert ["/configured/claude", "plugin", "marketplace", "update", "ouroboros"] in commands
        assert [
            "/configured/claude",
            "plugin",
            "update",
            "ouroboros@ouroboros",
        ] in commands
        assert [
            "/configured/codex",
            "plugin",
            "marketplace",
            "upgrade",
            "ouroboros",
        ] in commands
        assert [
            "/managed/uv/venvs/ouroboros-ai/bin/ouroboros",
            "setup",
            "refresh",
        ] in commands
        assert "Updated to v99.0.0" in _plain(result.output)
        assert "Restart active claude and codex sessions" in _plain(result.output)

    def test_all_runtime_update_reports_partial_artifact_refresh(self) -> None:
        def topology(runtime: str) -> RuntimeRefreshTopology:
            env_key = "OUROBOROS_CLI_PATH" if runtime == "claude" else "OUROBOROS_CODEX_CLI_PATH"
            return RuntimeRefreshTopology(
                runtime_backend=runtime,
                runtime_executable=f"/configured/{runtime}",
                runtime_executable_env_key=env_key,
            )

        def run_command(command: list[str], **_kwargs: object) -> MagicMock:
            if command == ["/managed/uv/venvs/ouroboros-ai/bin/ouroboros", "--version"]:
                return MagicMock(returncode=0, stdout="Ouroboros version 99.0.0\n")
            if command[-2:] == ["setup", "refresh"]:
                return MagicMock(returncode=1, stdout="")
            return MagicMock(returncode=0, stdout="")

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
                "ouroboros.cli.commands.update._configured_runtime_topology",
                side_effect=topology,
            ),
            patch(
                "ouroboros.cli.commands.update.subprocess.run",
                side_effect=run_command,
            ),
        ):
            result = runner.invoke(app, ["--yes", "--runtime", "all"])

        assert result.exit_code == 1
        output = _plain(result.output)
        assert "Ouroboros partially updated" in output
        assert "installed runtime artifact refresh" in output
        assert "Updated to v99.0.0" not in output

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

    @pytest.mark.parametrize("configured_backend", ["hermes", "codex", "claude"])
    def test_auto_update_preserves_configured_backend(
        self,
        configured_backend: str,
    ) -> None:
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
                "ouroboros.cli.commands.update._configured_runtime_topology",
                return_value=RuntimeRefreshTopology(
                    configured_backend,
                    runtime_executable=f"/configured/{configured_backend}",
                    runtime_executable_env_key=(
                        "OUROBOROS_CLI_PATH"
                        if configured_backend == "claude"
                        else f"OUROBOROS_{configured_backend.upper()}_CLI_PATH"
                    ),
                ),
            ),
            patch(
                "ouroboros.cli.commands.update.shutil.which",
                return_value="/usr/bin/available",
            ),
            patch("ouroboros.cli.commands.update.subprocess.run") as run,
        ):
            result = runner.invoke(app, ["--dry-run"])

        assert result.exit_code == 0
        output = _plain(result.output)
        expected_refresh = f"setup --runtime {configured_backend}"
        if configured_backend == "codex":
            expected_refresh += " --preserve-existing-llm"
        expected_refresh += " --non-interactive"
        assert expected_refresh in output
        expected_env_key = (
            "OUROBOROS_CLI_PATH"
            if configured_backend == "claude"
            else f"OUROBOROS_{configured_backend.upper()}_CLI_PATH"
        )
        assert f"{expected_env_key}=/configured/{configured_backend}" in output
        if configured_backend != "claude":
            assert "claude plugin" not in output
        run.assert_not_called()

    @pytest.mark.parametrize(
        "runtime_args",
        [[], ["--runtime", "opencode"]],
        ids=["auto", "explicit"],
    )
    def test_opencode_update_preserves_subprocess_topology(
        self,
        runtime_args: list[str],
    ) -> None:
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
                "ouroboros.cli.commands.update._configured_runtime_topology",
                return_value=RuntimeRefreshTopology(
                    "opencode",
                    "subprocess",
                    "/configured/opencode",
                    "OUROBOROS_OPENCODE_CLI_PATH",
                ),
            ),
            patch(
                "ouroboros.cli.commands.update.shutil.which",
                return_value="/usr/bin/claude",
            ),
            patch("ouroboros.cli.commands.update.subprocess.run") as run,
        ):
            result = runner.invoke(app, ["--dry-run", *runtime_args])

        assert result.exit_code == 0
        output = _plain(result.output)
        assert "setup --runtime opencode --opencode-mode subprocess --non-interactive" in output
        assert "OUROBOROS_OPENCODE_CLI_PATH=/configured/opencode" in output
        assert "claude plugin" not in output
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
            patch(
                "ouroboros.cli.commands.update._configured_runtime_topology",
                return_value=RuntimeRefreshTopology("claude"),
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
            patch(
                "ouroboros.cli.commands.update._configured_runtime_topology",
                return_value=RuntimeRefreshTopology("claude"),
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

    def test_invalid_existing_config_aborts_before_package_mutation(
        self,
    ) -> None:
        config_error = ConfigError(
            "Configuration validation failed",
            config_key="orchestrator.default_max_turns",
        )

        with (
            patch("ouroboros.cli.commands.update.__version__", "0.1.0"),
            patch(
                "ouroboros.cli.commands.update._latest_pypi_version",
                return_value="99.0.0",
            ),
            patch(
                "ouroboros.cli.commands.update._detect_installation_identity",
            ) as detect,
            patch(
                "ouroboros.cli.commands.update._configured_runtime_topology",
                side_effect=config_error,
            ),
            patch("ouroboros.cli.commands.update._run_step") as step,
            patch("ouroboros.cli.commands.update.subprocess.run") as run,
        ):
            result = runner.invoke(app, ["--yes"])

        assert result.exit_code == 1
        output = _plain(result.output)
        assert "Cannot update safely: runtime configuration is invalid" in output
        assert "No changes were made" in output
        detect.assert_not_called()
        step.assert_not_called()
        run.assert_not_called()

    def test_runtime_none_does_not_read_invalid_config(self) -> None:
        upgrade = MagicMock(returncode=0)
        version_probe = MagicMock(returncode=0, stdout="Ouroboros version 99.0.0\n")

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
            patch("ouroboros.cli.commands.update._configured_runtime_topology") as topology,
            patch(
                "ouroboros.cli.commands.update.subprocess.run",
                side_effect=[upgrade, version_probe],
            ) as run,
            patch(
                "ouroboros.cli.commands.update.configure_omp_tool_call_timeout",
                return_value=True,
            ) as configure_omp,
        ):
            result = runner.invoke(app, ["--yes", "--runtime", "none"])

        assert result.exit_code == 0
        assert "Runtime refresh skipped" in result.output
        assert "Updated to v99.0.0" in _plain(result.output)
        assert run.call_count == 2
        topology.assert_not_called()
        configure_omp.assert_called_once_with(dry_run=False)

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

    @pytest.mark.parametrize(
        "version_probe",
        [
            MagicMock(returncode=1, stdout="Ouroboros version 99.0.0\n"),
            MagicMock(returncode=0, stdout="version unavailable\n"),
        ],
        ids=["probe-exits-nonzero", "probe-has-no-version"],
    )
    def test_unverifiable_post_upgrade_version_aborts_before_runtime_refresh(
        self,
        version_probe: MagicMock,
    ) -> None:
        upgrade = MagicMock(returncode=0)
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
                side_effect=[upgrade, version_probe],
            ) as run,
        ):
            result = runner.invoke(app, ["--yes", "--runtime", "codex"])

        assert result.exit_code == 1
        output = _plain(result.output)
        assert "version could not be verified" in output
        assert "Runtime integration was not refreshed" in output
        assert run.call_count == 2

    def test_stale_post_upgrade_version_aborts_before_runtime_refresh(self) -> None:
        upgrade = MagicMock(returncode=0)
        stale_version = MagicMock(returncode=0, stdout="Ouroboros version 0.1.0\n")
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
            patch(
                "ouroboros.cli.commands.update.subprocess.run",
                side_effect=[upgrade, stale_version],
            ) as run,
        ):
            result = runner.invoke(app, ["--yes", "--runtime", "codex"])

        assert result.exit_code == 1
        output = _plain(result.output)
        assert "v0.1.0 (expected at least v99.0.0)" in output
        assert "Runtime integration was not refreshed" in output
        assert run.call_count == 2
