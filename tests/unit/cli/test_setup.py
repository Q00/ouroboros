"""Unit tests for the setup command."""

from __future__ import annotations

from contextlib import contextmanager, suppress
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import time
import tomllib
from unittest.mock import AsyncMock, patch

import pytest
import typer
from typer.testing import CliRunner
import yaml

from ouroboros.backends.capabilities import render_backend_skill_capability_guide
import ouroboros.cli.commands.setup as setup_cmd
from ouroboros.cli.commands.setup import (
    _codex_uses_profile_v2,  # real fn bound at import; bypasses the autouse probe guard
    _display_repos_table,
    _ensure_opencode_mcp_entry,
    _find_opencode_config,
    _list_repos,
    _prompt_repo_selection,
    _scan_and_register_repos,
    _set_default_repo,
)
import ouroboros.cli.runtime_activation as runtime_activation
from ouroboros.codex import CodexArtifactInstallResult
from ouroboros.codex.runtime_profile import codex_uses_profile_v2
from ouroboros.config._model_defaults import DEFAULT_OPUS_MODEL
from ouroboros.config.models import (
    CredentialsConfig,
    OuroborosConfig,
    ProviderCredentials,
    get_default_config,
    get_default_credentials,
)
from ouroboros.mcp.tools.execution_handlers import ExecuteSeedHandler
from ouroboros.providers.base import CompletionConfig
from ouroboros.providers.profiles import resolve_completion_profile
from ouroboros.router import Resolved, ResolveRequest, resolve_skill_dispatch
from ouroboros.skills.artifacts import resolve_packaged_skills_dir


def _terminate_and_reap_test_process(process: subprocess.Popen[str] | None) -> None:
    """Best-effort test cleanup that never replaces the triggering failure."""
    if process is None:
        return
    try:
        running = process.poll() is None
    except Exception:
        running = True
    if running:
        with suppress(Exception):
            process.terminate()
    try:
        process.communicate(timeout=5)
        return
    except Exception:
        pass
    with suppress(Exception):
        process.kill()
    with suppress(Exception):
        process.communicate(timeout=5)
    with suppress(Exception):
        process.wait(timeout=5)


# ── Codex setup tests ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("runtime", "env_key"),
    [
        ("claude", "OUROBOROS_CLI_PATH"),
        ("opencode", "OUROBOROS_OPENCODE_CLI_PATH"),
        ("hermes", "OUROBOROS_HERMES_CLI_PATH"),
    ],
)
def test_detect_runtimes_prefers_exact_override_over_stale_path(
    tmp_path: Path,
    runtime: str,
    env_key: str,
) -> None:
    configured = tmp_path / "configured" / f"{runtime}-wrapper"
    configured.parent.mkdir()
    configured.write_text("#!/bin/sh\n", encoding="utf-8")
    configured.chmod(0o755)

    def which(candidate: str) -> str | None:
        if candidate == str(configured):
            return str(configured)
        if candidate == runtime:
            return f"/stale/path/{runtime}"
        return None

    with (
        patch.dict(os.environ, {env_key: str(configured)}, clear=True),
        patch("ouroboros.cli.commands.setup.shutil.which", side_effect=which),
        patch("ouroboros.config.get_codex_cli_path", return_value=None),
        patch("ouroboros.cli.commands.setup._CODEX_APP_CLI_PATH", tmp_path / "missing-codex"),
    ):
        detected = setup_cmd._detect_runtimes()

    assert detected[runtime] == str(configured.resolve())


@pytest.mark.parametrize(
    ("runtime", "env_key"),
    [
        ("claude", "OUROBOROS_CLI_PATH"),
        ("opencode", "OUROBOROS_OPENCODE_CLI_PATH"),
        ("hermes", "OUROBOROS_HERMES_CLI_PATH"),
    ],
)
def test_detect_runtimes_invalid_override_does_not_use_stale_path(
    tmp_path: Path,
    runtime: str,
    env_key: str,
) -> None:
    missing = tmp_path / "missing" / f"{runtime}-wrapper"
    probes: list[str] = []

    def which(candidate: str) -> str | None:
        probes.append(candidate)
        return f"/stale/path/{runtime}" if candidate == runtime else None

    with (
        patch.dict(os.environ, {env_key: str(missing)}, clear=True),
        patch("ouroboros.cli.commands.setup.shutil.which", side_effect=which),
        patch("ouroboros.config.get_codex_cli_path", return_value=None),
        patch("ouroboros.cli.commands.setup._CODEX_APP_CLI_PATH", tmp_path / "missing-codex"),
    ):
        detected = setup_cmd._detect_runtimes()

    assert detected[runtime] is None
    assert runtime not in probes


class TestCodexSetup:
    """Tests for Codex-specific setup behavior."""

    def test_detect_runtimes_uses_bundled_codex_app_cli_when_path_is_missing(
        self, tmp_path: Path
    ) -> None:
        """Codex App-only users can complete setup without a PATH wrapper."""
        app_cli = tmp_path / "ChatGPT.app" / "Contents" / "Resources" / "codex"
        app_cli.parent.mkdir(parents=True)
        app_cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        app_cli.chmod(0o755)

        with (
            patch("ouroboros.cli.commands.setup.shutil.which", return_value=None),
            patch("ouroboros.cli.commands.setup._CODEX_APP_CLI_PATH", app_cli),
            patch("ouroboros.config.get_codex_cli_path", return_value=None),
        ):
            detected = setup_cmd._detect_runtimes()

        assert detected["codex"] == str(app_cli)

    def test_detect_runtimes_prefers_configured_codex_cli_path(self, tmp_path: Path) -> None:
        """A configured Codex executable must win over PATH and App fallback."""
        configured = tmp_path / "custom" / "codex"
        configured.parent.mkdir(parents=True)
        configured.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        configured.chmod(0o755)

        with (
            patch(
                "ouroboros.cli.commands.setup.shutil.which",
                side_effect=lambda name: "/usr/local/bin/codex" if name == "codex" else None,
            ),
            patch("ouroboros.config.get_codex_cli_path", return_value=str(configured)),
        ):
            detected = setup_cmd._detect_runtimes()

        assert detected["codex"] == str(configured)

    def test_detect_runtimes_expands_configured_codex_cli_path(self, tmp_path: Path) -> None:
        """Setup detection must accept the same ~/ path syntax runtime resolution accepts."""
        configured = tmp_path / "bin" / "codex-custom"
        configured.parent.mkdir(parents=True)
        configured.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        configured.chmod(0o755)

        with (
            patch.dict(os.environ, {"HOME": str(tmp_path)}),
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.cli.commands.setup.shutil.which", return_value=None),
            patch("ouroboros.config.get_codex_cli_path", return_value="~/bin/codex-custom"),
            patch("ouroboros.cli.commands.setup._CODEX_APP_CLI_PATH", tmp_path / "app-codex"),
        ):
            detected = setup_cmd._detect_runtimes()

        assert detected["codex"] == str(configured)

    def test_detect_runtimes_canonicalizes_relative_codex_cli_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Setup must not persist a workspace-relative Codex executable path."""
        configured = tmp_path / "tools" / "codex"
        configured.parent.mkdir(parents=True)
        configured.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        configured.chmod(0o755)

        monkeypatch.chdir(tmp_path)
        with (
            patch("ouroboros.cli.commands.setup.shutil.which", return_value=None),
            patch("ouroboros.config.get_codex_cli_path", return_value="tools/codex"),
            patch("ouroboros.cli.commands.setup._CODEX_APP_CLI_PATH", tmp_path / "app-codex"),
        ):
            detected = setup_cmd._detect_runtimes()

        assert detected["codex"] == str(configured)

    def test_detect_runtimes_canonicalizes_relative_path_codex_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A relative PATH hit must not be persisted relative to a later cwd."""
        configured = tmp_path / "tools" / "codex"
        configured.parent.mkdir(parents=True)
        configured.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        configured.chmod(0o755)

        monkeypatch.chdir(tmp_path)
        with (
            patch("ouroboros.cli.commands.setup.shutil.which", return_value="tools/codex"),
            patch("ouroboros.config.get_codex_cli_path", return_value=None),
            patch("ouroboros.cli.commands.setup._CODEX_APP_CLI_PATH", tmp_path / "app-codex"),
        ):
            detected = setup_cmd._detect_runtimes()

        assert detected["codex"] == str(configured)

    def test_detect_runtimes_rejects_stale_codex_env_before_path(self, tmp_path: Path) -> None:
        """A stale Codex env path must not be hidden by a valid PATH binary."""
        path_codex = tmp_path / "path" / "codex"
        path_codex.parent.mkdir(parents=True)
        path_codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path_codex.chmod(0o755)

        with (
            patch.dict(os.environ, {"OUROBOROS_CODEX_CLI_PATH": str(tmp_path / "missing")}),
            patch("ouroboros.cli.commands.setup.shutil.which", return_value=str(path_codex)),
            patch("ouroboros.cli.commands.setup._CODEX_APP_CLI_PATH", tmp_path / "app-codex"),
        ):
            detected = setup_cmd._detect_runtimes()

        assert detected["codex"] is None

    def test_codex_profile_provider_mapping_preserves_normalized_user_alias(self) -> None:
        """Setup must not shadow a user-owned Codex alias with a new canonical key."""
        profile = {"providers": {"CODEX_CLI": {"model": "user-pin"}}}

        provider = setup_cmd._ensure_codex_profile_provider_mapping(profile)

        assert provider == {"model": "user-pin"}
        assert "codex" not in profile["providers"]

    def test_codex_profile_provider_mapping_rejects_duplicate_aliases(self) -> None:
        """Existing canonical-plus-alias mappings are ambiguous and unsafe."""
        profile = {"providers": {"codex": {}, "codex_cli": {"model": "user-pin"}}}

        with pytest.raises(ValueError, match="Duplicate Codex provider aliases"):
            setup_cmd._ensure_codex_profile_provider_mapping(profile)

    def test_codex_profile_v2_detection_for_unified_profile_help(self) -> None:
        """Codex 0.134 uses --profile itself for profile-v2 files."""
        help_text = """
  -p, --profile <CONFIG_PROFILE_V2>
          Layer $CODEX_HOME/<name>.config.toml on top of the base user config
"""
        completed = subprocess.CompletedProcess(["codex", "--help"], 0, stdout=help_text, stderr="")

        with patch("ouroboros.cli.commands.setup.subprocess.run", return_value=completed):
            assert _codex_uses_profile_v2("/usr/local/bin/codex") is True

    def test_codex_profile_v2_detection_for_legacy_split_profile_help(self) -> None:
        """Codex 0.133 alpha keeps --profile legacy even when --profile-v2 exists."""
        help_text = """
  -p, --profile <CONFIG_PROFILE>
          Configuration profile from config.toml to specify default options

      --profile-v2 <CONFIG_PROFILE_V2>
          Layer $CODEX_HOME/<name>.config.toml on top of the base user config
"""
        completed = subprocess.CompletedProcess(["codex", "--help"], 0, stdout=help_text, stderr="")

        with patch("ouroboros.cli.commands.setup.subprocess.run", return_value=completed):
            assert _codex_uses_profile_v2("/Applications/Codex.app/codex") is False

    @pytest.mark.parametrize(
        "failure",
        (
            subprocess.TimeoutExpired(["codex", "--help"], timeout=5),
            OSError("cannot execute Codex help"),
        ),
    )
    def test_shared_codex_profile_detection_preserves_unknown_failures(
        self,
        failure: BaseException,
    ) -> None:
        """Help failures are unknown and must not be reported as legacy evidence."""

        def failing_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            raise failure

        assert codex_uses_profile_v2("/configured/codex", run_command=failing_run) is None

    def test_register_codex_mcp_server_writes_guidance_comment(self, tmp_path: Path) -> None:
        """The generated Codex config should explain the config file split."""
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "ouroboros.cli.commands.setup._is_source_tree_ouroboros_build", return_value=False
            ),
            patch("ouroboros.cli.commands.setup.importlib_metadata.version", return_value="0.38.2"),
            patch("ouroboros.cli.commands.setup.shutil.which", return_value="/usr/local/bin/uvx"),
        ):
            setup_cmd._register_codex_mcp_server()

        config_path = tmp_path / ".codex" / "config.toml"
        contents = config_path.read_text(encoding="utf-8")

        assert "Keep Ouroboros runtime settings and per-role model overrides in" in contents
        assert "~/.ouroboros/config.yaml" in contents
        assert "This file is only for the Codex MCP/env registration block." in contents
        assert "[mcp_servers.ouroboros]" in contents
        assert 'OUROBOROS_AGENT_RUNTIME = "codex"' in contents
        assert 'OUROBOROS_LLM_BACKEND = "codex"' in contents
        assert "tool_timeout_sec" not in contents
        assert 'command = "/usr/local/bin/uvx"' in contents
        assert (
            'args = ["--isolated", "--python", ">=3.12", "--from", "ouroboros-ai[mcp]", '
            '"ouroboros", "mcp", "serve"]' in contents
        )

    def test_register_codex_mcp_server_uses_direct_executable_for_dev_build(
        self, tmp_path: Path
    ) -> None:
        """Dev/git installs should not be rewritten to the latest PyPI uvx package."""
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "ouroboros.cli.commands.setup.importlib_metadata.version",
                return_value="0.38.3.dev110",
            ),
            patch(
                "ouroboros.cli.commands.setup._is_source_tree_ouroboros_build",
                return_value=False,
            ),
        ):
            setup_cmd._register_codex_mcp_server()

        contents = (tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8")

        assert f"command = {json.dumps(sys.executable)}" in contents
        assert (
            'args = ["-m", "ouroboros", "mcp", "serve", "--runtime", "codex", '
            '"--llm-backend", "codex"]'
        ) in contents
        assert 'command = "uvx"' not in contents

    def test_register_codex_mcp_server_uses_codex_home(self, tmp_path: Path, monkeypatch) -> None:
        """Setup must register the MCP server where the active Codex CLI reads it."""
        codex_home = tmp_path / "custom-codex-home"
        monkeypatch.setenv("CODEX_HOME", str(codex_home))

        with (
            patch(
                "ouroboros.cli.commands.setup._is_source_tree_ouroboros_build", return_value=False
            ),
            patch("ouroboros.cli.commands.setup.importlib_metadata.version", return_value="0.38.2"),
        ):
            setup_cmd._register_codex_mcp_server()

        assert (codex_home / "config.toml").is_file()
        assert not (tmp_path / ".codex" / "config.toml").exists()

    def test_register_codex_mcp_server_refreshes_stale_dev_module_entry(
        self, tmp_path: Path
    ) -> None:
        """Setup-owned dev module configs should be repairable after venv moves."""
        codex_config = tmp_path / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text(
            "\n".join(
                [
                    "# Ouroboros MCP hookup for Codex CLI.",
                    "# Keep Ouroboros runtime settings and per-role model overrides in",
                    "# ~/.ouroboros/config.yaml (for example: clarification.default_model,",
                    "# llm.qa_model, evaluation.semantic_model, consensus.*).",
                    "# This file is only for the Codex MCP/env registration block.",
                    "",
                    "[mcp_servers.ouroboros]",
                    'command = "/stale/venv/bin/python"',
                    (
                        'args = ["-m", "ouroboros", "mcp", "serve", "--runtime", '
                        '"codex", "--llm-backend", "codex"]'
                    ),
                    "",
                ]
            ),
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "ouroboros.cli.commands.setup.importlib_metadata.version",
                return_value="0.38.3.dev110",
            ),
        ):
            setup_cmd._register_codex_mcp_server()

        contents = codex_config.read_text(encoding="utf-8")

        assert f"command = {json.dumps(sys.executable)}" in contents
        assert "/stale/venv/bin/python" not in contents

    def test_register_codex_mcp_server_preserves_operator_comment_in_legacy_uvx_table(
        self,
        tmp_path: Path,
    ) -> None:
        """Refreshing a managed legacy table must not discard operator notes."""
        codex_config = tmp_path / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text(
            "\n".join(
                [
                    "[mcp_servers.ouroboros]",
                    'command = "uvx"',
                    "# keep this note for local support",
                    'args = ["--from", "ouroboros-ai[mcp]", "ouroboros", "mcp", "serve"]',
                    "",
                    "[projects.example]",
                    'trust_level = "trusted"',
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        with patch("pathlib.Path.home", return_value=tmp_path):
            setup_cmd._register_codex_mcp_server()

        contents = codex_config.read_text(encoding="utf-8")
        assert "# keep this note for local support" in contents
        assert contents.index("# keep this note for local support") < contents.index(
            "[projects.example]"
        )

    def test_register_codex_mcp_server_replaces_quoted_managed_table(
        self,
        tmp_path: Path,
    ) -> None:
        """Quoted TOML table keys must be recognized as the same managed MCP section."""
        codex_config = tmp_path / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text(
            "\n".join(
                [
                    "# Ouroboros MCP hookup for Codex CLI.",
                    "# Keep Ouroboros runtime settings and per-role model overrides in",
                    "# ~/.ouroboros/config.yaml (for example: clarification.default_model,",
                    "# llm.qa_model, evaluation.semantic_model, consensus.*).",
                    "# This file is only for the Codex MCP/env registration block.",
                    "",
                    '[mcp_servers."ouroboros"]',
                    'command = "uvx"',
                    'args = ["--from", "ouroboros-ai[mcp]", "ouroboros", "mcp", "serve"]',
                    "",
                    '[mcp_servers."ouroboros".env]',
                    'OUROBOROS_AGENT_RUNTIME = "codex"',
                    'OUROBOROS_LLM_BACKEND = "codex"',
                    "",
                ]
            ),
            encoding="utf-8",
        )

        with patch("pathlib.Path.home", return_value=tmp_path):
            assert setup_cmd._register_codex_mcp_server() is True

        contents = codex_config.read_text(encoding="utf-8")
        parsed = tomllib.loads(contents)

        assert contents.count("[mcp_servers.ouroboros]") == 1
        assert '[mcp_servers."ouroboros"]' not in contents
        assert parsed["mcp_servers"]["ouroboros"]["env"]["OUROBOROS_AGENT_RUNTIME"] == "codex"

    def test_register_codex_mcp_server_preserves_following_array_tables(
        self,
        tmp_path: Path,
    ) -> None:
        """Array-of-tables after the managed MCP block are unrelated TOML boundaries."""
        codex_config = tmp_path / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text(
            "\n".join(
                [
                    "[mcp_servers.ouroboros]",
                    'command = "uvx"',
                    "",
                    "[[custom_hooks]]",
                    'name = "after-mcp"',
                    "",
                ]
            ),
            encoding="utf-8",
        )

        with patch("pathlib.Path.home", return_value=tmp_path):
            setup_cmd._register_codex_mcp_server()

        contents = codex_config.read_text(encoding="utf-8")
        parsed = tomllib.loads(contents)

        assert parsed["custom_hooks"][0]["name"] == "after-mcp"
        assert "[[custom_hooks]]" in contents

    def test_register_codex_mcp_server_refreshes_legacy_direct_dev_entry(
        self, tmp_path: Path
    ) -> None:
        """Earlier setup-owned base-argv executable configs should not become stuck."""
        codex_config = tmp_path / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text(
            "\n".join(
                [
                    "# Ouroboros MCP hookup for Codex CLI.",
                    "# Keep Ouroboros runtime settings and per-role model overrides in",
                    "# ~/.ouroboros/config.yaml (for example: clarification.default_model,",
                    "# llm.qa_model, evaluation.semantic_model, consensus.*).",
                    "# This file is only for the Codex MCP/env registration block.",
                    "",
                    "[mcp_servers.ouroboros]",
                    'command = "/old/venv/bin/ouroboros"',
                    'args = ["mcp", "serve"]',
                    "",
                ]
            ),
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "ouroboros.cli.commands.setup.importlib_metadata.version",
                return_value="0.38.3.dev110",
            ),
        ):
            setup_cmd._register_codex_mcp_server()

        contents = codex_config.read_text(encoding="utf-8")

        assert f"command = {json.dumps(sys.executable)}" in contents
        assert "/old/venv/bin/ouroboros" not in contents

    def test_register_codex_mcp_server_refreshes_path_selected_legacy_direct_entry(
        self, tmp_path: Path
    ) -> None:
        """A canonical PATH launcher and Codex env are a safe legacy migration seam."""
        codex_config = tmp_path / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text(
            "\n".join(
                [
                    "[mcp_servers.ouroboros]",
                    'command = "/home/operator/.local/bin/ouroboros"',
                    'args = ["mcp", "serve"]',
                    "",
                    "[mcp_servers.ouroboros.env]",
                    'OUROBOROS_AGENT_RUNTIME = "codex"',
                    'OUROBOROS_LLM_BACKEND = "codex"',
                    "",
                ]
            ),
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "ouroboros.cli.commands.setup._is_source_tree_ouroboros_build",
                return_value=False,
            ),
            patch(
                "ouroboros.cli.commands.setup.importlib_metadata.version",
                return_value="0.51.4",
            ),
            patch(
                "ouroboros.cli.commands.setup._command_matches_path_program",
                return_value=True,
            ),
        ):
            assert setup_cmd._register_codex_mcp_server()

        contents = codex_config.read_text(encoding="utf-8")
        assert "/home/operator/.local/bin/ouroboros" not in contents
        assert "ouroboros-ai[mcp]" in contents

    def test_register_codex_mcp_server_preserves_path_direct_with_mismatched_env(
        self, tmp_path: Path
    ) -> None:
        """Canonical argv does not make a launcher with foreign env setup-owned."""
        selected = tmp_path / "bin" / "ouroboros"
        selected.parent.mkdir()
        selected.touch()
        codex_config = tmp_path / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        original = (
            "[mcp_servers.ouroboros]\n"
            f"command = {json.dumps(str(selected))}\n"
            'args = ["mcp", "serve"]\n'
            "[mcp_servers.ouroboros.env]\n"
            'OUROBOROS_AGENT_RUNTIME = "claude"\n'
            'OUROBOROS_LLM_BACKEND = "codex"\n'
        )
        codex_config.write_text(original, encoding="utf-8")

        def which(command: str) -> str | None:
            return str(selected) if command in {"ouroboros", str(selected)} else None

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.cli.commands.setup.shutil.which", side_effect=which),
            patch(
                "ouroboros.cli.commands.setup._render_codex_mcp_section",
                return_value='[mcp_servers.ouroboros]\ncommand = "uvx"\nargs = []\n',
            ),
        ):
            assert setup_cmd._register_codex_mcp_server()

        assert codex_config.read_text(encoding="utf-8") == original

    def test_register_codex_mcp_server_preserves_direct_not_selected_by_path(
        self, tmp_path: Path
    ) -> None:
        """Canonical base argv stays user-managed when PATH selects another executable."""
        configured = tmp_path / "custom" / "ouroboros"
        selected = tmp_path / "path" / "ouroboros"
        configured.parent.mkdir()
        selected.parent.mkdir()
        configured.touch()
        selected.touch()
        codex_config = tmp_path / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        original = (
            "[mcp_servers.ouroboros]\n"
            f"command = {json.dumps(str(configured))}\n"
            'args = ["mcp", "serve"]\n'
            "[mcp_servers.ouroboros.env]\n"
            'OUROBOROS_AGENT_RUNTIME = "codex"\n'
            'OUROBOROS_LLM_BACKEND = "codex"\n'
        )
        codex_config.write_text(original, encoding="utf-8")

        def which(command: str) -> str | None:
            if command == "ouroboros":
                return str(selected)
            return str(configured) if command == str(configured) else None

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.cli.commands.setup.shutil.which", side_effect=which),
            patch(
                "ouroboros.cli.commands.setup._render_codex_mcp_section",
                return_value='[mcp_servers.ouroboros]\ncommand = "uvx"\nargs = []\n',
            ),
        ):
            assert setup_cmd._register_codex_mcp_server()

        assert codex_config.read_text(encoding="utf-8") == original

    def test_register_codex_mcp_server_preserves_path_direct_with_process_controls(
        self, tmp_path: Path
    ) -> None:
        """Process controls keep an otherwise canonical direct entry user-managed."""
        codex_config = tmp_path / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        original = (
            "[mcp_servers.ouroboros]\n"
            'command = "/home/operator/.local/bin/ouroboros"\n'
            'args = ["mcp", "serve"]\n'
            "tool_timeout_sec = 600\n"
            "[mcp_servers.ouroboros.env]\n"
            'OUROBOROS_AGENT_RUNTIME = "codex"\n'
            'OUROBOROS_LLM_BACKEND = "codex"\n'
        )
        codex_config.write_text(original, encoding="utf-8")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "ouroboros.cli.commands.setup._command_matches_path_program",
                return_value=True,
            ),
        ):
            assert setup_cmd._register_codex_mcp_server()

        assert codex_config.read_text(encoding="utf-8") == original

    def test_register_codex_mcp_server_preserves_custom_python_module_by_default(
        self, tmp_path: Path
    ) -> None:
        """User-pinned Python module configs are preserved without the managed comment."""
        codex_config = tmp_path / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text(
            "\n".join(
                [
                    "[mcp_servers.ouroboros]",
                    'command = "/custom/venv/bin/python"',
                    (
                        'args = ["-m", "ouroboros", "mcp", "serve", "--runtime", '
                        '"codex", "--llm-backend", "codex"]'
                    ),
                    "",
                ]
            ),
            encoding="utf-8",
        )

        with patch("pathlib.Path.home", return_value=tmp_path):
            setup_cmd._register_codex_mcp_server()

        contents = codex_config.read_text(encoding="utf-8")
        assert 'command = "/custom/venv/bin/python"' in contents
        assert f"command = {json.dumps(sys.executable)}" not in contents

    def test_register_codex_mcp_server_uses_current_python_for_source_tree(
        self, tmp_path: Path
    ) -> None:
        """Source-tree runs should not fall back to the PyPI uvx MCP server."""
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "ouroboros.cli.commands.setup._is_source_tree_ouroboros_build",
                return_value=True,
            ),
            patch(
                "ouroboros.cli.commands.setup.importlib_metadata.version",
                side_effect=setup_cmd.importlib_metadata.PackageNotFoundError,
            ),
        ):
            setup_cmd._register_codex_mcp_server()

        contents = (tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8")
        assert f"command = {json.dumps(sys.executable)}" in contents
        assert 'command = "uvx"' not in contents

    def test_register_codex_mcp_server_keeps_release_uvx_inside_repo_venv(
        self, tmp_path: Path
    ) -> None:
        """A wheel installed in a repo-local venv is still a release install."""
        repo = tmp_path / "repo"
        source_package = repo / "src" / "ouroboros"
        wheel_package = repo / ".venv" / "lib" / "python3.12" / "site-packages" / "ouroboros"
        source_package.mkdir(parents=True)
        (repo / "pyproject.toml").write_text('name = "ouroboros-ai"\n', encoding="utf-8")
        wheel_setup = wheel_package / "cli" / "commands" / "setup.py"
        wheel_setup.parent.mkdir(parents=True)
        wheel_setup.write_text("# installed wheel module\n", encoding="utf-8")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.cli.commands.setup.__file__", str(wheel_setup)),
            patch("ouroboros.cli.commands.setup.importlib_metadata.version", return_value="0.38.2"),
            patch("ouroboros.cli.commands.setup.shutil.which", return_value="/usr/local/bin/uvx"),
        ):
            setup_cmd._register_codex_mcp_server()

        contents = (tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8")
        assert 'command = "/usr/local/bin/uvx"' in contents
        assert f"command = {json.dumps(sys.executable)}" not in contents

    def test_register_codex_mcp_server_preserves_customized_legacy_uvx_by_default(
        self,
        tmp_path: Path,
    ) -> None:
        """Auto mode must not delete custom fields from legacy-looking uvx entries."""
        codex_config = tmp_path / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text(
            "\n".join(
                [
                    "[mcp_servers.other]",
                    'command = "custom"',
                    "",
                    "# Ouroboros MCP hookup for Codex CLI.",
                    "[mcp_servers.ouroboros]",
                    'command = "uvx"',
                    'args = ["--from", "ouroboros-ai", "ouroboros", "mcp", "serve"]',
                    "tool_timeout_sec = 600",
                    "",
                    "[mcp_servers.ouroboros.env]",
                    'OUROBOROS_AGENT_RUNTIME = "claude"',
                    "",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        with patch("pathlib.Path.home", return_value=tmp_path):
            setup_cmd._register_codex_mcp_server()

        contents = codex_config.read_text(encoding="utf-8")

        assert "[mcp_servers.other]" in contents
        assert contents.count("[mcp_servers.ouroboros]") == 1
        assert contents.count("[mcp_servers.ouroboros.env]") == 1
        assert 'OUROBOROS_AGENT_RUNTIME = "claude"' in contents
        assert "tool_timeout_sec = 600" in contents

    def test_register_codex_mcp_server_preserves_url_config_by_default(
        self,
        tmp_path: Path,
    ) -> None:
        """URL-based Codex MCP configs are user-managed and preserved in auto mode."""
        codex_config = tmp_path / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text(
            '[mcp_servers.ouroboros]\nurl = "http://127.0.0.1:12000/mcp"\n',
            encoding="utf-8",
        )

        with patch("pathlib.Path.home", return_value=tmp_path):
            setup_cmd._register_codex_mcp_server()

        contents = codex_config.read_text(encoding="utf-8")
        assert 'url = "http://127.0.0.1:12000/mcp"' in contents
        assert 'command = "uvx"' not in contents
        assert "[mcp_servers.ouroboros.env]" not in contents

    def test_register_codex_mcp_server_preserves_custom_command_by_default(
        self,
        tmp_path: Path,
    ) -> None:
        """Custom command-based Codex MCP configs are preserved in auto mode."""
        codex_config = tmp_path / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text(
            "[mcp_servers.ouroboros]\n"
            'command = "/tmp/ouroboros/.venv/bin/ouroboros"\n'
            'args = ["mcp", "serve"]\n',
            encoding="utf-8",
        )

        with patch("pathlib.Path.home", return_value=tmp_path):
            setup_cmd._register_codex_mcp_server()

        contents = codex_config.read_text(encoding="utf-8")
        assert 'command = "/tmp/ouroboros/.venv/bin/ouroboros"' in contents
        assert 'command = "uvx"' not in contents

    def test_register_codex_mcp_server_replaces_managed_inline_entry(
        self,
        tmp_path: Path,
    ) -> None:
        """Inline `[mcp_servers] ouroboros = {...}` entries must not duplicate."""
        codex_config = tmp_path / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text(
            "\n".join(
                [
                    "[mcp_servers]",
                    'other = { command = "other" }',
                    (
                        'ouroboros = { command = "uvx", args = ["--from", '
                        '"ouroboros-ai", "ouroboros", "mcp", "serve"] }'
                    ),
                    "",
                ]
            ),
            encoding="utf-8",
        )

        with patch("pathlib.Path.home", return_value=tmp_path):
            assert setup_cmd._register_codex_mcp_server()

        contents = codex_config.read_text(encoding="utf-8")
        parsed = tomllib.loads(contents)

        assert parsed["mcp_servers"]["other"]["command"] == "other"
        assert parsed["mcp_servers"]["ouroboros"]["command"]
        assert contents.count("ouroboros = {") == 0
        assert contents.count("[mcp_servers.ouroboros]") == 1

    def test_register_codex_mcp_server_replaces_managed_root_dotted_entry(
        self,
        tmp_path: Path,
    ) -> None:
        """Root dotted `mcp_servers.ouroboros = {...}` entries must not duplicate."""
        codex_config = tmp_path / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text(
            "\n".join(
                [
                    'mcp_servers.other = { command = "other" }',
                    (
                        'mcp_servers.ouroboros = { command = "uvx", args = ["--from", '
                        '"ouroboros-ai", "ouroboros", "mcp", "serve"] }'
                    ),
                    "",
                ]
            ),
            encoding="utf-8",
        )

        with patch("pathlib.Path.home", return_value=tmp_path):
            assert setup_cmd._register_codex_mcp_server()

        contents = codex_config.read_text(encoding="utf-8")
        parsed = tomllib.loads(contents)

        assert parsed["mcp_servers"]["other"]["command"] == "other"
        assert parsed["mcp_servers"]["ouroboros"]["command"]
        assert "mcp_servers.ouroboros =" not in contents
        assert contents.count("[mcp_servers.ouroboros]") == 1

    def test_register_codex_mcp_server_removes_multiline_root_dotted_entry(
        self,
        tmp_path: Path,
    ) -> None:
        """Root dotted MCP assignments must be removed as whole TOML spans."""
        codex_config = tmp_path / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text(
            "\n".join(
                [
                    'mcp_servers.other = { command = "other" }',
                    'mcp_servers.ouroboros.command = "uvx"',
                    "mcp_servers.ouroboros.args = [",
                    '  "--from",',
                    '  "ouroboros-ai",',
                    '  "ouroboros",',
                    '  "mcp",',
                    '  "serve",',
                    "]",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        with patch("pathlib.Path.home", return_value=tmp_path):
            assert setup_cmd._register_codex_mcp_server()

        contents = codex_config.read_text(encoding="utf-8")
        parsed = tomllib.loads(contents)

        assert parsed["mcp_servers"]["other"]["command"] == "other"
        assert parsed["mcp_servers"]["ouroboros"]["command"]
        assert "--from" not in contents
        assert contents.count("[mcp_servers.ouroboros]") == 1

    def test_register_codex_mcp_server_rewrites_root_inline_table_entry(
        self,
        tmp_path: Path,
    ) -> None:
        """Root inline MCP mappings must be refreshable without losing siblings."""
        codex_config = tmp_path / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text(
            'mcp_servers = { other = { command = "other" }, ouroboros = { command = "uvx", '
            'args = ["--from", "ouroboros-ai", "ouroboros", "mcp", "serve"] } }\n',
            encoding="utf-8",
        )

        with patch("pathlib.Path.home", return_value=tmp_path):
            assert setup_cmd._register_codex_mcp_server()

        contents = codex_config.read_text(encoding="utf-8")
        parsed = tomllib.loads(contents)

        assert parsed["mcp_servers"]["other"]["command"] == "other"
        assert parsed["mcp_servers"]["ouroboros"]["command"]
        assert "mcp_servers = {" not in contents
        assert contents.count("[mcp_servers.ouroboros]") == 1

    def test_register_codex_mcp_server_preserves_non_bmp_inline_values(
        self,
        tmp_path: Path,
    ) -> None:
        """TOML rewrites emit Unicode scalars instead of invalid surrogate pairs."""
        codex_config = tmp_path / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text(
            'mcp_servers = { other = { command = "😀" }, ouroboros = { command = "uvx", '
            'args = ["--from", "ouroboros-ai", "ouroboros", "mcp", "serve"] } }\n',
            encoding="utf-8",
        )

        with patch("pathlib.Path.home", return_value=tmp_path):
            assert setup_cmd._register_codex_mcp_server()

        contents = codex_config.read_text(encoding="utf-8")
        parsed = tomllib.loads(contents)

        assert parsed["mcp_servers"]["other"]["command"] == "😀"
        assert "\\ud83d" not in contents.lower()

    def test_register_codex_mcp_server_preserves_structure_like_multiline_text(
        self,
        tmp_path: Path,
    ) -> None:
        """Header and assignment text inside strings must remain user content."""
        codex_config = tmp_path / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        instructions = (
            'operator notes\n[mcp_servers]\nouroboros = { command = "do-not-remove", args = [] }'
        )
        codex_config.write_text(
            'instructions = """operator notes\n[mcp_servers]\n'
            'ouroboros = { command = "do-not-remove", args = [] }"""\n',
            encoding="utf-8",
        )

        with patch("pathlib.Path.home", return_value=tmp_path):
            assert setup_cmd._register_codex_mcp_server()

        contents = codex_config.read_text(encoding="utf-8")
        parsed = tomllib.loads(contents)

        assert parsed["instructions"] == instructions
        assert parsed["mcp_servers"]["ouroboros"]["command"] != "do-not-remove"

    def test_register_codex_mcp_server_stdio_repairs_endpointless_entry(
        self,
        tmp_path: Path,
    ) -> None:
        """`--mcp-mode stdio` is the repair path for stale endpoint-less entries."""
        codex_config = tmp_path / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text(
            "\n".join(
                [
                    "[mcp_servers]",
                    'ouroboros = { env = { OUROBOROS_AGENT_RUNTIME = "codex" } }',
                    "",
                ]
            ),
            encoding="utf-8",
        )

        with patch("pathlib.Path.home", return_value=tmp_path):
            assert setup_cmd._register_codex_mcp_server(mode="stdio")

        parsed = tomllib.loads(codex_config.read_text(encoding="utf-8"))
        assert parsed["mcp_servers"]["ouroboros"]["command"]

    def test_register_codex_mcp_server_preserves_user_pinned_uvx_from(self, tmp_path: Path) -> None:
        """A user-pinned uvx --from fork is not a setup-owned legacy entry."""
        codex_config = tmp_path / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text(
            "[mcp_servers.ouroboros]\n"
            'command = "uvx"\n'
            'args = ["--from", "/opt/private/ouroboros-fork", "ouroboros", "mcp", "serve"]\n',
            encoding="utf-8",
        )

        with patch("pathlib.Path.home", return_value=tmp_path):
            setup_cmd._register_codex_mcp_server()

        contents = codex_config.read_text(encoding="utf-8")
        assert "/opt/private/ouroboros-fork" in contents
        assert "ouroboros-ai[mcp]" not in contents

    def test_register_codex_mcp_server_preserves_commented_user_pinned_uvx_from(
        self, tmp_path: Path
    ) -> None:
        """The managed comment alone must not authorize overwriting edited uvx pins."""
        codex_config = tmp_path / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text(
            "\n".join(
                [
                    "# Ouroboros MCP hookup for Codex CLI.",
                    "# Keep Ouroboros runtime settings and per-role model overrides in",
                    "# ~/.ouroboros/config.yaml (for example: clarification.default_model,",
                    "# llm.qa_model, evaluation.semantic_model, consensus.*).",
                    "# This file is only for the Codex MCP/env registration block.",
                    "",
                    "[mcp_servers.ouroboros]",
                    'command = "uvx"',
                    (
                        'args = ["--from", "/opt/private/ouroboros-fork", '
                        '"ouroboros", "mcp", "serve"]'
                    ),
                    "",
                ]
            ),
            encoding="utf-8",
        )

        with patch("pathlib.Path.home", return_value=tmp_path):
            setup_cmd._register_codex_mcp_server()

        contents = codex_config.read_text(encoding="utf-8")
        assert "/opt/private/ouroboros-fork" in contents
        assert "ouroboros-ai[mcp]" not in contents

    def test_register_codex_mcp_server_stdio_mode_replaces_url_config(
        self,
        tmp_path: Path,
    ) -> None:
        """Explicit stdio mode replaces a user-managed URL config."""
        codex_config = tmp_path / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text(
            '[mcp_servers.ouroboros]\nurl = "http://127.0.0.1:12000/mcp"\n',
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "ouroboros.cli.commands.setup._is_source_tree_ouroboros_build", return_value=False
            ),
            patch("ouroboros.cli.commands.setup.importlib_metadata.version", return_value="0.38.2"),
            patch("ouroboros.cli.commands.setup.shutil.which", return_value="/usr/local/bin/uvx"),
        ):
            setup_cmd._register_codex_mcp_server(mode="stdio")

        contents = codex_config.read_text(encoding="utf-8")
        assert 'url = "http://127.0.0.1:12000/mcp"' not in contents
        assert 'command = "/usr/local/bin/uvx"' in contents
        assert "[mcp_servers.ouroboros.env]" in contents

    def test_register_codex_mcp_server_preserve_mode_does_not_create_config(
        self,
        tmp_path: Path,
    ) -> None:
        """Preserve mode skips MCP config changes entirely."""
        with patch("pathlib.Path.home", return_value=tmp_path):
            setup_cmd._register_codex_mcp_server(mode="preserve")

        assert not (tmp_path / ".codex" / "config.toml").exists()

    def test_register_codex_default_profiles_writes_profile_anchors(
        self,
        tmp_path: Path,
    ) -> None:
        """Codex setup should create sparse profile anchors for Ouroboros roles."""
        with patch("pathlib.Path.home", return_value=tmp_path):
            setup_cmd._register_codex_default_profiles()

        config_path = tmp_path / ".codex" / "config.toml"
        contents = config_path.read_text(encoding="utf-8")

        assert "[profiles.ouroboros-fast]" in contents
        assert 'model_reasoning_effort = "low"' in contents
        assert "[profiles.ouroboros-standard]" in contents
        assert 'model_reasoning_effort = "medium"' in contents
        assert "[profiles.ouroboros-deep]" in contents
        assert 'model_reasoning_effort = "high"' in contents
        assert "[profiles.ouroboros-frontier]" in contents
        assert 'model_reasoning_effort = "xhigh"' in contents
        assert 'model = "' not in contents

    def test_retire_codex_default_profiles_removes_only_untouched_v2_files(
        self, tmp_path: Path
    ) -> None:
        """Per-run effort supersedes generated task anchors but not user edits."""
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        generated = codex_dir / "ouroboros-fast.config.toml"
        generated.write_text(
            setup_cmd._render_codex_profile_v2_file(
                setup_cmd._CODEX_DEFAULT_PROFILE_SECTIONS["ouroboros-fast"]
            ),
            encoding="utf-8",
        )
        customized = codex_dir / "ouroboros-deep.config.toml"
        customized.write_text('model = "custom-model"\n', encoding="utf-8")

        with patch("pathlib.Path.home", return_value=tmp_path):
            setup_cmd._retire_codex_default_profiles()

        assert not generated.exists()
        assert customized.read_text(encoding="utf-8") == 'model = "custom-model"\n'

    def test_migrates_untouched_legacy_profile_mapping_before_retiring_anchor(
        self, tmp_path: Path
    ) -> None:
        """Existing setup users move config and generated anchor as one safe migration."""
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        (codex_dir / "ouroboros-fast.config.toml").write_text(
            setup_cmd._render_codex_profile_v2_file(
                setup_cmd._CODEX_DEFAULT_PROFILE_SECTIONS["ouroboros-fast"]
            ),
            encoding="utf-8",
        )
        config_dict = {
            "llm_profiles": {"fast": {"providers": {"codex": {"profile": "ouroboros-fast"}}}}
        }

        with patch("pathlib.Path.home", return_value=tmp_path):
            assert setup_cmd._migrate_legacy_codex_profile_mappings(config_dict) == ["fast"]
            setup_cmd._retire_codex_default_profiles(
                protected_profile_names=setup_cmd._referenced_legacy_codex_profiles(config_dict)
            )

        assert config_dict["llm_profiles"]["fast"]["providers"]["codex"] == {
            "reasoning_effort": "low"
        }
        assert not (codex_dir / "ouroboros-fast.config.toml").exists()

    def test_preserves_customized_legacy_profile_mapping_and_anchor(self, tmp_path: Path) -> None:
        """A user model pin in a legacy anchor stays active after setup."""
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        anchor = codex_dir / "ouroboros-fast.config.toml"
        anchor.write_text('model = "terra"\n', encoding="utf-8")
        config_dict = {
            "llm_profiles": {"fast": {"providers": {"codex": {"profile": "ouroboros-fast"}}}}
        }

        with patch("pathlib.Path.home", return_value=tmp_path):
            assert setup_cmd._migrate_legacy_codex_profile_mappings(config_dict) == []
            assert setup_cmd._referenced_legacy_codex_profiles(config_dict) == {"ouroboros-fast"}
            setup_cmd._retire_codex_default_profiles(
                protected_profile_names=setup_cmd._referenced_legacy_codex_profiles(config_dict)
            )

        assert anchor.read_text(encoding="utf-8") == 'model = "terra"\n'

    def test_preserves_generated_anchor_referenced_through_codex_cli_alias(
        self, tmp_path: Path
    ) -> None:
        """A valid Codex alias must prevent retirement of its live profile-v2 file."""
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        anchor = codex_dir / "ouroboros-fast.config.toml"
        generated = setup_cmd._render_codex_profile_v2_file(
            setup_cmd._CODEX_DEFAULT_PROFILE_SECTIONS["ouroboros-fast"]
        )
        anchor.write_text(generated, encoding="utf-8")
        config_dict = {
            "llm_profiles": {"fast": {"providers": {"codex_cli": {"profile": "ouroboros-fast"}}}}
        }

        with patch("pathlib.Path.home", return_value=tmp_path):
            protected = setup_cmd._referenced_legacy_codex_profiles(config_dict)
            assert protected == {"ouroboros-fast"}
            setup_cmd._retire_codex_default_profiles(protected_profile_names=protected)

        assert anchor.read_text(encoding="utf-8") == generated

    def test_register_codex_default_profiles_preserves_existing_profile(
        self,
        tmp_path: Path,
    ) -> None:
        """Setup should not overwrite user-customized Codex profile anchors."""
        codex_config = tmp_path / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text(
            "\n".join(
                [
                    "[profiles.ouroboros-fast]",
                    'model = "custom-cheap-model"',
                    'model_reasoning_effort = "medium"',
                    "",
                    "[profiles.user-profile]",
                    'model = "custom-model"',
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        with patch("pathlib.Path.home", return_value=tmp_path):
            setup_cmd._register_codex_default_profiles()

        contents = codex_config.read_text(encoding="utf-8")

        assert contents.count("[profiles.ouroboros-fast]") == 1
        assert 'model = "custom-cheap-model"' in contents
        assert "[profiles.user-profile]" in contents
        assert "[profiles.ouroboros-standard]" in contents
        assert "[profiles.ouroboros-deep]" in contents
        assert "[profiles.ouroboros-frontier]" in contents

    def test_register_codex_default_profiles_writes_profile_v2_files(
        self,
        tmp_path: Path,
    ) -> None:
        """Current Codex CLI profile mode should write profile-v2 files."""
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.cli.commands.setup._codex_uses_profile_v2", return_value=True),
        ):
            setup_cmd._register_codex_default_profiles(codex_path="/usr/local/bin/codex")

        codex_dir = tmp_path / ".codex"
        config_path = codex_dir / "config.toml"

        assert not config_path.exists()
        assert (codex_dir / "ouroboros-fast.config.toml").read_text(encoding="utf-8").count(
            'model_reasoning_effort = "low"'
        ) == 1
        assert 'model_reasoning_effort = "medium"' in (
            codex_dir / "ouroboros-standard.config.toml"
        ).read_text(encoding="utf-8")
        assert 'model_reasoning_effort = "high"' in (
            codex_dir / "ouroboros-deep.config.toml"
        ).read_text(encoding="utf-8")
        assert 'model_reasoning_effort = "xhigh"' in (
            codex_dir / "ouroboros-frontier.config.toml"
        ).read_text(encoding="utf-8")

    def test_register_codex_default_profiles_migrates_legacy_tables_to_profile_v2(
        self,
        tmp_path: Path,
    ) -> None:
        """profile-v2 setup removes legacy anchors that current Codex rejects."""
        codex_config = tmp_path / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text(
            "\n".join(
                [
                    "[mcp_servers.ouroboros]",
                    'command = "uvx"',
                    'args = ["--from", "ouroboros-ai[mcp]", "ouroboros", "mcp", "serve"]',
                    "",
                    "[profiles.ouroboros-fast]",
                    'model = "custom-cheap-model"',
                    'model_reasoning_effort = "medium"',
                    "",
                    "[profiles.user-profile]",
                    'model = "custom-model"',
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.cli.commands.setup._codex_uses_profile_v2", return_value=True),
        ):
            setup_cmd._register_codex_default_profiles(codex_path="/usr/local/bin/codex")

        contents = codex_config.read_text(encoding="utf-8")
        fast_profile = (tmp_path / ".codex" / "ouroboros-fast.config.toml").read_text(
            encoding="utf-8"
        )

        assert "[mcp_servers.ouroboros]" in contents
        assert "[profiles.ouroboros-fast]" not in contents
        assert "[profiles.user-profile]" in contents
        assert 'model = "custom-cheap-model"' in fast_profile
        assert 'model_reasoning_effort = "medium"' in fast_profile

    def test_register_codex_default_profiles_keeps_legacy_table_when_v2_file_exists(
        self,
        tmp_path: Path,
    ) -> None:
        """Ambiguous legacy+v2 state should preserve the legacy copy of user settings."""
        codex_dir = tmp_path / ".codex"
        codex_config = codex_dir / "config.toml"
        codex_dir.mkdir(parents=True)
        codex_config.write_text(
            "\n".join(
                [
                    "[profiles.ouroboros-fast]",
                    'model = "custom-cheap-model"',
                    'model_reasoning_effort = "medium"',
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (codex_dir / "ouroboros-fast.config.toml").write_text(
            'model_reasoning_effort = "low"\n',
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.cli.commands.setup._codex_uses_profile_v2", return_value=True),
            patch("ouroboros.cli.commands.setup.print_warning") as mock_warning,
        ):
            setup_cmd._register_codex_default_profiles(codex_path="/usr/local/bin/codex")

        contents = codex_config.read_text(encoding="utf-8")
        fast_profile = (codex_dir / "ouroboros-fast.config.toml").read_text(encoding="utf-8")

        assert "[profiles.ouroboros-fast]" in contents
        assert 'model = "custom-cheap-model"' in contents
        assert 'model_reasoning_effort = "low"' in fast_profile
        mock_warning.assert_called_once()
        warning = mock_warning.call_args.args[0]
        assert "Preserved legacy Codex profile table(s)" in warning
        assert "ouroboros-fast" in warning
        assert "manually reconcile" in warning

    def test_register_codex_worker_profile_writes_section(self, tmp_path: Path) -> None:
        """First-time setup creates the [profiles.ouroboros-worker] block."""
        with patch("pathlib.Path.home", return_value=tmp_path):
            setup_cmd._register_codex_worker_profile()

        contents = (tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8")

        assert "[profiles.ouroboros-worker]" in contents
        assert "Ouroboros Agent OS runtime profile for Codex worker subprocesses." in contents
        assert "orchestrator.runtime_profile.backend_profile: worker" in contents

    def test_register_codex_worker_profile_preserves_mcp_and_default_profiles(
        self, tmp_path: Path
    ) -> None:
        """Worker-profile registration must not touch existing MCP/profile anchors."""
        with patch("pathlib.Path.home", return_value=tmp_path):
            setup_cmd._register_codex_mcp_server()
            setup_cmd._register_codex_default_profiles()
            setup_cmd._register_codex_worker_profile()

        contents = (tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8")

        assert contents.count("[mcp_servers.ouroboros]") == 1
        assert contents.count("[mcp_servers.ouroboros.env]") == 1
        assert contents.count("[profiles.ouroboros-fast]") == 1
        assert contents.count("[profiles.ouroboros-worker]") == 1
        assert contents.index("[mcp_servers.ouroboros]") < contents.index(
            "[profiles.ouroboros-worker]"
        )

    def test_register_codex_worker_profile_preserves_user_overrides(self, tmp_path: Path) -> None:
        """Rerunning setup must not clobber operator-authored worker keys."""
        codex_config = tmp_path / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text(
            "\n".join(
                [
                    "# Ouroboros Agent OS runtime profile for Codex worker subprocesses.",
                    "# Activated when ~/.ouroboros/config.yaml sets "
                    "`orchestrator.runtime_profile.backend_profile: worker`",
                    "# (or the OUROBOROS_RUNTIME_PROFILE=worker env var). Add per-worker Codex",
                    "# overrides below — for example a different model, sandbox, or notify hook —",
                    "# without affecting interactive `codex` sessions that share this config file.",
                    "",
                    "[profiles.ouroboros-worker]",
                    'model = "o3-mini"',
                    "notify = []",
                    'sandbox = "workspace-write"',
                    "",
                    "[profiles.ouroboros-worker.shell_environment_policy]",
                    'inherit = "core"',
                    "",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        with patch("pathlib.Path.home", return_value=tmp_path):
            setup_cmd._register_codex_worker_profile()

        contents = codex_config.read_text(encoding="utf-8")

        assert contents.count("[profiles.ouroboros-worker]") == 1
        assert 'model = "o3-mini"' in contents
        assert "notify = []" in contents
        assert 'sandbox = "workspace-write"' in contents
        assert "[profiles.ouroboros-worker.shell_environment_policy]" in contents
        assert 'inherit = "core"' in contents
        assert contents.count("Ouroboros Agent OS runtime profile") == 1

    def test_register_codex_worker_profile_idempotent_with_user_overrides(
        self, tmp_path: Path
    ) -> None:
        """Multiple reruns must converge without key loss or comment bloat."""
        codex_config = tmp_path / ".codex" / "config.toml"

        with patch("pathlib.Path.home", return_value=tmp_path):
            setup_cmd._register_codex_worker_profile()
            existing = codex_config.read_text(encoding="utf-8")
            codex_config.write_text(
                existing.rstrip()
                + "\n"
                + 'model = "o3-mini"\nnotify = []\nsandbox = "workspace-write"\n',
                encoding="utf-8",
            )

            after_user_edit = codex_config.read_text(encoding="utf-8")
            setup_cmd._register_codex_worker_profile()
            after_second = codex_config.read_text(encoding="utf-8")
            setup_cmd._register_codex_worker_profile()
            after_third = codex_config.read_text(encoding="utf-8")

        for snapshot in (after_second, after_third):
            assert snapshot.count("[profiles.ouroboros-worker]") == 1
            assert snapshot.count("Ouroboros Agent OS runtime profile") == 1
            assert 'model = "o3-mini"' in snapshot
            assert "notify = []" in snapshot
            assert 'sandbox = "workspace-write"' in snapshot
        assert after_second == after_user_edit
        assert after_third == after_second

    def test_register_codex_worker_profile_idempotent_when_user_inserts_own_comment(
        self, tmp_path: Path
    ) -> None:
        """Operator comments between managed comments and header must not stack blocks."""
        codex_config = tmp_path / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text(
            "\n".join(
                [
                    "# Ouroboros Agent OS runtime profile for Codex worker subprocesses.",
                    "# Activated when ~/.ouroboros/config.yaml sets "
                    "`orchestrator.runtime_profile.backend_profile: worker`",
                    "# (or the OUROBOROS_RUNTIME_PROFILE=worker env var). Add per-worker Codex",
                    "# overrides below — for example a different model, sandbox, or notify hook —",
                    "# without affecting interactive `codex` sessions that share this config file.",
                    "",
                    "# Operator note: keep this profile aligned with prod-staging.",
                    "[profiles.ouroboros-worker]",
                    'model = "o3-mini"',
                    "",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        with patch("pathlib.Path.home", return_value=tmp_path):
            setup_cmd._register_codex_worker_profile()
            after_first = codex_config.read_text(encoding="utf-8")
            setup_cmd._register_codex_worker_profile()
            after_second = codex_config.read_text(encoding="utf-8")

        for snapshot in (after_first, after_second):
            assert snapshot.count("Ouroboros Agent OS runtime profile") == 1
            assert "# Operator note: keep this profile aligned with prod-staging." in snapshot
            assert 'model = "o3-mini"' in snapshot
            assert snapshot.count("[profiles.ouroboros-worker]") == 1
        assert after_second == after_first

    def test_register_codex_worker_profile_skips_non_table_profiles_value(
        self, tmp_path: Path
    ) -> None:
        """Valid TOML with scalar profiles must not be corrupted by worker setup."""
        codex_config = tmp_path / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        original = 'profiles = "oops"\n'
        codex_config.write_text(original, encoding="utf-8")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.cli.commands.setup.print_error") as mock_error,
        ):
            setup_cmd._register_codex_worker_profile()

        mock_error.assert_called_once()
        assert codex_config.read_text(encoding="utf-8") == original

    def test_register_codex_worker_profile_skips_invalid_toml(self, tmp_path: Path) -> None:
        """Malformed TOML should produce an error message and leave the file alone."""
        codex_config = tmp_path / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        original = "this is = not = valid = toml\n[unterminated"
        codex_config.write_text(original, encoding="utf-8")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.cli.commands.setup.print_error") as mock_error,
        ):
            setup_cmd._register_codex_worker_profile()

        mock_error.assert_called_once()
        assert codex_config.read_text(encoding="utf-8") == original

    def test_register_codex_worker_profile_migrates_legacy_table_to_profile_v2(
        self, tmp_path: Path
    ) -> None:
        """Current Codex worker profiles live in profile-v2 files, not config.toml."""
        codex_config = tmp_path / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text(
            "\n".join(
                [
                    "[mcp_servers.ouroboros]",
                    'command = "uvx"',
                    'args = ["--from", "ouroboros-ai[mcp]", "ouroboros", "mcp", "serve"]',
                    "",
                    "[profiles.ouroboros-worker]",
                    'model = "o3-mini"',
                    "notify = []",
                    'sandbox = "workspace-write"',
                    "",
                    "[profiles.ouroboros-worker.shell_environment_policy]",
                    'inherit = "core"',
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.cli.commands.setup._codex_uses_profile_v2", return_value=True),
        ):
            setup_cmd._register_codex_worker_profile(codex_path="/usr/local/bin/codex")

        contents = codex_config.read_text(encoding="utf-8")
        worker_profile = (tmp_path / ".codex" / "ouroboros-worker.config.toml").read_text(
            encoding="utf-8"
        )

        assert "[mcp_servers.ouroboros]" in contents
        assert "[profiles.ouroboros-worker]" not in contents
        assert 'model = "o3-mini"' in worker_profile
        assert "notify = []" in worker_profile
        assert 'sandbox = "workspace-write"' in worker_profile
        assert "[shell_environment_policy]" in worker_profile
        assert 'inherit = "core"' in worker_profile

    def test_register_codex_worker_profile_migrates_quoted_legacy_table_to_profile_v2(
        self,
        tmp_path: Path,
    ) -> None:
        """Quoted legacy profile headers must be removed when migrated to profile-v2."""
        codex_dir = tmp_path / ".codex"
        codex_config = codex_dir / "config.toml"
        codex_dir.mkdir(parents=True)
        codex_config.write_text(
            "\n".join(
                [
                    "[mcp_servers.ouroboros]",
                    'command = "uvx"',
                    'args = ["--from", "ouroboros-ai[mcp]", "ouroboros", "mcp", "serve"]',
                    "",
                    '# Managed by Ouroboros setup. Safe to remove if you do not use "codex --profile ouroboros-worker".',
                    '[profiles."ouroboros-worker"]',
                    'model = "o3-mini"',
                    'sandbox = "workspace-write"',
                    "",
                    '[profiles."ouroboros-worker".shell_environment_policy]',
                    'inherit = "core"',
                    "",
                ]
            ),
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.cli.commands.setup._codex_uses_profile_v2", return_value=True),
        ):
            assert setup_cmd._register_codex_worker_profile(codex_path="/usr/local/bin/codex")

        contents = codex_config.read_text(encoding="utf-8")
        parsed = tomllib.loads(contents)
        worker_profile = (codex_dir / "ouroboros-worker.config.toml").read_text(encoding="utf-8")

        assert "ouroboros-worker" not in parsed.get("profiles", {})
        assert '[profiles."ouroboros-worker"]' not in contents
        assert 'model = "o3-mini"' in worker_profile
        assert 'sandbox = "workspace-write"' in worker_profile
        assert "[shell_environment_policy]" in worker_profile
        assert 'inherit = "core"' in worker_profile

    def test_register_codex_worker_profile_updates_quoted_legacy_section_without_profile_v2(
        self,
        tmp_path: Path,
    ) -> None:
        """Legacy Codex profile registration must not duplicate quoted worker tables."""
        codex_dir = tmp_path / ".codex"
        codex_config = codex_dir / "config.toml"
        codex_dir.mkdir(parents=True)
        codex_config.write_text(
            "\n".join(
                [
                    '[profiles."ouroboros-worker"]',
                    'model = "operator-model"',
                    "",
                    '[profiles."ouroboros-worker".shell_environment_policy]',
                    'inherit = "all"',
                    "",
                ]
            ),
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.cli.commands.setup._codex_uses_profile_v2", return_value=False),
        ):
            assert setup_cmd._register_codex_worker_profile(codex_path="/usr/local/bin/codex")

        contents = codex_config.read_text(encoding="utf-8")
        parsed = tomllib.loads(contents)

        assert list(parsed["profiles"]) == ["ouroboros-worker"]
        assert contents.count("[profiles.ouroboros-worker]") == 1
        assert contents.count('[profiles."ouroboros-worker"]') == 0
        assert 'model = "operator-model"' in contents
        assert 'inherit = "all"' in contents

    def test_register_codex_worker_profile_migrates_inline_parent_assignment_to_profile_v2(
        self,
        tmp_path: Path,
    ) -> None:
        """Inline `[profiles] "ouroboros-worker" = {...}` must be removed after v2 migration."""
        codex_dir = tmp_path / ".codex"
        codex_config = codex_dir / "config.toml"
        codex_dir.mkdir(parents=True)
        codex_config.write_text(
            "\n".join(
                [
                    "[profiles]",
                    'other = { model = "keep-me" }',
                    '"ouroboros-worker" = { model = "o3-mini", sandbox = "workspace-write" }',
                    "",
                ]
            ),
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.cli.commands.setup._codex_uses_profile_v2", return_value=True),
        ):
            assert setup_cmd._register_codex_worker_profile(codex_path="/usr/local/bin/codex")

        parsed = tomllib.loads(codex_config.read_text(encoding="utf-8"))
        worker_profile = (codex_dir / "ouroboros-worker.config.toml").read_text(encoding="utf-8")

        assert parsed["profiles"]["other"]["model"] == "keep-me"
        assert "ouroboros-worker" not in parsed["profiles"]
        assert 'model = "o3-mini"' in worker_profile
        assert 'sandbox = "workspace-write"' in worker_profile

    def test_register_codex_worker_profile_removes_multiline_dotted_assignment_to_profile_v2(
        self,
        tmp_path: Path,
    ) -> None:
        """Multiline dotted legacy assignments must not leave orphaned values."""
        codex_dir = tmp_path / ".codex"
        codex_config = codex_dir / "config.toml"
        codex_dir.mkdir(parents=True)
        codex_config.write_text(
            "\n".join(
                [
                    "[profiles]",
                    'other = { model = "keep-me" }',
                    '"ouroboros-worker".model = """',
                    "gpt-5",
                    '"""',
                    '"ouroboros-worker".sandbox = "workspace-write"',
                    "",
                ]
            ),
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.cli.commands.setup._codex_uses_profile_v2", return_value=True),
        ):
            assert setup_cmd._register_codex_worker_profile(codex_path="/usr/local/bin/codex")

        contents = codex_config.read_text(encoding="utf-8")
        parsed = tomllib.loads(contents)
        worker_profile = (codex_dir / "ouroboros-worker.config.toml").read_text(encoding="utf-8")

        assert parsed["profiles"]["other"]["model"] == "keep-me"
        assert "ouroboros-worker" not in parsed["profiles"]
        assert "gpt-5" in worker_profile
        assert '"""' not in contents

    def test_register_codex_worker_profile_rewrites_root_inline_profiles_table(
        self,
        tmp_path: Path,
    ) -> None:
        """Root inline profile mappings must remove only the migrated worker profile."""
        codex_dir = tmp_path / ".codex"
        codex_config = codex_dir / "config.toml"
        codex_dir.mkdir(parents=True)
        codex_config.write_text(
            'profiles = { other = { model = "keep-me" }, "ouroboros-worker" = { '
            'model = "o3-mini", sandbox = "workspace-write" } }\n',
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.cli.commands.setup._codex_uses_profile_v2", return_value=True),
        ):
            assert setup_cmd._register_codex_worker_profile(codex_path="/usr/local/bin/codex")

        contents = codex_config.read_text(encoding="utf-8")
        parsed = tomllib.loads(contents)
        worker_profile = (codex_dir / "ouroboros-worker.config.toml").read_text(encoding="utf-8")

        assert parsed["profiles"]["other"]["model"] == "keep-me"
        assert "ouroboros-worker" not in parsed["profiles"]
        assert 'model = "o3-mini"' in worker_profile
        assert "profiles = {" not in contents

    def test_register_codex_worker_profile_keeps_header_inside_multiline_string(
        self,
        tmp_path: Path,
    ) -> None:
        """Header-looking text inside multiline strings is not a table boundary."""
        codex_dir = tmp_path / ".codex"
        codex_config = codex_dir / "config.toml"
        codex_dir.mkdir(parents=True)
        codex_config.write_text(
            "\n".join(
                [
                    "[profiles.ouroboros-worker]",
                    'model = "o3-mini"',
                    'instructions = """',
                    "[launcher]",
                    '"""',
                    "",
                    "[operator]",
                    'value = "preserved"',
                    "",
                ]
            ),
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.cli.commands.setup._codex_uses_profile_v2", return_value=True),
        ):
            assert setup_cmd._register_codex_worker_profile(codex_path="/usr/local/bin/codex")

        contents = codex_config.read_text(encoding="utf-8")
        parsed = tomllib.loads(contents)
        worker_profile = (codex_dir / "ouroboros-worker.config.toml").read_text(encoding="utf-8")

        assert "ouroboros-worker" not in parsed.get("profiles", {})
        assert parsed["operator"]["value"] == "preserved"
        assert "[launcher]" in worker_profile

    def test_worker_profile_migration_preserves_profile_like_multiline_text(
        self,
        tmp_path: Path,
    ) -> None:
        """Profile-looking text outside real tables is never migrated."""
        codex_dir = tmp_path / ".codex"
        codex_config = codex_dir / "config.toml"
        codex_dir.mkdir(parents=True)
        instructions = 'operator notes\n[profiles.ouroboros-worker]\nmodel = "do-not-migrate'
        codex_config.write_text(
            'instructions = """operator notes\n[profiles.ouroboros-worker]\n'
            'model = "do-not-migrate"""\n',
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.cli.commands.setup._codex_uses_profile_v2", return_value=True),
        ):
            assert setup_cmd._register_codex_worker_profile(codex_path="/usr/local/bin/codex")

        contents = codex_config.read_text(encoding="utf-8")
        parsed = tomllib.loads(contents)
        worker_profile = (codex_dir / "ouroboros-worker.config.toml").read_text(encoding="utf-8")

        assert parsed["instructions"] == instructions
        assert "do-not-migrate" not in worker_profile

    def test_register_codex_worker_profile_migrates_trailing_comment_header_to_profile_v2(
        self,
        tmp_path: Path,
    ) -> None:
        """Trailing header comments are valid TOML and must not block section removal."""
        codex_dir = tmp_path / ".codex"
        codex_config = codex_dir / "config.toml"
        codex_dir.mkdir(parents=True)
        codex_config.write_text(
            "\n".join(
                [
                    '[profiles."ouroboros-worker"] # local note',
                    'model = "o3-mini"',
                    "",
                    "[profiles.unrelated] # keep",
                    'model = "gpt-5"',
                    "",
                ]
            ),
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.cli.commands.setup._codex_uses_profile_v2", return_value=True),
        ):
            assert setup_cmd._register_codex_worker_profile(codex_path="/usr/local/bin/codex")

        contents = codex_config.read_text(encoding="utf-8")
        parsed = tomllib.loads(contents)
        worker_profile = (codex_dir / "ouroboros-worker.config.toml").read_text(encoding="utf-8")

        assert "ouroboros-worker" not in parsed.get("profiles", {})
        assert parsed["profiles"]["unrelated"]["model"] == "gpt-5"
        assert "[profiles.unrelated] # keep" in contents
        assert 'model = "o3-mini"' in worker_profile

    def test_register_codex_worker_profile_preserves_following_array_tables(
        self,
        tmp_path: Path,
    ) -> None:
        """Legacy profile removal must stop before unrelated array-of-tables."""
        codex_dir = tmp_path / ".codex"
        codex_config = codex_dir / "config.toml"
        codex_dir.mkdir(parents=True)
        codex_config.write_text(
            "\n".join(
                [
                    "[profiles.ouroboros-worker]",
                    'model = "o3-mini"',
                    "",
                    "[[custom_hooks]]",
                    'name = "after-profile"',
                    "",
                ]
            ),
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.cli.commands.setup._codex_uses_profile_v2", return_value=True),
        ):
            assert setup_cmd._register_codex_worker_profile(codex_path="/usr/local/bin/codex")

        contents = codex_config.read_text(encoding="utf-8")
        parsed = tomllib.loads(contents)

        assert "ouroboros-worker" not in parsed.get("profiles", {})
        assert parsed["custom_hooks"][0]["name"] == "after-profile"
        assert "[[custom_hooks]]" in contents

    def test_register_codex_worker_profile_stops_at_trailing_comment_boundary(
        self,
        tmp_path: Path,
    ) -> None:
        """A commented table header after the worker section must remain a boundary."""
        codex_dir = tmp_path / ".codex"
        codex_config = codex_dir / "config.toml"
        codex_dir.mkdir(parents=True)
        codex_config.write_text(
            "\n".join(
                [
                    "[profiles.ouroboros-worker]",
                    'model = "o3-mini"',
                    "",
                    '[profiles."keep.me"] # operator table',
                    'custom = "value"',
                    "",
                ]
            ),
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.cli.commands.setup._codex_uses_profile_v2", return_value=True),
        ):
            assert setup_cmd._register_codex_worker_profile(codex_path="/usr/local/bin/codex")

        contents = codex_config.read_text(encoding="utf-8")
        parsed = tomllib.loads(contents)

        assert "ouroboros-worker" not in parsed.get("profiles", {})
        assert parsed["profiles"]["keep.me"]["custom"] == "value"
        assert '[profiles."keep.me"] # operator table' in contents

    def test_register_codex_worker_profile_preserves_quoted_custom_keys_in_profile_v2(
        self,
        tmp_path: Path,
    ) -> None:
        """Migrated user TOML keys must remain valid and semantically equivalent."""
        codex_dir = tmp_path / ".codex"
        codex_config = codex_dir / "config.toml"
        codex_dir.mkdir(parents=True)
        codex_config.write_text(
            "\n".join(
                [
                    '[profiles."ouroboros-worker"]',
                    '"custom key" = "operator-value"',
                    '"custom.key" = "literal-dot"',
                    "",
                    '[profiles."ouroboros-worker"."nested.key"]',
                    '"inner key" = "nested-value"',
                    "",
                ]
            ),
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.cli.commands.setup._codex_uses_profile_v2", return_value=True),
        ):
            assert setup_cmd._register_codex_worker_profile(codex_path="/usr/local/bin/codex")

        contents = codex_config.read_text(encoding="utf-8")
        profile_contents = (codex_dir / "ouroboros-worker.config.toml").read_text(encoding="utf-8")
        profile = tomllib.loads(profile_contents)

        assert "ouroboros-worker" not in tomllib.loads(contents).get("profiles", {})
        assert profile["custom key"] == "operator-value"
        assert profile["custom.key"] == "literal-dot"
        assert profile["nested.key"]["inner key"] == "nested-value"
        assert '"custom key" = "operator-value"' in profile_contents
        assert '"custom.key" = "literal-dot"' in profile_contents

    def test_register_codex_worker_profile_preserves_datetime_values_in_profile_v2(
        self,
        tmp_path: Path,
    ) -> None:
        """Worker migration must preserve non-JSON TOML scalar types semantically."""
        codex_dir = tmp_path / ".codex"
        codex_config = codex_dir / "config.toml"
        codex_dir.mkdir(parents=True)
        codex_config.write_text(
            "\n".join(
                [
                    "[profiles.ouroboros-worker]",
                    "expires = 1979-05-27T07:32:00Z",
                    "dates = [1979-05-27]",
                    "",
                    "[profiles.ouroboros-worker.window]",
                    "starts = 07:32:00",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.cli.commands.setup._codex_uses_profile_v2", return_value=True),
        ):
            assert setup_cmd._register_codex_worker_profile(codex_path="/usr/local/bin/codex")

        contents = codex_config.read_text(encoding="utf-8")
        profile_contents = (codex_dir / "ouroboros-worker.config.toml").read_text(encoding="utf-8")
        profile = tomllib.loads(profile_contents)

        assert "ouroboros-worker" not in tomllib.loads(contents).get("profiles", {})
        assert profile["expires"].isoformat() == "1979-05-27T07:32:00+00:00"
        assert profile["dates"][0].isoformat() == "1979-05-27"
        assert profile["window"]["starts"].isoformat() == "07:32:00"

    def test_register_codex_worker_profile_preserves_array_of_tables_in_profile_v2(
        self,
        tmp_path: Path,
    ) -> None:
        """TOML arrays of tables parse as list[dict] and must migrate safely."""
        codex_dir = tmp_path / ".codex"
        codex_config = codex_dir / "config.toml"
        codex_dir.mkdir(parents=True)
        codex_config.write_text(
            "\n".join(
                [
                    "[profiles.ouroboros-worker]",
                    'model = "o3-mini"',
                    "",
                    "[[profiles.ouroboros-worker.tools]]",
                    'name = "alpha"',
                    "enabled = true",
                    "",
                    "[[profiles.ouroboros-worker.tools]]",
                    'name = "beta"',
                    "limits = [1, 2]",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.cli.commands.setup._codex_uses_profile_v2", return_value=True),
        ):
            assert setup_cmd._register_codex_worker_profile(codex_path="/usr/local/bin/codex")

        contents = codex_config.read_text(encoding="utf-8")
        profile = tomllib.loads((codex_dir / "ouroboros-worker.config.toml").read_text())

        assert "ouroboros-worker" not in tomllib.loads(contents).get("profiles", {})
        assert profile["tools"][0]["name"] == "alpha"
        assert profile["tools"][0]["enabled"] is True
        assert profile["tools"][1]["limits"] == [1, 2]

    def test_register_codex_worker_profile_recovers_exact_profile_v2_migration(
        self, tmp_path: Path
    ) -> None:
        """Retry may remove legacy table only when the v2 file exactly matches setup output."""
        codex_dir = tmp_path / ".codex"
        codex_config = codex_dir / "config.toml"
        codex_dir.mkdir(parents=True)
        codex_config.write_text(
            "\n".join(
                [
                    "[mcp_servers.ouroboros]",
                    'command = "uvx"',
                    "",
                    "[profiles.ouroboros-worker]",
                    'model = "o3-mini"',
                    'sandbox = "workspace-write"',
                    "",
                    "[profiles.ouroboros-worker.shell_environment_policy]",
                    'inherit = "core"',
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        expected_profile = setup_cmd._render_codex_worker_profile_v2_file(
            {
                "model": "o3-mini",
                "sandbox": "workspace-write",
                "shell_environment_policy": {"inherit": "core"},
            }
        )
        (codex_dir / "ouroboros-worker.config.toml").write_text(
            expected_profile,
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.cli.commands.setup._codex_uses_profile_v2", return_value=True),
            patch("ouroboros.cli.commands.setup.print_warning") as mock_warning,
        ):
            assert setup_cmd._register_codex_worker_profile(codex_path="/usr/local/bin/codex")
            assert setup_cmd._register_codex_worker_profile(codex_path="/usr/local/bin/codex")

        contents = codex_config.read_text(encoding="utf-8")
        worker_profile = (codex_dir / "ouroboros-worker.config.toml").read_text(encoding="utf-8")

        assert "[mcp_servers.ouroboros]" in contents
        assert "[profiles.ouroboros-worker]" not in contents
        assert worker_profile == expected_profile
        mock_warning.assert_not_called()

    def test_register_codex_worker_profile_preserves_generated_prefix_profile_v2(
        self,
        tmp_path: Path,
    ) -> None:
        """Generated-prefix profile-v2 content is still user-owned without exact evidence."""
        codex_dir = tmp_path / ".codex"
        codex_config = codex_dir / "config.toml"
        profile_path = codex_dir / "ouroboros-worker.config.toml"
        codex_dir.mkdir(parents=True)
        codex_config.write_text(
            "\n".join(
                [
                    "[profiles.ouroboros-worker]",
                    'model = "o3-mini"',
                    'sandbox = "workspace-write"',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        prefix_profile = "\n".join([setup_cmd._CODEX_PROFILE_V2_COMMENT, 'model = "o3-mini"', ""])
        profile_path.write_text(prefix_profile, encoding="utf-8")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.cli.commands.setup._codex_uses_profile_v2", return_value=True),
            patch("ouroboros.cli.commands.setup.print_warning") as mock_warning,
        ):
            setup_cmd._register_codex_worker_profile(codex_path="/usr/local/bin/codex")

        contents = codex_config.read_text(encoding="utf-8")

        assert "[profiles.ouroboros-worker]" in contents
        assert profile_path.read_text(encoding="utf-8") == prefix_profile
        mock_warning.assert_called_once()

    def test_register_codex_worker_profile_keeps_legacy_table_when_v2_file_exists(
        self, tmp_path: Path
    ) -> None:
        """Worker migration should not delete legacy overrides when v2 already exists."""
        codex_dir = tmp_path / ".codex"
        codex_config = codex_dir / "config.toml"
        codex_dir.mkdir(parents=True)
        codex_config.write_text(
            "\n".join(
                [
                    "[profiles.ouroboros-worker]",
                    'model = "o3-mini"',
                    'sandbox = "workspace-write"',
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (codex_dir / "ouroboros-worker.config.toml").write_text(
            'sandbox = "read-only"\n',
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.cli.commands.setup._codex_uses_profile_v2", return_value=True),
            patch("ouroboros.cli.commands.setup.print_warning") as mock_warning,
        ):
            setup_cmd._register_codex_worker_profile(codex_path="/usr/local/bin/codex")

        contents = codex_config.read_text(encoding="utf-8")
        worker_profile = (codex_dir / "ouroboros-worker.config.toml").read_text(encoding="utf-8")

        assert "[profiles.ouroboros-worker]" in contents
        assert 'model = "o3-mini"' in contents
        assert 'sandbox = "read-only"' in worker_profile
        mock_warning.assert_called_once()
        warning = mock_warning.call_args.args[0]
        assert "Preserved legacy Codex profile table(s)" in warning
        assert "ouroboros-worker" in warning
        assert "manually reconcile" in warning

    def test_install_codex_artifacts_installs_rules_and_skills(self, tmp_path: Path) -> None:
        """Codex setup should install both managed rules and managed skills."""
        rules_path = tmp_path / ".codex" / "rules"
        skill_paths = [tmp_path / ".codex" / "skills" / "evaluate"]
        result = CodexArtifactInstallResult(rules_path, tuple(skill_paths))

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.codex.install_codex_artifacts", return_value=result) as mock_install,
            patch("ouroboros.cli.commands.setup.print_success") as mock_success,
        ):
            setup_cmd._install_codex_artifacts()

        mock_install.assert_called_once()
        success_messages = [call.args[0] for call in mock_success.call_args_list]
        assert any("Installed Codex rules" in message for message in success_messages)
        assert any("Installed 1 Codex skills" in message for message in success_messages)

    def test_install_codex_artifacts_rejects_symlinked_codex_home(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Setup must pass raw CODEX_HOME so artifact install can fail closed."""
        real_home = tmp_path / "real-codex-home"
        real_home.mkdir()
        codex_home_link = tmp_path / "codex-home-link"
        try:
            codex_home_link.symlink_to(real_home, target_is_directory=True)
        except OSError:
            pytest.skip("symlinks are not supported on this platform")

        monkeypatch.setenv("CODEX_HOME", str(codex_home_link))

        assert setup_cmd._install_codex_artifacts() is False
        assert not (real_home / "rules").exists()

    def test_setup_codex_updates_config_and_prints_config_split_guidance(
        self,
        tmp_path: Path,
    ) -> None:
        """Codex setup should configure config.yaml and explain where settings belong."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text("orchestrator:\n  runtime_backend: claude\n", encoding="utf-8")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._install_codex_artifacts") as mock_install,
            patch("ouroboros.cli.commands.setup._register_codex_mcp_server") as mock_register,
            patch("ouroboros.cli.commands.setup._retire_codex_default_profiles") as mock_retire,
            patch(
                "ouroboros.cli.commands.setup._register_codex_worker_profile"
            ) as mock_worker_profile,
            patch("ouroboros.cli.commands.setup.print_info") as mock_info,
        ):
            assert setup_cmd._setup_codex("/usr/local/bin/codex") is True

        config_dict = yaml.safe_load(config_path.read_text(encoding="utf-8"))

        assert config_dict["orchestrator"]["runtime_backend"] == "codex"
        assert config_dict["orchestrator"]["codex_cli_path"] == "/usr/local/bin/codex"
        assert config_dict["llm"]["backend"] == "codex"
        assert (
            config_dict["llm_profiles"]["fast"]["providers"]["codex"]["reasoning_effort"] == "low"
        )
        assert (
            config_dict["llm_profiles"]["frontier"]["providers"]["codex"]["reasoning_effort"]
            == "xhigh"
        )
        assert config_dict["llm_role_profiles"]["context_compression"] == "deep"
        assert config_dict["llm_role_profiles"]["qa"] == "frontier"
        assert config_dict["llm_role_profiles"]["brownfield_explore"] == "frontier"
        assert config_dict["llm_role_profiles"]["clarification"] == "frontier"
        assert config_dict["llm_role_profiles"]["semantic_evaluation"] == "deep"
        assert config_dict["llm_role_profiles"]["wonder"] == "frontier"
        assert config_dict["llm_role_profiles"]["consensus_judge"] == "frontier"
        assert config_dict["llm_role_profiles"]["agent_runtime"] == "standard"
        assert config_dict["llm_role_profiles"]["agent_runtime_implementation"] == "standard"
        assert config_dict["llm_role_profiles"]["agent_runtime_interview"] == "deep"
        assert config_dict["llm_role_profiles"]["agent_runtime_coordinator"] == "standard"
        assert config_dict["llm_role_profiles"]["agent_runtime_evaluation"] == "deep"
        mock_install.assert_called_once()
        assert isinstance(mock_install.call_args.kwargs["expected_snapshots"], dict)
        mock_register.assert_called_once()
        assert mock_register.call_args.kwargs["mode"] == "auto"
        assert isinstance(mock_register.call_args.kwargs["expected_snapshots"], dict)
        mock_retire.assert_called_once()
        assert mock_retire.call_args.kwargs["protected_profile_names"] == set()
        assert isinstance(mock_retire.call_args.kwargs["expected_snapshots"], dict)
        mock_worker_profile.assert_called_once()
        assert mock_worker_profile.call_args.kwargs["codex_path"] == "/usr/local/bin/codex"
        assert isinstance(mock_worker_profile.call_args.kwargs["expected_snapshots"], dict)

        info_messages = [call.args[0] for call in mock_info.call_args_list]
        assert any("Config saved to" in message for message in info_messages)
        assert any("Configure Ouroboros runtime" in message for message in info_messages)
        assert any("profiles you manage yourself" in message for message in info_messages)

    def test_setup_codex_fresh_setup_creates_secure_credentials(self, tmp_path: Path) -> None:
        """Fresh Codex setup must leave config_exists() true by creating credentials.yaml."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        credentials_path = config_dir / "credentials.yaml"

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._register_codex_mcp_server", return_value=True),
            patch("ouroboros.cli.commands.setup._install_codex_artifacts", return_value=True),
            patch("ouroboros.cli.commands.setup._retire_codex_default_profiles"),
            patch(
                "ouroboros.cli.commands.setup._register_codex_worker_profile",
                return_value=True,
            ),
        ):
            assert setup_cmd._setup_codex("/usr/local/bin/codex") is True

        assert (config_dir / "config.yaml").exists()
        assert credentials_path.exists()
        if os.name != "nt":
            assert credentials_path.stat().st_mode & 0o777 == 0o600

    def test_setup_codex_rolls_back_fresh_config_when_credentials_write_fails(
        self, tmp_path: Path
    ) -> None:
        """Fresh setup must not leave config.yaml without credentials.yaml."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        credentials_path = config_dir / "credentials.yaml"

        def _write(
            path: Path,
            text: str,
            *,
            mode: int | None = None,
            expected_current: setup_cmd._PathSnapshot | None = None,
        ) -> None:
            if expected_current is not None:
                setup_cmd._require_path_snapshot(path, expected_current)
            if path == credentials_path:
                assert mode == 0o600
                raise OSError("credentials disk full")
            path.write_text(text, encoding="utf-8")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._register_codex_mcp_server", return_value=True),
            patch("ouroboros.cli.commands.setup._atomic_write_text", side_effect=_write),
            patch("ouroboros.cli.commands.setup._install_codex_artifacts") as mock_install,
        ):
            assert setup_cmd._setup_codex("/usr/local/bin/codex") is False

        assert not (config_dir / "config.yaml").exists()
        assert not credentials_path.exists()
        mock_install.assert_not_called()

    def test_setup_codex_does_not_save_config_when_mcp_registration_fails(
        self,
        tmp_path: Path,
    ) -> None:
        """Codex setup must not persist a Codex runtime without a usable MCP endpoint."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        original = "orchestrator:\n  runtime_backend: claude\nllm:\n  backend: claude\n"
        config_path.write_text(original, encoding="utf-8")
        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        (codex_home / "config.toml").write_text("[mcp_servers.ouroboros\n", encoding="utf-8")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._install_codex_artifacts") as mock_install,
            patch("ouroboros.cli.commands.setup._retire_codex_default_profiles") as mock_retire,
            patch(
                "ouroboros.cli.commands.setup._register_codex_worker_profile"
            ) as mock_worker_profile,
        ):
            assert setup_cmd._setup_codex("/usr/local/bin/codex") is False

        assert config_path.read_text(encoding="utf-8") == original
        mock_install.assert_not_called()
        mock_retire.assert_not_called()
        mock_worker_profile.assert_not_called()

    def test_setup_codex_rolls_back_mcp_when_config_write_fails(self, tmp_path: Path) -> None:
        """Codex setup must not leave MCP configured when config.yaml fails."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        original_config = "orchestrator:\n  runtime_backend: claude\nllm:\n  backend: claude\n"
        config_path.write_text(original_config, encoding="utf-8")
        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        codex_config = codex_home / "config.toml"
        original_toml = '[mcp_servers.ouroboros]\ncommand = "old"\n'
        codex_config.write_text(original_toml, encoding="utf-8")

        def _register(**_kwargs: object) -> bool:
            codex_config.write_text('[mcp_servers.ouroboros]\ncommand = "new"\n', encoding="utf-8")
            expected = _kwargs["expected_snapshots"]
            assert isinstance(expected, dict)
            expected[codex_config] = setup_cmd._snapshot_path(codex_config)
            return True

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._register_codex_mcp_server", side_effect=_register),
            patch(
                "ouroboros.cli.commands.setup._atomic_write_text",
                side_effect=OSError("disk full"),
            ),
            patch("ouroboros.cli.commands.setup._install_codex_artifacts") as mock_install,
            patch("ouroboros.cli.commands.setup._retire_codex_default_profiles") as mock_retire,
            patch(
                "ouroboros.cli.commands.setup._register_codex_worker_profile"
            ) as mock_worker_profile,
        ):
            assert setup_cmd._setup_codex("/usr/local/bin/codex") is False

        assert config_path.read_text(encoding="utf-8") == original_config
        assert codex_config.read_text(encoding="utf-8") == original_toml
        mock_install.assert_not_called()
        mock_retire.assert_not_called()
        mock_worker_profile.assert_not_called()

    def test_setup_codex_fails_when_release_mcp_has_no_launcher(self, tmp_path: Path) -> None:
        """Setup never commits a release MCP endpoint whose command is unavailable."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        original_config = "orchestrator:\n  runtime_backend: claude\nllm:\n  backend: claude\n"
        config_path.write_text(original_config, encoding="utf-8")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._is_dev_ouroboros_build", return_value=False),
            patch("ouroboros.cli.commands.setup.shutil.which", return_value=None),
            patch("ouroboros.cli.commands.setup.importlib_util.find_spec", return_value=None),
        ):
            assert setup_cmd._setup_codex("/usr/local/bin/codex") is False

        assert config_path.read_text(encoding="utf-8") == original_config
        assert not (tmp_path / ".codex" / "config.toml").exists()

    def test_setup_codex_rolls_back_mcp_when_registration_write_raises(
        self, tmp_path: Path
    ) -> None:
        """A failed MCP config write must not truncate user Codex config or save runtime."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        original_config = "orchestrator:\n  runtime_backend: claude\nllm:\n  backend: claude\n"
        config_path.write_text(original_config, encoding="utf-8")
        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        codex_config = codex_home / "config.toml"
        original_toml = '[mcp_servers.ouroboros]\ncommand = "old"\n'
        codex_config.write_text(original_toml, encoding="utf-8")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.commands.setup._atomic_write_text",
                side_effect=OSError("disk full"),
            ),
            patch("ouroboros.cli.commands.setup._install_codex_artifacts") as mock_install,
            patch("ouroboros.cli.commands.setup._retire_codex_default_profiles") as mock_retire,
            patch(
                "ouroboros.cli.commands.setup._register_codex_worker_profile"
            ) as mock_worker_profile,
        ):
            assert setup_cmd._setup_codex("/usr/local/bin/codex") is False

        assert config_path.read_text(encoding="utf-8") == original_config
        assert codex_config.read_text(encoding="utf-8") == original_toml
        mock_install.assert_not_called()
        mock_retire.assert_not_called()
        mock_worker_profile.assert_not_called()

    def test_setup_codex_preserves_concurrent_codex_edit_when_initial_mcp_registration_raises(
        self, tmp_path: Path
    ) -> None:
        """Without a known post-write snapshot, first MCP rollback must not clobber edits."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        original_config = "orchestrator:\n  runtime_backend: claude\nllm:\n  backend: claude\n"
        config_path.write_text(original_config, encoding="utf-8")
        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        codex_config = codex_home / "config.toml"
        codex_config.write_text('model = "before"\n', encoding="utf-8")
        concurrent_toml = 'model = "operator-edit"\n'

        def _register(**_kwargs: object) -> bool:
            codex_config.write_text(concurrent_toml, encoding="utf-8")
            raise OSError("registration failed after write")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._register_codex_mcp_server", side_effect=_register),
            patch("ouroboros.cli.commands.setup._install_codex_artifacts") as mock_install,
            patch("ouroboros.cli.commands.setup._retire_codex_default_profiles") as mock_retire,
            patch(
                "ouroboros.cli.commands.setup._register_codex_worker_profile"
            ) as mock_worker_profile,
        ):
            assert setup_cmd._setup_codex("/usr/local/bin/codex") is False

        assert config_path.read_text(encoding="utf-8") == original_config
        assert codex_config.read_text(encoding="utf-8") == concurrent_toml
        mock_install.assert_not_called()
        mock_retire.assert_not_called()
        mock_worker_profile.assert_not_called()

    def test_setup_codex_removes_fresh_credentials_when_mode_write_fails(
        self, tmp_path: Path
    ) -> None:
        """Fresh credentials must never be left behind with a non-private mode."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        credentials_path = config_dir / "credentials.yaml"

        original_write = setup_cmd._atomic_write_text

        def _write(
            path: Path,
            text: str,
            *,
            mode: int | None = None,
            expected_current: setup_cmd._PathSnapshot | None = None,
        ) -> None:
            if path == credentials_path:
                assert mode == 0o600
                path.write_text(text, encoding="utf-8")
                path.chmod(0o644)
                raise OSError("chmod failed")
            original_write(path, text, mode=mode, expected_current=expected_current)

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._register_codex_mcp_server", return_value=True),
            patch("ouroboros.cli.commands.setup._atomic_write_text", side_effect=_write),
            patch("ouroboros.cli.commands.setup._install_codex_artifacts") as mock_install,
        ):
            assert setup_cmd._setup_codex("/usr/local/bin/codex") is False

        assert not (config_dir / "config.yaml").exists()
        assert not credentials_path.exists()
        mock_install.assert_not_called()

    def test_register_codex_worker_profile_removes_created_v2_when_config_write_fails(
        self,
        tmp_path: Path,
    ) -> None:
        """A failed second migration write must not leave a new v2 file active."""
        codex_dir = tmp_path / ".codex"
        codex_config = codex_dir / "config.toml"
        profile_path = codex_dir / "ouroboros-worker.config.toml"
        codex_dir.mkdir(parents=True)
        raw = '[profiles.ouroboros-worker]\nmodel = "o3-mini"\n'
        codex_config.write_text(raw, encoding="utf-8")

        original_write = setup_cmd._atomic_write_text

        def _write(
            path: Path,
            text: str,
            *,
            mode: int | None = None,
            expected_current: setup_cmd._PathSnapshot | None = None,
        ) -> None:
            if path == codex_config and text != raw:
                raise OSError("config write failed")
            original_write(path, text, mode=mode, expected_current=expected_current)

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.cli.commands.setup._codex_uses_profile_v2", return_value=True),
            patch("ouroboros.cli.commands.setup._atomic_write_text", side_effect=_write),
            pytest.raises(OSError, match="config write failed"),
        ):
            setup_cmd._register_codex_worker_profile(codex_path="/usr/local/bin/codex")

        assert codex_config.read_text(encoding="utf-8") == raw
        assert not profile_path.exists()

    def test_register_codex_worker_profile_restores_existing_v2_when_recovery_config_write_fails(
        self,
        tmp_path: Path,
    ) -> None:
        """Recovering an interrupted migration must restore a preexisting partial v2 file."""
        codex_dir = tmp_path / ".codex"
        codex_config = codex_dir / "config.toml"
        profile_path = codex_dir / "ouroboros-worker.config.toml"
        codex_dir.mkdir(parents=True)
        raw = '[profiles.ouroboros-worker]\nmodel = "o3-mini"\nsandbox = "workspace-write"\n'
        codex_config.write_text(raw, encoding="utf-8")
        expected_profile = setup_cmd._render_codex_worker_profile_v2_file(
            {"model": "o3-mini", "sandbox": "workspace-write"}
        )
        profile_path.write_text(expected_profile, encoding="utf-8")

        original_write = setup_cmd._atomic_write_text

        def _write(
            path: Path,
            text: str,
            *,
            mode: int | None = None,
            expected_current: setup_cmd._PathSnapshot | None = None,
        ) -> None:
            if path == codex_config and text != raw:
                raise OSError("config write failed")
            original_write(path, text, mode=mode, expected_current=expected_current)

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.cli.commands.setup._codex_uses_profile_v2", return_value=True),
            patch("ouroboros.cli.commands.setup._atomic_write_text", side_effect=_write),
            pytest.raises(OSError, match="config write failed"),
        ):
            setup_cmd._register_codex_worker_profile(codex_path="/usr/local/bin/codex")

        assert codex_config.read_text(encoding="utf-8") == raw
        assert profile_path.read_text(encoding="utf-8") == expected_profile

    def test_register_codex_worker_profile_preserves_concurrent_config_edit_between_writes(
        self,
        tmp_path: Path,
    ) -> None:
        """Worker migration must not overwrite config.toml if it changed mid-migration."""
        codex_dir = tmp_path / ".codex"
        codex_config = codex_dir / "config.toml"
        profile_path = codex_dir / "ouroboros-worker.config.toml"
        codex_dir.mkdir(parents=True)
        raw = '[profiles.ouroboros-worker]\nmodel = "o3-mini"\n'
        operator_raw = 'model = "operator-edit"\n'
        codex_config.write_text(raw, encoding="utf-8")

        original_write = setup_cmd._atomic_write_text

        def _write(
            path: Path,
            text: str,
            *,
            mode: int | None = None,
            expected_current: setup_cmd._PathSnapshot | None = None,
        ) -> None:
            original_write(path, text, mode=mode, expected_current=expected_current)
            if path == profile_path:
                codex_config.write_text(operator_raw, encoding="utf-8")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.cli.commands.setup._codex_uses_profile_v2", return_value=True),
            patch("ouroboros.cli.commands.setup._atomic_write_text", side_effect=_write),
        ):
            assert not setup_cmd._register_codex_worker_profile(codex_path="/usr/local/bin/codex")

        assert codex_config.read_text(encoding="utf-8") == operator_raw
        assert not profile_path.exists()

    def test_setup_codex_rolls_back_when_artifact_install_fails(self, tmp_path: Path) -> None:
        """Missing packaged Codex artifacts must fail setup instead of reporting success."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        original_config = "orchestrator:\n  runtime_backend: claude\nllm:\n  backend: claude\n"
        config_path.write_text(original_config, encoding="utf-8")
        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        codex_config = codex_home / "config.toml"
        original_toml = '[mcp_servers.ouroboros]\ncommand = "old"\n'
        codex_config.write_text(original_toml, encoding="utf-8")

        def _register(**_kwargs: object) -> bool:
            codex_config.write_text('[mcp_servers.ouroboros]\ncommand = "new"\n', encoding="utf-8")
            expected = _kwargs["expected_snapshots"]
            assert isinstance(expected, dict)
            expected[codex_config] = setup_cmd._snapshot_path(codex_config)
            return True

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._register_codex_mcp_server", side_effect=_register),
            patch("ouroboros.cli.commands.setup._install_codex_artifacts", return_value=False),
            patch("ouroboros.cli.commands.setup._retire_codex_default_profiles") as mock_retire,
            patch(
                "ouroboros.cli.commands.setup._register_codex_worker_profile"
            ) as mock_worker_profile,
        ):
            assert setup_cmd._setup_codex("/usr/local/bin/codex") is False

        assert config_path.read_text(encoding="utf-8") == original_config
        assert codex_config.read_text(encoding="utf-8") == original_toml
        mock_retire.assert_not_called()
        mock_worker_profile.assert_not_called()

    def test_setup_codex_removes_partial_artifacts_when_artifact_install_fails(
        self, tmp_path: Path
    ) -> None:
        """Artifacts written before an installer failure are setup-owned rollback targets."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        original_config = "orchestrator:\n  runtime_backend: claude\nllm:\n  backend: claude\n"
        config_path.write_text(original_config, encoding="utf-8")
        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        codex_config = codex_home / "config.toml"
        original_toml = '[mcp_servers.ouroboros]\ncommand = "old"\n'
        codex_config.write_text(original_toml, encoding="utf-8")

        def _register(**_kwargs: object) -> bool:
            codex_config.write_text('[mcp_servers.ouroboros]\ncommand = "new"\n', encoding="utf-8")
            expected = _kwargs["expected_snapshots"]
            assert isinstance(expected, dict)
            expected[codex_config] = setup_cmd._snapshot_path(codex_config)
            return True

        def _install_artifacts(**_kwargs: object) -> bool:
            rules_dir = codex_home / "rules"
            rules_dir.mkdir()
            rule_path = rules_dir / "ouroboros.md"
            rule_path.write_text("partial rule\n", encoding="utf-8")
            expected = _kwargs["expected_snapshots"]
            assert isinstance(expected, dict)
            expected[rule_path] = setup_cmd._snapshot_path(rule_path)
            return False

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._register_codex_mcp_server", side_effect=_register),
            patch(
                "ouroboros.cli.commands.setup._install_codex_artifacts",
                side_effect=_install_artifacts,
            ),
            patch("ouroboros.cli.commands.setup._retire_codex_default_profiles") as mock_retire,
            patch(
                "ouroboros.cli.commands.setup._register_codex_worker_profile"
            ) as mock_worker_profile,
        ):
            assert setup_cmd._setup_codex("/usr/local/bin/codex") is False

        assert config_path.read_text(encoding="utf-8") == original_config
        assert codex_config.read_text(encoding="utf-8") == original_toml
        assert not (codex_home / "rules").exists()
        mock_retire.assert_not_called()
        mock_worker_profile.assert_not_called()

    def test_setup_codex_rolls_back_type_error_during_worker_profile_registration(
        self, tmp_path: Path
    ) -> None:
        """Unsupported profile migration data must not escape the setup rollback boundary."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        original_config = "orchestrator:\n  runtime_backend: claude\nllm:\n  backend: claude\n"
        config_path.write_text(original_config, encoding="utf-8")
        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        codex_config = codex_home / "config.toml"
        original_toml = '[mcp_servers.ouroboros]\ncommand = "old"\n'
        codex_config.write_text(original_toml, encoding="utf-8")

        def _register(**_kwargs: object) -> bool:
            codex_config.write_text('[mcp_servers.ouroboros]\ncommand = "new"\n', encoding="utf-8")
            expected = _kwargs["expected_snapshots"]
            assert isinstance(expected, dict)
            expected[codex_config] = setup_cmd._snapshot_path(codex_config)
            return True

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._register_codex_mcp_server", side_effect=_register),
            patch("ouroboros.cli.commands.setup._install_codex_artifacts", return_value=True),
            patch("ouroboros.cli.commands.setup._retire_codex_default_profiles"),
            patch(
                "ouroboros.cli.commands.setup._register_codex_worker_profile",
                side_effect=TypeError("unsupported TOML value"),
            ),
        ):
            assert setup_cmd._setup_codex("/usr/local/bin/codex") is False

        assert config_path.read_text(encoding="utf-8") == original_config
        assert codex_config.read_text(encoding="utf-8") == original_toml

    def test_setup_codex_rolls_back_config_after_profile_retirement_failure(
        self, tmp_path: Path
    ) -> None:
        """Profile retirement edits to config.toml remain setup-owned on later failure."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        original_config = "orchestrator:\n  runtime_backend: claude\nllm:\n  backend: claude\n"
        config_path.write_text(original_config, encoding="utf-8")
        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        codex_config = codex_home / "config.toml"
        original_toml = '[mcp_servers.ouroboros]\ncommand = "old"\n'
        codex_config.write_text(original_toml, encoding="utf-8")

        def _register(**_kwargs: object) -> bool:
            codex_config.write_text('[mcp_servers.ouroboros]\ncommand = "new"\n', encoding="utf-8")
            expected = _kwargs["expected_snapshots"]
            assert isinstance(expected, dict)
            expected[codex_config] = setup_cmd._snapshot_path(codex_config)
            return True

        def _retire(**_kwargs: object) -> None:
            codex_config.write_text(
                '[mcp_servers.ouroboros]\ncommand = "new"\n\n[operator]\nvalue = "retired"\n',
                encoding="utf-8",
            )
            expected = _kwargs["expected_snapshots"]
            assert isinstance(expected, dict)
            expected[codex_config] = setup_cmd._snapshot_path(codex_config)

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._register_codex_mcp_server", side_effect=_register),
            patch("ouroboros.cli.commands.setup._install_codex_artifacts", return_value=True),
            patch(
                "ouroboros.cli.commands.setup._retire_codex_default_profiles", side_effect=_retire
            ),
            patch(
                "ouroboros.cli.commands.setup._register_codex_worker_profile", return_value=False
            ),
        ):
            assert setup_cmd._setup_codex("/usr/local/bin/codex") is False

        assert config_path.read_text(encoding="utf-8") == original_config
        assert codex_config.read_text(encoding="utf-8") == original_toml

    def test_setup_codex_preserves_post_mcp_operator_edit_on_late_failure(
        self, tmp_path: Path
    ) -> None:
        """Rollback must not treat post-write operator edits as setup-authored generation."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        original_config = "orchestrator:\n  runtime_backend: claude\nllm:\n  backend: claude\n"
        config_path.write_text(original_config, encoding="utf-8")
        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        codex_config = codex_home / "config.toml"
        original_toml = 'model = "before"\n'
        operator_toml = 'model = "operator-edit"\n'
        codex_config.write_text(original_toml, encoding="utf-8")
        original_write = setup_cmd._atomic_write_text

        def _write(
            path: Path,
            text: str,
            *,
            mode: int | None = None,
            expected_current: setup_cmd._PathSnapshot | None = None,
        ) -> object:
            snapshot = original_write(
                path,
                text,
                mode=mode,
                expected_current=expected_current,
            )
            if path == codex_config and "[mcp_servers.ouroboros]" in text:
                codex_config.write_text(operator_toml, encoding="utf-8")
            return snapshot

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._atomic_write_text", side_effect=_write),
            patch("ouroboros.cli.commands.setup._install_codex_artifacts", return_value=False),
        ):
            assert setup_cmd._setup_codex("/usr/local/bin/codex") is False

        assert config_path.read_text(encoding="utf-8") == original_config
        assert codex_config.read_text(encoding="utf-8") == operator_toml

    def test_setup_codex_preserves_operator_edit_when_mcp_reports_no_write(
        self, tmp_path: Path
    ) -> None:
        """A no-op registrar must not claim a concurrent edit as setup-authored."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        original_config = "orchestrator:\n  runtime_backend: claude\nllm:\n  backend: claude\n"
        config_path.write_text(original_config, encoding="utf-8")
        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        codex_config = codex_home / "config.toml"
        original_toml = 'model = "before"\n'
        operator_toml = 'model = "operator-edit"\n'
        codex_config.write_text(original_toml, encoding="utf-8")

        def _register_without_write(**_kwargs: object) -> bool:
            codex_config.write_text(operator_toml, encoding="utf-8")
            return True

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.commands.setup._register_codex_mcp_server",
                side_effect=_register_without_write,
            ),
            patch("ouroboros.cli.commands.setup._install_codex_artifacts", return_value=False),
        ):
            assert setup_cmd._setup_codex("/usr/local/bin/codex") is False

        assert config_path.read_text(encoding="utf-8") == original_config
        assert codex_config.read_text(encoding="utf-8") == operator_toml

    def test_setup_codex_refuses_concurrent_config_edit_before_runtime_commit(
        self, tmp_path: Path
    ) -> None:
        """MCP registration must not authorize overwriting a later operator edit."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text(
            "orchestrator:\n  runtime_backend: claude\nllm:\n  backend: claude\n",
            encoding="utf-8",
        )
        operator_config = "operator: config-edit\n"

        def _register(**_kwargs: object) -> bool:
            config_path.write_text(operator_config, encoding="utf-8")
            return True

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.commands.setup._register_codex_mcp_server",
                side_effect=_register,
            ),
            patch("ouroboros.cli.commands.setup._install_codex_artifacts") as mock_install,
        ):
            assert setup_cmd._setup_codex("/usr/local/bin/codex") is False

        assert config_path.read_text(encoding="utf-8") == operator_config
        mock_install.assert_not_called()

    def test_setup_codex_binds_config_generation_before_reading(self, tmp_path: Path) -> None:
        """An edit arriving immediately after the read must not be overwritten."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text(
            "orchestrator:\n  runtime_backend: claude\nllm:\n  backend: claude\n",
            encoding="utf-8",
        )
        operator_config = "operator: config-edit-after-read\n"
        original_read_text = Path.read_text
        injected = False

        def _read_text(path: Path, *args: object, **kwargs: object) -> str:
            nonlocal injected
            text = original_read_text(path, *args, **kwargs)
            if path == config_path and not injected:
                injected = True
                config_path.write_text(operator_config, encoding="utf-8")
            return text

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("pathlib.Path.read_text", side_effect=_read_text, autospec=True),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._register_codex_mcp_server", return_value=True),
            patch("ouroboros.cli.commands.setup._install_codex_artifacts") as mock_install,
        ):
            assert setup_cmd._setup_codex("/usr/local/bin/codex") is False

        assert injected
        assert config_path.read_text(encoding="utf-8") == operator_config
        mock_install.assert_not_called()

    def test_atomic_setup_write_rechecks_generation_immediately_before_replace(
        self, tmp_path: Path
    ) -> None:
        """An edit arriving while the temp file is written must win over setup."""
        target = tmp_path / "config.yaml"
        target.write_text("before\n", encoding="utf-8")
        expected = setup_cmd._snapshot_path(target)
        original_require = setup_cmd._require_path_snapshot
        checks = 0

        def _require(path: Path, snapshot: setup_cmd._PathSnapshot) -> setup_cmd._PathSnapshot:
            nonlocal checks
            checks += 1
            if checks == 2:
                path.write_text("operator edit\n", encoding="utf-8")
            return original_require(path, snapshot)

        with (
            patch(
                "ouroboros.cli.commands.setup._require_path_snapshot",
                side_effect=_require,
            ),
            pytest.raises(setup_cmd._ConcurrentSetupMutationError),
        ):
            setup_cmd._atomic_write_text_if_current_matches(target, "setup edit\n", expected)

        assert target.read_text(encoding="utf-8") == "operator edit\n"
        assert not list(tmp_path.glob(".config.yaml.*.tmp"))

    def test_atomic_setup_write_tracks_actual_mode_without_fchmod(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Windows writes must snapshot the mode the filesystem actually kept."""
        target = tmp_path / "credentials.yaml"
        monkeypatch.delattr(setup_cmd.os, "fchmod", raising=False)
        monkeypatch.setattr(setup_cmd.os, "chmod", lambda *_args, **_kwargs: None)

        snapshot = setup_cmd._atomic_write_text(target, "token: secret\n", mode=0o644)

        assert target.read_text(encoding="utf-8") == "token: secret\n"
        assert target.read_bytes() == b"token: secret\n"
        assert snapshot == setup_cmd._snapshot_path(target)
        assert setup_cmd._require_path_snapshot(target, snapshot) == snapshot
        assert not list(tmp_path.glob(".credentials.yaml.*.tmp"))

    def test_atomic_setup_write_metadata_failure_preserves_previous_generation(
        self,
        tmp_path: Path,
    ) -> None:
        """A reported metadata failure must happen before replacement commits."""
        target = tmp_path / "config.yaml"
        target.write_text("operator: original\n", encoding="utf-8")

        with (
            patch(
                "pathlib.Path.lstat",
                autospec=True,
                side_effect=PermissionError("transient metadata failure"),
            ),
            pytest.raises(PermissionError, match="transient metadata failure"),
        ):
            setup_cmd._atomic_write_text(target, "setup: new\n", mode=0o644)

        assert target.read_text(encoding="utf-8") == "operator: original\n"
        assert not list(tmp_path.glob(".config.yaml.*.tmp"))

    def test_setup_codex_refuses_concurrent_codex_edit_before_profile_retirement(
        self, tmp_path: Path
    ) -> None:
        """A later TOML rewrite must not absorb an edit outside setup's generation."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text(
            "orchestrator:\n  runtime_backend: claude\nllm:\n  backend: claude\n",
            encoding="utf-8",
        )
        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        codex_config = codex_home / "config.toml"
        codex_config.write_text('model = "before"\n', encoding="utf-8")
        operator_toml = 'model = "operator-edit"\n'

        def _register(**kwargs: object) -> bool:
            codex_config.write_text('model = "setup-authored"\n', encoding="utf-8")
            expected = kwargs["expected_snapshots"]
            assert isinstance(expected, dict)
            expected[codex_config] = setup_cmd._snapshot_path(codex_config)
            return True

        def _install(**_kwargs: object) -> bool:
            codex_config.write_text(operator_toml, encoding="utf-8")
            return True

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.commands.setup._register_codex_mcp_server",
                side_effect=_register,
            ),
            patch(
                "ouroboros.cli.commands.setup._install_codex_artifacts",
                side_effect=_install,
            ),
            patch("ouroboros.cli.commands.setup._register_codex_worker_profile") as mock_worker,
        ):
            assert setup_cmd._setup_codex("/usr/local/bin/codex") is False

        assert codex_config.read_text(encoding="utf-8") == operator_toml
        mock_worker.assert_not_called()

    def test_setup_codex_preserves_post_write_ouroboros_config_edits(self, tmp_path: Path) -> None:
        """Authored generations cover config and credentials without a snapshot race."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        credentials_path = config_dir / "credentials.yaml"
        operator_config = "operator: config-edit\n"
        operator_credentials = "operator: credentials-edit\n"
        original_write = setup_cmd._atomic_write_text

        def _write(
            path: Path,
            text: str,
            *,
            mode: int | None = None,
            expected_current: setup_cmd._PathSnapshot | None = None,
        ) -> object:
            snapshot = original_write(
                path,
                text,
                mode=mode,
                expected_current=expected_current,
            )
            if path == config_path:
                config_path.write_text(operator_config, encoding="utf-8")
            elif path == credentials_path:
                credentials_path.write_text(operator_credentials, encoding="utf-8")
            return snapshot

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._atomic_write_text", side_effect=_write),
            patch("ouroboros.cli.commands.setup._install_codex_artifacts", return_value=False),
        ):
            assert setup_cmd._setup_codex("/usr/local/bin/codex") is False

        assert config_path.read_text(encoding="utf-8") == operator_config
        assert credentials_path.read_text(encoding="utf-8") == operator_credentials

    def test_setup_codex_preserves_post_install_artifact_edit(self, tmp_path: Path) -> None:
        """Artifact rollback compares against packaged bytes, not a later read."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text(
            "orchestrator:\n  runtime_backend: claude\nllm:\n  backend: claude\n",
            encoding="utf-8",
        )
        codex_home = tmp_path / ".codex"
        rules_dir = codex_home / "rules"
        rules_dir.mkdir(parents=True)
        rule_path = rules_dir / "ouroboros.md"
        rule_path.write_text("pre-setup rule\n", encoding="utf-8")
        operator_rule = "operator edit after artifact install\n"
        original_install = setup_cmd._install_codex_artifacts

        def _install_then_edit(**kwargs: object) -> bool:
            installed = original_install(**kwargs)
            assert installed is True
            rule_path.write_text(operator_rule, encoding="utf-8")
            return True

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.commands.setup._install_codex_artifacts",
                side_effect=_install_then_edit,
            ),
            patch(
                "ouroboros.cli.commands.setup._register_codex_worker_profile",
                return_value=False,
            ),
        ):
            assert setup_cmd._setup_codex("/usr/local/bin/codex") is False

        assert rule_path.read_text(encoding="utf-8") == operator_rule

    def test_install_codex_artifacts_restores_skill_when_generation_snapshot_fails(
        self, tmp_path: Path
    ) -> None:
        """Setup bookkeeping failure must restore the prior managed skill generation."""
        codex_home = tmp_path / ".codex"
        skill_path = codex_home / "skills" / "ouroboros-auto"
        skill_path.mkdir(parents=True)
        skill_entrypoint = skill_path / "SKILL.md"
        skill_entrypoint.write_text("installed auto skill\n", encoding="utf-8")
        required_snapshots = setup_cmd._snapshot_managed_codex_setup_paths(codex_home)
        written_snapshots: dict[Path, setup_cmd._PathSnapshot] = {}
        original_snapshot = setup_cmd._snapshot_path

        def _fail_packaged_skill_snapshot(
            path: Path,
            *,
            _seen: frozenset[Path] = frozenset(),
        ) -> setup_cmd._PathSnapshot:
            if path.name == "auto" and path.is_dir() and not path.is_relative_to(tmp_path):
                raise OSError("synthetic packaged skill snapshot failure")
            return original_snapshot(path, _seen=_seen)

        with patch(
            "ouroboros.cli.commands.setup._snapshot_path",
            side_effect=_fail_packaged_skill_snapshot,
        ):
            assert (
                setup_cmd._install_codex_artifacts(
                    codex_dir=codex_home,
                    expected_snapshots=written_snapshots,
                    required_snapshots=required_snapshots,
                )
                is False
            )

        assert skill_entrypoint.read_text(encoding="utf-8") == "installed auto skill\n"
        assert not tuple(skill_path.parent.glob(f".{skill_path.name}.*.tmp"))
        assert not tuple(skill_path.parent.glob(f".{skill_path.name}.*.backup"))

    def test_install_codex_artifacts_retains_rule_backup_on_snapshot_conflict(
        self, tmp_path: Path
    ) -> None:
        """Setup must retain the prior rule if bookkeeping races with another writer."""
        codex_home = tmp_path / ".codex"
        rule_path = codex_home / "rules" / "ouroboros.md"
        rule_path.parent.mkdir(parents=True)
        rule_path.write_text("previous operator rule\n", encoding="utf-8")
        required_snapshots = setup_cmd._snapshot_managed_codex_setup_paths(codex_home)
        written_snapshots: dict[Path, setup_cmd._PathSnapshot] = {}
        original_snapshot = setup_cmd._snapshot_path

        def _recreate_rule_while_packaged_snapshot_fails(
            path: Path,
            *,
            _seen: frozenset[Path] = frozenset(),
        ) -> setup_cmd._PathSnapshot:
            if path.name == "ouroboros.md" and path != rule_path:
                rule_path.write_text("concurrent operator rule\n", encoding="utf-8")
                raise OSError("synthetic packaged rule snapshot failure")
            return original_snapshot(path, _seen=_seen)

        with (
            patch(
                "ouroboros.cli.commands.setup._snapshot_path",
                side_effect=_recreate_rule_while_packaged_snapshot_fails,
            ),
            patch("ouroboros.cli.commands.setup.print_error") as mock_print_error,
        ):
            assert (
                setup_cmd._install_codex_artifacts(
                    codex_dir=codex_home,
                    expected_snapshots=written_snapshots,
                    required_snapshots=required_snapshots,
                )
                is False
            )

        assert rule_path.read_text(encoding="utf-8") == "concurrent operator rule\n"
        backups = tuple(rule_path.parent.glob(f".{rule_path.name}.*.backup"))
        assert len(backups) == 1
        assert backups[0].read_text(encoding="utf-8") == "previous operator rule\n"
        assert not tuple(rule_path.parent.glob(f".{rule_path.name}.*.tmp"))
        assert any(
            "Managed Codex artifact changed during rollback" in str(call.args[0])
            for call in mock_print_error.call_args_list
        )

    def test_install_codex_artifacts_preserves_rule_recreated_before_activation(
        self, tmp_path: Path
    ) -> None:
        """Setup must not replace a rule recreated after generation bookkeeping."""
        codex_home = tmp_path / ".codex"
        rule_path = codex_home / "rules" / "ouroboros.md"
        rule_path.parent.mkdir(parents=True)
        rule_path.write_text("previous operator rule\n", encoding="utf-8")
        required_snapshots = setup_cmd._snapshot_managed_codex_setup_paths(codex_home)
        written_snapshots: dict[Path, setup_cmd._PathSnapshot] = {}
        original_snapshot = setup_cmd._snapshot_path
        injected = False

        def _recreate_rule_after_packaged_snapshot(
            path: Path,
            *,
            _seen: frozenset[Path] = frozenset(),
        ) -> setup_cmd._PathSnapshot:
            nonlocal injected
            snapshot = original_snapshot(path, _seen=_seen)
            if path.name == "ouroboros.md" and path != rule_path and not injected:
                injected = True
                rule_path.write_text("concurrent operator rule\n", encoding="utf-8")
            return snapshot

        with (
            patch(
                "ouroboros.cli.commands.setup._snapshot_path",
                side_effect=_recreate_rule_after_packaged_snapshot,
            ),
            patch("ouroboros.cli.commands.setup.print_error") as mock_print_error,
        ):
            assert (
                setup_cmd._install_codex_artifacts(
                    codex_dir=codex_home,
                    expected_snapshots=written_snapshots,
                    required_snapshots=required_snapshots,
                )
                is False
            )

        assert injected is True
        assert rule_path.read_text(encoding="utf-8") == "concurrent operator rule\n"
        backups = tuple(rule_path.parent.glob(f".{rule_path.name}.*.backup"))
        assert len(backups) == 1
        assert backups[0].read_text(encoding="utf-8") == "previous operator rule\n"
        assert not tuple(rule_path.parent.glob(f".{rule_path.name}.*.tmp"))
        assert any(
            "Managed Codex artifact changed during rollback" in str(call.args[0])
            for call in mock_print_error.call_args_list
        )

    def test_install_codex_artifacts_restores_rule_replaced_during_backup_acquisition(
        self, tmp_path: Path
    ) -> None:
        """Setup must reject a different generation captured as its rollback backup."""
        codex_home = tmp_path / ".codex"
        rule_path = codex_home / "rules" / "ouroboros.md"
        rule_path.parent.mkdir(parents=True)
        rule_path.write_text("previous operator rule\n", encoding="utf-8")
        required_snapshots = setup_cmd._snapshot_managed_codex_setup_paths(codex_home)
        written_snapshots: dict[Path, setup_cmd._PathSnapshot] = {}
        original_replace = os.replace
        injected = False

        def _replace_rule_before_backup(source: str | Path, destination: str | Path) -> None:
            nonlocal injected
            source_path = Path(source)
            destination_path = Path(destination)
            if (
                source_path == rule_path
                and destination_path.name.endswith(".backup")
                and not injected
            ):
                injected = True
                rule_path.write_text("concurrent operator rule\n", encoding="utf-8")
            original_replace(source, destination)

        with (
            patch("ouroboros.codex.artifacts.os.replace", side_effect=_replace_rule_before_backup),
            patch("ouroboros.cli.commands.setup.print_error") as mock_print_error,
        ):
            assert (
                setup_cmd._install_codex_artifacts(
                    codex_dir=codex_home,
                    expected_snapshots=written_snapshots,
                    required_snapshots=required_snapshots,
                )
                is False
            )

        assert injected is True
        assert rule_path.read_text(encoding="utf-8") == "concurrent operator rule\n"
        assert not tuple(rule_path.parent.glob(f".{rule_path.name}.*.tmp"))
        assert not tuple(rule_path.parent.glob(f".{rule_path.name}.*.backup"))
        assert any(
            "Managed Codex artifact changed before activation" in str(call.args[0])
            for call in mock_print_error.call_args_list
        )

    @pytest.mark.parametrize("artifact_kind", ["rule", "skill"])
    def test_install_codex_artifacts_restores_pruned_generation_replaced_during_acquisition(
        self,
        tmp_path: Path,
        artifact_kind: str,
    ) -> None:
        """Setup pruning must acquire the exact stale rule or skill it validated."""
        codex_home = tmp_path / ".codex"
        if artifact_kind == "rule":
            target_path = codex_home / "rules" / "ouroboros-stale.md"
            target_path.parent.mkdir(parents=True)
            target_path.write_text("previous stale rule\n", encoding="utf-8")
        else:
            target_path = codex_home / "skills" / "ouroboros-stale"
            target_path.mkdir(parents=True)
            target_path.joinpath("SKILL.md").write_text("previous stale skill\n", encoding="utf-8")
        required_snapshots = setup_cmd._snapshot_managed_codex_setup_paths(codex_home)
        written_snapshots: dict[Path, setup_cmd._PathSnapshot] = {}
        original_replace = os.replace
        injected = False

        def _replace_artifact_before_backup(source: str | Path, destination: str | Path) -> None:
            nonlocal injected
            source_path = Path(source)
            destination_path = Path(destination)
            if (
                source_path == target_path
                and destination_path.name.endswith(".backup")
                and not injected
            ):
                injected = True
                if artifact_kind == "rule":
                    target_path.write_text("concurrent stale rule\n", encoding="utf-8")
                else:
                    target_path.joinpath("SKILL.md").write_text(
                        "concurrent stale skill\n", encoding="utf-8"
                    )
            original_replace(source, destination)

        with (
            patch(
                "ouroboros.codex.artifacts.os.replace",
                side_effect=_replace_artifact_before_backup,
            ),
            patch("ouroboros.cli.commands.setup.print_error") as mock_print_error,
        ):
            assert (
                setup_cmd._install_codex_artifacts(
                    codex_dir=codex_home,
                    expected_snapshots=written_snapshots,
                    required_snapshots=required_snapshots,
                )
                is False
            )

        assert injected is True
        if artifact_kind == "rule":
            assert target_path.read_text(encoding="utf-8") == "concurrent stale rule\n"
        else:
            assert target_path.joinpath("SKILL.md").read_text(encoding="utf-8") == (
                "concurrent stale skill\n"
            )
        assert not tuple(target_path.parent.glob(f".{target_path.name}.*.backup"))
        assert any(
            "Managed Codex artifact changed before removal" in str(call.args[0])
            for call in mock_print_error.call_args_list
        )

    def test_setup_codex_rejects_rule_changed_after_read_before_install(
        self, tmp_path: Path
    ) -> None:
        """Artifact installation must not overwrite a newer pre-install rule generation."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        original_config = "orchestrator:\n  runtime_backend: claude\nllm:\n  backend: claude\n"
        config_path.write_text(original_config, encoding="utf-8")
        codex_home = tmp_path / ".codex"
        rule_path = codex_home / "rules" / "ouroboros.md"
        rule_path.parent.mkdir(parents=True)
        rule_path.write_text("pre-setup rule\n", encoding="utf-8")
        operator_rule = "operator rule written before install\n"
        original_install = setup_cmd._install_codex_artifacts

        def _edit_then_install(**kwargs: object) -> bool:
            rule_path.write_text(operator_rule, encoding="utf-8")
            return original_install(**kwargs)

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.commands.setup._install_codex_artifacts",
                side_effect=_edit_then_install,
            ),
        ):
            assert setup_cmd._setup_codex("/usr/local/bin/codex") is False

        assert rule_path.read_text(encoding="utf-8") == operator_rule
        assert config_path.read_text(encoding="utf-8") == original_config

    def test_setup_codex_rejects_skill_changed_after_read_before_install(
        self, tmp_path: Path
    ) -> None:
        """Artifact installation must not remove a newer pre-install skill generation."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        original_config = "orchestrator:\n  runtime_backend: claude\nllm:\n  backend: claude\n"
        config_path.write_text(original_config, encoding="utf-8")
        codex_home = tmp_path / ".codex"
        skill_path = codex_home / "skills" / "ouroboros-welcome"
        skill_path.mkdir(parents=True)
        skill_entrypoint = skill_path / "SKILL.md"
        skill_entrypoint.write_text("pre-setup skill\n", encoding="utf-8")
        operator_skill = "operator skill written before install\n"
        original_install = setup_cmd._install_codex_artifacts

        def _edit_then_install(**kwargs: object) -> bool:
            skill_entrypoint.write_text(operator_skill, encoding="utf-8")
            return original_install(**kwargs)

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.commands.setup._install_codex_artifacts",
                side_effect=_edit_then_install,
            ),
        ):
            assert setup_cmd._setup_codex("/usr/local/bin/codex") is False

        assert skill_entrypoint.read_text(encoding="utf-8") == operator_skill
        assert config_path.read_text(encoding="utf-8") == original_config

    def test_setup_codex_preserves_post_retirement_config_edit(self, tmp_path: Path) -> None:
        """Profile retirement returns its authored generation before operator edits."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text(
            "orchestrator:\n  runtime_backend: claude\nllm:\n  backend: claude\n",
            encoding="utf-8",
        )
        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        codex_config = codex_home / "config.toml"
        profile_name = next(iter(setup_cmd._CODEX_DEFAULT_PROFILE_SECTIONS))
        settings = setup_cmd._CODEX_DEFAULT_PROFILE_SECTIONS[profile_name]
        codex_config.write_text(
            setup_cmd._render_codex_profile_section(profile_name, settings) + "\n",
            encoding="utf-8",
        )
        operator_marker = '\n[operator]\nvalue = "post-retirement-edit"\n'
        original_retire = setup_cmd._retire_codex_default_profiles

        def _retire_then_edit(**kwargs: object) -> None:
            original_retire(**kwargs)
            with codex_config.open("a", encoding="utf-8") as config_file:
                config_file.write(operator_marker)

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._install_codex_artifacts", return_value=True),
            patch(
                "ouroboros.cli.commands.setup._retire_codex_default_profiles",
                side_effect=_retire_then_edit,
            ),
            patch(
                "ouroboros.cli.commands.setup._register_codex_worker_profile",
                return_value=False,
            ),
        ):
            assert setup_cmd._setup_codex("/usr/local/bin/codex") is False

        assert "post-retirement-edit" in codex_config.read_text(encoding="utf-8")

    def test_setup_codex_removes_fresh_parent_topology_on_late_failure(
        self, tmp_path: Path
    ) -> None:
        """Fresh setup failure must remove empty config and Codex roots it created."""
        config_dir = tmp_path / ".ouroboros"
        codex_home = tmp_path / ".codex"

        def _register(**_kwargs: object) -> bool:
            codex_home.mkdir(parents=True)
            codex_config = codex_home / "config.toml"
            codex_config.write_text(
                '[mcp_servers.ouroboros]\ncommand = "new"\n',
                encoding="utf-8",
            )
            expected = _kwargs["expected_snapshots"]
            assert isinstance(expected, dict)
            expected[codex_config] = setup_cmd._snapshot_path(codex_config)
            return True

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.cli.commands.setup._register_codex_mcp_server", side_effect=_register),
            patch("ouroboros.cli.commands.setup._install_codex_artifacts", return_value=False),
            patch("ouroboros.cli.commands.setup._retire_codex_default_profiles") as mock_retire,
            patch(
                "ouroboros.cli.commands.setup._register_codex_worker_profile"
            ) as mock_worker_profile,
        ):
            assert setup_cmd._setup_codex("/usr/local/bin/codex") is False

        assert not config_dir.exists()
        assert not codex_home.exists()
        mock_retire.assert_not_called()
        mock_worker_profile.assert_not_called()

    def test_setup_codex_rolls_back_codex_home_artifacts_when_finish_fails(
        self, tmp_path: Path
    ) -> None:
        """Late setup failures must restore profile/rules/skills artifacts too."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        original_config = "orchestrator:\n  runtime_backend: claude\nllm:\n  backend: claude\n"
        config_path.write_text(original_config, encoding="utf-8")

        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        codex_config = codex_home / "config.toml"
        original_toml = '[mcp_servers.ouroboros]\ncommand = "old"\n'
        codex_config.write_text(original_toml, encoding="utf-8")
        profile_path = codex_home / "ouroboros-fast.config.toml"
        profile_contents = setup_cmd._render_codex_profile_v2_file(
            setup_cmd._CODEX_DEFAULT_PROFILE_SECTIONS["ouroboros-fast"]
        )
        profile_path.write_text(profile_contents, encoding="utf-8")

        def _install_artifacts(**_kwargs: object) -> bool:
            (codex_home / "rules").mkdir()
            rule_path = codex_home / "rules" / "ouroboros.md"
            rule_path.write_text("new rule\n", encoding="utf-8")
            (codex_home / "skills").mkdir()
            skill_path = codex_home / "skills" / "ouroboros-welcome"
            skill_path.mkdir()
            (skill_path / "SKILL.md").write_text(
                "new skill\n",
                encoding="utf-8",
            )
            (codex_home / "sessions").mkdir()
            (codex_home / "sessions" / "active.jsonl").write_text(
                "user session created during setup\n",
                encoding="utf-8",
            )
            expected = _kwargs["expected_snapshots"]
            assert isinstance(expected, dict)
            expected[rule_path] = setup_cmd._snapshot_path(rule_path)
            expected[skill_path] = setup_cmd._snapshot_path(skill_path)
            return True

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._register_codex_mcp_server", return_value=True),
            patch(
                "ouroboros.cli.commands.setup._install_codex_artifacts",
                side_effect=_install_artifacts,
            ),
            patch("ouroboros.cli.commands.setup._codex_uses_profile_v2", return_value=True),
            patch(
                "ouroboros.cli.commands.setup._register_codex_worker_profile",
                return_value=False,
            ),
        ):
            assert setup_cmd._setup_codex("/usr/local/bin/codex") is False

        assert config_path.read_text(encoding="utf-8") == original_config
        assert codex_config.read_text(encoding="utf-8") == original_toml
        assert profile_path.read_text(encoding="utf-8") == profile_contents
        assert not (codex_home / "rules").exists()
        assert not (codex_home / "rules" / "ouroboros.md").exists()
        assert not (codex_home / "skills").exists()
        assert not (codex_home / "skills" / "ouroboros-welcome").exists()
        assert (codex_home / "sessions" / "active.jsonl").read_text(encoding="utf-8") == (
            "user session created during setup\n"
        )

    def test_setup_codex_rollback_does_not_restore_nested_symlink_targets(
        self, tmp_path: Path
    ) -> None:
        """Rollback must restore managed links without rewriting external targets."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        original_config = "orchestrator:\n  runtime_backend: claude\nllm:\n  backend: claude\n"
        config_path.write_text(original_config, encoding="utf-8")

        codex_home = tmp_path / ".codex"
        skill_dir = codex_home / "skills" / "ouroboros-welcome"
        skill_dir.mkdir(parents=True)
        external_target = tmp_path / "external-skill-note.txt"
        external_target.write_text("before setup\n", encoding="utf-8")
        nested_link = skill_dir / "external-note"
        nested_link.symlink_to(external_target)

        def _install_artifacts(**_kwargs: object) -> bool:
            external_target.write_text("concurrent external update\n", encoding="utf-8")
            return True

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._register_codex_mcp_server", return_value=True),
            patch(
                "ouroboros.cli.commands.setup._install_codex_artifacts",
                side_effect=_install_artifacts,
            ),
            patch("ouroboros.cli.commands.setup._codex_uses_profile_v2", return_value=True),
            patch(
                "ouroboros.cli.commands.setup._register_codex_worker_profile",
                return_value=False,
            ),
        ):
            assert setup_cmd._setup_codex("/usr/local/bin/codex") is False

        assert config_path.read_text(encoding="utf-8") == original_config
        assert nested_link.is_symlink()
        assert os.readlink(nested_link) == str(external_target)
        assert external_target.read_text(encoding="utf-8") == "concurrent external update\n"

    def test_setup_codex_rollback_preserves_concurrent_user_rules_under_fresh_parent(
        self, tmp_path: Path
    ) -> None:
        """Rollback must remove managed children without deleting concurrent user rules."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        original_config = "orchestrator:\n  runtime_backend: claude\nllm:\n  backend: claude\n"
        config_path.write_text(original_config, encoding="utf-8")

        codex_home = tmp_path / ".codex"
        codex_home.mkdir()

        def _install_artifacts(**_kwargs: object) -> bool:
            rules_dir = codex_home / "rules"
            rules_dir.mkdir()
            rule_path = rules_dir / "ouroboros.md"
            rule_path.write_text("managed rule\n", encoding="utf-8")
            (rules_dir / "user.md").write_text("user rule\n", encoding="utf-8")
            expected = _kwargs["expected_snapshots"]
            assert isinstance(expected, dict)
            expected[rule_path] = setup_cmd._snapshot_path(rule_path)
            return True

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._register_codex_mcp_server", return_value=True),
            patch(
                "ouroboros.cli.commands.setup._install_codex_artifacts",
                side_effect=_install_artifacts,
            ),
            patch("ouroboros.cli.commands.setup._retire_codex_default_profiles"),
            patch(
                "ouroboros.cli.commands.setup._register_codex_worker_profile",
                return_value=False,
            ),
        ):
            assert setup_cmd._setup_codex("/usr/local/bin/codex") is False

        assert config_path.read_text(encoding="utf-8") == original_config
        assert not (codex_home / "rules" / "ouroboros.md").exists()
        assert (codex_home / "rules" / "user.md").read_text(encoding="utf-8") == "user rule\n"

    def test_setup_codex_rollback_preserves_concurrent_managed_file_edits(
        self, tmp_path: Path
    ) -> None:
        """Rollback must not overwrite operator edits made after setup writes."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text(
            "orchestrator:\n  runtime_backend: claude\nllm:\n  backend: claude\n",
            encoding="utf-8",
        )

        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        codex_config = codex_home / "config.toml"
        codex_config.write_text('model = "before"\n', encoding="utf-8")

        concurrent_config = "operator: config-edit\n"
        concurrent_codex_config = 'model = "operator-edit"\n'

        def _install_artifacts(**_kwargs: object) -> bool:
            config_path.write_text(concurrent_config, encoding="utf-8")
            codex_config.write_text(concurrent_codex_config, encoding="utf-8")
            return True

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.commands.setup._install_codex_artifacts",
                side_effect=_install_artifacts,
            ),
            patch("ouroboros.cli.commands.setup._retire_codex_default_profiles"),
            patch(
                "ouroboros.cli.commands.setup._register_codex_worker_profile",
                return_value=False,
            ),
        ):
            assert setup_cmd._setup_codex("/usr/local/bin/codex") is False

        assert config_path.read_text(encoding="utf-8") == concurrent_config
        assert codex_config.read_text(encoding="utf-8") == concurrent_codex_config

    def test_register_codex_mcp_server_preserves_existing_config_mode(self, tmp_path: Path) -> None:
        """Rewriting Codex config must not widen a private existing file."""
        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        codex_config = codex_home / "config.toml"
        codex_config.write_text('model = "gpt-test"\n', encoding="utf-8")
        codex_config.chmod(0o600)

        with patch("pathlib.Path.home", return_value=tmp_path):
            assert setup_cmd._register_codex_mcp_server() is True

        assert stat.S_IMODE(codex_config.stat().st_mode) == 0o600
        assert "[mcp_servers.ouroboros]" in codex_config.read_text(encoding="utf-8")

    def test_setup_codex_rejects_managed_codex_symlink_before_writing(self, tmp_path: Path) -> None:
        """Setup must not write MCP config through a symlinked managed path."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        original_config = "orchestrator:\n  runtime_backend: claude\nllm:\n  backend: claude\n"
        config_path.write_text(original_config, encoding="utf-8")

        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        target_dir = tmp_path / "targets"
        target_dir.mkdir()
        config_target = target_dir / "config-target.toml"
        original_toml = '[mcp_servers.ouroboros]\ncommand = "old"\n'
        config_target.write_text(original_toml, encoding="utf-8")
        codex_config = codex_home / "config.toml"
        codex_config.symlink_to(config_target)

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.commands.setup._register_codex_mcp_server",
                side_effect=AssertionError("must fail before MCP write"),
            ),
        ):
            assert setup_cmd._setup_codex("/usr/local/bin/codex") is False

        assert config_path.read_text(encoding="utf-8") == original_config
        assert codex_config.is_symlink()
        assert os.readlink(codex_config) == str(config_target)
        assert config_target.read_text(encoding="utf-8") == original_toml

    def test_setup_codex_rejects_dangling_config_symlink_before_writing(
        self, tmp_path: Path
    ) -> None:
        """A dangling managed symlink is still an unsafe write-through topology."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text(
            "orchestrator:\n  runtime_backend: claude\nllm:\n  backend: claude\n",
            encoding="utf-8",
        )

        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        target_dir = tmp_path / "targets"
        target_dir.mkdir()
        dangling_target = target_dir / "missing-config.toml"
        codex_config = codex_home / "config.toml"
        codex_config.symlink_to(dangling_target)

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.commands.setup._register_codex_mcp_server",
                side_effect=AssertionError("must fail before MCP write"),
            ),
        ):
            assert setup_cmd._setup_codex("/usr/local/bin/codex") is False

        assert codex_config.is_symlink()
        assert os.readlink(codex_config) == str(dangling_target)
        assert not dangling_target.exists()

    def test_setup_codex_snapshot_handles_managed_symlink_cycle(self, tmp_path: Path) -> None:
        """Managed path snapshots must not recurse through a symlink back to Codex home."""
        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        codex_config = codex_home / "config.toml"
        codex_config.symlink_to(codex_home, target_is_directory=True)

        snapshot = setup_cmd._snapshot_path(codex_config)

        assert snapshot.kind == "symlink"
        assert snapshot.link_target == str(codex_home)
        assert snapshot.link_target_snapshot is not None
        assert snapshot.link_target_snapshot.kind == "directory"

    def test_setup_codex_managed_paths_accept_stale_rules_file(self, tmp_path: Path) -> None:
        """A stale regular rules path must not crash setup snapshot discovery."""
        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        rules_path = codex_home / "rules"
        rules_path.write_text("not a directory\n", encoding="utf-8")

        paths = setup_cmd._managed_codex_setup_paths(codex_home)

        assert rules_path in paths

    def test_setup_codex_rejects_stale_rules_file_before_snapshot(self, tmp_path: Path) -> None:
        """A stale regular rules leaf must be reported without traceback."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text(
            "orchestrator:\n  runtime_backend: claude\nllm:\n  backend: claude\n",
            encoding="utf-8",
        )
        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        (codex_home / "rules").write_text("not a directory\n", encoding="utf-8")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.commands.setup._register_codex_mcp_server",
                side_effect=AssertionError("must fail before MCP write"),
            ),
        ):
            assert setup_cmd._setup_codex("/usr/local/bin/codex") is False

    def test_legacy_codex_profile_with_comment_is_customized(
        self,
        tmp_path: Path,
    ) -> None:
        """Operator comments in generated legacy profiles must prevent retirement."""
        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        profile_name = "ouroboros-fast"
        generated = setup_cmd._render_codex_profile_section(
            profile_name,
            setup_cmd._CODEX_DEFAULT_PROFILE_SECTIONS[profile_name],
        )
        (codex_home / "config.toml").write_text(
            generated + "\n# keep this aligned with staging\n",
            encoding="utf-8",
        )

        with patch("pathlib.Path.home", return_value=tmp_path):
            assert setup_cmd._legacy_codex_profile_is_customized(profile_name) is True

    def test_retire_codex_default_profiles_uses_atomic_write_and_propagates_failure(
        self, tmp_path: Path
    ) -> None:
        """Retiring legacy profile anchors must not truncate config.toml silently."""
        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        codex_config = codex_home / "config.toml"
        codex_config.write_text(
            setup_cmd._render_codex_profile_section(
                "ouroboros-fast",
                setup_cmd._CODEX_DEFAULT_PROFILE_SECTIONS["ouroboros-fast"],
            )
            + "\n",
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "ouroboros.cli.commands.setup._atomic_write_text",
                side_effect=OSError("disk full"),
            ) as mock_atomic,
        ):
            with pytest.raises(OSError, match="disk full"):
                setup_cmd._retire_codex_default_profiles()

        mock_atomic.assert_called_once()
        assert codex_config.read_text(encoding="utf-8") == (
            setup_cmd._render_codex_profile_section(
                "ouroboros-fast",
                setup_cmd._CODEX_DEFAULT_PROFILE_SECTIONS["ouroboros-fast"],
            )
            + "\n"
        )

    def test_setup_cli_codex_failure_exits_before_success_banner(self) -> None:
        """Top-level setup must propagate Codex setup failure to exit status."""
        runner = CliRunner()
        with (
            patch(
                "ouroboros.cli.commands.setup._detect_runtimes",
                return_value={"claude": None, "codex": "/usr/bin/codex", "opencode": None},
            ),
            patch("ouroboros.cli.commands.setup._setup_codex", return_value=False),
        ):
            result = runner.invoke(
                setup_cmd.app,
                ["--runtime", "codex", "--non-interactive"],
            )

        assert result.exit_code == 1
        assert "Setup complete!" not in result.output

    def test_fresh_codex_setup_installs_every_role_effort_mapping(self, tmp_path: Path) -> None:
        """Fresh config is model-neutral while Codex role defaults stay effective."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._install_codex_artifacts"),
            patch("ouroboros.cli.commands.setup._register_codex_mcp_server"),
            patch("ouroboros.cli.commands.setup._retire_codex_default_profiles"),
            patch("ouroboros.cli.commands.setup._register_codex_worker_profile"),
        ):
            setup_cmd._setup_codex("/usr/local/bin/codex")

        config_dict = yaml.safe_load((config_dir / "config.yaml").read_text(encoding="utf-8"))
        assert "qa_model" not in config_dict["llm"]
        assert "dependency_analysis_model" not in config_dict["llm"]
        assert "ontology_analysis_model" not in config_dict["llm"]
        assert "context_compression_model" not in config_dict["llm"]
        assert "default_model" not in config_dict["clarification"]
        assert "semantic_model" not in config_dict["evaluation"]
        assert "assertion_extraction_model" not in config_dict["evaluation"]
        assert "wonder_model" not in config_dict["resilience"]
        assert "reflect_model" not in config_dict["resilience"]
        assert "models" not in config_dict["consensus"]
        assert "advocate_model" not in config_dict["consensus"]
        assert "devil_model" not in config_dict["consensus"]
        assert "judge_model" not in config_dict["consensus"]

        role_profiles = config_dict["llm_role_profiles"]
        assert role_profiles == setup_cmd._CODEX_DEFAULT_LLM_ROLE_PROFILES
        for profile_name in set(role_profiles.values()):
            effort = config_dict["llm_profiles"][profile_name]["providers"]["codex"][
                "reasoning_effort"
            ]
            assert effort in {"low", "medium", "high", "xhigh"}

        from ouroboros.config.loader import get_qa_model, load_config

        loaded = load_config(config_dir / "config.yaml")
        with patch("ouroboros.config.loader.load_config", return_value=loaded):
            assert get_qa_model(backend="codex") == "default"
        with patch("ouroboros.providers.profiles.load_config", return_value=loaded):
            resolved = resolve_completion_profile(
                CompletionConfig(model="default", role="qa"),
                backend="codex",
            )
        assert resolved.config.model == "default"
        assert resolved.config.reasoning_effort == "xhigh"

    def test_existing_default_config_installs_every_role_effort_mapping(self) -> None:
        """Serialized shipped defaults must not suppress Codex effort profiles."""
        config_dict = get_default_config().model_dump(mode="python")

        _, _, added_roles = setup_cmd._install_codex_default_llm_profiles(config_dict)

        assert set(added_roles) == set(setup_cmd._CODEX_DEFAULT_LLM_ROLE_PROFILES)
        assert config_dict["llm_role_profiles"] == setup_cmd._CODEX_DEFAULT_LLM_ROLE_PROFILES

    def test_codex_setup_preserves_effective_codex_cli_alias_profile(self) -> None:
        """An aliased pin stays effective instead of being shadowed by ``codex``."""
        config_dict = {
            "llm_profiles": {
                "fast": {
                    "providers": {"codex_cli": {"model": "user-pin", "profile": "user-profile"}}
                }
            }
        }

        setup_cmd._install_codex_default_llm_profiles(config_dict)

        providers = config_dict["llm_profiles"]["fast"]["providers"]
        assert set(providers) == {"codex_cli"}
        config = OuroborosConfig.model_validate(config_dict)
        with patch("ouroboros.providers.profiles.load_config", return_value=config):
            resolved = resolve_completion_profile(
                CompletionConfig(model="default", role="assertion_extraction"), backend="codex"
            )

        assert resolved.config.model == "user-pin"
        assert resolved.backend_profile == "user-profile"

    def test_codex_setup_preserves_existing_profile_top_level_model_for_codex(self) -> None:
        """Provider-neutral profile models stay effective for Codex."""
        config_dict = {
            "llm_profiles": {
                "fast": {
                    "model": "anthropic/custom-fast",
                    "providers": {},
                }
            }
        }

        setup_cmd._install_codex_default_llm_profiles(config_dict)

        assert config_dict["llm_profiles"]["fast"]["providers"]["codex"] == {
            "reasoning_effort": "low",
        }
        config = OuroborosConfig.model_validate(config_dict)
        with patch("ouroboros.providers.profiles.load_config", return_value=config):
            resolved = resolve_completion_profile(
                CompletionConfig(model="default", role="assertion_extraction"), backend="codex"
            )

        assert resolved.config.model == "anthropic/custom-fast"

    def test_codex_setup_neutralizes_existing_effort_only_provider_model(self) -> None:
        """Effort-only Codex providers must not inherit provider-neutral model pins."""
        config_dict = {
            "llm_profiles": {
                "fast": {
                    "model": "anthropic/custom-fast",
                    "providers": {"codex": {"reasoning_effort": "low"}},
                }
            }
        }

        setup_cmd._install_codex_default_llm_profiles(config_dict)

        assert config_dict["llm_profiles"]["fast"]["providers"]["codex"] == {
            "reasoning_effort": "low",
        }

    def test_setup_codex_aborts_on_non_mapping_config(self, tmp_path: Path) -> None:
        """Malformed top-level config should not be rewritten by Codex setup."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        original = "- not-a-mapping\n"
        config_path.write_text(original, encoding="utf-8")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._install_codex_artifacts") as mock_install,
            patch("ouroboros.cli.commands.setup._register_codex_mcp_server") as mock_register,
            patch("ouroboros.cli.commands.setup._register_codex_default_profiles") as mock_profiles,
            patch("ouroboros.cli.commands.setup.print_error") as mock_error,
        ):
            setup_cmd._setup_codex("/usr/local/bin/codex")

        assert config_path.read_text(encoding="utf-8") == original
        mock_error.assert_called_once()
        mock_install.assert_not_called()
        mock_register.assert_not_called()
        mock_profiles.assert_not_called()

    def test_setup_codex_aborts_on_unreadable_existing_config_path(self, tmp_path: Path) -> None:
        """A stale config.yaml directory should fail closed before side effects."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.mkdir()

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._install_codex_artifacts") as mock_install,
            patch("ouroboros.cli.commands.setup._register_codex_mcp_server") as mock_register,
            patch("ouroboros.cli.commands.setup._register_codex_default_profiles") as mock_profiles,
            patch("ouroboros.cli.commands.setup.print_error") as mock_error,
        ):
            assert setup_cmd._setup_codex("/usr/local/bin/codex") is False

        assert "Could not read config.yaml" in mock_error.call_args.args[0]
        mock_install.assert_not_called()
        mock_register.assert_not_called()
        mock_profiles.assert_not_called()

    def test_setup_codex_aborts_on_malformed_existing_config_yaml(self, tmp_path: Path) -> None:
        """Malformed config.yaml should fail closed before side effects."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        original = "orchestrator: [\n"
        config_path.write_text(original, encoding="utf-8")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._install_codex_artifacts") as mock_install,
            patch("ouroboros.cli.commands.setup._register_codex_mcp_server") as mock_register,
            patch("ouroboros.cli.commands.setup._register_codex_default_profiles") as mock_profiles,
            patch("ouroboros.cli.commands.setup.print_error") as mock_error,
        ):
            assert setup_cmd._setup_codex("/usr/local/bin/codex") is False

        assert config_path.read_text(encoding="utf-8") == original
        assert "Could not read config.yaml" in mock_error.call_args.args[0]
        mock_install.assert_not_called()
        mock_register.assert_not_called()
        mock_profiles.assert_not_called()

    def test_setup_codex_aborts_on_invalid_existing_llm_profiles_section(
        self, tmp_path: Path
    ) -> None:
        """Invalid existing profile sections should be reported, not replaced."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        original = yaml.safe_dump({"llm_profiles": ["not", "a", "mapping"]}, sort_keys=False)
        config_path.write_text(original, encoding="utf-8")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._install_codex_artifacts") as mock_install,
            patch("ouroboros.cli.commands.setup._register_codex_mcp_server") as mock_register,
            patch("ouroboros.cli.commands.setup._register_codex_default_profiles") as mock_profiles,
            patch("ouroboros.cli.commands.setup.print_error") as mock_error,
        ):
            setup_cmd._setup_codex("/usr/local/bin/codex")

        assert config_path.read_text(encoding="utf-8") == original
        assert "llm_profiles" in mock_error.call_args.args[0]
        mock_install.assert_not_called()
        mock_register.assert_not_called()
        mock_profiles.assert_not_called()

    def test_setup_codex_aborts_on_invalid_existing_profile_provider_mapping(
        self, tmp_path: Path
    ) -> None:
        """Invalid nested provider profile mappings should not be auto-repaired."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        original = yaml.safe_dump(
            {"llm_profiles": {"fast": {"providers": ["not-a-mapping"]}}},
            sort_keys=False,
        )
        config_path.write_text(original, encoding="utf-8")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._install_codex_artifacts") as mock_install,
            patch("ouroboros.cli.commands.setup._register_codex_mcp_server") as mock_register,
            patch("ouroboros.cli.commands.setup._register_codex_default_profiles") as mock_profiles,
            patch("ouroboros.cli.commands.setup.print_error") as mock_error,
        ):
            setup_cmd._setup_codex("/usr/local/bin/codex")

        assert config_path.read_text(encoding="utf-8") == original
        assert "providers" in mock_error.call_args.args[0]
        mock_install.assert_not_called()
        mock_register.assert_not_called()
        mock_profiles.assert_not_called()

    def test_setup_codex_preserves_existing_role_overrides(self, tmp_path: Path) -> None:
        """Re-running Codex setup should not wipe role-specific model overrides."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text(
            yaml.safe_dump(
                {
                    "orchestrator": {
                        "runtime_backend": "claude",
                        "default_max_turns": 15,
                    },
                    "llm": {
                        "backend": "litellm",
                        "qa_model": "gpt-5.4",
                    },
                    "clarification": {
                        "default_model": "gpt-5.4",
                    },
                    "evaluation": {
                        "semantic_model": "gpt-5.4",
                    },
                    "consensus": {
                        "advocate_model": "gpt-5.4",
                        "devil_model": "gpt-5.4",
                        "judge_model": "gpt-5.4",
                    },
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._install_codex_artifacts"),
            patch("ouroboros.cli.commands.setup._register_codex_mcp_server"),
            patch("ouroboros.cli.commands.setup._register_codex_default_profiles"),
        ):
            setup_cmd._setup_codex("/usr/local/bin/codex")

        config_dict = yaml.safe_load(config_path.read_text(encoding="utf-8"))

        assert config_dict["orchestrator"]["runtime_backend"] == "codex"
        assert config_dict["orchestrator"]["codex_cli_path"] == "/usr/local/bin/codex"
        assert config_dict["orchestrator"]["default_max_turns"] == 15
        assert config_dict["llm"]["backend"] == "codex"
        assert config_dict["llm"]["qa_model"] == "gpt-5.4"
        assert config_dict["clarification"]["default_model"] == "gpt-5.4"
        assert config_dict["evaluation"]["semantic_model"] == "gpt-5.4"
        assert config_dict["consensus"]["advocate_model"] == "gpt-5.4"
        assert config_dict["consensus"]["devil_model"] == "gpt-5.4"
        assert config_dict["consensus"]["judge_model"] == "gpt-5.4"
        assert "qa" not in config_dict["llm_role_profiles"]
        assert "clarification" not in config_dict["llm_role_profiles"]
        assert "semantic_evaluation" not in config_dict["llm_role_profiles"]
        assert "consensus_advocate" not in config_dict["llm_role_profiles"]
        assert "consensus_judge" not in config_dict["llm_role_profiles"]
        assert "ontology_analysis" not in config_dict["llm_role_profiles"]

    def test_setup_codex_update_refresh_preserves_split_llm_backend(self, tmp_path: Path) -> None:
        """Updater refreshes Codex integration without rerouting LLM traffic."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text(
            yaml.safe_dump(
                {
                    "orchestrator": {"runtime_backend": "codex"},
                    "llm": {"backend": "litellm", "qa_model": "openai/gpt-5.4"},
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._install_codex_artifacts"),
            patch("ouroboros.cli.commands.setup._register_codex_mcp_server"),
            patch("ouroboros.cli.commands.setup._register_codex_worker_profile"),
        ):
            assert (
                setup_cmd._setup_codex(
                    "/usr/local/bin/codex",
                    preserve_existing_llm=True,
                )
                is True
            )

        config_dict = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert config_dict["orchestrator"]["runtime_backend"] == "codex"
        assert config_dict["orchestrator"]["codex_cli_path"] == "/usr/local/bin/codex"
        assert config_dict["llm"]["backend"] == "litellm"
        assert config_dict["llm"]["qa_model"] == "openai/gpt-5.4"

    def test_setup_codex_clears_execute_default_model_when_execute_switches_to_codex(
        self, tmp_path: Path
    ) -> None:
        """A legacy Execute-stage model pin must not shadow Codex's selected model."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text(
            yaml.safe_dump(
                {
                    "orchestrator": {
                        "runtime_backend": "claude",
                    },
                    "llm": {
                        "backend": "claude_code",
                    },
                    "execution": {
                        "default_model": "gpt-5",
                    },
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._install_codex_artifacts"),
            patch("ouroboros.cli.commands.setup._register_codex_mcp_server"),
            patch("ouroboros.cli.commands.setup._register_codex_default_profiles"),
        ):
            setup_cmd._setup_codex("/usr/local/bin/codex")

        config_dict = yaml.safe_load(config_path.read_text(encoding="utf-8"))

        assert config_dict["orchestrator"]["runtime_backend"] == "codex"
        assert config_dict["execution"]["default_model"] is None

    def test_setup_codex_treats_shipped_legacy_default_model_as_unpinned(
        self, tmp_path: Path
    ) -> None:
        """A historical shipped default is not distinguishable from an untouched config."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text(
            yaml.safe_dump(
                {
                    "llm": {
                        "backend": "litellm",
                        "qa_model": "claude-sonnet-4-20250514",
                    },
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._install_codex_artifacts"),
            patch("ouroboros.cli.commands.setup._register_codex_mcp_server"),
            patch("ouroboros.cli.commands.setup._register_codex_default_profiles"),
        ):
            setup_cmd._setup_codex("/usr/local/bin/codex")

        config_dict = yaml.safe_load(config_path.read_text(encoding="utf-8"))

        assert config_dict["llm"]["backend"] == "codex"
        assert config_dict["llm"]["qa_model"] == "claude-sonnet-4-20250514"
        assert config_dict["llm_role_profiles"]["qa"] == "frontier"

    def test_setup_codex_preserves_existing_custom_consensus_roster(self, tmp_path: Path) -> None:
        """A user-authored consensus roster remains a pin across Codex setup."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        custom_roster = ["vendor/alpha", "vendor/beta", "vendor/gamma"]
        config_path.write_text(
            yaml.safe_dump({"consensus": {"models": custom_roster}}, sort_keys=False),
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._install_codex_artifacts"),
            patch("ouroboros.cli.commands.setup._register_codex_mcp_server"),
            patch("ouroboros.cli.commands.setup._retire_codex_default_profiles"),
            patch("ouroboros.cli.commands.setup._register_codex_worker_profile"),
        ):
            assert setup_cmd._setup_codex("/usr/local/bin/codex") is True

        config_dict = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert config_dict["consensus"]["models"] == custom_roster
        assert "consensus_vote" not in config_dict["llm_role_profiles"]

    def test_setup_codex_merges_codex_mapping_into_existing_profiles(self, tmp_path: Path) -> None:
        """Existing same-name profiles should be made safe before role mappings target them."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text(
            yaml.safe_dump(
                {
                    "llm_profiles": {
                        "fast": {
                            "model": "anthropic/custom-fast",
                            "providers": {"anthropic": {"model": "claude-haiku"}},
                        }
                    }
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._install_codex_artifacts"),
            patch("ouroboros.cli.commands.setup._register_codex_mcp_server"),
            patch("ouroboros.cli.commands.setup._register_codex_default_profiles"),
        ):
            setup_cmd._setup_codex("/usr/local/bin/codex")

        config_dict = yaml.safe_load(config_path.read_text(encoding="utf-8"))

        fast_profile = config_dict["llm_profiles"]["fast"]
        assert fast_profile["model"] == "anthropic/custom-fast"
        assert fast_profile["providers"]["anthropic"]["model"] == "claude-haiku"
        assert fast_profile["providers"]["codex"]["reasoning_effort"] == "low"
        assert config_dict["llm_role_profiles"]["assertion_extraction"] == "fast"

    def test_setup_codex_preserves_existing_codex_model_profile_mapping(
        self, tmp_path: Path
    ) -> None:
        """Existing same-name Codex model pins should not be shadowed by profile anchors."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text(
            yaml.safe_dump(
                {
                    "llm_profiles": {
                        "fast": {
                            "providers": {"codex": {"model": "gpt-existing-pin"}},
                        }
                    }
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._install_codex_artifacts"),
            patch("ouroboros.cli.commands.setup._register_codex_mcp_server"),
            patch("ouroboros.cli.commands.setup._register_codex_default_profiles"),
        ):
            setup_cmd._setup_codex("/usr/local/bin/codex")

        config_dict = yaml.safe_load(config_path.read_text(encoding="utf-8"))

        codex_profile = config_dict["llm_profiles"]["fast"]["providers"]["codex"]
        assert codex_profile == {"model": "gpt-existing-pin"}
        assert config_dict["llm_role_profiles"]["assertion_extraction"] == "fast"

    def test_setup_codex_does_not_register_claude_integration(self, tmp_path: Path) -> None:
        """Codex setup should stay scoped to Codex even when Claude is installed."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text("{}", encoding="utf-8")

        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._install_codex_artifacts"),
            patch("ouroboros.cli.commands.setup._register_codex_mcp_server"),
            patch("ouroboros.cli.commands.setup._ensure_claude_mcp_entry") as mock_claude,
        ):
            setup_cmd._setup_codex("/usr/local/bin/codex")

        mock_claude.assert_not_called()

    def test_setup_codex_rejects_dangling_ouroboros_config_symlink_before_writing(
        self, tmp_path: Path
    ) -> None:
        """Setup must not write or roll back through Ouroboros config symlinks."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_target = tmp_path / "missing-config-target.yaml"
        config_path.symlink_to(config_target)
        credentials_path = config_dir / "credentials.yaml"
        credentials_target = tmp_path / "missing-credentials-target.yaml"
        credentials_path.symlink_to(credentials_target)

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.commands.setup._register_codex_mcp_server",
                side_effect=AssertionError("must fail before MCP write"),
            ),
        ):
            assert setup_cmd._setup_codex("/usr/local/bin/codex") is False

        assert config_path.is_symlink()
        assert os.readlink(config_path) == str(config_target)
        assert not config_target.exists()
        assert credentials_path.is_symlink()
        assert os.readlink(credentials_path) == str(credentials_target)
        assert not credentials_target.exists()

    def test_setup_codex_rejects_config_symlink_chain_before_writing(self, tmp_path: Path) -> None:
        """Setup must not write through config.yaml symlink chains."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        final_target = tmp_path / "actual-config.yaml"
        final_target.write_text("llm:\n  backend: claude_code\n", encoding="utf-8")
        middle_link = tmp_path / "middle-config.yaml"
        middle_link.symlink_to(final_target)
        config_path = config_dir / "config.yaml"
        config_path.symlink_to(middle_link)
        before = final_target.read_text(encoding="utf-8")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.commands.setup._register_codex_mcp_server",
                side_effect=AssertionError("must fail before MCP write"),
            ),
        ):
            assert setup_cmd._setup_codex("/usr/local/bin/codex") is False

        assert config_path.is_symlink()
        assert os.readlink(config_path) == str(middle_link)
        assert middle_link.is_symlink()
        assert os.readlink(middle_link) == str(final_target)
        assert final_target.read_text(encoding="utf-8") == before


class TestClaudeSetup:
    """Tests for Claude-specific setup behavior."""

    def test_native_windows_auto_skips_persistent_mcp_registration(self, tmp_path: Path) -> None:
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.cli.commands.setup.is_native_windows", return_value=True),
        ):
            assert setup_cmd._register_codex_mcp_server(mode="auto") is True

        assert not (tmp_path / ".codex" / "config.toml").exists()

    def test_native_windows_stdio_fails_closed(self, tmp_path: Path) -> None:
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.cli.commands.setup.is_native_windows", return_value=True),
        ):
            assert setup_cmd._register_codex_mcp_server(mode="stdio") is False

    def test_native_windows_http_mode_writes_explicit_loopback_url(self, tmp_path: Path) -> None:
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.cli.commands.setup.is_native_windows", return_value=True),
        ):
            assert setup_cmd._register_codex_mcp_server(mode="http") is True

        config = (tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8")
        assert 'url = "http://127.0.0.1:8765/mcp"' in config

    def test_native_windows_http_fails_without_launchable_mcp(self, tmp_path: Path) -> None:
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.cli.commands.setup.is_native_windows", return_value=True),
            patch("ouroboros.cli.commands.setup._codex_release_mcp_launcher", return_value=None),
            patch("ouroboros.cli.commands.setup._is_dev_ouroboros_build", return_value=False),
        ):
            assert setup_cmd._register_codex_mcp_server(mode="http") is False

        assert not (tmp_path / ".codex" / "config.toml").exists()

    def test_native_windows_http_repairs_endpointless_entry(self, tmp_path: Path) -> None:
        config = tmp_path / ".codex" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text("[mcp_servers.ouroboros]\n", encoding="utf-8")
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.cli.commands.setup.is_native_windows", return_value=True),
            patch(
                "ouroboros.cli.commands.setup._codex_release_mcp_launcher",
                return_value=("uvx", ["--from", "ouroboros-ai[mcp]", "ouroboros", "mcp", "serve"]),
            ),
            patch("ouroboros.cli.windows_codex_mcp._launcher_is_usable", return_value=True),
        ):
            assert setup_cmd._register_codex_mcp_server(mode="http") is True

        assert 'url = "http://127.0.0.1:8765/mcp"' in config.read_text(encoding="utf-8")

    def test_windows_prepared_file_ignores_synthetic_posix_mode(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.yaml"

        with patch.object(runtime_activation.os, "name", "nt"):
            runtime_activation._validate_prepared_mode(
                path,
                0o666,
                requested_mode=0o600,
                preserve_exact_mode=False,
            )

    def test_posix_prepared_file_still_rejects_broader_mode(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.yaml"

        with (
            patch.object(runtime_activation.os, "name", "posix"),
            pytest.raises(OSError, match="Prepared file mode exceeds"),
        ):
            runtime_activation._validate_prepared_mode(
                path,
                0o666,
                requested_mode=0o600,
                preserve_exact_mode=False,
            )

    @pytest.mark.parametrize(
        ("persisted", "profile"),
        [("claude_mcp", "claude-cli"), ("claude", "claude")],
    )
    def test_current_backend_preserves_claude_profile_identity(
        self, tmp_path: Path, persisted: str, profile: str
    ) -> None:
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text(
            f"orchestrator:\n  runtime_backend: {persisted}\n",
            encoding="utf-8",
        )

        with patch("pathlib.Path.home", return_value=tmp_path):
            assert setup_cmd._get_current_backend() == profile

    def test_setup_claude_selects_sdk_runtime(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text("{}", encoding="utf-8")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
        ):
            setup_cmd._setup_claude("/usr/local/bin/claude")

        config_dict = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert config_dict["orchestrator"]["runtime_backend"] == "claude"
        assert config_dict["orchestrator"]["cli_path"] == "/usr/local/bin/claude"
        assert config_dict["llm"]["backend"] == "claude"

    def test_forced_mcp2_sdk_mix_fails_before_setup_detection(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        original = "orchestrator:\n  runtime_backend: codex\n"
        config_path.write_text(original, encoding="utf-8")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "ouroboros.cli.commands.setup.has_unsupported_claude_sdk_mcp_mix",
                return_value=True,
            ),
            patch(
                "ouroboros.cli.commands.setup._detect_runtimes",
                side_effect=AssertionError("must fail before runtime detection"),
            ),
        ):
            result = CliRunner().invoke(setup_cmd.app, ["--runtime", "codex"])

        assert result.exit_code == 1
        for profile in (
            "ouroboros-ai[mcp]",
            "ouroboros-ai[claude]",
            "[claude-sdk]",
            "[claude-cli]",
        ):
            assert profile in result.output
        assert config_path.read_text(encoding="utf-8") == original

    def test_setup_claude_cli_selects_dependency_free_worker(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text("{}", encoding="utf-8")

        with patch("ouroboros.config.models.get_config_dir", return_value=config_dir):
            setup_cmd._setup_claude_cli("/usr/local/bin/claude")

        config_dict = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert config_dict["orchestrator"]["runtime_backend"] == "claude_mcp"
        assert config_dict["orchestrator"]["cli_path"] == "/usr/local/bin/claude"

    def test_setup_claude_sdk_fails_closed_before_config_write(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        original = "orchestrator:\n  runtime_backend: codex\n"
        config_path.write_text(original, encoding="utf-8")

        with (
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.commands.claude_setup.has_unsupported_claude_sdk_mcp_mix",
                return_value=True,
            ),
            pytest.raises(typer.Exit) as exc_info,
        ):
            setup_cmd._setup_claude_sdk("/usr/local/bin/claude")

        assert exc_info.value.exit_code == 1
        assert config_path.read_text(encoding="utf-8") == original

    def test_setup_claude_sdk_reports_requested_alias_but_persists_sdk_backend(
        self, tmp_path: Path
    ) -> None:
        """The explicit alias remains visible without creating a new backend identity."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text("{}", encoding="utf-8")

        with (
            patch("ouroboros.config.models.get_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.claude_setup.print_info") as print_info,
        ):
            setup_cmd._setup_claude_sdk("/usr/local/bin/claude")

        messages = [str(call.args[0]).replace("\\", "") for call in print_info.call_args_list]
        assert any("ouroboros-ai[claude-sdk]" in message for message in messages)
        assert all("ouroboros-ai[claude] (SDK" not in message for message in messages)
        config_dict = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert config_dict["orchestrator"]["runtime_backend"] == "claude"
        assert config_dict["llm"]["backend"] == "claude"

    @pytest.mark.parametrize(
        "setup_name",
        ["_setup_claude", "_setup_claude_cli", "_setup_claude_sdk"],
    )
    def test_setup_claude_profile_activation_failure_prints_no_success(
        self, setup_name: str
    ) -> None:
        with (
            patch(
                "ouroboros.cli.commands.claude_setup._activate_claude_runtime_config",
                return_value=None,
            ),
            patch("ouroboros.cli.commands.claude_setup.print_success") as mock_success,
        ):
            setup_profile = getattr(setup_cmd, setup_name)
            assert setup_profile("/usr/local/bin/claude") is False

        mock_success.assert_not_called()

    def test_legacy_claude_mcp_registration_shim_is_fail_closed(self, tmp_path: Path) -> None:
        """Older plugin callers cannot reactivate the incompatible MCP path."""
        with patch("pathlib.Path.home", return_value=tmp_path):
            setup_cmd._ensure_claude_mcp_entry()

        assert not (tmp_path / ".claude" / "mcp.json").exists()

    @staticmethod
    def _write_credentials(config_dir: Path, *, mode: int = 0o600) -> Path:
        credentials_path = config_dir / "credentials.yaml"
        credentials_path.write_text(
            yaml.safe_dump(
                get_default_credentials().model_dump(mode="json"),
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        credentials_path.chmod(mode)
        return credentials_path

    @pytest.mark.parametrize(
        ("setup_name", "expected_backend"),
        [("_setup_claude", "claude"), ("_setup_claude_cli", "claude_mcp")],
    )
    def test_setup_claude_profile_preserves_operator_keys_modes_and_never_reads_mcp(
        self,
        tmp_path: Path,
        setup_name: str,
        expected_backend: str,
    ) -> None:
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text(
            yaml.safe_dump(
                {
                    "operator": {"keep": True},
                    "orchestrator": {"runtime_backend": "codex"},
                    "llm": {"backend": "codex"},
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        config_path.chmod(0o640)
        credentials_path = self._write_credentials(config_dir, mode=0o640)
        mcp_path = tmp_path / ".claude" / "mcp.json"
        mcp_path.parent.mkdir()
        mcp_bytes = b'{"operator": "untouched"}'
        mcp_path.write_bytes(mcp_bytes)
        original_read_text = Path.read_text

        def _read_text(path: Path, *args: object, **kwargs: object) -> str:
            if path == mcp_path:
                raise AssertionError("Claude MCP configuration must not be read")
            return original_read_text(path, *args, **kwargs)

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("pathlib.Path.read_text", autospec=True, side_effect=_read_text),
        ):
            setup_profile = getattr(setup_cmd, setup_name)
            assert setup_profile("/usr/local/bin/claude") is True

        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert config["operator"] == {"keep": True}
        assert config["orchestrator"]["runtime_backend"] == expected_backend
        assert config["orchestrator"]["cli_path"] == "/usr/local/bin/claude"
        assert config["llm"]["backend"] == "claude"
        assert stat.S_IMODE(config_path.stat().st_mode) == 0o640
        assert stat.S_IMODE(credentials_path.stat().st_mode) == 0o640
        assert mcp_path.read_bytes() == mcp_bytes

    @pytest.mark.parametrize(
        "setup_name",
        ["_setup_claude", "_setup_claude_cli"],
    )
    @pytest.mark.parametrize("target_name", ["config.yaml", "credentials.yaml"])
    def test_setup_claude_rejects_duplicate_yaml_keys_before_rewrite(
        self, tmp_path: Path, target_name: str, setup_name: str
    ) -> None:
        """YAML's default last-key-wins behavior must not erase ambiguous input."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text(
            "orchestrator:\n  runtime_backend: codex\nllm:\n  backend: codex\n",
            encoding="utf-8",
        )
        credentials_path = self._write_credentials(config_dir)
        target = config_dir / target_name
        if target_name == "config.yaml":
            target.write_text(
                "orchestrator:\n  runtime_backend: codex\n"
                "orchestrator:\n  runtime_backend: claude\n",
                encoding="utf-8",
            )
        else:
            target.write_text("providers: {}\nproviders: {}\n", encoding="utf-8")
        before = target.read_bytes()
        other = credentials_path if target == config_path else config_path
        other_before = other.read_bytes()

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
        ):
            setup_profile = getattr(setup_cmd, setup_name)
            assert setup_profile("/usr/local/bin/claude") is False

        assert target.read_bytes() == before
        assert other.read_bytes() == other_before

    @pytest.mark.skipif(os.name == "nt", reason="POSIX umask semantics")
    @pytest.mark.parametrize(("mask", "config_mode"), [(0o027, 0o640), (0o077, 0o600)])
    def test_setup_claude_fresh_modes_respect_restrictive_umask(
        self, tmp_path: Path, mask: int, config_mode: int
    ) -> None:
        """Fresh config must not be chmod-broadened beyond the process umask."""
        script = """
import os
from ouroboros.cli.runtime_activation import activate_claude_runtime
os.umask(int(os.environ["TEST_UMASK"], 8))
raise SystemExit(0 if activate_claude_runtime("/usr/local/bin/claude") else 2)
"""
        env = os.environ.copy()
        env["HOME"] = str(tmp_path)
        env["TEST_UMASK"] = oct(mask)

        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[3],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert completed.returncode == 0, completed.stdout + completed.stderr
        config_dir = tmp_path / ".ouroboros"
        assert stat.S_IMODE((config_dir / "config.yaml").stat().st_mode) == config_mode
        assert stat.S_IMODE((config_dir / "credentials.yaml").stat().st_mode) == 0o600

    @pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
    def test_setup_claude_supports_linked_home_with_real_activation_directory(
        self, tmp_path: Path
    ) -> None:
        """A linked home parent resolves to one pinned physical generation."""
        real_home = tmp_path / "real-home"
        real_home.mkdir()
        linked_home = tmp_path / "linked-home"
        linked_home.symlink_to(real_home, target_is_directory=True)
        config_dir = linked_home / ".ouroboros"
        config_dir.mkdir()

        with patch("ouroboros.config.models.get_config_dir", return_value=config_dir):
            result = runtime_activation.activate_claude_runtime("/usr/local/bin/claude")

        assert result == config_dir / "config.yaml"

        physical_config_dir = real_home / ".ouroboros"
        assert physical_config_dir.is_dir()
        assert not physical_config_dir.is_symlink()
        config = yaml.safe_load((physical_config_dir / "config.yaml").read_text(encoding="utf-8"))
        assert config["orchestrator"]["cli_path"] == "/usr/local/bin/claude"
        assert (physical_config_dir / "credentials.yaml").is_file()

    @pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
    def test_setup_claude_rejects_linked_home_retarget_before_parent_pin(
        self, tmp_path: Path
    ) -> None:
        """A home link retarget during selection cannot redirect activation."""
        real_home = tmp_path / "real-home"
        real_home.mkdir()
        real_config_dir = real_home / ".ouroboros"
        real_config_dir.mkdir()
        victim_home = tmp_path / "victim-home"
        victim_home.mkdir()
        victim_config_dir = victim_home / ".ouroboros"
        victim_config_dir.mkdir()
        victim_marker = victim_config_dir / "operator.marker"
        victim_marker.write_bytes(b"foreign")
        linked_home = tmp_path / "linked-home"
        linked_home.symlink_to(real_home, target_is_directory=True)
        config_dir = linked_home / ".ouroboros"
        injected = False

        def _retarget_parent(phase: str, path: Path) -> None:
            nonlocal injected
            if phase != "parent-resolved" or path != linked_home or injected:
                return
            injected = True
            linked_home.unlink()
            linked_home.symlink_to(victim_home, target_is_directory=True)

        with (
            patch("ouroboros.config.models.get_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.runtime_activation._directory_authority_checkpoint",
                side_effect=_retarget_parent,
            ),
        ):
            assert setup_cmd._setup_claude("/usr/local/bin/claude") is False

        assert injected
        assert not (real_config_dir / "config.yaml").exists()
        assert not (victim_config_dir / "config.yaml").exists()
        assert victim_marker.read_bytes() == b"foreign"

    @pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
    def test_setup_claude_linked_home_retarget_after_publish_rolls_back_selected_parent(
        self, tmp_path: Path
    ) -> None:
        """A late home retarget rolls back only within the pinned physical parent."""
        real_home = tmp_path / "real-home"
        real_home.mkdir()
        real_config_dir = real_home / ".ouroboros"
        real_config_dir.mkdir()
        real_config = real_config_dir / "config.yaml"
        original = b"orchestrator:\n  runtime_backend: codex\nllm:\n  backend: codex\n"
        real_config.write_bytes(original)
        real_credentials = self._write_credentials(real_config_dir)
        credentials_before = real_credentials.read_bytes()
        victim_home = tmp_path / "victim-home"
        victim_home.mkdir()
        victim_config_dir = victim_home / ".ouroboros"
        victim_config_dir.mkdir()
        victim_marker = victim_config_dir / "operator.marker"
        victim_marker.write_bytes(b"foreign")
        linked_home = tmp_path / "linked-home"
        linked_home.symlink_to(real_home, target_is_directory=True)
        requested_config_dir = linked_home / ".ouroboros"
        injected = False

        def _retarget_after_publish(name: str) -> None:
            nonlocal injected
            if name != "config" or injected:
                return
            injected = True
            linked_home.unlink()
            linked_home.symlink_to(victim_home, target_is_directory=True)

        with (
            patch(
                "ouroboros.config.models.get_config_dir",
                return_value=requested_config_dir,
            ),
            patch(
                "ouroboros.cli.runtime_activation._publication_checkpoint",
                side_effect=_retarget_after_publish,
            ),
        ):
            assert setup_cmd._setup_claude("/usr/local/bin/claude") is False

        assert injected
        assert real_config.read_bytes() == original
        assert real_credentials.read_bytes() == credentials_before
        assert not (victim_config_dir / "config.yaml").exists()
        assert victim_marker.read_bytes() == b"foreign"

    def test_windows_junction_parent_contract_uses_resolved_physical_authority(
        self, tmp_path: Path
    ) -> None:
        """The Windows branch leases physical paths behind a junction-like parent."""
        real_home = tmp_path / "real-home"
        real_home.mkdir()
        physical_config_dir = real_home / ".ouroboros"
        physical_config_dir.mkdir()
        linked_home = tmp_path / "linked-home"
        linked_home.symlink_to(real_home, target_is_directory=True)
        requested_config_dir = linked_home / ".ouroboros"
        locked: list[Path] = []
        leased: list[Path] = []

        @contextmanager
        def _record_lock(path: Path, **_kwargs: object):
            locked.append(path)
            yield

        @contextmanager
        def _record_lease(path: Path):
            leased.append(path)
            yield

        with (
            patch("ouroboros.cli.runtime_activation.os.name", "nt"),
            patch(
                "ouroboros.cli.runtime_activation.file_lock",
                side_effect=_record_lock,
            ),
            patch(
                "ouroboros.cli.runtime_activation._windows_directory_lease",
                side_effect=_record_lease,
            ),
        ):
            with runtime_activation._activation_directory_authority(
                requested_config_dir
            ) as selected:
                assert selected == physical_config_dir.resolve()
                runtime_activation._require_active_directory_binding()

        assert locked == [real_home.resolve() / runtime_activation._ACTIVATION_LOCK_NAME]
        assert leased == [physical_config_dir.resolve()]

    def test_windows_junction_parent_contract_rejects_visible_retarget(
        self, tmp_path: Path
    ) -> None:
        """A junction-like binding change fails without selecting the new target."""
        real_home = tmp_path / "real-home"
        real_home.mkdir()
        physical_config_dir = real_home / ".ouroboros"
        physical_config_dir.mkdir()
        victim_home = tmp_path / "victim-home"
        victim_home.mkdir()
        victim_config_dir = victim_home / ".ouroboros"
        victim_config_dir.mkdir()
        victim_marker = victim_config_dir / "operator.marker"
        victim_marker.write_bytes(b"foreign")
        linked_home = tmp_path / "linked-home"
        linked_home.symlink_to(real_home, target_is_directory=True)
        requested_config_dir = linked_home / ".ouroboros"

        @contextmanager
        def _no_op_authority(*_args: object, **_kwargs: object):
            yield

        with (
            patch("ouroboros.cli.runtime_activation.os.name", "nt"),
            patch(
                "ouroboros.cli.runtime_activation.file_lock",
                side_effect=_no_op_authority,
            ),
            patch(
                "ouroboros.cli.runtime_activation._windows_directory_lease",
                side_effect=_no_op_authority,
            ),
            pytest.raises(runtime_activation._ConcurrentActivationError),
        ):
            with runtime_activation._activation_directory_authority(requested_config_dir):
                linked_home.unlink()
                linked_home.symlink_to(victim_home, target_is_directory=True)

        assert not (physical_config_dir / "config.yaml").exists()
        assert not (victim_config_dir / "config.yaml").exists()
        assert victim_marker.read_bytes() == b"foreign"

    def test_windows_junction_parent_contract_still_rejects_reparse_leaf(
        self, tmp_path: Path
    ) -> None:
        """Resolving the home parent never permits a linked `.ouroboros` leaf."""
        real_home = tmp_path / "real-home"
        real_home.mkdir()
        victim_config_dir = tmp_path / "victim-config"
        victim_config_dir.mkdir()
        victim_marker = victim_config_dir / "operator.marker"
        victim_marker.write_bytes(b"foreign")
        (real_home / ".ouroboros").symlink_to(
            victim_config_dir,
            target_is_directory=True,
        )
        linked_home = tmp_path / "linked-home"
        linked_home.symlink_to(real_home, target_is_directory=True)
        requested_config_dir = linked_home / ".ouroboros"

        with (
            patch("ouroboros.cli.runtime_activation.os.name", "nt"),
            pytest.raises(ValueError, match="Activation directory must be real"),
        ):
            with runtime_activation._activation_directory_authority(requested_config_dir):
                pytest.fail("a reparse activation-directory leaf must never be selected")

        assert victim_marker.read_bytes() == b"foreign"

    @pytest.mark.skipif(os.name == "nt", reason="POSIX directory-fd authority")
    def test_setup_claude_precreation_symlink_never_cleans_foreign_topology(
        self, tmp_path: Path
    ) -> None:
        """A missing config directory cannot be swapped into a foreign cleanup root."""
        home = tmp_path / "home"
        home.mkdir()
        config_dir = home / ".ouroboros"
        victim = tmp_path / "victim"
        victim.mkdir()
        victim_data = victim / "data"
        victim_logs = victim / "logs"
        victim_data.mkdir()
        victim_logs.mkdir()
        victim_identities = {
            path: (path.stat().st_dev, path.stat().st_ino)
            for path in (victim, victim_data, victim_logs)
        }
        injected = False

        def _swap_before_create(phase: str, path: Path) -> None:
            nonlocal injected
            if phase != "before-create" or path != config_dir or injected:
                return
            injected = True
            config_dir.symlink_to(victim, target_is_directory=True)

        with (
            patch("pathlib.Path.home", return_value=home),
            patch(
                "ouroboros.cli.runtime_activation._directory_authority_checkpoint",
                side_effect=_swap_before_create,
            ),
            patch("ouroboros.cli.commands.claude_setup.print_success") as mock_success,
        ):
            assert setup_cmd._setup_claude("/usr/local/bin/claude") is False

        assert injected
        assert config_dir.is_symlink()
        assert os.readlink(config_dir) == str(victim)
        for path, identity in victim_identities.items():
            assert path.is_dir()
            assert (path.stat().st_dev, path.stat().st_ino) == identity
        mock_success.assert_not_called()

    @pytest.mark.skipif(os.name == "nt", reason="POSIX directory-fd authority")
    def test_setup_claude_opened_directory_swap_never_selects_foreign_generation(
        self, tmp_path: Path
    ) -> None:
        """The pinned descriptor must still match the generation selected by name."""
        home = tmp_path / "home"
        home.mkdir()
        config_dir = home / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        original = b"orchestrator:\n  runtime_backend: codex\nllm:\n  backend: codex\n"
        config_path.write_bytes(original)
        self._write_credentials(config_dir)
        displaced = home / ".ouroboros.displaced"
        victim = tmp_path / "victim"
        victim.mkdir()
        marker = victim / "operator.marker"
        marker.write_bytes(b"foreign")
        victim_identity = (victim.stat().st_dev, victim.stat().st_ino)
        injected = False

        def _swap_after_open(phase: str, path: Path) -> None:
            nonlocal injected
            if phase != "opened" or path != config_dir or injected:
                return
            injected = True
            config_dir.rename(displaced)
            victim.rename(config_dir)

        with (
            patch("ouroboros.config.models.get_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.runtime_activation._directory_authority_checkpoint",
                side_effect=_swap_after_open,
            ),
            patch("ouroboros.cli.commands.claude_setup.print_success") as mock_success,
        ):
            assert setup_cmd._setup_claude("/usr/local/bin/claude") is False

        assert injected
        assert (config_dir.stat().st_dev, config_dir.stat().st_ino) == victim_identity
        assert (config_dir / marker.name).read_bytes() == b"foreign"
        assert not (config_dir / "config.yaml").exists()
        assert (displaced / "config.yaml").read_bytes() == original
        mock_success.assert_not_called()

    @pytest.mark.skipif(os.name == "nt", reason="POSIX directory-fd authority")
    def test_setup_claude_created_directory_swap_never_cleans_foreign_topology(
        self, tmp_path: Path
    ) -> None:
        """A just-created pathname replacement cannot become cleanup authority."""
        home = tmp_path / "home"
        home.mkdir()
        config_dir = home / ".ouroboros"
        displaced = home / ".ouroboros.displaced"
        victim = tmp_path / "victim"
        victim.mkdir()
        victim_data = victim / "data"
        victim_logs = victim / "logs"
        victim_data.mkdir()
        victim_logs.mkdir()
        identities = {
            path: (path.stat().st_dev, path.stat().st_ino)
            for path in (victim, victim_data, victim_logs)
        }
        injected = False

        def _swap_created_name(phase: str, path: Path) -> None:
            nonlocal injected
            if phase != "created-snapshotted" or path != config_dir or injected:
                return
            injected = True
            config_dir.rename(displaced)
            config_dir.symlink_to(victim, target_is_directory=True)

        with (
            patch("ouroboros.config.models.get_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.runtime_activation._directory_authority_checkpoint",
                side_effect=_swap_created_name,
            ),
            patch("ouroboros.cli.commands.claude_setup.print_success") as mock_success,
        ):
            assert setup_cmd._setup_claude("/usr/local/bin/claude") is False

        assert injected
        assert config_dir.is_symlink()
        assert displaced.is_dir()
        for path, identity in identities.items():
            assert path.is_dir()
            assert (path.stat().st_dev, path.stat().st_ino) == identity
        mock_success.assert_not_called()

    @pytest.mark.skipif(os.name == "nt", reason="POSIX directory durability")
    def test_setup_claude_fails_before_files_when_directory_sync_fails(
        self, tmp_path: Path
    ) -> None:
        """A fresh directory is not treated as durable when its parent sync fails."""
        home = tmp_path / "home"
        home.mkdir()
        config_dir = home / ".ouroboros"

        with (
            patch("ouroboros.config.models.get_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.runtime_activation._fsync_directory_descriptor",
                side_effect=OSError("directory sync failed"),
            ),
            patch("ouroboros.cli.commands.claude_setup.print_success") as mock_success,
        ):
            assert setup_cmd._setup_claude("/usr/local/bin/claude") is False

        assert config_dir.is_dir()
        assert not (config_dir / "config.yaml").exists()
        assert not (config_dir / "credentials.yaml").exists()
        mock_success.assert_not_called()

    @pytest.mark.skipif(os.name == "nt", reason="POSIX directory-fd authority")
    def test_setup_claude_directory_swap_after_publish_rolls_back_pinned_generation(
        self, tmp_path: Path
    ) -> None:
        """A late pathname swap cannot commit config into a displaced authority."""
        home = tmp_path / "home"
        home.mkdir()
        config_dir = home / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        original = b"orchestrator:\n  runtime_backend: codex\nllm:\n  backend: codex\n"
        config_path.write_bytes(original)
        self._write_credentials(config_dir)
        displaced = home / ".ouroboros.displaced"
        victim = tmp_path / "victim"
        victim.mkdir()
        marker = victim / "operator.marker"
        marker.write_bytes(b"foreign")
        victim_identity = (victim.stat().st_dev, victim.stat().st_ino)
        injected = False

        def _swap_after_publish(name: str) -> None:
            nonlocal injected
            if name != "config" or injected:
                return
            injected = True
            config_dir.rename(displaced)
            victim.rename(config_dir)

        with (
            patch("ouroboros.config.models.get_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.runtime_activation._publication_checkpoint",
                side_effect=_swap_after_publish,
            ),
            patch("ouroboros.cli.commands.claude_setup.print_success") as mock_success,
        ):
            assert setup_cmd._setup_claude("/usr/local/bin/claude") is False

        assert injected
        assert (config_dir.stat().st_dev, config_dir.stat().st_ino) == victim_identity
        assert (config_dir / marker.name).read_bytes() == b"foreign"
        assert not (config_dir / "config.yaml").exists()
        assert (displaced / "config.yaml").read_bytes() == original
        mock_success.assert_not_called()

    @pytest.mark.skipif(not hasattr(os, "fchown"), reason="POSIX ownership semantics")
    def test_setup_claude_preserves_existing_owner_and_group(self, tmp_path: Path) -> None:
        """Replacement creation explicitly preserves both ownership fields."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text(
            "orchestrator:\n  runtime_backend: codex\nllm:\n  backend: codex\n",
            encoding="utf-8",
        )
        credentials_path = self._write_credentials(config_dir)
        expected_owner = (config_path.stat().st_uid, config_path.stat().st_gid)
        credentials_owner = (
            credentials_path.stat().st_uid,
            credentials_path.stat().st_gid,
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.cli.runtime_activation.os.fchown", wraps=os.fchown) as fchown,
        ):
            assert setup_cmd._setup_claude("/usr/local/bin/claude") is True

        assert (config_path.stat().st_uid, config_path.stat().st_gid) == expected_owner
        assert (
            credentials_path.stat().st_uid,
            credentials_path.stat().st_gid,
        ) == credentials_owner
        preserved = {(call.args[1], call.args[2]) for call in fchown.call_args_list}
        assert expected_owner in preserved
        assert credentials_owner in preserved

    @pytest.mark.skipif(not hasattr(os, "fchown"), reason="POSIX ownership semantics")
    def test_setup_claude_preserves_nondefault_existing_group(self, tmp_path: Path) -> None:
        """A replacement retains an operator-selected supplementary group."""
        alternative_groups = [group for group in os.getgroups() if group != os.getgid()]
        if not alternative_groups:
            pytest.skip("no supplementary group is available")
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text(
            "orchestrator:\n  runtime_backend: codex\nllm:\n  backend: codex\n",
            encoding="utf-8",
        )
        credentials_path = self._write_credentials(config_dir)
        selected_group = alternative_groups[0]
        os.chown(config_path, -1, selected_group)
        os.chown(credentials_path, -1, selected_group)
        expected_owner = (os.geteuid(), selected_group)

        with patch("ouroboros.config.models.get_config_dir", return_value=config_dir):
            assert setup_cmd._setup_claude("/usr/local/bin/claude") is True

        assert (config_path.stat().st_uid, config_path.stat().st_gid) == expected_owner
        assert (
            credentials_path.stat().st_uid,
            credentials_path.stat().st_gid,
        ) == expected_owner

    @pytest.mark.skipif(
        not hasattr(os, "geteuid") or os.geteuid() != 0,
        reason="changing a file to a foreign uid requires root",
    )
    def test_setup_claude_preserves_direct_foreign_owner_and_group(self, tmp_path: Path) -> None:
        """A supported foreign owner is preserved rather than silently normalized."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text(
            "orchestrator:\n  runtime_backend: codex\nllm:\n  backend: codex\n",
            encoding="utf-8",
        )
        credentials_path = self._write_credentials(config_dir)
        foreign_owner = (65534, 65534)
        os.chown(config_path, *foreign_owner)
        os.chown(credentials_path, *foreign_owner)

        with patch("ouroboros.config.models.get_config_dir", return_value=config_dir):
            assert setup_cmd._setup_claude("/usr/local/bin/claude") is True

        assert (config_path.stat().st_uid, config_path.stat().st_gid) == foreign_owner
        assert (
            credentials_path.stat().st_uid,
            credentials_path.stat().st_gid,
        ) == foreign_owner

    @pytest.mark.skipif(not hasattr(os, "fchown"), reason="POSIX ownership semantics")
    def test_setup_claude_fails_closed_when_ownership_cannot_be_preserved(
        self, tmp_path: Path
    ) -> None:
        """An unsupported owner/group replacement cannot publish new config."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text(
            "orchestrator:\n  runtime_backend: codex\nllm:\n  backend: codex\n",
            encoding="utf-8",
        )
        credentials_path = self._write_credentials(config_dir)
        before = {
            path: (
                path.read_bytes(),
                stat.S_IMODE(path.stat().st_mode),
                path.stat().st_uid,
                path.stat().st_gid,
                path.stat().st_ino,
            )
            for path in (config_path, credentials_path)
        }

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "ouroboros.cli.runtime_activation.os.fchown",
                side_effect=PermissionError("ownership preservation denied"),
            ),
            patch("ouroboros.cli.commands.claude_setup.print_success") as mock_success,
        ):
            assert setup_cmd._setup_claude("/usr/local/bin/claude") is False

        for path, expected in before.items():
            assert (
                path.read_bytes(),
                stat.S_IMODE(path.stat().st_mode),
                path.stat().st_uid,
                path.stat().st_gid,
                path.stat().st_ino,
            ) == expected
        mock_success.assert_not_called()

    def test_setup_claude_recovers_subprocess_crash_after_credentials_publish(
        self, tmp_path: Path
    ) -> None:
        """The next invocation rolls back the journal-owned partial generation."""
        crashing_script = """
import os
import ouroboros.cli.runtime_activation as activation
def checkpoint(name):
    if name == "credentials":
        os._exit(91)
activation._publication_checkpoint = checkpoint
activation.activate_claude_runtime("/first/claude")
"""
        env = os.environ.copy()
        env["HOME"] = str(tmp_path)
        root = Path(__file__).resolve().parents[3]
        crashed = subprocess.run(
            [sys.executable, "-c", crashing_script],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert crashed.returncode == 91
        config_dir = tmp_path / ".ouroboros"
        assert (config_dir / "credentials.yaml").is_file()
        assert not (config_dir / "config.yaml").exists()
        assert (config_dir / runtime_activation._JOURNAL_NAME).is_file()

        recovery_script = """
from ouroboros.cli.runtime_activation import activate_claude_runtime
raise SystemExit(0 if activate_claude_runtime("/second/claude") else 2)
"""
        recovered = subprocess.run(
            [sys.executable, "-c", recovery_script],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert recovered.returncode == 0, recovered.stdout + recovered.stderr
        config = yaml.safe_load((config_dir / "config.yaml").read_text(encoding="utf-8"))
        assert config["orchestrator"]["cli_path"] == "/second/claude"
        assert not (config_dir / runtime_activation._JOURNAL_NAME).exists()
        assert not tuple(config_dir.glob(".claude-runtime-activation.*.stage"))

    @pytest.mark.parametrize("restoration_phase", ["linked", "durable"])
    def test_setup_claude_recovery_is_idempotent_across_claim_restoration_crash(
        self, tmp_path: Path, restoration_phase: str
    ) -> None:
        """A crash around atomic claim restoration remains recoverable on the third run."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        original = b"orchestrator:\n  runtime_backend: codex\nllm:\n  backend: codex\n"
        config_path.write_bytes(original)
        self._write_credentials(config_dir)
        env = os.environ.copy()
        env["HOME"] = str(tmp_path)
        env["RESTORATION_PHASE"] = restoration_phase
        root = Path(__file__).resolve().parents[3]
        claim_crash_script = """
import os
import ouroboros.cli.runtime_activation as activation
def checkpoint(target):
    if target.name == "config.yaml":
        os._exit(91)
activation._claim_publication_checkpoint = checkpoint
activation.activate_claude_runtime("/first/claude")
"""
        first = subprocess.run(
            [sys.executable, "-c", claim_crash_script],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert first.returncode == 91
        assert not config_path.exists()
        assert tuple(config_dir.glob("*.config-original.claim"))

        restoration_crash_script = """
import os
import ouroboros.cli.runtime_activation as activation
def checkpoint(phase, target):
    if target.name == "config.yaml" and phase == os.environ["RESTORATION_PHASE"]:
        os._exit(92)
activation._claim_restoration_checkpoint = checkpoint
activation.activate_claude_runtime("/second/claude")
"""
        second = subprocess.run(
            [sys.executable, "-c", restoration_crash_script],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert second.returncode == 92
        assert config_path.read_bytes() == original
        assert not tuple(config_dir.glob("*.config-original.claim"))

        final_script = """
from ouroboros.cli.runtime_activation import activate_claude_runtime
raise SystemExit(0 if activate_claude_runtime("/third/claude") else 2)
"""
        third = subprocess.run(
            [sys.executable, "-c", final_script],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert third.returncode == 0, third.stdout + third.stderr
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert config["orchestrator"]["cli_path"] == "/third/claude"
        assert not (config_dir / runtime_activation._JOURNAL_NAME).exists()

    @pytest.mark.parametrize("race_operation", ["claim", "publish"])
    def test_setup_claude_preserves_noncooperative_replace_at_publish_boundary(
        self, tmp_path: Path, race_operation: str
    ) -> None:
        """The live generation is claimed before inspect, so the race cannot overwrite it."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text(
            "orchestrator:\n  runtime_backend: codex\nllm:\n  backend: codex\n",
            encoding="utf-8",
        )
        self._write_credentials(config_dir)
        operator = (
            f"orchestrator:\n  runtime_backend: codex\noperator: {race_operation}-race\n"
        ).encode()
        injected = False

        def _replace_at_last_boundary(operation: str, target: Path) -> None:
            nonlocal injected
            if operation != race_operation or target != config_path or injected:
                return
            injected = True
            replacement = config_dir / ".operator-config"
            replacement.write_bytes(operator)
            replacement.chmod(0o600)
            os.replace(replacement, config_path)

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.runtime_activation._promotion_checkpoint",
                side_effect=_replace_at_last_boundary,
            ),
            patch("ouroboros.cli.commands.claude_setup.print_success") as mock_success,
        ):
            assert setup_cmd._setup_claude("/usr/local/bin/claude") is False

        assert injected
        assert config_path.read_bytes() == operator
        assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
        mock_success.assert_not_called()

    def test_setup_claude_preserves_noncooperative_replace_at_rollback_boundary(
        self, tmp_path: Path
    ) -> None:
        """Rollback claims then inspects, so it never unlinks a last-moment edit."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text(
            "orchestrator:\n  runtime_backend: codex\nllm:\n  backend: codex\n",
            encoding="utf-8",
        )
        self._write_credentials(config_dir)
        operator = b"orchestrator:\n  runtime_backend: codex\noperator: rollback-race\n"
        injected = False

        def _fail_after_config(name: str) -> None:
            if name == "config":
                raise OSError("force rollback")

        def _replace_before_rollback_claim(operation: str, target: Path) -> None:
            nonlocal injected
            if operation != "rollback-claim" or target != config_path or injected:
                return
            injected = True
            replacement = config_dir / ".operator-config"
            replacement.write_bytes(operator)
            replacement.chmod(0o640)
            os.replace(replacement, config_path)

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.runtime_activation._publication_checkpoint",
                side_effect=_fail_after_config,
            ),
            patch(
                "ouroboros.cli.runtime_activation._promotion_checkpoint",
                side_effect=_replace_before_rollback_claim,
            ),
            patch("ouroboros.cli.commands.setup.print_success") as mock_success,
        ):
            assert setup_cmd._setup_claude("/usr/local/bin/claude") is False

        assert injected
        assert config_path.read_bytes() == operator
        assert stat.S_IMODE(config_path.stat().st_mode) == 0o640
        mock_success.assert_not_called()

    @pytest.mark.parametrize(
        "claim_label",
        [
            "config-original",
            "credentials-original",
            "config-rollback",
            "credentials-rollback",
        ],
    )
    def test_setup_claude_rejects_preexisting_generation_claims_without_loss(
        self, tmp_path: Path, claim_label: str
    ) -> None:
        """Disclosed claim names are reservations, never overwrite destinations."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text(
            "orchestrator:\n  runtime_backend: codex\nllm:\n  backend: codex\n",
            encoding="utf-8",
        )
        credentials_path = self._write_credentials(config_dir)
        config_before = config_path.read_bytes()
        credentials_before = credentials_path.read_bytes()
        generation = "1" * 32
        claim_path = config_dir / (f".claude-runtime-activation.{generation}.{claim_label}.claim")
        malicious = f"operator-owned-{claim_label}\n".encode()
        claim_path.write_bytes(malicious)
        claim_path.chmod(0o640)
        claim_identity = (claim_path.stat().st_dev, claim_path.stat().st_ino)
        config_identity = (config_path.stat().st_dev, config_path.stat().st_ino)
        credentials_identity = (
            credentials_path.stat().st_dev,
            credentials_path.stat().st_ino,
        )

        class _FixedUuid:
            hex = generation

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.runtime_activation.uuid4", return_value=_FixedUuid()),
            patch("ouroboros.cli.commands.setup.print_success") as mock_success,
        ):
            assert setup_cmd._setup_claude("/usr/local/bin/claude") is False

        assert claim_path.read_bytes() == malicious
        assert stat.S_IMODE(claim_path.stat().st_mode) == 0o640
        assert (claim_path.stat().st_dev, claim_path.stat().st_ino) == claim_identity
        assert (config_path.stat().st_dev, config_path.stat().st_ino) == config_identity
        assert (
            credentials_path.stat().st_dev,
            credentials_path.stat().st_ino,
        ) == credentials_identity
        assert config_path.read_bytes() == config_before
        assert credentials_path.read_bytes() == credentials_before
        assert not (config_dir / runtime_activation._JOURNAL_NAME).exists()
        mock_success.assert_not_called()

    def test_setup_claude_journal_publish_never_overwrites_last_moment_operator_file(
        self, tmp_path: Path
    ) -> None:
        """Journal publication is create-if-absent at the final filesystem boundary."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text(
            "orchestrator:\n  runtime_backend: codex\nllm:\n  backend: codex\n",
            encoding="utf-8",
        )
        credentials_path = self._write_credentials(config_dir)
        config_before = config_path.read_bytes()
        credentials_before = credentials_path.read_bytes()
        operator_journal = b'{"operator":"journal-race"}\n'

        def _inject_operator_journal(path: Path) -> None:
            path.write_bytes(operator_journal)
            path.chmod(0o640)
            operator_identity.append((path.stat().st_dev, path.stat().st_ino))

        operator_identity: list[tuple[int, int]] = []

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.runtime_activation._journal_publication_checkpoint",
                side_effect=_inject_operator_journal,
            ),
            patch("ouroboros.cli.commands.setup.print_success") as mock_success,
        ):
            assert setup_cmd._setup_claude("/usr/local/bin/claude") is False

        journal_path = config_dir / runtime_activation._JOURNAL_NAME
        assert journal_path.read_bytes() == operator_journal
        assert stat.S_IMODE(journal_path.stat().st_mode) == 0o640
        assert (journal_path.stat().st_dev, journal_path.stat().st_ino) == operator_identity[0]
        assert config_path.read_bytes() == config_before
        assert credentials_path.read_bytes() == credentials_before
        mock_success.assert_not_called()

    @pytest.mark.parametrize(
        ("operation", "claim_label"),
        [("claim", "config-original"), ("rollback-claim", "config-rollback")],
    )
    def test_setup_claude_claim_move_is_destination_absent_at_final_boundary(
        self, tmp_path: Path, operation: str, claim_label: str
    ) -> None:
        """A claim created after validation is preserved and blocks the no-replace move."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text(
            "orchestrator:\n  runtime_backend: codex\nllm:\n  backend: codex\n",
            encoding="utf-8",
        )
        self._write_credentials(config_dir)
        generation = "2" * 32
        claim_path = config_dir / (f".claude-runtime-activation.{generation}.{claim_label}.claim")
        malicious = f"last-moment-{claim_label}\n".encode()
        injected = False

        class _FixedUuid:
            hex = generation

        def _inject_claim(current_operation: str, target: Path) -> None:
            nonlocal injected
            if current_operation != operation or target != config_path or injected:
                return
            injected = True
            claim_path.write_bytes(malicious)
            claim_path.chmod(0o600)
            claim_identity.append((claim_path.stat().st_dev, claim_path.stat().st_ino))

        claim_identity: list[tuple[int, int]] = []

        def _fail_after_config(name: str) -> None:
            if operation == "rollback-claim" and name == "config":
                raise OSError("force rollback")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.runtime_activation.uuid4", return_value=_FixedUuid()),
            patch(
                "ouroboros.cli.runtime_activation._promotion_checkpoint",
                side_effect=_inject_claim,
            ),
            patch(
                "ouroboros.cli.runtime_activation._publication_checkpoint",
                side_effect=_fail_after_config,
            ),
            patch("ouroboros.cli.commands.setup.print_success") as mock_success,
        ):
            assert setup_cmd._setup_claude("/usr/local/bin/claude") is False

        assert injected
        assert claim_path.read_bytes() == malicious
        assert stat.S_IMODE(claim_path.stat().st_mode) == 0o600
        assert (claim_path.stat().st_dev, claim_path.stat().st_ino) == claim_identity[0]
        assert config_path.is_file()
        mock_success.assert_not_called()

    @pytest.mark.parametrize("artifact", ["journal", "original_claim", "rollback_claim"])
    def test_setup_claude_cleanup_never_unlinks_last_moment_operator_artifact(
        self, tmp_path: Path, artifact: str
    ) -> None:
        """Cleanup atomically quarantines the live path before judging ownership."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        original = b"orchestrator:\n  runtime_backend: codex\nllm:\n  backend: codex\n"
        config_path.write_bytes(original)
        self._write_credentials(config_dir)
        operator = f"operator-{artifact}\n".encode()
        injected_path: list[Path] = []
        injected_identity: list[tuple[int, int]] = []

        def _matches_target(path: Path) -> bool:
            if artifact == "journal":
                return path == config_dir / runtime_activation._JOURNAL_NAME
            if artifact == "original_claim":
                return path.name.endswith(".config-original.claim")
            return path.name.endswith(".config-rollback.claim")

        def _replace_before_quarantine(path: Path) -> None:
            if injected_path or not _matches_target(path):
                return
            replacement = config_dir / f".operator-{artifact}"
            replacement.write_bytes(operator)
            replacement.chmod(0o640)
            os.replace(replacement, path)
            injected_path.append(path)
            injected_identity.append((path.stat().st_dev, path.stat().st_ino))

        def _force_rollback(name: str) -> None:
            if artifact == "rollback_claim" and name == "config":
                raise OSError("force rollback cleanup")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.runtime_activation._artifact_cleanup_checkpoint",
                side_effect=_replace_before_quarantine,
            ),
            patch(
                "ouroboros.cli.runtime_activation._publication_checkpoint",
                side_effect=_force_rollback,
            ),
            patch("ouroboros.cli.commands.setup.print_success") as mock_success,
        ):
            result = setup_cmd._setup_claude("/usr/local/bin/claude")

        assert result is (artifact != "rollback_claim")
        assert injected_path
        preserved = injected_path[0]
        assert preserved.read_bytes() == operator
        assert stat.S_IMODE(preserved.stat().st_mode) == 0o640
        assert (preserved.stat().st_dev, preserved.stat().st_ino) == injected_identity[0]
        assert config_path.is_file()
        if artifact == "rollback_claim":
            assert config_path.read_bytes() == original
            mock_success.assert_not_called()

    def test_setup_claude_serializes_competing_operator_generations(self, tmp_path: Path) -> None:
        """A second cooperative process cannot pass the first process's CAS boundary."""
        marker = tmp_path / "first-published"
        release = tmp_path / "release-first"
        first_script = """
import os
import time
from pathlib import Path
import ouroboros.cli.runtime_activation as activation
marker = Path(os.environ["TEST_MARKER"])
release = Path(os.environ["TEST_RELEASE"])
def checkpoint(name):
    if name == "credentials":
        marker.write_text("ready")
        while not release.exists():
            time.sleep(0.01)
activation._publication_checkpoint = checkpoint
raise SystemExit(0 if activation.activate_claude_runtime("/first/claude") else 2)
"""
        second_script = """
from ouroboros.cli.runtime_activation import activate_claude_runtime
raise SystemExit(0 if activate_claude_runtime("/second/claude") else 2)
"""
        env = os.environ.copy()
        env.update(
            HOME=str(tmp_path),
            TEST_MARKER=str(marker),
            TEST_RELEASE=str(release),
        )
        root = Path(__file__).resolve().parents[3]
        first = subprocess.Popen(
            [sys.executable, "-c", first_script],
            cwd=root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        second: subprocess.Popen[str] | None = None
        try:
            import time

            startup_timeout = 30.0
            deadlock_timeout = 30.0
            startup_deadline = time.monotonic() + startup_timeout
            while time.monotonic() < startup_deadline:
                if marker.exists():
                    break
                if first.poll() is not None:
                    break
                time.sleep(0.01)
            if not marker.exists():
                if first.poll() is None:
                    first.kill()
                first_stdout, first_stderr = first.communicate()
                pytest.fail(
                    "first setup subprocess did not reach the credentials publication "
                    f"checkpoint within {startup_timeout:.0f} seconds "
                    f"(returncode={first.returncode})\n"
                    f"stdout:\n{first_stdout}\n"
                    f"stderr:\n{first_stderr}"
                )
            second = subprocess.Popen(
                [sys.executable, "-c", second_script],
                cwd=root,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            # The second process must still be waiting on the activation lock.
            time.sleep(0.1)
            assert second.poll() is None
            release.write_text("go", encoding="utf-8")
            first_stdout, first_stderr = first.communicate(timeout=deadlock_timeout)
            second_stdout, second_stderr = second.communicate(timeout=deadlock_timeout)
        finally:
            primary_error = sys.exception()
            cleanup_errors: list[tuple[str, Exception]] = []
            for name, process in (("first", first), ("second", second)):
                if process is None:
                    continue
                try:
                    if process.poll() is None:
                        process.kill()
                    process.communicate()
                except Exception as error:  # pragma: no cover - OS-level cleanup failure
                    cleanup_errors.append((name, error))
            if cleanup_errors:
                details = "; ".join(
                    f"{name} subprocess cleanup failed: {error!r}" for name, error in cleanup_errors
                )
                if primary_error is not None:
                    primary_error.add_note(details)
                else:
                    raise AssertionError(details) from cleanup_errors[0][1]

        assert first.poll() is not None
        assert first.returncode == 0, first_stdout + first_stderr
        assert second is not None
        assert second.poll() is not None
        assert second.returncode == 0, second_stdout + second_stderr
        config_dir = tmp_path / ".ouroboros"
        config = yaml.safe_load((config_dir / "config.yaml").read_text(encoding="utf-8"))
        assert config["orchestrator"]["cli_path"] == "/second/claude"
        assert not (config_dir / runtime_activation._JOURNAL_NAME).exists()
        with runtime_activation.file_lock(
            tmp_path / runtime_activation._ACTIVATION_LOCK_NAME,
            blocking=False,
            stable_parent_authority=True,
        ):
            pass

    def test_setup_process_cleanup_reaps_after_communicate_failure(self) -> None:
        """Cleanup escalates and reaps even when its first communicate call fails."""
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        real_communicate = process.communicate
        attempts = 0

        def fail_once_then_communicate(*, timeout: float) -> tuple[str, str]:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise subprocess.TimeoutExpired(process.args, timeout)
            return real_communicate(timeout=timeout)

        with patch.object(process, "communicate", side_effect=fail_once_then_communicate):
            _terminate_and_reap_test_process(process)

        assert attempts == 2
        assert process.poll() is not None

    def test_setup_process_cleanup_releases_lock_after_release_write_failure(
        self, tmp_path: Path
    ) -> None:
        """A failed parent release write cannot strand either lock participant."""
        lock_target = tmp_path / "activation-test"
        marker = tmp_path / "first-holds-lock"
        first_script = """
import os
import time
from pathlib import Path
from ouroboros.core.file_lock import file_lock
lock_target = Path(os.environ["TEST_LOCK_TARGET"])
marker = Path(os.environ["TEST_MARKER"])
with file_lock(lock_target, stable_parent_authority=True):
    marker.write_text("ready")
    time.sleep(60)
"""
        second_script = """
import os
from pathlib import Path
from ouroboros.core.file_lock import file_lock
with file_lock(Path(os.environ["TEST_LOCK_TARGET"]), stable_parent_authority=True):
    pass
"""
        env = os.environ.copy()
        env.update(TEST_LOCK_TARGET=str(lock_target), TEST_MARKER=str(marker))
        root = Path(__file__).resolve().parents[3]
        first = subprocess.Popen(
            [sys.executable, "-c", first_script],
            cwd=root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        second: subprocess.Popen[str] | None = None
        try:
            startup_deadline = time.monotonic() + 30
            while time.monotonic() < startup_deadline:
                if marker.exists() or first.poll() is not None:
                    break
                time.sleep(0.01)
            assert marker.exists(), "first process did not acquire the activation lock"
            second = subprocess.Popen(
                [sys.executable, "-c", second_script],
                cwd=root,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            time.sleep(0.1)
            assert second.poll() is None
            with (
                patch.object(Path, "write_text", side_effect=OSError("release write failed")),
                pytest.raises(OSError, match="release write failed"),
            ):
                (tmp_path / "release-first").write_text("go", encoding="utf-8")
        finally:
            _terminate_and_reap_test_process(second)
            _terminate_and_reap_test_process(first)

        assert first.poll() is not None
        assert second is not None
        assert second.poll() is not None
        with runtime_activation.file_lock(
            lock_target,
            blocking=False,
            stable_parent_authority=True,
        ):
            pass

    @pytest.mark.parametrize(
        ("target_name", "kind"),
        [
            ("config.yaml", "symlink"),
            ("config.yaml", "hardlink"),
            ("config.yaml", "fifo"),
            ("config.yaml", "directory"),
            ("credentials.yaml", "symlink"),
            ("credentials.yaml", "hardlink"),
            ("credentials.yaml", "fifo"),
            ("credentials.yaml", "directory"),
        ],
    )
    def test_setup_claude_rejects_links_and_special_files_without_mutation(
        self,
        tmp_path: Path,
        target_name: str,
        kind: str,
    ) -> None:
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text(
            "orchestrator:\n  runtime_backend: codex\nllm:\n  backend: codex\n",
            encoding="utf-8",
        )
        config_bytes = config_path.read_bytes()
        self._write_credentials(config_dir)
        target = config_dir / target_name
        target.unlink()
        backing = tmp_path / f"{target_name}.backing"
        backing.write_bytes(
            config_bytes
            if target_name == "config.yaml"
            else yaml.safe_dump(
                get_default_credentials().model_dump(mode="json"), sort_keys=False
            ).encode()
        )
        if kind == "symlink":
            target.symlink_to(backing)
        elif kind == "hardlink":
            os.link(backing, target)
        elif kind == "fifo":
            os.mkfifo(target)
        else:
            target.mkdir()

        before_backing = backing.read_bytes()
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup.print_success") as mock_success,
        ):
            assert setup_cmd._setup_claude("/usr/local/bin/claude") is False

        assert backing.read_bytes() == before_backing
        assert target.lstat()
        mock_success.assert_not_called()

    @pytest.mark.parametrize(
        ("target_name", "contents"),
        [
            ("config.yaml", b"orchestrator: [\n"),
            ("config.yaml", b"- not\n- a\n- mapping\n"),
            ("config.yaml", b"null\n"),
            ("config.yaml", b""),
            ("credentials.yaml", b"\xff\xfe"),
            ("credentials.yaml", b"providers: []\n"),
            ("credentials.yaml", b"null\n"),
            ("credentials.yaml", b""),
        ],
    )
    def test_setup_claude_rejects_malformed_documents_byte_for_byte(
        self, tmp_path: Path, target_name: str, contents: bytes
    ) -> None:
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text(
            "orchestrator:\n  runtime_backend: codex\nllm:\n  backend: codex\n",
            encoding="utf-8",
        )
        credentials_path = self._write_credentials(config_dir)
        target = config_dir / target_name
        target.write_bytes(contents)
        other = credentials_path if target == config_path else config_path
        other_before = other.read_bytes()

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup.print_success") as mock_success,
        ):
            assert setup_cmd._setup_claude("/usr/local/bin/claude") is False

        assert target.read_bytes() == contents
        assert other.read_bytes() == other_before
        mock_success.assert_not_called()

    def test_setup_claude_rejects_same_content_replacement_generation(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        original = b"orchestrator:\n  runtime_backend: codex\nllm:\n  backend: codex\n"
        config_path.write_bytes(original)
        self._write_credentials(config_dir)
        real_promote = runtime_activation._promote_prepared_under_lock
        replaced = False

        def _replace_then_promote(
            path: Path,
            prepared: runtime_activation._PreparedFile,
            expected: runtime_activation._FileSnapshot,
            claim: Path,
        ) -> runtime_activation._FileSnapshot:
            nonlocal replaced
            if path == config_path and not replaced:
                replaced = True
                replacement = config_path.with_suffix(".replacement")
                replacement.write_bytes(original)
                os.replace(replacement, config_path)
            return real_promote(path, prepared, expected, claim)

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.runtime_activation._promote_prepared_under_lock",
                side_effect=_replace_then_promote,
            ),
        ):
            assert setup_cmd._setup_claude("/usr/local/bin/claude") is False

        assert replaced
        assert config_path.read_bytes() == original

    def test_setup_claude_rolls_back_created_credentials_when_config_replace_fails(
        self, tmp_path: Path
    ) -> None:
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        original = b"orchestrator:\n  runtime_backend: codex\nllm:\n  backend: codex\n"
        config_path.write_bytes(original)
        credentials_path = config_dir / "credentials.yaml"

        def _fail_config_publish(operation: str, target: Path) -> None:
            if operation == "publish" and target == config_path:
                raise OSError("simulated config replacement failure")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.runtime_activation._promotion_checkpoint",
                side_effect=_fail_config_publish,
            ),
        ):
            assert setup_cmd._setup_claude("/usr/local/bin/claude") is False

        assert config_path.read_bytes() == original
        assert not credentials_path.exists()

    def test_setup_claude_rolls_back_published_config_on_directory_sync_failure(
        self, tmp_path: Path
    ) -> None:
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        original = b"orchestrator:\n  runtime_backend: codex\nllm:\n  backend: codex\n"
        config_path.write_bytes(original)
        self._write_credentials(config_dir)
        sync_calls = 0
        observed_published_config = False

        def _fail_live_config_sync(_path: Path) -> bool:
            nonlocal sync_calls, observed_published_config
            sync_calls += 1
            if sync_calls == 3:
                live = yaml.safe_load(config_path.read_text(encoding="utf-8"))
                observed_published_config = live["orchestrator"]["runtime_backend"] == "claude"
                return False
            return True

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.runtime_activation.fsync_parent_directory",
                side_effect=_fail_live_config_sync,
            ),
            patch("ouroboros.cli.commands.setup.print_success") as mock_success,
        ):
            assert setup_cmd._setup_claude("/usr/local/bin/claude") is False

        assert config_path.read_bytes() == original
        assert observed_published_config
        mock_success.assert_not_called()

    def test_setup_claude_cleanup_sync_failure_retains_rollback_evidence(
        self, tmp_path: Path
    ) -> None:
        """A failed first cleanup sync occurs while journal/original claim still exist."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        original = b"orchestrator:\n  runtime_backend: codex\nllm:\n  backend: codex\n"
        config_path.write_bytes(original)
        self._write_credentials(config_dir)
        sync_calls = 0
        saw_recovery_evidence = False

        def _fail_fourth_sync(_path: Path) -> bool:
            nonlocal sync_calls, saw_recovery_evidence
            sync_calls += 1
            if sync_calls == 4:
                saw_recovery_evidence = (
                    (config_dir / runtime_activation._JOURNAL_NAME).is_file()
                    and bool(tuple(config_dir.glob("*.config-original.claim")))
                    and config_path.is_file()
                )
                return False
            return True

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.runtime_activation.fsync_parent_directory",
                side_effect=_fail_fourth_sync,
            ),
            patch("ouroboros.cli.commands.setup.print_success") as mock_success,
        ):
            assert setup_cmd._setup_claude("/usr/local/bin/claude") is False

        assert sync_calls >= 4
        assert saw_recovery_evidence
        assert config_path.read_bytes() == original
        assert not tuple(config_dir.glob("*.config-rollback.claim"))
        mock_success.assert_not_called()

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
        ):
            assert setup_cmd._setup_claude("/second/claude") is True

    @pytest.mark.parametrize("published_target", ["config", "credentials"])
    def test_setup_claude_publication_consumes_stage_without_cleanup_race(
        self, tmp_path: Path, published_target: str
    ) -> None:
        """No-replace rename publishes a stage without a later unlink boundary."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        original = b"orchestrator:\n  runtime_backend: codex\nllm:\n  backend: codex\n"
        config_path.write_bytes(original)
        credentials_path = config_dir / "credentials.yaml"
        if published_target == "config":
            self._write_credentials(config_dir)
        injected = False

        def _observe_published_stage_cleanup(path: Path) -> None:
            nonlocal injected
            if injected or not path.name.endswith(f".{published_target}.stage"):
                return
            target = config_path if published_target == "config" else credentials_path
            if not target.exists():
                return
            injected = True

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.runtime_activation._artifact_cleanup_checkpoint",
                side_effect=_observe_published_stage_cleanup,
            ),
            patch("ouroboros.cli.commands.claude_setup.print_success") as mock_success,
        ):
            assert setup_cmd._setup_claude("/usr/local/bin/claude") is True

        assert not injected
        assert (
            yaml.safe_load(config_path.read_text(encoding="utf-8"))["orchestrator"][
                "runtime_backend"
            ]
            == "claude"
        )
        assert credentials_path.is_file()
        assert not (config_dir / runtime_activation._JOURNAL_NAME).exists()
        assert not tuple(config_dir.glob(f"*.{published_target}.stage"))
        mock_success.assert_called_once()

    def test_remove_owned_artifact_never_unlinks_replaced_retired_generation(
        self, tmp_path: Path
    ) -> None:
        """A same-UID observer may replace a tombstone after its guard snapshot."""
        owned = tmp_path / ".owned.stage"
        owned.write_bytes(b"setup-owned\n")
        expected = runtime_activation._snapshot_target(owned)
        operator_source = tmp_path / ".operator-retired"
        operator_source.write_bytes(b"operator-generation\n")
        operator_source.chmod(0o640)
        operator_identity = (operator_source.stat().st_dev, operator_source.stat().st_ino)
        injected_path: list[Path] = []
        original_matches = runtime_activation._matches_guard

        def _replace_after_match(
            snapshot: runtime_activation._FileSnapshot,
            guard: object,
            *,
            strict: bool,
        ) -> bool:
            matched = original_matches(snapshot, guard, strict=strict)
            if matched and not injected_path:
                retired = next(tmp_path.glob(".*.retired"))
                os.replace(operator_source, retired)
                injected_path.append(retired)
            return matched

        with patch(
            "ouroboros.cli.runtime_activation._matches_guard",
            side_effect=_replace_after_match,
        ):
            runtime_activation._remove_owned_artifact(
                owned,
                runtime_activation._owned_guard(expected),
                durable=False,
            )

        assert not owned.exists()
        assert injected_path
        preserved = injected_path[0]
        assert preserved.read_bytes() == b"operator-generation\n"
        assert stat.S_IMODE(preserved.stat().st_mode) == 0o640
        assert (preserved.stat().st_dev, preserved.stat().st_ino) == operator_identity

    def test_setup_claude_repeated_activation_keeps_live_targets_single_linked(
        self, tmp_path: Path
    ) -> None:
        """Repeated setup never duplicates live credential secrets into artifacts."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text(
            "orchestrator:\n  runtime_backend: codex\nllm:\n  backend: codex\n",
            encoding="utf-8",
        )
        credentials_path = config_dir / "credentials.yaml"
        secret = b"sk-existing-operator-secret"
        credentials_path.write_text(
            yaml.safe_dump(
                {"providers": {"openai": {"api_key": secret.decode()}}},
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        credentials_path.chmod(0o600)

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
        ):
            assert setup_cmd._setup_claude("/first/claude") is True
            assert setup_cmd._setup_claude("/second/claude") is True

        assert config_path.stat().st_nlink == 1
        assert credentials_path.stat().st_nlink == 1
        retired = tuple(config_dir.glob("*.retired"))
        assert retired
        secret_bearing = [
            path for path in config_dir.iterdir() if path.is_file() and secret in path.read_bytes()
        ]
        assert secret_bearing == [credentials_path]
        assert not tuple(config_dir.glob("*.credentials.stage"))

    def test_setup_claude_failed_fresh_credentials_rollback_retires_secret_artifacts(
        self, tmp_path: Path
    ) -> None:
        """A failed activation quarantines generated bytes for explicit recovery."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text(
            "orchestrator:\n  runtime_backend: codex\nllm:\n  backend: codex\n",
            encoding="utf-8",
        )
        secret = b"sk-generated-rollback-secret"
        generated = CredentialsConfig(
            providers={
                "openai": ProviderCredentials(api_key=secret.decode()),
            }
        )

        def _fail_config_publish(operation: str, target: Path) -> None:
            if operation == "publish" and target == config_path:
                raise OSError("config publication failed")

        with (
            patch("ouroboros.config.models.get_config_dir", return_value=config_dir),
            patch("ouroboros.config.models.get_default_credentials", return_value=generated),
            patch(
                "ouroboros.cli.runtime_activation._promotion_checkpoint",
                side_effect=_fail_config_publish,
            ),
        ):
            assert setup_cmd._setup_claude("/usr/local/bin/claude") is False

        assert not (config_dir / "credentials.yaml").exists()
        retained = [
            path for path in config_dir.iterdir() if path.is_file() and secret in path.read_bytes()
        ]
        assert len(retained) == 1
        assert retained[0].name.endswith(".retired")
        assert stat.S_IMODE(retained[0].stat().st_mode) == 0o600

    @pytest.mark.skipif(os.name == "nt", reason="POSIX hard-link semantics")
    def test_setup_claude_post_publish_hard_link_is_preserved_and_retired(
        self, tmp_path: Path
    ) -> None:
        """A post-publication alias keeps its bytes while the setup name retires."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        original = b"orchestrator:\n  runtime_backend: codex\nllm:\n  backend: codex\n"
        config_path.write_bytes(original)
        credentials_path = config_dir / "credentials.yaml"
        operator_link = config_dir / "operator-hardlink"
        secret = b"sk-post-publish-hardlink-secret"
        generated = CredentialsConfig(
            providers={"openai": ProviderCredentials(api_key=secret.decode())}
        )

        def _link_published_credentials(path: Path) -> None:
            if path == credentials_path:
                os.link(credentials_path, operator_link)

        with (
            patch("ouroboros.config.models.get_config_dir", return_value=config_dir),
            patch("ouroboros.config.models.get_default_credentials", return_value=generated),
            patch(
                "ouroboros.cli.runtime_activation._post_publication_checkpoint",
                side_effect=_link_published_credentials,
            ),
        ):
            assert setup_cmd._setup_claude("/usr/local/bin/claude") is False

        assert config_path.read_bytes() == original
        assert not credentials_path.exists()
        assert operator_link.read_bytes() != b""
        assert secret in operator_link.read_bytes()
        linked_retired = [
            path
            for path in config_dir.glob("*.retired")
            if path.stat().st_ino == operator_link.stat().st_ino
        ]
        assert len(linked_retired) == 1
        assert linked_retired[0].read_bytes() == operator_link.read_bytes()

    def test_setup_claude_post_publish_content_change_is_preserved(self, tmp_path: Path) -> None:
        """An in-place operator content generation survives failed handoff."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        original = b"orchestrator:\n  runtime_backend: codex\nllm:\n  backend: codex\n"
        config_path.write_bytes(original)
        credentials_path = config_dir / "credentials.yaml"
        operator = b"providers: {}\noperator: post-publication\n"

        def _replace_published_contents(path: Path) -> None:
            if path == credentials_path:
                credentials_path.write_bytes(operator)

        with (
            patch("ouroboros.config.models.get_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.runtime_activation._post_publication_checkpoint",
                side_effect=_replace_published_contents,
            ),
        ):
            assert setup_cmd._setup_claude("/usr/local/bin/claude") is False

        assert config_path.read_bytes() == original
        assert credentials_path.read_bytes() == operator
        assert (config_dir / runtime_activation._JOURNAL_NAME).exists()

    @pytest.mark.skipif(os.name == "nt", reason="POSIX ownership semantics")
    def test_setup_claude_post_publish_ownership_change_is_preserved(self, tmp_path: Path) -> None:
        """An operator ownership generation is not mutated during rollback."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        original = b"orchestrator:\n  runtime_backend: codex\nllm:\n  backend: codex\n"
        config_path.write_bytes(original)
        credentials_path = config_dir / "credentials.yaml"
        alternate_uid = os.getuid()
        alternate_gid = next(
            (group for group in os.getgroups() if group != os.getgid()),
            None,
        )
        if os.geteuid() == 0:
            alternate_uid = 65534
            alternate_gid = 65534
        elif alternate_gid is None:
            pytest.skip("No alternate permitted ownership generation")
        assert alternate_gid is not None

        def _change_published_owner(path: Path) -> None:
            if path == credentials_path:
                os.chown(credentials_path, alternate_uid, alternate_gid)

        with (
            patch("ouroboros.config.models.get_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.runtime_activation._post_publication_checkpoint",
                side_effect=_change_published_owner,
            ),
        ):
            assert setup_cmd._setup_claude("/usr/local/bin/claude") is False

        assert config_path.read_bytes() == original
        assert credentials_path.exists()
        assert (credentials_path.stat().st_uid, credentials_path.stat().st_gid) == (
            alternate_uid,
            alternate_gid,
        )
        assert (config_dir / runtime_activation._JOURNAL_NAME).exists()

    @pytest.mark.skipif(os.name == "nt", reason="POSIX guarded-name retirement semantics")
    def test_secret_retirement_preserves_foreign_replacement_inode(self, tmp_path: Path) -> None:
        """Guarded retirement never mutates a replacement at its pathname."""
        secret = b"sk-owned-stage-secret"
        stage = tmp_path / ".credentials.stage"
        stage.write_bytes(secret)
        expected = runtime_activation._snapshot_target(stage)
        displaced = tmp_path / ".credentials.displaced"
        foreign_source = tmp_path / ".operator-stage"
        foreign = b"operator-owned-foreign-generation"
        foreign_source.write_bytes(foreign)
        foreign_source.chmod(0o640)
        foreign_identity = (foreign_source.stat().st_dev, foreign_source.stat().st_ino)
        injected = False

        def _replace_before_move(path: Path) -> None:
            nonlocal injected
            if path != stage or injected:
                return
            injected = True
            stage.rename(displaced)
            foreign_source.rename(stage)

        with (
            patch(
                "ouroboros.cli.runtime_activation._artifact_cleanup_checkpoint",
                side_effect=_replace_before_move,
            ),
            pytest.raises(runtime_activation._ConcurrentActivationError),
        ):
            runtime_activation._retire_owned_secret(stage, expected)

        assert injected
        assert stage.read_bytes() == foreign
        assert stat.S_IMODE(stage.stat().st_mode) == 0o640
        assert (stage.stat().st_dev, stage.stat().st_ino) == foreign_identity
        assert displaced.read_bytes() == secret

    @pytest.mark.skipif(os.name == "nt", reason="POSIX hard-link semantics")
    def test_secret_retirement_rejects_new_hard_link_without_mutation(self, tmp_path: Path) -> None:
        """A changed link topology cannot become a retirement target."""
        secret = b"sk-hard-link-guard-secret"
        stage = tmp_path / ".credentials.stage"
        stage.write_bytes(secret)
        expected = runtime_activation._snapshot_target(stage)
        observer_link = tmp_path / "operator-link"
        os.link(stage, observer_link)

        with pytest.raises(runtime_activation._ConcurrentActivationError):
            runtime_activation._retire_owned_secret(stage, expected)

        assert stage.read_bytes() == secret
        assert observer_link.read_bytes() == secret
        assert stage.stat().st_nlink == 2

    @pytest.mark.skipif(os.name == "nt", reason="POSIX hard-link semantics")
    def test_secret_retirement_rechecks_hard_link_immediately_before_move(
        self, tmp_path: Path
    ) -> None:
        """A link added at the move boundary causes fail-closed cleanup."""
        secret = b"sk-hard-link-after-pin-secret"
        stage = tmp_path / ".credentials.stage"
        stage.write_bytes(secret)
        expected = runtime_activation._snapshot_target(stage)
        observer_link = tmp_path / "operator-link"

        def _link_before_move(path: Path) -> None:
            if path == stage:
                os.link(stage, observer_link)

        with (
            patch(
                "ouroboros.cli.runtime_activation._artifact_cleanup_checkpoint",
                side_effect=_link_before_move,
            ),
            pytest.raises(runtime_activation._ConcurrentActivationError),
        ):
            runtime_activation._retire_owned_secret(stage, expected)

        assert stage.read_bytes() == secret
        assert observer_link.read_bytes() == secret
        assert stage.stat().st_nlink == 2

    @pytest.mark.skipif(os.name == "nt", reason="POSIX hard-link semantics")
    def test_secret_retirement_never_calls_final_ftruncate_hook(self, tmp_path: Path) -> None:
        """The reported final-mutation hook cannot create and truncate an alias."""
        secret = b"sk-final-ftruncate-boundary-secret"
        stage = tmp_path / ".credentials.stage"
        stage.write_bytes(secret)
        expected = runtime_activation._snapshot_target(stage)
        observer_link = tmp_path / "operator-link"
        real_ftruncate = os.ftruncate

        def _link_then_truncate(descriptor: int, length: int) -> None:
            os.link(stage, observer_link)
            real_ftruncate(descriptor, length)

        with patch(
            "ouroboros.cli.runtime_activation.os.ftruncate",
            side_effect=_link_then_truncate,
        ) as destructive_hook:
            runtime_activation._retire_owned_secret(stage, expected)

        destructive_hook.assert_not_called()
        assert not observer_link.exists()
        retired = tuple(tmp_path.glob("*.retired"))
        assert len(retired) == 1
        assert retired[0].read_bytes() == secret

    def test_secret_retirement_rechecks_contents_when_mtime_is_restored(
        self, tmp_path: Path
    ) -> None:
        """Same-size replacement bytes cannot bypass the guarded move."""
        secret = b"sk-owned-content-generation"
        stage = tmp_path / ".credentials.stage"
        stage.write_bytes(secret)
        expected = runtime_activation._snapshot_target(stage)
        operator = b"x" * len(secret)

        def _replace_contents_and_restore_mtime(path: Path) -> None:
            if path != stage:
                return
            original_atime = path.stat().st_atime_ns
            path.write_bytes(operator)
            os.utime(
                path,
                ns=(original_atime, expected.modified_ns),
                follow_symlinks=False,
            )

        with (
            patch(
                "ouroboros.cli.runtime_activation._artifact_cleanup_checkpoint",
                side_effect=_replace_contents_and_restore_mtime,
            ),
            pytest.raises(runtime_activation._ConcurrentActivationError),
        ):
            runtime_activation._retire_owned_secret(stage, expected)

        assert stage.read_bytes() == operator
        assert stage.stat().st_nlink == 1

    def test_secret_retirement_rechecks_mode_at_move_boundary(self, tmp_path: Path) -> None:
        """A mode change at retirement is preserved without inode mutation."""
        secret = b"sk-mode-at-retirement-secret"
        stage = tmp_path / ".credentials.stage"
        stage.write_bytes(secret)
        stage.chmod(0o600)
        expected = runtime_activation._snapshot_target(stage)
        mutated = False

        def _change_mode_before_move(path: Path) -> None:
            nonlocal mutated
            if path == stage and not mutated:
                mutated = True
                stage.chmod(0o640)

        with (
            patch(
                "ouroboros.cli.runtime_activation._artifact_cleanup_checkpoint",
                side_effect=_change_mode_before_move,
            ),
            pytest.raises(runtime_activation._ConcurrentActivationError),
        ):
            runtime_activation._retire_owned_secret(stage, expected)

        assert mutated
        assert stage.read_bytes() == secret
        assert stat.S_IMODE(stage.stat().st_mode) == 0o640
        assert not tuple(tmp_path.glob("*.retired"))

    @pytest.mark.skipif(os.name == "nt", reason="POSIX hard-link semantics")
    def test_secret_retirement_rechecks_link_count_at_move_boundary(self, tmp_path: Path) -> None:
        """A link added at retirement is preserved without inode mutation."""
        secret = b"sk-link-at-retirement-secret"
        stage = tmp_path / ".credentials.stage"
        stage.write_bytes(secret)
        expected = runtime_activation._snapshot_target(stage)
        operator_link = tmp_path / "operator-link"
        mutated = False

        def _link_before_move(path: Path) -> None:
            nonlocal mutated
            if path == stage and not mutated:
                mutated = True
                os.link(stage, operator_link)

        with (
            patch(
                "ouroboros.cli.runtime_activation._artifact_cleanup_checkpoint",
                side_effect=_link_before_move,
            ),
            pytest.raises(runtime_activation._ConcurrentActivationError),
        ):
            runtime_activation._retire_owned_secret(stage, expected)

        assert mutated
        assert stage.read_bytes() == secret
        assert operator_link.read_bytes() == secret
        assert stage.stat().st_nlink == 2

    @pytest.mark.skipif(os.name == "nt", reason="POSIX ownership semantics")
    def test_secret_retirement_rechecks_owner_at_move_boundary(self, tmp_path: Path) -> None:
        """An ownership change at retirement is preserved without inode mutation."""
        alternate_uid = os.getuid()
        alternate_gid = next(
            (group for group in os.getgroups() if group != os.getgid()),
            None,
        )
        if os.geteuid() == 0:
            alternate_uid = 65534
            alternate_gid = 65534
        elif alternate_gid is None:
            pytest.skip("No alternate permitted ownership generation")
        assert alternate_gid is not None
        secret = b"sk-owner-at-retirement-secret"
        stage = tmp_path / ".credentials.stage"
        stage.write_bytes(secret)
        expected = runtime_activation._snapshot_target(stage)
        mutated = False

        def _change_owner_before_move(path: Path) -> None:
            nonlocal mutated
            if path == stage and not mutated:
                mutated = True
                os.chown(stage, alternate_uid, alternate_gid)

        with (
            patch(
                "ouroboros.cli.runtime_activation._artifact_cleanup_checkpoint",
                side_effect=_change_owner_before_move,
            ),
            pytest.raises(runtime_activation._ConcurrentActivationError),
        ):
            runtime_activation._retire_owned_secret(stage, expected)

        assert mutated
        assert stage.read_bytes() == secret
        assert (stage.stat().st_uid, stage.stat().st_gid) == (
            alternate_uid,
            alternate_gid,
        )

    def test_journal_cleanup_rechecks_secret_guard_immediately_before_retirement(
        self, tmp_path: Path
    ) -> None:
        """A replacement after journal validation is preserved without mutation."""
        generation = "1" * 32
        credentials_stage = tmp_path / (
            f".claude-runtime-activation.{generation}.credentials.stage"
        )
        secret = b"sk-owned-journal-stage-secret"
        credentials_stage.write_bytes(secret)
        credentials_snapshot = runtime_activation._snapshot_target(credentials_stage)
        journal_path = tmp_path / runtime_activation._JOURNAL_NAME
        journal_path.write_text("{}\n", encoding="utf-8")
        journal_snapshot = runtime_activation._snapshot_target(journal_path)
        missing_guard = runtime_activation._owned_guard(
            runtime_activation._FileSnapshot(kind="missing")
        )
        journal: dict[str, object] = {
            "generation": generation,
            "creates_credentials": True,
            "credentials_published": runtime_activation._owned_guard(credentials_snapshot),
            "config_published": missing_guard,
            "credentials_original_owned": missing_guard,
            "config_original_owned": missing_guard,
            "credentials_stage": credentials_stage.name,
            "config_stage": f".claude-runtime-activation.{generation}.config.stage",
            "credentials_claim": (
                f".claude-runtime-activation.{generation}.credentials-original.claim"
            ),
            "config_claim": f".claude-runtime-activation.{generation}.config-original.claim",
            "credentials_rollback_claim": (
                f".claude-runtime-activation.{generation}.credentials-rollback.claim"
            ),
            "config_rollback_claim": (
                f".claude-runtime-activation.{generation}.config-rollback.claim"
            ),
        }
        foreign_source = tmp_path / ".operator-secret-stage"
        foreign = b"operator-owned-foreign-secret-stage"
        foreign_source.write_bytes(foreign)
        foreign_source.chmod(0o640)
        foreign_identity = (foreign_source.stat().st_dev, foreign_source.stat().st_ino)
        real_snapshot = runtime_activation._snapshot_target
        stage_reads = 0

        def _replace_before_cleanup_retirement(
            path: Path,
        ) -> runtime_activation._FileSnapshot:
            nonlocal stage_reads
            if path == credentials_stage:
                stage_reads += 1
                if stage_reads == 2:
                    os.replace(foreign_source, credentials_stage)
            return real_snapshot(path)

        with (
            patch(
                "ouroboros.cli.runtime_activation._snapshot_target",
                side_effect=_replace_before_cleanup_retirement,
            ),
            pytest.raises(runtime_activation._ConcurrentActivationError),
        ):
            runtime_activation._cleanup_journal_artifacts(
                tmp_path,
                journal_path,
                journal,
                journal_snapshot,
            )

        assert stage_reads == 2
        assert credentials_stage.read_bytes() == foreign
        assert stat.S_IMODE(credentials_stage.stat().st_mode) == 0o640
        assert (
            credentials_stage.stat().st_dev,
            credentials_stage.stat().st_ino,
        ) == foreign_identity

    @pytest.mark.parametrize("prepared_target", ["config", "credentials"])
    def test_setup_claude_preserves_operator_stage_when_prepare_sync_fails(
        self, tmp_path: Path, prepared_target: str
    ) -> None:
        """A prepare failure never path-unlinks a replacement staging inode."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        original = b"orchestrator:\n  runtime_backend: codex\nllm:\n  backend: codex\n"
        config_path.write_bytes(original)
        if prepared_target == "config":
            self._write_credentials(config_dir)
        operator = f"operator-{prepared_target}-prepare\n".encode()
        injected_path: list[Path] = []
        injected_identity: list[tuple[int, int]] = []
        real_fsync = os.fsync

        def _replace_stage_then_fail(descriptor: int) -> None:
            descriptor_stat = os.fstat(descriptor)
            pattern = f"*.{prepared_target}.stage"
            for stage in config_dir.glob(pattern):
                stage_stat = stage.stat()
                if (stage_stat.st_dev, stage_stat.st_ino) != (
                    descriptor_stat.st_dev,
                    descriptor_stat.st_ino,
                ):
                    continue
                replacement = config_dir / f".operator-{prepared_target}-prepare"
                replacement.write_bytes(operator)
                replacement.chmod(0o640)
                os.replace(replacement, stage)
                injected_path.append(stage)
                injected_identity.append((stage.stat().st_dev, stage.stat().st_ino))
                raise OSError("prepared stage sync failed after operator replacement")
            real_fsync(descriptor)

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.runtime_activation.os.fsync",
                side_effect=_replace_stage_then_fail,
            ),
            patch("ouroboros.cli.commands.setup.print_success") as mock_success,
        ):
            assert setup_cmd._setup_claude("/usr/local/bin/claude") is False

        assert injected_path
        preserved = injected_path[0]
        assert preserved.read_bytes() == operator
        assert stat.S_IMODE(preserved.stat().st_mode) == 0o640
        assert (preserved.stat().st_dev, preserved.stat().st_ino) == injected_identity[0]
        assert config_path.read_bytes() == original
        mock_success.assert_not_called()

    @pytest.mark.parametrize("prepared_target", ["config", "credentials"])
    def test_setup_claude_preserves_operator_stage_when_journal_write_fails(
        self, tmp_path: Path, prepared_target: str
    ) -> None:
        """Pre-journal cleanup removes only descriptor-identified stages."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        original = b"orchestrator:\n  runtime_backend: codex\nllm:\n  backend: codex\n"
        config_path.write_bytes(original)
        if prepared_target == "config":
            self._write_credentials(config_dir)
        operator = f"operator-{prepared_target}-journal-failure\n".encode()
        injected_path: list[Path] = []
        injected_identity: list[tuple[int, int]] = []

        def _replace_stage_then_fail(*_args: object, **_kwargs: object) -> None:
            stage = next(config_dir.glob(f"*.{prepared_target}.stage"))
            replacement = config_dir / f".operator-{prepared_target}-journal-failure"
            replacement.write_bytes(operator)
            replacement.chmod(0o640)
            os.replace(replacement, stage)
            injected_path.append(stage)
            injected_identity.append((stage.stat().st_dev, stage.stat().st_ino))
            raise OSError("journal write failed after operator stage replacement")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.runtime_activation._write_journal",
                side_effect=_replace_stage_then_fail,
            ),
            patch("ouroboros.cli.commands.setup.print_success") as mock_success,
        ):
            assert setup_cmd._setup_claude("/usr/local/bin/claude") is False

        assert injected_path
        preserved = injected_path[0]
        assert preserved.read_bytes() == operator
        assert stat.S_IMODE(preserved.stat().st_mode) == 0o640
        assert (preserved.stat().st_dev, preserved.stat().st_ino) == injected_identity[0]
        assert config_path.read_bytes() == original
        mock_success.assert_not_called()

    def test_setup_claude_preserves_operator_journal_stage_republished_before_finally(
        self, tmp_path: Path
    ) -> None:
        """Journal finalization never deletes a generation published after cleanup."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text(
            "orchestrator:\n  runtime_backend: codex\nllm:\n  backend: codex\n",
            encoding="utf-8",
        )
        self._write_credentials(config_dir)
        operator = b"operator-journal-stage-finalizer\n"
        injected_path: list[Path] = []
        injected_identity: list[tuple[int, int]] = []
        original_remove = runtime_activation._remove_owned_artifact

        def _remove_then_republish(
            path: Path, expected_guard: object, *, durable: bool = True
        ) -> None:
            original_remove(path, expected_guard, durable=durable)
            if injected_path or not (
                path.name.startswith(f".{runtime_activation._JOURNAL_NAME}.")
                and path.name.endswith(".tmp")
            ):
                return
            path.write_bytes(operator)
            path.chmod(0o640)
            injected_path.append(path)
            injected_identity.append((path.stat().st_dev, path.stat().st_ino))

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.runtime_activation._remove_owned_artifact",
                side_effect=_remove_then_republish,
            ),
        ):
            assert setup_cmd._setup_claude("/usr/local/bin/claude") is True

        assert injected_path
        preserved = injected_path[0]
        assert preserved.read_bytes() == operator
        assert stat.S_IMODE(preserved.stat().st_mode) == 0o640
        assert (preserved.stat().st_dev, preserved.stat().st_ino) == injected_identity[0]

    def test_atomic_write_preserves_operator_stage_republished_before_finally(
        self, tmp_path: Path
    ) -> None:
        """One-file rollback finalization guards a republished staging path."""
        target = tmp_path / "config.yaml"
        target.write_text("before\n", encoding="utf-8")
        expected = runtime_activation._snapshot_target(target)
        operator = b"operator-atomic-stage-finalizer\n"
        injected_path: list[Path] = []
        injected_identity: list[tuple[int, int]] = []
        original_remove = runtime_activation._remove_owned_artifact

        def _remove_then_republish(
            path: Path, expected_guard: object, *, durable: bool = True
        ) -> None:
            original_remove(path, expected_guard, durable=durable)
            if injected_path or not path.name.endswith(".config.stage"):
                return
            path.write_bytes(operator)
            path.chmod(0o640)
            injected_path.append(path)
            injected_identity.append((path.stat().st_dev, path.stat().st_ino))

        with patch(
            "ouroboros.cli.runtime_activation._remove_owned_artifact",
            side_effect=_remove_then_republish,
        ):
            published = runtime_activation._atomic_write_text_if_current_matches(
                target,
                "after\n",
                expected,
                mode=0o644,
            )

        assert published.contents == b"after\n"
        assert target.read_bytes() == b"after\n"
        assert injected_path
        preserved = injected_path[0]
        assert preserved.read_bytes() == operator
        assert stat.S_IMODE(preserved.stat().st_mode) == 0o640
        assert (preserved.stat().st_dev, preserved.stat().st_ino) == injected_identity[0]

    def test_setup_claude_preserves_original_on_file_sync_failure(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        original = b"orchestrator:\n  runtime_backend: codex\nllm:\n  backend: codex\n"
        config_path.write_bytes(original)
        self._write_credentials(config_dir)

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.runtime_activation.os.fsync", side_effect=OSError("sync failed")),
        ):
            assert setup_cmd._setup_claude("/usr/local/bin/claude") is False

        assert config_path.read_bytes() == original
        assert not tuple(config_dir.glob(".*.tmp"))

    def test_setup_claude_retires_fresh_topology_when_config_replace_fails(
        self, tmp_path: Path
    ) -> None:
        config_dir = tmp_path / ".ouroboros"
        config_path = config_dir / "config.yaml"

        def _fail_config_publish(operation: str, target: Path) -> None:
            if operation == "publish" and target == config_path:
                raise OSError("simulated fresh config replacement failure")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "ouroboros.cli.runtime_activation._promotion_checkpoint",
                side_effect=_fail_config_publish,
            ),
        ):
            assert setup_cmd._setup_claude("/usr/local/bin/claude") is False

        assert not config_path.exists()
        assert not (config_dir / "credentials.yaml").exists()
        assert not (config_dir / runtime_activation._JOURNAL_NAME).exists()
        assert not tuple(config_dir.glob("*.stage"))
        assert not tuple(config_dir.glob("*.claim"))
        retired = tuple(config_dir.glob("*.retired"))
        assert retired
        assert all(path.is_file() for path in retired)

    def test_setup_claude_rolls_back_config_but_preserves_concurrent_credentials_edit(
        self, tmp_path: Path
    ) -> None:
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        original = b"orchestrator:\n  runtime_backend: codex\nllm:\n  backend: codex\n"
        config_path.write_bytes(original)
        credentials_path = self._write_credentials(config_dir)
        operator_credentials = b"providers: {}\noperator: concurrent\n"
        real_promote = runtime_activation._promote_prepared_under_lock

        def _promote_then_edit(
            path: Path,
            prepared: runtime_activation._PreparedFile,
            expected: runtime_activation._FileSnapshot,
            claim: Path,
        ) -> runtime_activation._FileSnapshot:
            written = real_promote(path, prepared, expected, claim)
            if path == config_path:
                credentials_path.write_bytes(operator_credentials)
            return written

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.runtime_activation._promote_prepared_under_lock",
                side_effect=_promote_then_edit,
            ),
        ):
            assert setup_cmd._setup_claude("/usr/local/bin/claude") is False

        assert config_path.read_bytes() == original
        assert credentials_path.read_bytes() == operator_credentials

    def test_setup_claude_preserves_config_edit_after_its_replace(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text(
            "orchestrator:\n  runtime_backend: codex\nllm:\n  backend: codex\n",
            encoding="utf-8",
        )
        self._write_credentials(config_dir)
        operator_config = b"orchestrator:\n  runtime_backend: codex\noperator: concurrent\n"
        real_promote = runtime_activation._promote_prepared_under_lock

        def _promote_then_edit(
            path: Path,
            prepared: runtime_activation._PreparedFile,
            expected: runtime_activation._FileSnapshot,
            claim: Path,
        ) -> runtime_activation._FileSnapshot:
            written = real_promote(path, prepared, expected, claim)
            if path == config_path:
                config_path.write_bytes(operator_config)
            return written

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.runtime_activation._promote_prepared_under_lock",
                side_effect=_promote_then_edit,
            ),
            patch("ouroboros.cli.commands.setup.print_success") as mock_success,
        ):
            assert setup_cmd._setup_claude("/usr/local/bin/claude") is False

        assert config_path.read_bytes() == operator_config
        mock_success.assert_not_called()

    def test_setup_claude_leaves_existing_mcp_entry_untouched(self, tmp_path: Path) -> None:
        """The standalone Claude SDK profile must not mutate MCP wiring."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text("{}", encoding="utf-8")

        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        claude_config = claude_dir / "mcp.json"
        claude_config.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "ouroboros": {
                            "command": "uvx",
                            "args": ["--from", "ouroboros-ai[mcp]", "ouroboros", "mcp", "serve"],
                            "timeout": 600,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.commands.setup.shutil.which",
                side_effect=lambda cmd: "/usr/local/bin/uvx" if cmd == "uvx" else None,
            ),
        ):
            setup_cmd._setup_claude_sdk("/usr/local/bin/claude")

        claude_mcp = json.loads(claude_config.read_text(encoding="utf-8"))
        config_dict = yaml.safe_load(config_path.read_text(encoding="utf-8"))

        assert claude_mcp["mcpServers"]["ouroboros"]["timeout"] == 600
        assert claude_mcp["mcpServers"]["ouroboros"]["command"] == "uvx"
        assert config_dict["orchestrator"]["runtime_backend"] == "claude"
        assert config_dict["llm"]["backend"] == "claude"

    @pytest.mark.parametrize(
        "which_side_effect",
        [
            # uvx available → isolated MCP 2 entry
            (lambda cmd: "/usr/local/bin/uvx" if cmd == "uvx" else None,),
            # no uvx → pipx provides another isolated package environment
            (lambda cmd: "/usr/local/bin/pipx" if cmd == "pipx" else None,),
        ],
        ids=["uvx", "pipx-isolated"],
    )
    def test_setup_claude_does_not_create_mcp_entry(
        self,
        tmp_path: Path,
        which_side_effect,
    ) -> None:
        """Even isolated launchers cannot satisfy the Claude SDK backend lazily."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text("{}", encoding="utf-8")

        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        claude_config = claude_dir / "mcp.json"
        claude_config.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup.shutil.which", side_effect=which_side_effect),
            patch("ouroboros.cli.commands.setup.subprocess.run"),
        ):
            setup_cmd._setup_claude_sdk("/usr/local/bin/claude")

        claude_mcp = json.loads(claude_config.read_text(encoding="utf-8"))
        assert "ouroboros" not in claude_mcp["mcpServers"]

    def test_setup_claude_does_not_register_unisolated_binary(self, tmp_path: Path) -> None:
        """Without uvx/pipx, setup must not reuse the Claude SDK environment."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text("{}", encoding="utf-8")
        claude_config = tmp_path / ".claude" / "mcp.json"
        claude_config.parent.mkdir()
        claude_config.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup.shutil.which", return_value=None),
        ):
            setup_cmd._setup_claude_sdk("/usr/local/bin/claude")

        data = json.loads(claude_config.read_text(encoding="utf-8"))
        assert "ouroboros" not in data["mcpServers"]

    def test_setup_claude_preserves_custom_command(self, tmp_path: Path) -> None:
        """Custom (non-standard) MCP command should not be overwritten."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text("{}", encoding="utf-8")

        custom_args = ["run", "--rm", "ouroboros-mcp"]
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        claude_config = claude_dir / "mcp.json"
        claude_config.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "ouroboros": {
                            "command": "docker",
                            "args": custom_args,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.commands.setup.shutil.which",
                side_effect=lambda cmd: "/usr/local/bin/uvx" if cmd == "uvx" else None,
            ),
        ):
            setup_cmd._setup_claude_sdk("/usr/local/bin/claude")

        claude_mcp = json.loads(claude_config.read_text(encoding="utf-8"))
        # Custom command (docker) should be left untouched
        assert claude_mcp["mcpServers"]["ouroboros"]["command"] == "docker"
        assert claude_mcp["mcpServers"]["ouroboros"]["args"] == custom_args

    def test_setup_claude_preserves_stale_standard_entry(self, tmp_path: Path) -> None:
        """Claude setup owns runtime config, not pre-existing MCP registration."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text("{}", encoding="utf-8")

        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        claude_config = claude_dir / "mcp.json"
        claude_config.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "ouroboros": {
                            "command": "python3",
                            "args": ["-m", "ouroboros", "mcp", "serve"],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        # Simulate uvx now being available
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.commands.setup.shutil.which",
                side_effect=lambda cmd: "/usr/local/bin/uvx" if cmd == "uvx" else None,
            ),
        ):
            setup_cmd._setup_claude_sdk("/usr/local/bin/claude")

        claude_mcp = json.loads(claude_config.read_text(encoding="utf-8"))
        assert claude_mcp["mcpServers"]["ouroboros"] == {
            "command": "python3",
            "args": ["-m", "ouroboros", "mcp", "serve"],
        }

    def test_setup_claude_skips_write_when_args_already_current(self, tmp_path: Path) -> None:
        """No file write when args are already up to date."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text("{}", encoding="utf-8")

        current_args = ["--from", "ouroboros-ai[mcp]", "ouroboros", "mcp", "serve"]
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        claude_config = claude_dir / "mcp.json"
        claude_config.write_text(
            json.dumps({"mcpServers": {"ouroboros": {"command": "uvx", "args": current_args}}}),
            encoding="utf-8",
        )
        mtime_before = claude_config.stat().st_mtime

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.commands.setup.shutil.which",
                side_effect=lambda cmd: "/usr/local/bin/uvx" if cmd == "uvx" else None,
            ),
        ):
            setup_cmd._setup_claude_sdk("/usr/local/bin/claude")

        # File should not be rewritten when nothing changed
        assert claude_config.stat().st_mtime == mtime_before


class TestIsolatedMCPLaunchers:
    """MCP 2 registrations must never inherit an arbitrary host environment."""

    def test_uvx_launchers_explicitly_disable_installed_tool_reuse(self) -> None:
        """``--from`` alone may reuse an installed Ouroboros MCP 1 environment."""
        with patch(
            "ouroboros.cli.commands.setup.shutil.which",
            side_effect=lambda command: "/usr/local/bin/uvx" if command == "uvx" else None,
        ):
            common = setup_cmd._detect_mcp_entry()
            kiro = setup_cmd._detect_mcp_entry_for_kiro()
            opencode = setup_cmd._detect_opencode_mcp_command()

        expected_args = [
            "--isolated",
            "--python",
            ">=3.12",
            "--from",
            "ouroboros-ai[mcp]",
            "ouroboros",
            "mcp",
            "serve",
        ]
        assert common == {"command": "uvx", "args": expected_args}
        assert kiro == common
        assert opencode == {"command": ["uvx", *expected_args]}
        assert expected_args == setup_cmd._CODEX_UVX_MCP_ARGS

    def test_direct_binary_and_python_fallbacks_are_rejected(self) -> None:
        def direct_only(command: str) -> str | None:
            if command in {"ouroboros", "python", "python3"}:
                return f"/usr/local/bin/{command}"
            return None

        with patch("ouroboros.cli.commands.setup.shutil.which", side_effect=direct_only):
            assert setup_cmd._detect_mcp_entry() is None
            assert setup_cmd._detect_mcp_entry_for_kiro() is None
            assert setup_cmd._detect_opencode_mcp_command() is None

    def test_pipx_is_the_isolated_fallback_for_all_host_configs(self) -> None:
        with patch(
            "ouroboros.cli.commands.setup.shutil.which",
            side_effect=lambda command: "/usr/local/bin/pipx" if command == "pipx" else None,
        ):
            common = setup_cmd._detect_mcp_entry()
            kiro = setup_cmd._detect_mcp_entry_for_kiro()
            opencode = setup_cmd._detect_opencode_mcp_command()

        expected_args = [
            "run",
            "--spec",
            "ouroboros-ai[mcp]",
            "ouroboros",
            "mcp",
            "serve",
        ]
        assert common == {
            "command": "pipx",
            "args": expected_args,
        }
        assert kiro == common
        assert opencode == {"command": ["pipx", *expected_args]}

    @pytest.mark.parametrize(
        "register",
        (
            setup_cmd._register_hermes_mcp_server,
            setup_cmd._register_kiro_mcp_server,
            setup_cmd._register_copilot_mcp_server,
        ),
    )
    def test_registration_without_isolated_launcher_is_fail_closed(
        self,
        register,
        tmp_path: Path,
    ) -> None:
        """A pip-only PATH cannot create any host registration artifact."""
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.cli.commands.setup.shutil.which", return_value=None),
        ):
            assert register() is False

        assert not (tmp_path / ".hermes").exists()
        assert not (tmp_path / ".kiro").exists()
        assert not (tmp_path / ".copilot").exists()

    @pytest.mark.parametrize("runtime", ("hermes", "kiro", "copilot"))
    def test_setup_without_isolated_launcher_preserves_runtime_config(
        self,
        runtime: str,
        tmp_path: Path,
    ) -> None:
        """Activation failure occurs before any Ouroboros config write."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        original = "orchestrator:\n  runtime_backend: claude\nllm:\n  backend: claude_code\n"
        config_path.write_text(original, encoding="utf-8")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.cli.commands.setup.shutil.which", return_value=None),
            patch("ouroboros.config.loader.ensure_config_dir") as ensure_config_dir,
        ):
            if runtime == "hermes":
                result = setup_cmd._setup_hermes("/opt/bin/hermes")
            elif runtime == "kiro":
                result = setup_cmd._setup_kiro("/opt/bin/kiro-cli")
            else:
                result = setup_cmd._setup_copilot(
                    "/opt/bin/copilot",
                    non_interactive=True,
                )

        assert result is False
        ensure_config_dir.assert_not_called()
        assert config_path.read_text(encoding="utf-8") == original

    @pytest.mark.parametrize("runtime", ("hermes", "kiro", "copilot"))
    @pytest.mark.parametrize("host_existed", (True, False))
    def test_runtime_setup_rolls_back_host_registration_when_config_commit_fails(
        self,
        runtime: str,
        host_existed: bool,
        tmp_path: Path,
    ) -> None:
        """Host registration and Ouroboros runtime config commit atomically."""
        host_paths = {
            "hermes": tmp_path / ".hermes" / "config.yaml",
            "kiro": tmp_path / ".kiro" / "settings" / "mcp.json",
            "copilot": tmp_path / ".copilot" / "mcp-config.json",
        }
        original_hosts = {
            "hermes": "mcp_servers:\n  custom:\n    command: custom\n",
            "kiro": '{"mcpServers":{"custom":{"command":"custom"}}}\n',
            "copilot": '{"mcpServers":{"custom":{"command":"custom"}}}\n',
        }
        host_path = host_paths[runtime]
        if host_existed:
            host_path.parent.mkdir(parents=True)
            host_path.write_text(original_hosts[runtime], encoding="utf-8")

        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        original_config = "orchestrator:\n  runtime_backend: claude\nllm:\n  backend: claude_code\n"
        config_path.write_text(original_config, encoding="utf-8")

        real_atomic_write = setup_cmd._atomic_write_text
        config_write_failed = False

        def fail_first_runtime_config_write(
            path: Path,
            content: str,
            *,
            mode: int = 0o644,
        ) -> None:
            nonlocal config_write_failed
            if path == config_path and not config_write_failed:
                config_write_failed = True
                raise OSError("simulated config commit failure")
            real_atomic_write(path, content, mode=mode)

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.commands.setup._detect_mcp_entry",
                return_value={
                    "command": "uvx",
                    "args": [
                        "--from",
                        "ouroboros-ai[mcp]",
                        "ouroboros",
                        "mcp",
                        "serve",
                    ],
                },
            ),
            patch(
                "ouroboros.cli.commands.setup._atomic_write_text",
                side_effect=fail_first_runtime_config_write,
            ),
            patch(
                "ouroboros.copilot.model_discovery.list_copilot_models",
                return_value=TestCopilotSetup._stub_models(),
            ),
            patch("ouroboros.copilot.model_discovery.used_fallback", return_value=False),
        ):
            if runtime == "hermes":
                result = setup_cmd._setup_hermes("/opt/bin/hermes")
            elif runtime == "kiro":
                result = setup_cmd._setup_kiro("/opt/bin/kiro-cli")
            else:
                result = setup_cmd._setup_copilot(
                    "/opt/bin/copilot",
                    non_interactive=True,
                )

        assert result is False
        assert config_write_failed is True
        assert config_path.read_text(encoding="utf-8") == original_config
        if host_existed:
            assert host_path.read_text(encoding="utf-8") == original_hosts[runtime]
        else:
            assert not host_path.exists()

    @pytest.mark.parametrize("runtime", ("hermes", "kiro", "copilot"))
    def test_runtime_setup_rolls_back_partial_default_config_creation(
        self,
        runtime: str,
        tmp_path: Path,
    ) -> None:
        """A failing first-time config bootstrap cannot leave host state behind."""
        host_paths = {
            "hermes": tmp_path / ".hermes" / "config.yaml",
            "kiro": tmp_path / ".kiro" / "settings" / "mcp.json",
            "copilot": tmp_path / ".copilot" / "mcp-config.json",
        }
        original_hosts = {
            "hermes": "mcp_servers:\n  custom:\n    command: custom\n",
            "kiro": '{"mcpServers":{"custom":{"command":"custom"}}}\n',
            "copilot": '{"mcpServers":{"custom":{"command":"custom"}}}\n',
        }
        host_path = host_paths[runtime]
        host_path.parent.mkdir(parents=True)
        host_path.write_text(original_hosts[runtime], encoding="utf-8")

        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        credentials_path = config_dir / "credentials.yaml"

        def fail_after_partial_default_creation(target_dir: Path) -> None:
            (target_dir / "config.yaml").write_text("partial config", encoding="utf-8")
            (target_dir / "credentials.yaml").write_text("partial credentials", encoding="utf-8")
            raise OSError("simulated default config failure")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.config.loader.create_default_config",
                side_effect=fail_after_partial_default_creation,
            ),
            patch(
                "ouroboros.cli.commands.setup._detect_mcp_entry",
                return_value={
                    "command": "uvx",
                    "args": [
                        "--from",
                        "ouroboros-ai[mcp]",
                        "ouroboros",
                        "mcp",
                        "serve",
                    ],
                },
            ),
            patch(
                "ouroboros.copilot.model_discovery.list_copilot_models",
                return_value=TestCopilotSetup._stub_models(),
            ),
            patch("ouroboros.copilot.model_discovery.used_fallback", return_value=False),
        ):
            if runtime == "hermes":
                result = setup_cmd._setup_hermes("/opt/bin/hermes")
            elif runtime == "kiro":
                result = setup_cmd._setup_kiro("/opt/bin/kiro-cli")
            else:
                result = setup_cmd._setup_copilot(
                    "/opt/bin/copilot",
                    non_interactive=True,
                )

        assert result is False
        assert host_path.read_text(encoding="utf-8") == original_hosts[runtime]
        assert not config_path.exists()
        assert not credentials_path.exists()


class TestHermesSetup:
    """Tests for Hermes-specific setup behavior."""

    def test_register_hermes_mcp_server_uses_runtime_neutral_mcp_package(
        self,
        tmp_path: Path,
    ) -> None:
        """Hermes MCP registration should not require Claude extras."""
        hermes_dir = tmp_path / ".hermes"
        hermes_dir.mkdir()

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "ouroboros.cli.commands.setup.shutil.which",
                side_effect=lambda cmd: "/usr/local/bin/uvx" if cmd == "uvx" else None,
            ),
        ):
            setup_cmd._register_hermes_mcp_server()

        config = yaml.safe_load((hermes_dir / "config.yaml").read_text(encoding="utf-8"))
        assert config["mcp_servers"]["ouroboros"]["command"] == "uvx"
        assert config["mcp_servers"]["ouroboros"]["args"] == [
            "--isolated",
            "--python",
            ">=3.12",
            "--from",
            "ouroboros-ai[mcp]",
            "ouroboros",
            "mcp",
            "serve",
        ]
        assert config["mcp_servers"]["ouroboros"]["enabled"] is True

    def test_setup_hermes_updates_config_without_overwriting_llm_backend(
        self,
        tmp_path: Path,
    ) -> None:
        """Hermes setup should configure runtime state but leave LLM backend intact."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text(
            yaml.safe_dump(
                {
                    "orchestrator": {"runtime_backend": "claude"},
                    "llm": {"backend": "codex", "qa_model": "gpt-5.4"},
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._install_hermes_artifacts") as mock_install,
            patch("ouroboros.cli.commands.setup._register_hermes_mcp_server") as mock_register,
        ):
            setup_cmd._setup_hermes("/usr/local/bin/hermes")

        config_dict = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert config_dict["orchestrator"]["runtime_backend"] == "hermes"
        assert config_dict["orchestrator"]["hermes_cli_path"] == "/usr/local/bin/hermes"
        assert config_dict["llm"]["backend"] == "codex"
        assert config_dict["llm"]["qa_model"] == "gpt-5.4"
        mock_install.assert_called_once_with()
        mock_register.assert_called_once()
        assert mock_register.call_args.kwargs["detected"]["command"] in {"uvx", "pipx"}

    def test_setup_hermes_reports_failure_when_required_skills_are_not_installed(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        original_runtime_config = "orchestrator:\n  runtime_backend: codex\n"
        config_path.write_text(original_runtime_config, encoding="utf-8")
        hermes_config = tmp_path / ".hermes" / "config.yaml"
        hermes_config.parent.mkdir()
        original_hermes_config = "mcp_servers:\n  existing:\n    command: keep\n"
        hermes_config.write_text(original_hermes_config, encoding="utf-8")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.commands.setup._install_hermes_artifacts",
                return_value=False,
            ),
            patch("ouroboros.cli.commands.setup._register_hermes_mcp_server") as mock_register,
        ):
            result = setup_cmd._setup_hermes("/usr/local/bin/hermes")

        output = capsys.readouterr().out
        assert result is False
        assert "activation incomplete" in output
        assert "Configured Hermes runtime" not in output
        assert config_path.read_text(encoding="utf-8") == original_runtime_config
        assert hermes_config.read_text(encoding="utf-8") == original_hermes_config
        mock_register.assert_not_called()

    @pytest.mark.parametrize("target_existed", (False, True))
    def test_setup_hermes_rolls_back_skills_when_activation_fails(
        self, tmp_path: Path, target_existed: bool
    ) -> None:
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text("orchestrator:\n  runtime_backend: claude\n", encoding="utf-8")
        source_skills = tmp_path / "packaged-skills"
        source_run = source_skills / "run"
        source_run.mkdir(parents=True)
        source_run.joinpath("SKILL.md").write_text("fresh skill\n", encoding="utf-8")
        target = tmp_path / ".hermes" / "skills" / "autonomous-ai-agents" / "ouroboros"
        if target_existed:
            target.joinpath("run").mkdir(parents=True)
            target.joinpath("run", "SKILL.md").write_text("operator generation\n", encoding="utf-8")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.commands.setup._detect_mcp_entry",
                return_value={"command": "uvx", "args": ["ouroboros", "mcp", "serve"]},
            ),
            patch(
                "ouroboros.hermes.artifacts._repo_root_skills_dir",
                return_value=source_skills,
            ),
            patch(
                "ouroboros.cli.commands.setup._register_hermes_mcp_server",
                return_value=False,
            ),
        ):
            result = setup_cmd._setup_hermes("/usr/local/bin/hermes")

        assert result is False
        if target_existed:
            assert target.joinpath("run", "SKILL.md").read_text(encoding="utf-8") == (
                "operator generation\n"
            )
        else:
            assert not target.exists()

    def test_setup_hermes_failed_first_publication_leaves_no_unactivated_artifact(
        self, tmp_path: Path
    ) -> None:
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text("orchestrator:\n  runtime_backend: claude\n", encoding="utf-8")
        source_skills = tmp_path / "packaged-skills"
        source_skills.joinpath("run").mkdir(parents=True)
        source_skills.joinpath("run", "SKILL.md").write_text("fresh\n", encoding="utf-8")
        target = tmp_path / ".hermes" / "skills" / "autonomous-ai-agents" / "ouroboros"
        real_replace = os.replace

        def interrupt_after_first_publication(src, dst):
            result = real_replace(src, dst)
            if Path(dst) == target and Path(src).name.startswith(".ouroboros-skills-"):
                raise OSError("committed first publication")
            return result

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.commands.setup._detect_mcp_entry",
                return_value={"command": "uvx", "args": ["ouroboros", "mcp", "serve"]},
            ),
            patch(
                "ouroboros.hermes.artifacts._repo_root_skills_dir",
                return_value=source_skills,
            ),
            patch(
                "ouroboros.hermes.artifacts.os.replace",
                side_effect=interrupt_after_first_publication,
            ),
            patch("ouroboros.cli.commands.setup._register_hermes_mcp_server") as register,
        ):
            assert setup_cmd._setup_hermes("/usr/local/bin/hermes") is False

        assert not target.exists()
        register.assert_not_called()
        assert config_path.read_text(encoding="utf-8") == (
            "orchestrator:\n  runtime_backend: claude\n"
        )

    def test_setup_hermes_rolls_back_when_post_publication_snapshot_fails(
        self, tmp_path: Path
    ) -> None:
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        original_config = "orchestrator:\n  runtime_backend: claude\n"
        config_path.write_text(original_config, encoding="utf-8")
        source_skills = tmp_path / "packaged-skills"
        source_skills.joinpath("run").mkdir(parents=True)
        source_skills.joinpath("run", "SKILL.md").write_text("fresh\n", encoding="utf-8")
        target = tmp_path / ".hermes" / "skills" / "autonomous-ai-agents" / "ouroboros"
        target.joinpath("run").mkdir(parents=True)
        target.joinpath("run", "SKILL.md").write_text("previous\n", encoding="utf-8")
        original_snapshot = setup_cmd._snapshot_path

        def fail_published_snapshot(path: Path, **kwargs):
            skill = target / "run" / "SKILL.md"
            if path == target and skill.exists() and skill.read_text(encoding="utf-8") == "fresh\n":
                raise OSError("synthetic post-publication snapshot failure")
            return original_snapshot(path, **kwargs)

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.commands.setup._detect_mcp_entry",
                return_value={"command": "uvx", "args": ["ouroboros", "mcp", "serve"]},
            ),
            patch(
                "ouroboros.hermes.artifacts._repo_root_skills_dir",
                return_value=source_skills,
            ),
            patch(
                "ouroboros.cli.commands.setup._snapshot_path",
                side_effect=fail_published_snapshot,
            ),
            patch("ouroboros.cli.commands.setup._register_hermes_mcp_server") as register,
        ):
            assert setup_cmd._setup_hermes("/usr/local/bin/hermes") is False

        assert target.joinpath("run", "SKILL.md").read_text(encoding="utf-8") == "previous\n"
        assert config_path.read_text(encoding="utf-8") == original_config
        register.assert_not_called()

    def test_setup_hermes_preserves_edit_after_publication_before_receipt(
        self, tmp_path: Path
    ) -> None:
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text(
            "orchestrator:\n  runtime_backend: claude\n", encoding="utf-8"
        )
        source_skills = tmp_path / "packaged-skills"
        source_skills.joinpath("run").mkdir(parents=True)
        source_skills.joinpath("run", "SKILL.md").write_text("fresh\n", encoding="utf-8")
        target = tmp_path / ".hermes" / "skills" / "autonomous-ai-agents" / "ouroboros"
        from ouroboros.hermes.artifacts import install_hermes_skills

        real_install = install_hermes_skills

        def publish_then_operator_edit(**kwargs):
            receipt = real_install(**kwargs)
            target.joinpath("operator-after-publish.txt").write_text("preserve\n", encoding="utf-8")
            return receipt

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.commands.setup._detect_mcp_entry",
                return_value={"command": "uvx", "args": ["ouroboros", "mcp", "serve"]},
            ),
            patch(
                "ouroboros.hermes.artifacts._repo_root_skills_dir",
                return_value=source_skills,
            ),
            patch(
                "ouroboros.hermes.artifacts.install_hermes_skills",
                side_effect=publish_then_operator_edit,
            ),
            patch(
                "ouroboros.cli.commands.setup._register_hermes_mcp_server",
                return_value=False,
            ),
        ):
            assert setup_cmd._setup_hermes("/usr/local/bin/hermes") is False

        assert target.joinpath("operator-after-publish.txt").read_text(encoding="utf-8") == (
            "preserve\n"
        )

    def test_setup_hermes_rollback_staging_failure_keeps_published_generation(
        self, tmp_path: Path
    ) -> None:
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text(
            "orchestrator:\n  runtime_backend: claude\n", encoding="utf-8"
        )
        target = tmp_path / ".hermes" / "skills" / "autonomous-ai-agents" / "ouroboros"
        target.joinpath("run").mkdir(parents=True)
        target.joinpath("run", "SKILL.md").write_text("previous\n", encoding="utf-8")

        def publish() -> bool:
            target.joinpath("run", "SKILL.md").write_text("published\n", encoding="utf-8")
            return True

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.commands.setup._detect_mcp_entry",
                return_value={"command": "uvx", "args": ["ouroboros", "mcp", "serve"]},
            ),
            patch("ouroboros.cli.commands.setup._install_hermes_artifacts", side_effect=publish),
            patch(
                "ouroboros.cli.commands.setup._register_hermes_mcp_server",
                return_value=False,
            ),
            patch(
                "ouroboros.cli.commands.setup._restore_path_snapshot",
                side_effect=OSError("synthetic staging write failure"),
            ),
        ):
            assert setup_cmd._setup_hermes("/usr/local/bin/hermes") is False

        assert target.joinpath("run", "SKILL.md").read_text(encoding="utf-8") == "published\n"
        assert not tuple(target.parent.glob(".ouroboros-rollback-*"))

    def test_setup_hermes_rollback_revalidates_after_staging(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text(
            "orchestrator:\n  runtime_backend: claude\n", encoding="utf-8"
        )
        target = tmp_path / ".hermes" / "skills" / "autonomous-ai-agents" / "ouroboros"
        target.joinpath("run").mkdir(parents=True)
        target.joinpath("run", "SKILL.md").write_text("previous\n", encoding="utf-8")
        real_restore = setup_cmd._restore_path_snapshot

        def publish() -> bool:
            target.joinpath("run", "SKILL.md").write_text("published\n", encoding="utf-8")
            return True

        def stage_then_mutate(path, snapshot, **kwargs) -> None:
            real_restore(path, snapshot, **kwargs)
            target.joinpath("concurrent.txt").write_text("preserve\n", encoding="utf-8")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.commands.setup._detect_mcp_entry",
                return_value={"command": "uvx", "args": ["ouroboros", "mcp", "serve"]},
            ),
            patch("ouroboros.cli.commands.setup._install_hermes_artifacts", side_effect=publish),
            patch("ouroboros.cli.commands.setup._register_hermes_mcp_server", return_value=False),
            patch(
                "ouroboros.cli.commands.setup._restore_path_snapshot",
                side_effect=stage_then_mutate,
            ),
        ):
            assert setup_cmd._setup_hermes("/usr/local/bin/hermes") is False

        assert target.joinpath("concurrent.txt").read_text(encoding="utf-8") == "preserve\n"
        assert not tuple(target.parent.glob(".ouroboros-rollback-*"))

    def test_setup_hermes_rollback_preserves_concurrent_symlink_target_update(
        self, tmp_path: Path
    ) -> None:
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text("orchestrator:\n  runtime_backend: claude\n", encoding="utf-8")
        source_skills = tmp_path / "packaged-skills"
        source_skills.joinpath("run").mkdir(parents=True)
        source_skills.joinpath("run", "SKILL.md").write_text("fresh\n", encoding="utf-8")
        target = tmp_path / ".hermes" / "skills" / "autonomous-ai-agents" / "ouroboros"
        target.mkdir(parents=True)
        external = tmp_path / "operator-state.txt"
        external.write_text("before\n", encoding="utf-8")
        target.joinpath("operator-link").symlink_to(external)

        def fail_after_external_update(*, detected) -> bool:  # noqa: ARG001
            external.write_text("concurrent\n", encoding="utf-8")
            return False

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.commands.setup._detect_mcp_entry",
                return_value={"command": "uvx", "args": ["ouroboros", "mcp", "serve"]},
            ),
            patch(
                "ouroboros.hermes.artifacts._repo_root_skills_dir",
                return_value=source_skills,
            ),
            patch(
                "ouroboros.cli.commands.setup._register_hermes_mcp_server",
                side_effect=fail_after_external_update,
            ),
        ):
            result = setup_cmd._setup_hermes("/usr/local/bin/hermes")

        assert result is False
        assert target.joinpath("operator-link").is_symlink()
        assert external.read_text(encoding="utf-8") == "concurrent\n"

    def test_setup_hermes_never_reads_nested_symlink_targets(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text("orchestrator:\n  runtime_backend: claude\n", encoding="utf-8")
        target = tmp_path / ".hermes" / "skills" / "autonomous-ai-agents" / "ouroboros"
        target.mkdir(parents=True)
        external = tmp_path / "operator-secret.txt"
        external.write_text("secret\n", encoding="utf-8")
        target.joinpath("operator-link").symlink_to(external)
        real_read_bytes = Path.read_bytes

        def refuse_external_read(path: Path) -> bytes:
            if path == external:
                raise AssertionError("nested symlink target was read")
            return real_read_bytes(path)

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("pathlib.Path.read_bytes", refuse_external_read),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.commands.setup._detect_mcp_entry",
                return_value={"command": "uvx", "args": ["ouroboros", "mcp", "serve"]},
            ),
            patch("ouroboros.cli.commands.setup._install_hermes_artifacts", return_value=True),
            patch(
                "ouroboros.cli.commands.setup._register_hermes_mcp_server",
                return_value=False,
            ),
        ):
            result = setup_cmd._setup_hermes("/usr/local/bin/hermes")

        assert result is False
        assert target.joinpath("operator-link").is_symlink()
        assert external.read_text(encoding="utf-8") == "secret\n"

    def test_setup_hermes_rollback_preserves_concurrent_managed_tree_edits(
        self, tmp_path: Path
    ) -> None:
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text("orchestrator:\n  runtime_backend: claude\n", encoding="utf-8")
        source_skills = tmp_path / "packaged-skills"
        source_skills.joinpath("run").mkdir(parents=True)
        source_skills.joinpath("run", "SKILL.md").write_text("fresh\n", encoding="utf-8")
        target = tmp_path / ".hermes" / "skills" / "autonomous-ai-agents" / "ouroboros"
        target.joinpath("operator-note.txt").parent.mkdir(parents=True)
        target.joinpath("operator-note.txt").write_text("before\n", encoding="utf-8")

        def fail_after_tree_update(*, detected) -> bool:  # noqa: ARG001
            target.joinpath("operator-note.txt").write_text("concurrent\n", encoding="utf-8")
            target.joinpath("new-note.txt").write_text("new concurrent file\n", encoding="utf-8")
            return False

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.commands.setup._detect_mcp_entry",
                return_value={"command": "uvx", "args": ["ouroboros", "mcp", "serve"]},
            ),
            patch(
                "ouroboros.hermes.artifacts._repo_root_skills_dir",
                return_value=source_skills,
            ),
            patch(
                "ouroboros.cli.commands.setup._register_hermes_mcp_server",
                side_effect=fail_after_tree_update,
            ),
        ):
            result = setup_cmd._setup_hermes("/usr/local/bin/hermes")

        assert result is False
        assert target.joinpath("operator-note.txt").read_text(encoding="utf-8") == "concurrent\n"
        assert target.joinpath("new-note.txt").read_text(encoding="utf-8") == (
            "new concurrent file\n"
        )

    def test_setup_hermes_repairs_scalar_top_level_config(self, tmp_path: Path) -> None:
        """Hermes setup should recover from malformed scalar config.yaml contents."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text("just_a_string\n", encoding="utf-8")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._install_hermes_artifacts"),
            patch("ouroboros.cli.commands.setup._register_hermes_mcp_server"),
        ):
            setup_cmd._setup_hermes("/usr/bin/hermes")

        result = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert isinstance(result, dict)
        assert result["orchestrator"]["runtime_backend"] == "hermes"
        assert result["orchestrator"]["hermes_cli_path"] == "/usr/bin/hermes"

    def test_setup_hermes_repairs_scalar_hermes_config(self, tmp_path: Path) -> None:
        """Hermes setup should recover from malformed ~/.hermes/config.yaml contents."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text("{}", encoding="utf-8")

        hermes_dir = tmp_path / ".hermes"
        hermes_dir.mkdir()
        (hermes_dir / "config.yaml").write_text("just_a_string\n", encoding="utf-8")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._install_hermes_artifacts"),
            patch(
                "ouroboros.cli.commands.setup.shutil.which",
                side_effect=lambda cmd: "/usr/local/bin/uvx" if cmd == "uvx" else None,
            ),
        ):
            setup_cmd._setup_hermes("/usr/bin/hermes")

        result = yaml.safe_load((hermes_dir / "config.yaml").read_text(encoding="utf-8"))
        assert result["mcp_servers"]["ouroboros"]["command"] == "uvx"
        assert result["mcp_servers"]["ouroboros"]["args"] == [
            "--isolated",
            "--python",
            ">=3.12",
            "--from",
            "ouroboros-ai[mcp]",
            "ouroboros",
            "mcp",
            "serve",
        ]
        assert result["mcp_servers"]["ouroboros"]["enabled"] is True

    def test_register_hermes_mcp_server_repairs_malformed_mcp_servers_section(
        self,
        tmp_path: Path,
    ) -> None:
        """Reset non-mapping ``mcp_servers:`` section instead of crashing.

        Regression guard for the PR #457 round-2 review finding — previously
        a hand-edited config like ``mcp_servers: just_a_string`` slipped past
        ``setdefault`` and tripped ``TypeError: 'str' object does not support
        item assignment`` on the very next line, so
        ``ouroboros setup --runtime hermes`` failed instead of self-repairing.
        """
        hermes_dir = tmp_path / ".hermes"
        hermes_dir.mkdir()
        (hermes_dir / "config.yaml").write_text(
            "mcp_servers: just_a_string\n",
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "ouroboros.cli.commands.setup.shutil.which",
                side_effect=lambda cmd: "/usr/local/bin/uvx" if cmd == "uvx" else None,
            ),
        ):
            setup_cmd._register_hermes_mcp_server()

        result = yaml.safe_load((hermes_dir / "config.yaml").read_text(encoding="utf-8"))
        assert isinstance(result["mcp_servers"], dict)
        assert result["mcp_servers"]["ouroboros"]["command"] == "uvx"
        assert result["mcp_servers"]["ouroboros"]["enabled"] is True

    def test_setup_hermes_does_not_register_claude_integration(self, tmp_path: Path) -> None:
        """Hermes setup should stay scoped to Hermes even when Claude is installed."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text("{}", encoding="utf-8")

        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._install_hermes_artifacts"),
            patch("ouroboros.cli.commands.setup._register_hermes_mcp_server"),
            patch("ouroboros.cli.commands.setup._ensure_claude_mcp_entry") as mock_claude,
        ):
            setup_cmd._setup_hermes("/usr/bin/hermes")

        mock_claude.assert_not_called()


# ── Brownfield helper function tests ─────────────────────────────


class TestDisplayReposTable:
    """Tests for _display_repos_table rendering."""

    @pytest.mark.parametrize(
        ("repos", "kwargs"),
        [
            pytest.param(
                [
                    {
                        "path": "/home/user/proj",
                        "name": "proj",
                        "desc": "A project",
                        "is_default": True,
                    },
                    {"path": "/home/user/other", "name": "other", "desc": "", "is_default": False},
                ],
                {},
                id="typical-repo-data",
            ),
            pytest.param([], {}, id="empty-list"),
            pytest.param(
                [{"path": "/p", "name": "n", "desc": "d", "is_default": False}],
                {"show_default": False},
                id="without-default-column",
            ),
        ],
    )
    def test_renders_without_error(self, repos: list[dict], kwargs: dict) -> None:
        """Table renders without raising, with or without the default column."""
        _display_repos_table(repos, **kwargs)


class TestPromptRepoSelection:
    """Tests for _prompt_repo_selection interactive input."""

    @pytest.mark.parametrize(
        ("repo_count", "answer", "expected"),
        [
            pytest.param(3, "2", 1, id="valid-number-is-zero-based"),
            pytest.param(2, "1", 0, id="first-repo"),
            pytest.param(1, "skip", None, id="skip"),
            pytest.param(1, "abc", None, id="non-number"),
            pytest.param(1, "5", None, id="out-of-range"),
        ],
    )
    def test_selection(self, repo_count: int, answer: str, expected: int | None) -> None:
        """A valid number maps to a 0-based index; anything else returns None."""
        repos = [{"path": f"/{chr(97 + i)}", "name": chr(97 + i)} for i in range(repo_count)]
        with patch("ouroboros.cli.commands.setup.Prompt.ask", return_value=answer):
            result = _prompt_repo_selection(repos)
        assert result == expected


# ── Brownfield async core logic tests ─────────────────────────────


class TestScanAndRegisterRepos:
    """Tests for _scan_and_register_repos async function."""

    @pytest.mark.asyncio
    async def test_empty_scan(self) -> None:
        """Returns empty list when no repos found."""
        mock_store = AsyncMock()
        mock_store.initialize = AsyncMock()
        mock_store.close = AsyncMock()
        mock_store.clear_all = AsyncMock(return_value=0)

        with (
            patch(
                "ouroboros.cli.commands.setup.BrownfieldStore",
                return_value=mock_store,
            ),
            patch(
                "ouroboros.cli.commands.setup.scan_and_register",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            result = await _scan_and_register_repos()

        assert result == []

    @pytest.mark.asyncio
    async def test_store_closed_on_success(self) -> None:
        """Store is closed even after successful operation."""
        mock_store = AsyncMock()
        mock_store.initialize = AsyncMock()
        mock_store.close = AsyncMock()
        mock_store.clear_all = AsyncMock(return_value=0)

        with (
            patch(
                "ouroboros.cli.commands.setup.BrownfieldStore",
                return_value=mock_store,
            ),
            patch(
                "ouroboros.cli.commands.setup.scan_and_register",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            await _scan_and_register_repos()

        mock_store.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_store_closed_on_error(self) -> None:
        """Store is closed even when scan raises."""
        mock_store = AsyncMock()
        mock_store.initialize = AsyncMock()
        mock_store.close = AsyncMock()
        mock_store.clear_all = AsyncMock(return_value=0)

        with (
            patch(
                "ouroboros.cli.commands.setup.BrownfieldStore",
                return_value=mock_store,
            ),
            patch(
                "ouroboros.cli.commands.setup.scan_and_register",
                new_callable=AsyncMock,
                side_effect=RuntimeError("scan failed"),
            ),
        ):
            with pytest.raises(RuntimeError, match="scan failed"):
                await _scan_and_register_repos()

        mock_store.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_does_not_call_clear_all_before_scan(self) -> None:
        """Setup delegates clearing to scan_and_register — no separate clear_all."""
        mock_store = AsyncMock()
        mock_store.initialize = AsyncMock()
        mock_store.close = AsyncMock()
        mock_store.clear_all = AsyncMock(return_value=0)

        with (
            patch(
                "ouroboros.cli.commands.setup.BrownfieldStore",
                return_value=mock_store,
            ),
            patch(
                "ouroboros.cli.commands.setup.scan_and_register",
                new_callable=AsyncMock,
                return_value=[],
            ) as mock_scan,
        ):
            await _scan_and_register_repos()

        # clear_all should NOT be called — scan_and_register handles it internally
        mock_store.clear_all.assert_not_awaited()
        mock_scan.assert_awaited_once()


class TestListRepos:
    """Tests for _list_repos async function."""

    @pytest.mark.asyncio
    async def test_returns_all_repos(self) -> None:
        """Returns all registered repos as dicts."""
        from ouroboros.persistence.brownfield import BrownfieldRepo

        mock_repos = [
            BrownfieldRepo(path="/a", name="a", desc="desc-a", is_default=False),
        ]

        mock_store = AsyncMock()
        mock_store.initialize = AsyncMock()
        mock_store.close = AsyncMock()
        mock_store.list = AsyncMock(return_value=mock_repos)

        with patch(
            "ouroboros.cli.commands.setup.BrownfieldStore",
            return_value=mock_store,
        ):
            result = await _list_repos()

        assert len(result) == 1
        assert result[0]["path"] == "/a"
        assert result[0]["desc"] == "desc-a"


class TestSetDefaultRepo:
    """Tests for _set_default_repo async function."""

    @pytest.mark.asyncio
    async def test_set_default_success(self) -> None:
        """Returns True when toggling a non-default repo to default."""
        from ouroboros.persistence.brownfield import BrownfieldRepo

        mock_repo = BrownfieldRepo(path="/a", name="a", is_default=False)
        mock_repo_updated = BrownfieldRepo(path="/a", name="a", is_default=True)

        mock_store = AsyncMock()
        mock_store.initialize = AsyncMock()
        mock_store.close = AsyncMock()
        mock_store.list = AsyncMock(return_value=[mock_repo])

        with (
            patch(
                "ouroboros.cli.commands.setup.BrownfieldStore",
                return_value=mock_store,
            ),
            patch(
                "ouroboros.cli.commands.setup.set_default_repo",
                new_callable=AsyncMock,
                return_value=mock_repo_updated,
            ),
        ):
            result = await _set_default_repo("/a")

        assert result is True

    @pytest.mark.asyncio
    async def test_toggle_removes_existing_default(self) -> None:
        """Returns True when toggling a default repo to non-default."""
        from ouroboros.persistence.brownfield import BrownfieldRepo

        mock_repo = BrownfieldRepo(path="/a", name="a", is_default=True)
        mock_repo_updated = BrownfieldRepo(path="/a", name="a", is_default=False)

        mock_store = AsyncMock()
        mock_store.initialize = AsyncMock()
        mock_store.close = AsyncMock()
        mock_store.list = AsyncMock(return_value=[mock_repo])
        mock_store.update_is_default = AsyncMock(return_value=mock_repo_updated)

        with patch(
            "ouroboros.cli.commands.setup.BrownfieldStore",
            return_value=mock_store,
        ):
            result = await _set_default_repo("/a")

        assert result is True
        mock_store.update_is_default.assert_awaited_once_with("/a", is_default=False)

    @pytest.mark.asyncio
    async def test_set_default_not_found(self) -> None:
        """Returns False when path is not registered."""
        mock_store = AsyncMock()
        mock_store.initialize = AsyncMock()
        mock_store.close = AsyncMock()
        mock_store.list = AsyncMock(return_value=[])

        with (
            patch(
                "ouroboros.cli.commands.setup.BrownfieldStore",
                return_value=mock_store,
            ),
            patch(
                "ouroboros.cli.commands.setup.set_default_repo",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            result = await _set_default_repo("/nonexistent")

        assert result is False


# ── Scan-Register pipeline tests ──────────────────────────────────


class TestScanRegisterPipeline:
    """Tests verifying the scan → register pipeline in setup context.

    These tests verify that _scan_and_register_repos correctly orchestrates
    the BrownfieldStore lifecycle (initialize → clear_all → scan → close).
    """

    @pytest.mark.asyncio
    async def test_store_lifecycle_order(self) -> None:
        """Store operations happen in correct order: init → scan → close (no separate clear)."""
        call_order: list[str] = []

        mock_store = AsyncMock()
        mock_store.initialize = AsyncMock(side_effect=lambda: call_order.append("initialize"))
        mock_store.close = AsyncMock(side_effect=lambda: call_order.append("close"))

        async def fake_scan(store, *, root=None):
            _ = store, root
            call_order.append("scan_and_register")
            return []

        with (
            patch(
                "ouroboros.cli.commands.setup.BrownfieldStore",
                return_value=mock_store,
            ),
            patch(
                "ouroboros.cli.commands.setup.scan_and_register",
                side_effect=fake_scan,
            ),
        ):
            await _scan_and_register_repos()

        assert call_order == ["initialize", "scan_and_register", "close"]

    @pytest.mark.asyncio
    async def test_scan_passes_store_to_scan_and_register(self) -> None:
        """The store instance is passed to scan_and_register."""
        mock_store = AsyncMock()
        mock_store.initialize = AsyncMock()
        mock_store.close = AsyncMock()
        mock_store.clear_all = AsyncMock(return_value=0)

        captured_store = None

        async def capture_store(store, *, root=None):
            _ = root
            nonlocal captured_store
            captured_store = store
            return []

        with (
            patch(
                "ouroboros.cli.commands.setup.BrownfieldStore",
                return_value=mock_store,
            ),
            patch(
                "ouroboros.cli.commands.setup.scan_and_register",
                side_effect=capture_store,
            ),
        ):
            await _scan_and_register_repos()

        assert captured_store is mock_store

    @pytest.mark.asyncio
    async def test_scan_passes_scan_root_to_scan_and_register(self, tmp_path: Path) -> None:
        """The requested scan root is passed to scan_and_register."""
        mock_store = AsyncMock()
        mock_store.initialize = AsyncMock()
        mock_store.close = AsyncMock()

        captured_root = None

        async def capture_root(store, *, root=None):
            _ = store
            nonlocal captured_root
            captured_root = root
            return []

        with (
            patch(
                "ouroboros.cli.commands.setup.BrownfieldStore",
                return_value=mock_store,
            ),
            patch(
                "ouroboros.cli.commands.setup.scan_and_register",
                side_effect=capture_root,
            ),
        ):
            await _scan_and_register_repos(tmp_path)

        assert captured_root == tmp_path

    @pytest.mark.asyncio
    async def test_converts_brownfield_repo_to_dict(self) -> None:
        """BrownfieldRepo objects are converted to plain dicts with all fields."""
        from ouroboros.persistence.brownfield import BrownfieldRepo

        mock_store = AsyncMock()
        mock_store.initialize = AsyncMock()
        mock_store.close = AsyncMock()
        mock_store.clear_all = AsyncMock(return_value=0)

        mock_repos = [
            BrownfieldRepo(path="/home/user/proj", name="proj", desc="My project", is_default=True),
            BrownfieldRepo(path="/home/user/lib", name="lib", desc=None, is_default=False),
        ]

        with (
            patch(
                "ouroboros.cli.commands.setup.BrownfieldStore",
                return_value=mock_store,
            ),
            patch(
                "ouroboros.cli.commands.setup.scan_and_register",
                new_callable=AsyncMock,
                return_value=mock_repos,
            ),
        ):
            result = await _scan_and_register_repos()

        assert len(result) == 2
        # Verify dict structure
        assert result[0] == {
            "path": "/home/user/proj",
            "name": "proj",
            "desc": "My project",
            "is_default": True,
        }
        # None desc should be converted to ""
        assert result[1] == {
            "path": "/home/user/lib",
            "name": "lib",
            "desc": "",
            "is_default": False,
        }

    @pytest.mark.asyncio
    async def test_many_repos_all_returned(self) -> None:
        """Large number of scanned repos are all correctly returned."""
        from ouroboros.persistence.brownfield import BrownfieldRepo

        count = 50
        mock_repos = [
            BrownfieldRepo(
                path=f"/home/user/repo-{i}", name=f"repo-{i}", desc="", is_default=(i == 0)
            )
            for i in range(count)
        ]

        mock_store = AsyncMock()
        mock_store.initialize = AsyncMock()
        mock_store.close = AsyncMock()
        mock_store.clear_all = AsyncMock(return_value=0)

        with (
            patch(
                "ouroboros.cli.commands.setup.BrownfieldStore",
                return_value=mock_store,
            ),
            patch(
                "ouroboros.cli.commands.setup.scan_and_register",
                new_callable=AsyncMock,
                return_value=mock_repos,
            ),
        ):
            result = await _scan_and_register_repos()

        assert len(result) == count
        assert result[0]["is_default"] is True
        assert all(r["is_default"] is False for r in result[1:])


class TestScanCommand:
    """Tests for the brownfield scan CLI command."""

    def test_scan_command_accepts_scan_root_argument(self, tmp_path: Path) -> None:
        runner = CliRunner()

        with patch(
            "ouroboros.cli.commands.setup._run_scan_only",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_run:
            result = runner.invoke(setup_cmd.app, ["scan", str(tmp_path)])

        assert result.exit_code == 0
        mock_run.assert_awaited_once_with(tmp_path.resolve())

    def test_scan_command_defaults_scan_root_to_current_user_home(
        self,
        tmp_path: Path,
    ) -> None:
        runner = CliRunner()

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "ouroboros.cli.commands.setup._run_scan_only",
                new_callable=AsyncMock,
                return_value=[],
            ) as mock_run,
        ):
            result = runner.invoke(setup_cmd.app, ["scan"])

        assert result.exit_code == 0
        mock_run.assert_awaited_once_with(tmp_path)


# ── List repos extended tests ─────────────────────────────────────


class TestListReposExtended:
    """Extended tests for _list_repos async function."""

    @pytest.mark.asyncio
    async def test_list_converts_none_desc_to_empty(self) -> None:
        """None desc values are converted to empty strings."""
        from ouroboros.persistence.brownfield import BrownfieldRepo

        mock_store = AsyncMock()
        mock_store.initialize = AsyncMock()
        mock_store.close = AsyncMock()
        mock_store.list = AsyncMock(
            return_value=[
                BrownfieldRepo(path="/a", name="a", desc=None, is_default=False),
            ]
        )

        with patch(
            "ouroboros.cli.commands.setup.BrownfieldStore",
            return_value=mock_store,
        ):
            result = await _list_repos()

        assert result[0]["desc"] == ""

    @pytest.mark.asyncio
    async def test_list_empty_db(self) -> None:
        """Returns empty list when no repos in DB."""
        mock_store = AsyncMock()
        mock_store.initialize = AsyncMock()
        mock_store.close = AsyncMock()
        mock_store.list = AsyncMock(return_value=[])

        with patch(
            "ouroboros.cli.commands.setup.BrownfieldStore",
            return_value=mock_store,
        ):
            result = await _list_repos()

        assert result == []

    @pytest.mark.asyncio
    async def test_list_store_closed_after_query(self) -> None:
        """Store is always closed after listing."""
        mock_store = AsyncMock()
        mock_store.initialize = AsyncMock()
        mock_store.close = AsyncMock()
        mock_store.list = AsyncMock(return_value=[])

        with patch(
            "ouroboros.cli.commands.setup.BrownfieldStore",
            return_value=mock_store,
        ):
            await _list_repos()

        mock_store.close.assert_awaited_once()


# ── Set default repo extended tests ───────────────────────────────


class TestSetDefaultRepoExtended:
    """Extended tests for _set_default_repo in setup context."""

    @pytest.mark.asyncio
    async def test_set_default_store_closed_on_success(self) -> None:
        """Store is closed after successful set_default."""
        from ouroboros.persistence.brownfield import BrownfieldRepo

        mock_store = AsyncMock()
        mock_store.initialize = AsyncMock()
        mock_store.close = AsyncMock()

        with (
            patch(
                "ouroboros.cli.commands.setup.BrownfieldStore",
                return_value=mock_store,
            ),
            patch(
                "ouroboros.cli.commands.setup.set_default_repo",
                new_callable=AsyncMock,
                return_value=BrownfieldRepo(path="/a", name="a", is_default=True),
            ),
        ):
            await _set_default_repo("/a")

        mock_store.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_set_default_store_closed_on_error(self) -> None:
        """Store is closed even when list_repos raises."""
        mock_store = AsyncMock()
        mock_store.initialize = AsyncMock()
        mock_store.close = AsyncMock()
        mock_store.list = AsyncMock(side_effect=RuntimeError("DB error"))

        with patch(
            "ouroboros.cli.commands.setup.BrownfieldStore",
            return_value=mock_store,
        ):
            with pytest.raises(RuntimeError, match="DB error"):
                await _set_default_repo("/a")

        mock_store.close.assert_awaited_once()


# ── OpenCode MCP setup tests ─────────────────────────────────────


class TestOpenCodeMCPSetup:
    """Tests for OpenCode JSONC config handling in _ensure_opencode_mcp_entry.

    Patches ``opencode_config_dir`` directly for platform-agnostic tests.
    """

    _OCD = "ouroboros.cli.opencode_config.opencode_config_dir"

    @pytest.mark.parametrize(
        ("raw_config", "preserved"),
        [
            pytest.param(
                '{\n  // line comment\n  /* block comment */\n  "theme": "dark",\n  "mcp": {}\n}\n',
                {"theme": "dark"},
                id="jsonc-line-and-block-comments",
            ),
            pytest.param(
                '{\n  "editor": "vim",\n  "mcp": {},\n}\n',
                {"editor": "vim"},
                id="jsonc-trailing-commas",
            ),
            pytest.param(
                json.dumps(
                    {"$schema": "https://example.com/schema.json", "plugin": ["foo"], "mcp": {}}
                ),
                {"$schema": "https://example.com/schema.json", "plugin": ["foo"]},
                id="schema-and-plugin-keys",
            ),
            pytest.param(
                '{\n  "$schema": "https://opencode.ai/config.json",\n  "mcp": {}\n}\n',
                {"$schema": "https://opencode.ai/config.json"},
                id="quoted-slashes-inside-values",
            ),
        ],
    )
    def test_foreign_keys_survive_setup(
        self, tmp_path: Path, raw_config: str, preserved: dict
    ) -> None:
        """Non-MCP keys survive setup and the ouroboros entry is added."""
        config_dir = tmp_path / "opencode"
        config_dir.mkdir()
        config_path = config_dir / "opencode.json"
        config_path.write_text(raw_config, encoding="utf-8")

        with (
            patch(self._OCD, return_value=config_dir),
            patch(
                "ouroboros.cli.commands.setup._detect_opencode_mcp_command",
                return_value={"command": ["ouroboros", "mcp", "serve"]},
            ),
        ):
            _ensure_opencode_mcp_entry()

        data = json.loads(config_path.read_text(encoding="utf-8"))
        for key, value in preserved.items():
            assert key in data
            assert data[key] == value
        assert "ouroboros" in data["mcp"]

    @pytest.mark.parametrize(
        "raw_config",
        [
            pytest.param(json.dumps({"mcp": ["invalid"]}), id="mcp-as-list"),
            pytest.param(json.dumps({"mcp": {"ouroboros": "disabled"}}), id="entry-as-string"),
            pytest.param(
                json.dumps(
                    {
                        "mcp": {
                            "ouroboros": {
                                "type": "local",
                                "command": ["ouroboros", "mcp", "serve"],
                                "environment": "BROKEN_STRING_VALUE",
                            },
                        }
                    }
                ),
                id="environment-as-string",
            ),
        ],
    )
    def test_malformed_shapes_are_replaced(self, tmp_path: Path, raw_config: str) -> None:
        """Non-dict mcp/entry/environment shapes are repaired into a valid entry."""
        config_dir = tmp_path / "opencode"
        config_dir.mkdir()
        config_path = config_dir / "opencode.json"
        config_path.write_text(raw_config, encoding="utf-8")

        with (
            patch(self._OCD, return_value=config_dir),
            patch(
                "ouroboros.cli.commands.setup._detect_opencode_mcp_command",
                return_value={"command": ["ouroboros", "mcp", "serve"]},
            ),
        ):
            _ensure_opencode_mcp_entry()

        data = json.loads(config_path.read_text(encoding="utf-8"))
        assert isinstance(data["mcp"], dict)
        assert "ouroboros" in data["mcp"]
        entry = data["mcp"]["ouroboros"]
        assert isinstance(entry, dict)
        assert entry["type"] == "local"
        assert isinstance(entry["environment"], dict)

    def test_malformed_json_aborts_without_overwriting(self, tmp_path: Path) -> None:
        """If the config file is unparseable, setup must abort — not overwrite it."""
        config_dir = tmp_path / "opencode"
        config_dir.mkdir()
        config_path = config_dir / "opencode.json"
        original_content = '{"theme": "dark", BROKEN JSON HERE}'
        config_path.write_text(original_content, encoding="utf-8")

        with (
            patch(self._OCD, return_value=config_dir),
            patch(
                "ouroboros.cli.commands.setup._detect_opencode_mcp_command",
                return_value={"command": ["ouroboros", "mcp", "serve"]},
            ),
        ):
            _ensure_opencode_mcp_entry()

        # File must be unchanged — setup should not have touched it
        assert config_path.read_text(encoding="utf-8") == original_content

    def test_custom_command_not_overwritten(self, tmp_path: Path) -> None:
        """User-managed commands (docker, nix, etc.) must survive setup."""
        config_dir = tmp_path / "opencode"
        config_dir.mkdir()
        config_path = config_dir / "opencode.json"
        custom_cmd = ["docker", "run", "--rm", "ouroboros", "mcp", "serve"]
        config_path.write_text(
            json.dumps(
                {
                    "mcp": {
                        "ouroboros": {
                            "type": "local",
                            "command": custom_cmd,
                            "environment": {},
                        },
                    }
                }
            ),
            encoding="utf-8",
        )

        with (
            patch(self._OCD, return_value=config_dir),
            patch(
                "ouroboros.cli.commands.setup._detect_opencode_mcp_command",
                return_value={"command": ["ouroboros", "mcp", "serve"]},
            ),
        ):
            _ensure_opencode_mcp_entry()

        data = json.loads(config_path.read_text(encoding="utf-8"))
        assert data["mcp"]["ouroboros"]["command"] == custom_cmd, (
            "Custom command must not be overwritten by setup"
        )

    def test_stale_type_remote_rewritten_to_local(self, tmp_path: Path) -> None:
        """A stale type='remote' must be normalised to 'local' by setup."""
        config_dir = tmp_path / "opencode"
        config_dir.mkdir()
        config_path = config_dir / "opencode.json"
        config_path.write_text(
            json.dumps(
                {
                    "mcp": {
                        "ouroboros": {
                            "type": "remote",
                            "command": ["ouroboros", "mcp", "serve"],
                            "environment": {},
                        },
                    }
                }
            ),
            encoding="utf-8",
        )

        with (
            patch(self._OCD, return_value=config_dir),
            patch(
                "ouroboros.cli.commands.setup._detect_opencode_mcp_command",
                return_value={"command": ["ouroboros", "mcp", "serve"]},
            ),
        ):
            _ensure_opencode_mcp_entry()

        data = json.loads(config_path.read_text(encoding="utf-8"))
        assert data["mcp"]["ouroboros"]["type"] == "local"

    @pytest.mark.parametrize(
        "stale_command",
        [
            pytest.param("ouroboros mcp serve", id="bare-string"),
            pytest.param([], id="empty-list"),
            pytest.param([123, "mcp", "serve"], id="non-string-first-element"),
            pytest.param([None, "mcp", "serve"], id="none-first-element"),
        ],
    )
    def test_unusable_command_replaced_with_detected_array(
        self, tmp_path: Path, stale_command: object
    ) -> None:
        """A command that is not a usable argv array is replaced by the detected launcher."""
        config_dir = tmp_path / "opencode"
        config_dir.mkdir()
        config_path = config_dir / "opencode.json"
        config_path.write_text(
            json.dumps(
                {
                    "mcp": {
                        "ouroboros": {
                            "type": "local",
                            "command": stale_command,
                            "environment": {},
                        },
                    }
                }
            ),
            encoding="utf-8",
        )

        with (
            patch(self._OCD, return_value=config_dir),
            patch(
                "ouroboros.cli.commands.setup._detect_opencode_mcp_command",
                return_value={"command": ["ouroboros", "mcp", "serve"]},
            ),
        ):
            _ensure_opencode_mcp_entry()

        data = json.loads(config_path.read_text(encoding="utf-8"))
        assert isinstance(data["mcp"]["ouroboros"]["command"], list)
        assert data["mcp"]["ouroboros"]["command"] == ["ouroboros", "mcp", "serve"]


class TestOpenCodeSetupConfigYaml:
    """Tests for _setup_opencode config.yaml shape handling."""

    def test_scalar_top_level_repaired(self, tmp_path: Path) -> None:
        """If config.yaml is a scalar, _setup_opencode repairs it."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text("just_a_string\n", encoding="utf-8")

        with (
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._ensure_opencode_mcp_entry"),
            patch("ouroboros.cli.commands.setup._ensure_claude_mcp_entry"),
            patch("ouroboros.cli.commands.setup._cleanup_plugin_artifacts"),
            patch("ouroboros.cli.commands.setup._install_runtime_instruction_artifact"),
        ):
            from ouroboros.cli.commands.setup import _setup_opencode

            _setup_opencode("/usr/bin/opencode", mode="subprocess")

        result = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert isinstance(result, dict)
        assert result["orchestrator"]["runtime_backend"] == "opencode"
        assert result["llm"]["backend"] == "opencode"

    def test_subprocess_setup_installs_instruction_artifact(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text("{}", encoding="utf-8")
        opencode_dir = tmp_path / "opencode-config"

        with (
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._cleanup_plugin_artifacts"),
            patch("ouroboros.cli.commands.setup.opencode_config_dir", return_value=opencode_dir),
        ):
            from ouroboros.cli.commands.setup import _setup_opencode

            _setup_opencode("/usr/bin/opencode", mode="subprocess")

        guide_path = opencode_dir / "AGENTS.md"
        assert guide_path.is_file()
        assert "## Ouroboros Skill Capability Guide: Opencode" in guide_path.read_text(
            encoding="utf-8"
        )

    def test_plugin_setup_installs_instruction_artifact_after_success(
        self,
        tmp_path: Path,
    ) -> None:
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text("{}", encoding="utf-8")
        opencode_dir = tmp_path / "opencode-config"

        with (
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.commands.setup._install_opencode_bridge_plugin", return_value=True
            ),
            patch("ouroboros.cli.commands.setup._ensure_opencode_mcp_entry", return_value=True),
            patch("ouroboros.cli.commands.setup._ensure_opencode_plugin_entry", return_value=True),
            patch("ouroboros.cli.commands.setup.opencode_config_dir", return_value=opencode_dir),
        ):
            from ouroboros.cli.commands.setup import _setup_opencode

            assert _setup_opencode("/usr/bin/opencode", mode="plugin") is True

        guide_path = opencode_dir / "AGENTS.md"
        assert guide_path.is_file()
        assert "## Ouroboros Skill Capability Guide: Opencode" in guide_path.read_text(
            encoding="utf-8"
        )

    def test_orchestrator_as_list_repaired(self, tmp_path: Path) -> None:
        """If orchestrator is a list, _setup_opencode replaces with dict."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text(
            yaml.dump({"orchestrator": ["bad"], "llm": "codex"}),
            encoding="utf-8",
        )

        with (
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._ensure_opencode_mcp_entry"),
            patch("ouroboros.cli.commands.setup._ensure_claude_mcp_entry"),
            patch("ouroboros.cli.commands.setup._cleanup_plugin_artifacts"),
            patch("ouroboros.cli.commands.setup._install_runtime_instruction_artifact"),
        ):
            from ouroboros.cli.commands.setup import _setup_opencode

            _setup_opencode("/usr/bin/opencode", mode="subprocess")

        result = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert isinstance(result["orchestrator"], dict)

    def test_setup_opencode_does_not_register_claude_integration(self, tmp_path: Path) -> None:
        """OpenCode setup should stay scoped to OpenCode even when Claude is installed."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text("{}", encoding="utf-8")

        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()

        with (
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._ensure_opencode_mcp_entry"),
            patch("ouroboros.cli.commands.setup._ensure_opencode_plugin_entry"),
            patch("ouroboros.cli.commands.setup._install_opencode_bridge_plugin"),
            patch("ouroboros.cli.commands.setup._ensure_claude_mcp_entry") as mock_claude,
            patch("ouroboros.cli.commands.setup._install_runtime_instruction_artifact"),
        ):
            from ouroboros.cli.commands.setup import _setup_opencode

            _setup_opencode("/usr/bin/opencode")

        result = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        mock_claude.assert_not_called()
        assert result["orchestrator"]["opencode_mode"] == "plugin"

    def test_plugin_setup_exposes_selected_cli_path_during_discovery(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Plugin setup queries paths through the user-selected OpenCode binary."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text("{}", encoding="utf-8")
        cli_path = "/custom/bin/opencode"
        observed: list[str | None] = []
        monkeypatch.delenv("OUROBOROS_OPENCODE_CLI_PATH", raising=False)

        def record_cli_path(*_args: object, **_kwargs: object) -> bool:
            observed.append(os.environ.get("OUROBOROS_OPENCODE_CLI_PATH"))
            return True

        with (
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.commands.setup._install_opencode_bridge_plugin",
                side_effect=record_cli_path,
            ),
            patch(
                "ouroboros.cli.commands.setup._ensure_opencode_mcp_entry",
                side_effect=record_cli_path,
            ),
            patch(
                "ouroboros.cli.commands.setup._ensure_opencode_plugin_entry",
                side_effect=record_cli_path,
            ),
            patch(
                "ouroboros.cli.commands.setup._install_runtime_instruction_artifact",
                side_effect=record_cli_path,
            ),
        ):
            from ouroboros.cli.commands.setup import _setup_opencode

            assert _setup_opencode(cli_path, mode="plugin") is True

        assert observed == [cli_path, cli_path, cli_path, cli_path]
        assert os.environ.get("OUROBOROS_OPENCODE_CLI_PATH") is None

    def test_plugin_setup_failure_returns_false_without_persisting_config(
        self,
        tmp_path: Path,
    ) -> None:
        """Plugin setup failure must not be reported as a completed helper run."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text("{}", encoding="utf-8")
        opencode_dir = tmp_path / "opencode-config"

        with (
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.commands.setup._install_opencode_bridge_plugin", return_value=False
            ),
            patch("ouroboros.cli.commands.setup._ensure_opencode_mcp_entry", return_value=True),
            patch("ouroboros.cli.commands.setup._ensure_opencode_plugin_entry", return_value=True),
            patch("ouroboros.cli.commands.setup.opencode_config_dir", return_value=opencode_dir),
        ):
            from ouroboros.cli.commands.setup import _setup_opencode

            assert _setup_opencode("/usr/bin/opencode", mode="plugin") is False

        assert yaml.safe_load(config_path.read_text(encoding="utf-8")) == {}
        assert not (opencode_dir / "AGENTS.md").exists()

    def test_plugin_setup_failure_exits_before_success_banner(self, tmp_path: Path) -> None:
        """Top-level setup must propagate plugin setup failure to exit status."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text("{}", encoding="utf-8")

        runner = CliRunner()
        with (
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.commands.setup._detect_runtimes",
                return_value={
                    "claude": None,
                    "codex": None,
                    "opencode": "/usr/bin/opencode",
                    "hermes": None,
                },
            ),
            patch(
                "ouroboros.cli.commands.setup._install_opencode_bridge_plugin", return_value=False
            ),
            patch("ouroboros.cli.commands.setup._ensure_opencode_mcp_entry", return_value=True),
            patch("ouroboros.cli.commands.setup._ensure_opencode_plugin_entry", return_value=True),
        ):
            result = runner.invoke(
                setup_cmd.app,
                ["--runtime", "opencode", "--non-interactive"],
            )

        assert result.exit_code == 1
        assert "Plugin-mode setup incomplete" in result.output
        assert "Setup complete!" not in result.output


class TestOpenCodeModePersisted:
    """_setup_opencode persists orchestrator.opencode_mode in both branches."""

    def _run(self, tmp_path: Path, mode: str) -> dict:
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        with (
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._ensure_opencode_mcp_entry"),
            patch("ouroboros.cli.commands.setup._ensure_opencode_plugin_entry"),
            patch("ouroboros.cli.commands.setup._install_opencode_bridge_plugin"),
            patch("ouroboros.cli.commands.setup._ensure_claude_mcp_entry"),
            patch("ouroboros.cli.commands.setup._cleanup_plugin_artifacts"),
            patch("ouroboros.cli.commands.setup._install_runtime_instruction_artifact"),
        ):
            from ouroboros.cli.commands.setup import _setup_opencode

            _setup_opencode("/usr/bin/opencode", mode=mode)
        return yaml.safe_load(config_path.read_text(encoding="utf-8"))

    @pytest.mark.parametrize("mode", ["plugin", "subprocess"])
    def test_mode_persisted(self, tmp_path: Path, mode: str) -> None:
        result = self._run(tmp_path, mode)
        assert result["orchestrator"]["opencode_mode"] == mode
        # Both branches set runtime_backend=opencode so the MCP server's
        # should_dispatch_via_plugin() gate recognises the OpenCode context.
        assert result["orchestrator"]["runtime_backend"] == "opencode"


# ── JSONC config file detection tests ────────────────────────────


class TestFindOpencodeConfig:
    """Tests for _find_opencode_config — .jsonc/.json detection logic.

    Patches ``opencode_config_dir`` directly so tests are platform-agnostic
    (no reliance on Linux-specific ``~/.config/opencode`` paths).
    """

    _OCD = "ouroboros.cli.opencode_config.opencode_config_dir"

    @pytest.mark.parametrize(
        ("existing", "expected_name", "expected_exists"),
        [
            pytest.param(
                ("opencode.jsonc", "opencode.json"), "opencode.jsonc", True, id="jsonc-wins"
            ),
            pytest.param(("opencode.json",), "opencode.json", True, id="json-only"),
            pytest.param(("opencode.jsonc",), "opencode.jsonc", True, id="jsonc-only"),
            pytest.param((), "opencode.json", False, id="neither-defaults-to-json"),
        ],
    )
    def test_config_detection(
        self,
        tmp_path: Path,
        existing: tuple[str, ...],
        expected_name: str,
        expected_exists: bool,
    ) -> None:
        """.jsonc wins when present; opencode.json is the default for creation."""
        config_dir = tmp_path / "opencode"
        config_dir.mkdir()
        for name in existing:
            (config_dir / name).write_text("{}", encoding="utf-8")

        with patch(self._OCD, return_value=config_dir):
            result = _find_opencode_config()

        assert result.name == expected_name
        assert result.exists() is expected_exists


class TestSetupJsoncDetection:
    """Tests for _ensure_opencode_mcp_entry picking up .jsonc files.

    Patches ``opencode_config_dir`` directly for platform-agnostic tests.
    """

    _OCD = "ouroboros.cli.opencode_config.opencode_config_dir"

    def test_setup_reads_existing_jsonc(self, tmp_path: Path) -> None:
        """Setup should read and update an existing opencode.jsonc file."""
        config_dir = tmp_path / "opencode"
        config_dir.mkdir()
        jsonc_path = config_dir / "opencode.jsonc"
        jsonc_path.write_text(
            '{\n  // user comment\n  "theme": "dark",\n  "mcp": {}\n}\n',
            encoding="utf-8",
        )

        with (
            patch(self._OCD, return_value=config_dir),
            patch(
                "ouroboros.cli.commands.setup._detect_opencode_mcp_command",
                return_value={"command": ["ouroboros", "mcp", "serve"]},
            ),
        ):
            _ensure_opencode_mcp_entry()

        # Must write back to .jsonc, not create a separate .json
        assert jsonc_path.exists()
        data = json.loads(jsonc_path.read_text(encoding="utf-8"))
        assert "ouroboros" in data["mcp"]
        assert data["theme"] == "dark"
        assert not (config_dir / "opencode.json").exists()


class TestGooseSetup:
    """Tests for Goose-specific setup behavior."""

    def test_detect_runtimes_honors_config_goose_path(self) -> None:
        """Explicit Goose path takes precedence over PATH-only discovery."""
        with (
            patch(
                "ouroboros.cli.commands.setup.shutil.which",
                side_effect=lambda name: "/custom/goose" if name == "/custom/goose" else None,
            ),
            patch("ouroboros.config.get_gemini_cli_path", return_value=None),
            patch("ouroboros.config.get_goose_cli_path", return_value="/custom/goose"),
            patch("ouroboros.config.get_kiro_cli_path", return_value=None),
            patch("ouroboros.config.get_copilot_cli_path", return_value=None),
        ):
            detected = setup_cmd._detect_runtimes()

        assert detected["goose"] == "/custom/goose"

    def test_setup_runtime_goose_uses_detected_explicit_path(self) -> None:
        chosen: dict[str, str] = {}

        def _goose(path: str) -> None:
            chosen["path"] = path

        runner = CliRunner()
        with (
            patch.object(setup_cmd, "_detect_runtimes", return_value={"goose": "/custom/goose"}),
            patch.object(setup_cmd, "_setup_goose", side_effect=_goose),
        ):
            result = runner.invoke(setup_cmd.app, ["--runtime", "goose", "--non-interactive"])

        assert result.exit_code == 0, result.output
        assert chosen["path"] == "/custom/goose"


class TestGeminiSetup:
    """Tests for Gemini-specific setup behavior."""

    def test_setup_gemini_installs_instruction_artifact(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text("{}", encoding="utf-8")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
        ):
            setup_cmd._setup_gemini("/opt/bin/gemini")

        guide_path = tmp_path / ".gemini" / "GEMINI.md"
        assert guide_path.is_file()
        assert "## Ouroboros Skill Capability Guide: Gemini" in guide_path.read_text(
            encoding="utf-8"
        )


_RUNTIME_ONLY_BACKENDS = [
    pytest.param("antigravity", "agy", "/usr/bin/agy", "/opt/bin/agy", "/opt/bin/agy", id="agy"),
    pytest.param("grok", "grok", "/usr/bin/grok", "/opt/bin/grok", "/opt/bin/grok", id="grok"),
    pytest.param(
        "zcode",
        "zcode",
        "/usr/local/bin/zcode",
        "/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs",
        None,
        id="zcode",
    ),
]


class TestRuntimeOnlyBackendSetup:
    """The runtime-only backends (Antigravity/agy, Grok Build, Zcode).

    Each records `orchestrator.runtime_backend` plus its `*_cli_path` without
    ever touching the completion-only `llm.backend`, and each must dispatch
    from `--runtime <name>` rather than failing with 'Unsupported runtime'.
    """

    @pytest.mark.parametrize(
        ("runtime", "binary", "path_on_path", "setup_path", "expected_cli_path"),
        _RUNTIME_ONLY_BACKENDS,
    )
    def test_setup_writes_runtime_only_config(
        self,
        tmp_path: Path,
        runtime: str,
        binary: str,
        path_on_path: str,
        setup_path: str,
        expected_cli_path: str | None,
    ) -> None:
        """Setup records the runtime + CLI path but leaves llm.backend alone,
        and the written config round-trips through schema validation."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text("{}", encoding="utf-8")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
        ):
            getattr(setup_cmd, f"_setup_{runtime}")(setup_path)

        data = yaml.safe_load((config_dir / "config.yaml").read_text(encoding="utf-8"))
        assert data["orchestrator"]["runtime_backend"] == runtime
        written_path = data["orchestrator"][f"{runtime}_cli_path"]
        assert written_path.endswith(Path(setup_path).name)
        if expected_cli_path is not None:
            assert written_path == expected_cli_path
        # Runtime-only: the completion-only llm.backend is never set to it.
        assert data.get("llm", {}).get("backend") != runtime
        # The persisted config must round-trip through schema validation.
        from ouroboros.config.models import OuroborosConfig

        OuroborosConfig.model_validate(data)

    @pytest.mark.parametrize(
        ("runtime", "binary", "path_on_path", "setup_path", "expected_cli_path"),
        _RUNTIME_ONLY_BACKENDS,
    )
    def test_detect_runtimes_includes_backend(
        self,
        runtime: str,
        binary: str,
        path_on_path: str,
        setup_path: str,
        expected_cli_path: str | None,
    ) -> None:
        with (
            patch(
                "ouroboros.cli.commands.setup.shutil.which",
                side_effect=lambda name: path_on_path if name == binary else None,
            ),
            patch(f"ouroboros.config.get_{runtime}_cli_path", return_value=None),
        ):
            detected = setup_cmd._detect_runtimes()

        assert detected[runtime] == path_on_path

    @pytest.mark.parametrize(
        ("runtime", "binary", "path_on_path", "setup_path", "expected_cli_path"),
        _RUNTIME_ONLY_BACKENDS,
    )
    def test_setup_runtime_dispatches_not_unsupported(
        self,
        tmp_path: Path,
        runtime: str,
        binary: str,
        path_on_path: str,
        setup_path: str,
        expected_cli_path: str | None,
    ) -> None:
        """`setup --runtime <name> --non-interactive` configures the backend
        rather than failing with 'Unsupported runtime' (the prior contract gap)."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text("{}", encoding="utf-8")
        detected_path = expected_cli_path or path_on_path
        runner = CliRunner()
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.commands.setup._detect_runtimes",
                return_value={runtime: detected_path},
            ),
        ):
            result = runner.invoke(setup_cmd.app, ["--runtime", runtime, "--non-interactive"])

        assert "Unsupported runtime" not in result.output
        assert result.exit_code == 0, result.output
        data = yaml.safe_load((config_dir / "config.yaml").read_text(encoding="utf-8"))
        assert data["orchestrator"]["runtime_backend"] == runtime


class TestHostRuntimeSetup:
    """``host`` has no CLI to detect at all: unlike the runtime-only backends
    above, ``--runtime host`` must configure
    ``orchestrator.runtime_backend: host`` with no ``*_cli_path`` and without
    a `_detect_runtimes()` entry — this is what dsh's ``cordis.patch.yml``
    now defaults to (see ``integrations/dsh-plugin``)."""

    def test_setup_host_writes_runtime_only_config(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text("{}", encoding="utf-8")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
        ):
            setup_cmd._setup_host()

        data = yaml.safe_load((config_dir / "config.yaml").read_text(encoding="utf-8"))
        assert data["orchestrator"]["runtime_backend"] == "host"
        assert "host_cli_path" not in data["orchestrator"]
        # Runtime-only: the completion-only llm.backend is never set to it.
        assert data.get("llm", {}).get("backend") != "host"
        # The persisted config must round-trip through schema validation.
        from ouroboros.config.models import OuroborosConfig

        OuroborosConfig.model_validate(data)

    def test_setup_runtime_host_dispatches_not_unsupported(self, tmp_path: Path) -> None:
        """`setup --runtime host --non-interactive` configures the backend
        rather than failing with 'Unsupported runtime' (the prior contract gap
        docs/cli-reference.md advertised but the dispatcher never implemented)."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text("{}", encoding="utf-8")
        runner = CliRunner()
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._detect_runtimes", return_value={}),
        ):
            result = runner.invoke(setup_cmd.app, ["--runtime", "host", "--non-interactive"])

        assert "Unsupported runtime" not in result.output
        assert result.exit_code == 0, result.output
        data = yaml.safe_load((config_dir / "config.yaml").read_text(encoding="utf-8"))
        assert data["orchestrator"]["runtime_backend"] == "host"

        assert "In your MCP host chat, type: ooo run" in result.output
        assert "ouroboros run workflow seed.yaml" not in result.output

    def test_setup_host_migrates_setup_managed_codex_launcher(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text(
            "orchestrator:\n  runtime_backend: codex\n", encoding="utf-8"
        )
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        codex_config = codex_dir / "config.toml"
        codex_config.write_text(
            setup_cmd._CODEX_MCP_SECTION_TEMPLATE.format(
                command_lines=(
                    'command = "ouroboros"\n'
                    'args = ["mcp", "serve", "--runtime", "codex", '
                    '"--llm-backend", "codex"]'
                )
            ),
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
        ):
            assert setup_cmd._setup_host() is True

        entry = tomllib.loads(codex_config.read_text(encoding="utf-8"))["mcp_servers"]["ouroboros"]
        assert entry["args"] == [
            "mcp",
            "serve",
            "--runtime",
            "host",
            "--llm-backend",
            "codex",
        ]
        assert entry["env"] == {
            "OUROBOROS_AGENT_RUNTIME": "host",
            "OUROBOROS_LLM_BACKEND": "codex",
        }
        data = yaml.safe_load((config_dir / "config.yaml").read_text(encoding="utf-8"))
        assert data["orchestrator"]["runtime_backend"] == "host"

    @pytest.mark.parametrize(
        "opencode_command",
        [
            ["uvx", "ouroboros-ai[mcp]", "mcp", "serve"],
            ["uvx", *setup_cmd._CODEX_UVX_MCP_ARGS],
            [
                "pipx",
                "run",
                "--spec",
                "ouroboros-ai[mcp]",
                "ouroboros",
                "mcp",
                "serve",
            ],
        ],
        ids=["legacy-uvx", "isolated-uvx", "isolated-pipx"],
    )
    def test_setup_host_migrates_all_setup_managed_launchers(
        self, tmp_path: Path, opencode_command: list[str]
    ) -> None:
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text(
            "orchestrator:\n  runtime_backend: kiro\n", encoding="utf-8"
        )
        kiro_path = tmp_path / ".kiro" / "settings" / "mcp.json"
        kiro_path.parent.mkdir(parents=True)
        kiro_path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "ouroboros": {
                            "command": "uvx",
                            "args": ["ouroboros-ai[mcp]", "mcp", "serve"],
                            "env": {
                                "OUROBOROS_RUNTIME": "kiro",
                                "OUROBOROS_LLM_BACKEND": "kiro",
                                "KEEP": "kiro",
                            },
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        copilot_path = tmp_path / ".copilot" / "mcp-config.json"
        copilot_path.parent.mkdir(parents=True)
        copilot_path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "ouroboros": {
                            "command": "pipx",
                            "args": ["run", "ouroboros-ai[mcp]", "mcp", "serve"],
                            "env": {
                                "OUROBOROS_AGENT_RUNTIME": "copilot",
                                "OUROBOROS_LLM_BACKEND": "copilot",
                            },
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        opencode_path = tmp_path / ".config" / "opencode" / "opencode.json"
        opencode_path.parent.mkdir(parents=True)
        opencode_path.write_text(
            json.dumps(
                {
                    "mcp": {
                        "ouroboros": {
                            "type": "local",
                            "command": opencode_command,
                            "environment": {
                                "OUROBOROS_AGENT_RUNTIME": "opencode",
                                "OUROBOROS_LLM_BACKEND": "opencode",
                            },
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._find_opencode_config", return_value=opencode_path),
        ):
            assert setup_cmd._setup_host() is True

        kiro_env = json.loads(kiro_path.read_text(encoding="utf-8"))["mcpServers"]["ouroboros"][
            "env"
        ]
        assert kiro_env == {
            "OUROBOROS_RUNTIME": "host",
            "OUROBOROS_LLM_BACKEND": "kiro",
            "KEEP": "kiro",
            "OUROBOROS_AGENT_RUNTIME": "host",
        }
        copilot_env = json.loads(copilot_path.read_text(encoding="utf-8"))["mcpServers"][
            "ouroboros"
        ]["env"]
        assert copilot_env == {
            "OUROBOROS_AGENT_RUNTIME": "host",
            "OUROBOROS_LLM_BACKEND": "copilot",
        }
        opencode_env = json.loads(opencode_path.read_text(encoding="utf-8"))["mcp"]["ouroboros"][
            "environment"
        ]
        assert opencode_env == {
            "OUROBOROS_AGENT_RUNTIME": "host",
            "OUROBOROS_LLM_BACKEND": "opencode",
        }
        data = yaml.safe_load((config_dir / "config.yaml").read_text(encoding="utf-8"))
        assert data["orchestrator"]["runtime_backend"] == "host"

    def test_setup_host_rolls_back_launchers_when_later_selector_is_user_managed(
        self, tmp_path: Path
    ) -> None:
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        original_config = "orchestrator:\n  runtime_backend: kiro\n"
        config_path.write_text(original_config, encoding="utf-8")
        kiro_path = tmp_path / ".kiro" / "settings" / "mcp.json"
        kiro_path.parent.mkdir(parents=True)
        original_kiro = json.dumps(
            {
                "mcpServers": {
                    "ouroboros": {
                        "command": "uvx",
                        "args": ["ouroboros-ai[mcp]", "mcp", "serve"],
                        "env": {"OUROBOROS_RUNTIME": "kiro"},
                    }
                }
            }
        )
        kiro_path.write_text(original_kiro, encoding="utf-8")
        opencode_path = tmp_path / ".config" / "opencode" / "opencode.json"
        opencode_path.parent.mkdir(parents=True)
        original_opencode = json.dumps(
            {
                "mcp": {
                    "ouroboros": {
                        "type": "local",
                        "command": ["docker", "run", "ouroboros"],
                        "environment": {"OUROBOROS_AGENT_RUNTIME": "opencode"},
                    }
                }
            }
        )
        opencode_path.write_text(original_opencode, encoding="utf-8")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._find_opencode_config", return_value=opencode_path),
        ):
            assert setup_cmd._setup_host() is False

        assert config_path.read_text(encoding="utf-8") == original_config
        assert kiro_path.read_text(encoding="utf-8") == original_kiro
        assert opencode_path.read_text(encoding="utf-8") == original_opencode

    def test_setup_host_rejects_user_managed_runtime_override(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        original_config = "orchestrator:\n  runtime_backend: codex\n"
        config_path.write_text(original_config, encoding="utf-8")
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        codex_config = codex_dir / "config.toml"
        original_codex = (
            '[mcp_servers.ouroboros]\ncommand = "/custom/mcp-wrapper"\n'
            'args = ["serve", "--runtime", "codex"]\n'
        )
        codex_config.write_text(original_codex, encoding="utf-8")

        runner = CliRunner()
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._detect_runtimes", return_value={}),
        ):
            result = runner.invoke(setup_cmd.app, ["--runtime", "host", "--non-interactive"])

        assert result.exit_code == 1
        assert "user-managed" in result.output
        assert "Setup complete" not in result.output

        assert config_path.read_text(encoding="utf-8") == original_config
        assert codex_config.read_text(encoding="utf-8") == original_codex

    @pytest.mark.parametrize(
        ("command", "args"),
        [
            ("python", ["/opt/custom/server.py", "--runtime", "codex"]),
            ("uv", ["run", "/opt/custom/server.py", "--runtime", "codex"]),
            ("uvx", ["custom-package", "serve", "--runtime", "codex"]),
        ],
    )
    def test_setup_host_preserves_custom_common_launcher(
        self,
        tmp_path: Path,
        command: str,
        args: list[str],
    ) -> None:
        """Common executable names do not prove setup ownership."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text(
            "orchestrator:\n  runtime_backend: kiro\n", encoding="utf-8"
        )
        mcp_path = tmp_path / ".kiro" / "settings" / "mcp.json"
        mcp_path.parent.mkdir(parents=True)
        original = json.dumps(
            {
                "mcpServers": {
                    "ouroboros": {
                        "command": command,
                        "args": args,
                        "env": {"OUROBOROS_LLM_BACKEND": "codex"},
                    }
                }
            },
            indent=2,
        )
        mcp_path.write_text(original, encoding="utf-8")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
        ):
            assert setup_cmd._setup_host() is False

        assert mcp_path.read_text(encoding="utf-8") == original


class TestKiroSetup:
    """Tests for Kiro-specific setup behavior."""

    def test_setup_kiro_installs_instruction_artifact(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text("{}", encoding="utf-8")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._register_kiro_mcp_server"),
        ):
            setup_cmd._setup_kiro("/opt/bin/kiro-cli")

        guide_path = tmp_path / ".kiro" / "steering" / "ouroboros-skill-capability-guide.md"
        assert guide_path.is_file()
        assert "### When a skill requires `run_lateral_review`" in guide_path.read_text(
            encoding="utf-8"
        )

    def test_detect_runtimes_includes_kiro(self, tmp_path: Path) -> None:
        """_detect_runtimes should surface kiro when kiro-cli is on PATH.

        Explicit-path config helpers are stubbed to None so PATH lookup wins.
        """
        which_calls: dict[str, str | None] = {
            "claude": None,
            "codex": None,
            "opencode": None,
            "hermes": None,
            "gemini": None,
            "kiro-cli": "/opt/bin/kiro-cli",
        }

        with (
            patch(
                "ouroboros.cli.commands.setup.shutil.which",
                side_effect=lambda name: which_calls.get(name),
            ),
            patch("ouroboros.config.get_gemini_cli_path", return_value=None),
            patch("ouroboros.config.get_kiro_cli_path", return_value=None),
        ):
            detected = setup_cmd._detect_runtimes()

        assert detected["kiro"] == "/opt/bin/kiro-cli"

    def test_detect_runtimes_honors_config_kiro_path(self, tmp_path: Path) -> None:
        """Explicit orchestrator.kiro_cli_path takes precedence over PATH."""
        with (
            patch(
                "ouroboros.cli.commands.setup.shutil.which",
                side_effect=lambda name: "/custom/kiro" if name == "/custom/kiro" else None,
            ),
            patch(
                "ouroboros.config.get_kiro_cli_path",
                return_value="/custom/kiro",
            ),
        ):
            detected = setup_cmd._detect_runtimes()

        assert detected["kiro"] == "/custom/kiro"

    def test_detect_runtimes_rejects_stale_config_kiro_path(self, tmp_path: Path) -> None:
        """Stale explicit Kiro paths must not make setup report Kiro available."""
        with (
            patch(
                "ouroboros.cli.commands.setup.shutil.which",
                return_value=None,
            ),
            patch(
                "ouroboros.config.get_kiro_cli_path",
                return_value="/missing/kiro-cli",
            ),
        ):
            detected = setup_cmd._detect_runtimes()

        assert detected["kiro"] is None


class TestGjcSetup:
    """Tests for GJC setup artifacts and bridge wiring."""

    def test_detect_runtimes_includes_gjc_from_path(self) -> None:
        which_calls = {
            "claude": None,
            "codex": None,
            "opencode": None,
            "hermes": None,
            "gemini": None,
            "goose": None,
            "kiro-cli": None,
            "copilot": None,
            "pi": None,
            "gjc": "/opt/bin/gjc",
        }
        with (
            patch(
                "ouroboros.cli.commands.setup.shutil.which",
                side_effect=lambda name: which_calls.get(name),
            ),
            patch("ouroboros.config.get_gemini_cli_path", return_value=None),
            patch("ouroboros.config.get_goose_cli_path", return_value=None),
            patch("ouroboros.config.get_kiro_cli_path", return_value=None),
            patch("ouroboros.config.get_copilot_cli_path", return_value=None),
            patch("ouroboros.config.get_pi_cli_path", return_value=None),
            patch("ouroboros.config.get_gjc_cli_path", return_value=None),
        ):
            detected = setup_cmd._detect_runtimes()

        assert detected["gjc"] == "/opt/bin/gjc"

    def test_setup_gjc_writes_config_artifact_and_bridge(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text("{}", encoding="utf-8")
        agent_dir = tmp_path / "gjc-agent"
        monkeypatch.setenv("GJC_CODING_AGENT_DIR", str(agent_dir))

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
        ):
            setup_cmd._setup_gjc("/opt/bin/gjc")
            setup_cmd._setup_gjc("/opt/bin/gjc")

        config = yaml.safe_load((config_dir / "config.yaml").read_text(encoding="utf-8"))
        assert config["orchestrator"]["runtime_backend"] == "gjc"
        assert config["orchestrator"]["gjc_cli_path"] == "/opt/bin/gjc"
        assert config["llm"]["backend"] == "gjc"

        guide_path = agent_dir / "rules" / "ouroboros-skill-capability-guide.md"
        assert guide_path.read_text(encoding="utf-8") == render_backend_skill_capability_guide(
            "gjc"
        )

        bridge_path = agent_dir / "extensions" / "ouroboros-ooo-bridge" / "index.ts"
        bridge = bridge_path.read_text(encoding="utf-8")
        assert not (
            agent_dir / "extensions" / "ouroboros-ooo-bridge" / "ouroboros-ooo-bridge.ts"
        ).exists()
        assert '"dispatch", "--runtime", "gjc"' in bridge
        assert '"--cwd", cwd' in bridge
        assert "process.env.OUROBOROS_CLI" in bridge
        assert "DEFAULT_COMMAND" in bridge
        assert '"-m", "ouroboros"' in bridge
        assert "{ cwd, env, timeout: TIMEOUT_MS }" in bridge
        assert "UNSUPPORTED_DISPATCH_EXIT_CODE = 78" in bridge
        assert "_OUROBOROS_GJC_BRIDGE_DEPTH" in bridge

    def test_register_kiro_mcp_server_creates_fresh_entry(self, tmp_path: Path) -> None:
        """Fresh setup writes a valid entry with the Kiro env vars baked in."""
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "ouroboros.cli.commands.setup.shutil.which",
                side_effect=lambda cmd: "/usr/local/bin/uvx" if cmd == "uvx" else None,
            ),
        ):
            setup_cmd._register_kiro_mcp_server()

        mcp_path = tmp_path / ".kiro" / "settings" / "mcp.json"
        assert mcp_path.exists()
        data = json.loads(mcp_path.read_text(encoding="utf-8"))
        entry = data["mcpServers"]["ouroboros"]

        assert entry["command"] == "uvx"
        assert entry["args"] == [
            "--isolated",
            "--python",
            ">=3.12",
            "--from",
            "ouroboros-ai[mcp]",
            "ouroboros",
            "mcp",
            "serve",
        ]
        assert entry["disabled"] is False
        assert entry["env"]["OUROBOROS_RUNTIME"] == "kiro"
        assert entry["env"]["OUROBOROS_LLM_BACKEND"] == "kiro"

    def test_register_kiro_mcp_server_preserves_other_servers(
        self,
        tmp_path: Path,
    ) -> None:
        """Existing non-ouroboros entries must survive re-registration."""
        mcp_path = tmp_path / ".kiro" / "settings" / "mcp.json"
        mcp_path.parent.mkdir(parents=True)
        mcp_path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "awslabs.aws-documentation-mcp-server": {
                            "command": "uvx",
                            "args": ["aws-docs-mcp@latest"],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "ouroboros.cli.commands.setup.shutil.which",
                side_effect=lambda cmd: "/usr/local/bin/uvx" if cmd == "uvx" else None,
            ),
        ):
            setup_cmd._register_kiro_mcp_server()

        data = json.loads(mcp_path.read_text(encoding="utf-8"))
        assert "awslabs.aws-documentation-mcp-server" in data["mcpServers"]
        assert "ouroboros" in data["mcpServers"]

    def test_register_kiro_mcp_server_is_idempotent(self, tmp_path: Path) -> None:
        """Running the registration twice must not drift the entry."""
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "ouroboros.cli.commands.setup.shutil.which",
                side_effect=lambda cmd: "/usr/local/bin/uvx" if cmd == "uvx" else None,
            ),
        ):
            setup_cmd._register_kiro_mcp_server()
            first = (tmp_path / ".kiro" / "settings" / "mcp.json").read_text(encoding="utf-8")
            setup_cmd._register_kiro_mcp_server()
            second = (tmp_path / ".kiro" / "settings" / "mcp.json").read_text(encoding="utf-8")

        assert first == second

    def test_register_kiro_mcp_server_replaces_malformed_existing_entry(
        self,
        tmp_path: Path,
    ) -> None:
        """Malformed mcpServers.ouroboros entries should be repaired, not crash setup."""
        mcp_path = tmp_path / ".kiro" / "settings" / "mcp.json"
        mcp_path.parent.mkdir(parents=True)
        mcp_path.write_text(
            json.dumps({"mcpServers": {"ouroboros": "disabled"}}),
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "ouroboros.cli.commands.setup.shutil.which",
                side_effect=lambda cmd: "/usr/local/bin/uvx" if cmd == "uvx" else None,
            ),
        ):
            setup_cmd._register_kiro_mcp_server()

        data = json.loads(mcp_path.read_text(encoding="utf-8"))
        entry = data["mcpServers"]["ouroboros"]
        assert isinstance(entry, dict)
        assert entry["command"] == "uvx"
        assert entry["env"]["OUROBOROS_RUNTIME"] == "kiro"

    def test_register_kiro_mcp_server_merges_env_when_entry_exists(
        self,
        tmp_path: Path,
    ) -> None:
        """An existing ouroboros entry without env gets env injected; custom
        keys survive."""
        mcp_path = tmp_path / ".kiro" / "settings" / "mcp.json"
        mcp_path.parent.mkdir(parents=True)
        mcp_path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "ouroboros": {
                            "command": "uvx",
                            "args": [
                                "--from",
                                "ouroboros-ai[mcp]",
                                "ouroboros",
                                "mcp",
                                "serve",
                            ],
                            "env": {"CUSTOM_VAR": "keep_me"},
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "ouroboros.cli.commands.setup.shutil.which",
                side_effect=lambda cmd: "/usr/local/bin/uvx" if cmd == "uvx" else None,
            ),
        ):
            setup_cmd._register_kiro_mcp_server()

        data = json.loads(mcp_path.read_text(encoding="utf-8"))
        env = data["mcpServers"]["ouroboros"]["env"]
        assert env["OUROBOROS_RUNTIME"] == "kiro"
        assert env["OUROBOROS_LLM_BACKEND"] == "kiro"
        assert env["CUSTOM_VAR"] == "keep_me"

    def test_setup_kiro_updates_config_and_registers_mcp(self, tmp_path: Path) -> None:
        """_setup_kiro writes runtime_backend/llm.backend and delegates MCP
        registration."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text(
            yaml.safe_dump(
                {
                    "orchestrator": {"runtime_backend": "claude"},
                    "llm": {"backend": "claude_code", "qa_model": "x"},
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._register_kiro_mcp_server") as mock_register,
        ):
            setup_cmd._setup_kiro("/opt/bin/kiro-cli")

        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert config["orchestrator"]["runtime_backend"] == "kiro"
        assert config["orchestrator"]["kiro_cli_path"] == "/opt/bin/kiro-cli"
        assert config["llm"]["backend"] == "kiro"
        # Unrelated keys preserved.
        assert config["llm"]["qa_model"] == "x"
        mock_register.assert_called_once()
        assert mock_register.call_args.kwargs["detected"]["command"] in {"uvx", "pipx"}

    def test_setup_kiro_aborts_on_non_mapping_ouroboros_config(self, tmp_path: Path) -> None:
        """Kiro setup must not clobber malformed existing config.yaml contents."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        original = "- not-a-mapping\n- keep-me\n"
        config_path.write_text(original, encoding="utf-8")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._register_kiro_mcp_server") as mock_register,
        ):
            setup_cmd._setup_kiro("/opt/bin/kiro-cli")

        assert config_path.read_text(encoding="utf-8") == original
        mock_register.assert_not_called()

    def test_setup_cli_propagates_kiro_activation_failure(self, tmp_path: Path) -> None:
        """A host-registration failure must become a non-zero CLI result."""
        runner = CliRunner()
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "ouroboros.cli.commands.setup._detect_runtimes",
                return_value={"kiro": "/opt/bin/kiro-cli"},
            ),
            patch("ouroboros.cli.commands.setup._setup_kiro", return_value=False),
        ):
            result = runner.invoke(
                setup_cmd.app,
                ["--runtime", "kiro", "--non-interactive"],
            )

        assert result.exit_code == 1


class TestCopilotSetup:
    """`_setup_copilot`, `_register_copilot_mcp_server`, and the CLI dispatcher.

    Mirrors `TestKiroSetup` for parity. Focuses on what is *unique* to the
    Copilot path: live model discovery (with fallback warning), the dotted
    MCP entry written to `~/.copilot/mcp-config.json`, and the new
    `--runtime copilot` CLI branch.
    """

    @staticmethod
    def _stub_models() -> list:
        from ouroboros.copilot.model_discovery import CopilotModel

        return [
            CopilotModel(id="claude-opus-4.6", family="claude-opus-4.6"),
            CopilotModel(id="claude-sonnet-4.5", family="claude-sonnet-4.5"),
        ]

    def test_setup_copilot_writes_runtime_and_default_model(self, tmp_path: Path) -> None:
        """Non-interactive setup writes runtime/llm/clarification config plus
        the chosen default model picked from live discovery."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text(
            yaml.safe_dump(
                {
                    "orchestrator": {"runtime_backend": "claude"},
                    "llm": {"backend": "claude_code", "qa_model": "x"},
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.copilot.model_discovery.list_copilot_models",
                return_value=self._stub_models(),
            ),
            patch(
                "ouroboros.copilot.model_discovery.used_fallback",
                return_value=False,
            ),
            patch("ouroboros.cli.commands.setup._register_copilot_mcp_server") as mock_register,
        ):
            setup_cmd._setup_copilot("/opt/bin/copilot", non_interactive=True)

        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert config["orchestrator"]["runtime_backend"] == "copilot"
        assert config["orchestrator"]["copilot_cli_path"] == "/opt/bin/copilot"
        assert config["llm"]["backend"] == "copilot"
        # Default model is the recommended dotted Copilot ID, persisted only
        # through supported config fields.
        assert "default_model" not in config["llm"]
        assert config["clarification"]["default_model"] == "claude-opus-4.6"
        # Explicit user overrides are preserved.
        assert config["llm"]["qa_model"] == "x"
        guide_path = tmp_path / ".copilot" / "ouroboros-instructions" / "AGENTS.md"
        assert guide_path.is_file()
        assert "### When a skill requires `run_lateral_review`" in guide_path.read_text(
            encoding="utf-8"
        )
        mock_register.assert_called_once()
        assert mock_register.call_args.kwargs["detected"]["command"] in {"uvx", "pipx"}

    def test_setup_copilot_replaces_shipped_default_model_fields(self, tmp_path: Path) -> None:
        """Fresh/default configs should honor the model selected during setup."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text(
            yaml.safe_dump(
                {
                    "orchestrator": {"runtime_backend": "claude"},
                    "llm": {"backend": "claude_code"},
                    "clarification": {"default_model": DEFAULT_OPUS_MODEL},
                    "evaluation": {"semantic_model": DEFAULT_OPUS_MODEL},
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.copilot.model_discovery.list_copilot_models",
                return_value=self._stub_models(),
            ),
            patch("ouroboros.copilot.model_discovery.used_fallback", return_value=False),
            patch("ouroboros.cli.commands.setup._register_copilot_mcp_server"),
        ):
            setup_cmd._setup_copilot("/opt/bin/copilot", non_interactive=True)

        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert config["llm"]["qa_model"] == "claude-opus-4.6"
        assert config["llm"]["dependency_analysis_model"] == "claude-opus-4.6"
        assert config["clarification"]["default_model"] == "claude-opus-4.6"
        assert config["evaluation"]["semantic_model"] == "claude-opus-4.6"
        assert config["consensus"]["models"] == [
            "claude-opus-4.6",
            "claude-sonnet-4.5",
            "claude-opus-4.6",
        ]
        assert config["consensus"]["advocate_model"] == "claude-opus-4.6"
        assert config["consensus"]["devil_model"] == "claude-opus-4.6"
        assert config["consensus"]["judge_model"] == "claude-opus-4.6"
        assert "default_model" not in config["llm"]

    def test_setup_copilot_replaces_legacy_shipped_default_model_fields(
        self, tmp_path: Path
    ) -> None:
        """Regression for #1324 (ouroboros-agent[bot] req_1780391230_63).

        A config persisted by a prior release holds the OLD shipped defaults
        (``claude-opus-4-6``, ``claude-sonnet-4-20250514``, the old OpenRouter
        consensus slug). These are untouched shipped defaults the user never
        chose, so Copilot setup must rewrite them to the discovered model just
        like the current shipped defaults — not mistake them for explicit
        overrides and leave unrunnable Claude ids in config.yaml. An explicit
        non-shipped override must still be preserved.
        """
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text(
            yaml.safe_dump(
                {
                    "orchestrator": {"runtime_backend": "claude"},
                    "llm": {"backend": "claude_code", "qa_model": "claude-sonnet-4-20250514"},
                    "clarification": {"default_model": "claude-opus-4-6"},
                    "evaluation": {"semantic_model": "claude-opus-4-6"},
                    "resilience": {
                        "wonder_model": "claude-opus-4-6",
                        # Explicit, never-shipped override — must be preserved.
                        "reflect_model": "gpt-5-mini",
                    },
                    "consensus": {
                        "advocate_model": "openrouter/anthropic/claude-opus-4-6",
                        "models": [
                            "openrouter/openai/gpt-4o",
                            "openrouter/anthropic/claude-opus-4-6",
                            "openrouter/google/gemini-2.5-pro",
                        ],
                    },
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.copilot.model_discovery.list_copilot_models",
                return_value=self._stub_models(),
            ),
            patch("ouroboros.copilot.model_discovery.used_fallback", return_value=False),
            patch("ouroboros.cli.commands.setup._register_copilot_mcp_server"),
        ):
            setup_cmd._setup_copilot("/opt/bin/copilot", non_interactive=True)

        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        # Legacy shipped defaults are rewritten to the discovered Copilot model.
        assert config["llm"]["qa_model"] == "claude-opus-4.6"
        assert config["clarification"]["default_model"] == "claude-opus-4.6"
        assert config["evaluation"]["semantic_model"] == "claude-opus-4.6"
        assert config["resilience"]["wonder_model"] == "claude-opus-4.6"
        assert config["consensus"]["advocate_model"] == "claude-opus-4.6"
        assert config["consensus"]["models"] == [
            "claude-opus-4.6",
            "claude-sonnet-4.5",
            "claude-opus-4.6",
        ]
        # Explicit, never-shipped override is preserved (no over-broadening).
        assert config["resilience"]["reflect_model"] == "gpt-5-mini"

    @pytest.mark.parametrize(
        "original",
        [
            pytest.param(
                yaml.safe_dump(
                    {
                        "orchestrator": ["keep", "me"],
                        "llm": {"backend": "claude_code"},
                    },
                    sort_keys=False,
                ),
                id="non-mapping-orchestrator",
            ),
            pytest.param(
                yaml.safe_dump(
                    {
                        "orchestrator": {"runtime_backend": "claude"},
                        "llm": {"backend": "claude_code"},
                        "consensus": ["keep", "me"],
                    },
                    sort_keys=False,
                ),
                id="non-mapping-model-section",
            ),
            pytest.param("- not-a-mapping\n- keep-me\n", id="non-mapping-config-root"),
        ],
    )
    def test_setup_copilot_aborts_on_non_mapping_config(
        self, tmp_path: Path, original: str
    ) -> None:
        """Malformed sections are validated before rewrite — never clobbered,
        never partially rewritten, and MCP registration never runs."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text(original, encoding="utf-8")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.copilot.model_discovery.list_copilot_models",
                return_value=self._stub_models(),
            ),
            patch("ouroboros.copilot.model_discovery.used_fallback", return_value=False),
            patch("ouroboros.cli.commands.setup._register_copilot_mcp_server") as mock_register,
        ):
            setup_cmd._setup_copilot("/opt/bin/copilot", non_interactive=True)

        assert config_path.read_text(encoding="utf-8") == original
        mock_register.assert_not_called()

    def test_setup_copilot_warns_when_discovery_used_fallback(self, tmp_path: Path) -> None:
        """Setup must visibly warn when it could not reach the live API."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text(
            yaml.safe_dump({}, sort_keys=False), encoding="utf-8"
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.copilot.model_discovery.list_copilot_models",
                return_value=self._stub_models(),
            ),
            patch(
                "ouroboros.copilot.model_discovery.used_fallback",
                return_value=True,
            ),
            patch("ouroboros.cli.commands.setup._register_copilot_mcp_server"),
            patch("ouroboros.cli.commands.setup.print_warning") as mock_warning,
        ):
            setup_cmd._setup_copilot("/opt/bin/copilot", non_interactive=True)

        mock_warning.assert_called_once_with(
            "Could not reach the GitHub Copilot models API — using a bundled "
            "fallback list. Run `gh auth login` and re-run setup to refresh."
        )

    def test_setup_copilot_aborts_when_no_models_discovered(self, tmp_path: Path) -> None:
        """If discovery returns an empty list, setup must abort cleanly
        instead of writing a default-less config."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text(
            yaml.safe_dump({"orchestrator": {"runtime_backend": "claude"}}, sort_keys=False),
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.copilot.model_discovery.list_copilot_models",
                return_value=[],
            ),
            patch(
                "ouroboros.copilot.model_discovery.used_fallback",
                return_value=False,
            ),
            patch("ouroboros.cli.commands.setup._register_copilot_mcp_server") as mock_register,
        ):
            setup_cmd._setup_copilot("/opt/bin/copilot", non_interactive=True)

        # Aborted before mutating runtime_backend or registering MCP.
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert config["orchestrator"]["runtime_backend"] == "claude"
        mock_register.assert_not_called()

    def test_register_copilot_mcp_creates_new_entry(self, tmp_path: Path) -> None:
        """An empty mcp-config.json gets the ouroboros entry with the
        copilot env block."""
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "ouroboros.cli.commands.setup._detect_mcp_entry",
                return_value={
                    "command": "uvx",
                    "args": [
                        "--from",
                        "ouroboros-ai[mcp]",
                        "ouroboros",
                        "mcp",
                        "serve",
                    ],
                },
            ),
        ):
            setup_cmd._register_copilot_mcp_server()

        mcp_path = tmp_path / ".copilot" / "mcp-config.json"
        assert mcp_path.exists()
        data = json.loads(mcp_path.read_text(encoding="utf-8"))
        entry = data["mcpServers"]["ouroboros"]
        assert entry["command"] == "uvx"
        assert entry["env"]["OUROBOROS_AGENT_RUNTIME"] == "copilot"
        assert entry["env"]["OUROBOROS_LLM_BACKEND"] == "copilot"

    def test_register_copilot_mcp_is_idempotent(self, tmp_path: Path) -> None:
        """Re-running with an identical detected entry must not rewrite the file."""
        mcp_path = tmp_path / ".copilot" / "mcp-config.json"
        mcp_path.parent.mkdir(parents=True)
        existing_entry = {
            "command": "uvx",
            "args": [
                "--from",
                "ouroboros-ai[mcp]",
                "ouroboros",
                "mcp",
                "serve",
            ],
            "env": {
                "OUROBOROS_AGENT_RUNTIME": "copilot",
                "OUROBOROS_LLM_BACKEND": "copilot",
            },
        }
        mcp_path.write_text(
            json.dumps({"mcpServers": {"ouroboros": existing_entry}}, indent=2),
            encoding="utf-8",
        )
        before_mtime = mcp_path.stat().st_mtime_ns

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "ouroboros.cli.commands.setup._detect_mcp_entry",
                return_value={
                    "command": "uvx",
                    "args": [
                        "--from",
                        "ouroboros-ai[mcp]",
                        "ouroboros",
                        "mcp",
                        "serve",
                    ],
                },
            ),
        ):
            setup_cmd._register_copilot_mcp_server()

        # File must remain byte-identical (no spurious rewrite).
        after = json.loads(mcp_path.read_text(encoding="utf-8"))
        assert after["mcpServers"]["ouroboros"] == existing_entry
        assert mcp_path.stat().st_mtime_ns == before_mtime

    def test_register_copilot_mcp_preserves_custom_entry_and_merges_env(
        self, tmp_path: Path
    ) -> None:
        """Custom Copilot MCP wrappers should not be replaced by setup."""
        mcp_path = tmp_path / ".copilot" / "mcp-config.json"
        mcp_path.parent.mkdir(parents=True)
        mcp_path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "ouroboros": {
                            "command": "/opt/custom/wrapper",
                            "args": ["--custom"],
                            "env": {"CUSTOM": "1"},
                        }
                    }
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "ouroboros.cli.commands.setup._detect_mcp_entry",
                return_value={"command": "uvx", "args": ["ouroboros", "mcp", "serve"]},
            ),
        ):
            setup_cmd._register_copilot_mcp_server()

        entry = json.loads(mcp_path.read_text(encoding="utf-8"))["mcpServers"]["ouroboros"]
        assert entry["command"] == "/opt/custom/wrapper"
        assert entry["args"] == ["--custom"]
        assert entry["env"] == {
            "CUSTOM": "1",
            "OUROBOROS_AGENT_RUNTIME": "copilot",
            "OUROBOROS_LLM_BACKEND": "copilot",
        }

    def test_register_copilot_mcp_updates_setup_managed_entry(self, tmp_path: Path) -> None:
        """Setup-managed entries can be upgraded while preserving extra env."""
        mcp_path = tmp_path / ".copilot" / "mcp-config.json"
        mcp_path.parent.mkdir(parents=True)
        mcp_path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "ouroboros": {
                            "command": "uvx",
                            "args": ["old"],
                            "env": {"CUSTOM": "1"},
                        }
                    }
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "ouroboros.cli.commands.setup._detect_mcp_entry",
                return_value={"command": "uvx", "args": ["new"]},
            ),
        ):
            setup_cmd._register_copilot_mcp_server()

        entry = json.loads(mcp_path.read_text(encoding="utf-8"))["mcpServers"]["ouroboros"]
        assert entry["command"] == "uvx"
        assert entry["args"] == ["new"]
        assert entry["env"]["CUSTOM"] == "1"
        assert entry["env"]["OUROBOROS_AGENT_RUNTIME"] == "copilot"
        assert entry["env"]["OUROBOROS_LLM_BACKEND"] == "copilot"

    def test_register_copilot_mcp_skips_invalid_json(self, tmp_path: Path) -> None:
        """Malformed mcp-config.json is left untouched; no crash."""
        mcp_path = tmp_path / ".copilot" / "mcp-config.json"
        mcp_path.parent.mkdir(parents=True)
        original = "{this is not json"
        mcp_path.write_text(original, encoding="utf-8")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "ouroboros.cli.commands.setup._detect_mcp_entry",
                return_value={"command": "uvx", "args": []},
            ),
        ):
            setup_cmd._register_copilot_mcp_server()

        assert mcp_path.read_text(encoding="utf-8") == original

    def test_register_copilot_mcp_warns_when_no_install_detected(self, tmp_path: Path) -> None:
        """When no working ouroboros install exists, do not write a broken entry."""
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "ouroboros.cli.commands.setup._detect_mcp_entry",
                return_value=None,
            ),
        ):
            setup_cmd._register_copilot_mcp_server()

        mcp_path = tmp_path / ".copilot" / "mcp-config.json"
        # Either nothing was created, or if the path was touched, no
        # ouroboros entry was inserted.
        if mcp_path.exists():
            data = json.loads(mcp_path.read_text(encoding="utf-8"))
            assert "ouroboros" not in data.get("mcpServers", {})

    def test_detect_runtimes_picks_up_copilot_from_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """`_detect_runtimes()` should report copilot when the binary is on PATH."""
        fake = tmp_path / "copilot"
        fake.write_text("#!/bin/sh\n", encoding="utf-8")

        monkeypatch.delenv("OUROBOROS_COPILOT_CLI_PATH", raising=False)

        def fake_which(name: str) -> str | None:
            return str(fake) if name == "copilot" else None

        with patch("shutil.which", side_effect=fake_which):
            runtimes = setup_cmd._detect_runtimes()

        assert runtimes["copilot"] == str(fake)

    def test_detect_runtimes_honours_explicit_copilot_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """OUROBOROS_COPILOT_CLI_PATH wins over the bare PATH lookup."""
        explicit = tmp_path / "from-env-copilot"
        explicit.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setenv("OUROBOROS_COPILOT_CLI_PATH", str(explicit))

        on_path = tmp_path / "from-path-copilot"
        on_path.write_text("#!/bin/sh\n", encoding="utf-8")

        def fake_which(name: str) -> str | None:
            # `_detect_runtimes` validates env paths via shutil.which too.
            if name == str(explicit):
                return str(explicit)
            if name == "copilot":
                return str(on_path)
            return None

        with patch("shutil.which", side_effect=fake_which):
            runtimes = setup_cmd._detect_runtimes()

        assert runtimes["copilot"] == str(explicit)

    def test_detect_runtimes_picks_up_pi_from_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """`_detect_runtimes()` should report pi when the binary is on PATH."""
        fake = tmp_path / "pi"
        fake.write_text("#!/bin/sh\n", encoding="utf-8")

        monkeypatch.delenv("OUROBOROS_PI_CLI_PATH", raising=False)

        def fake_which(name: str) -> str | None:
            return str(fake) if name == "pi" else None

        with patch("shutil.which", side_effect=fake_which):
            runtimes = setup_cmd._detect_runtimes()

        assert runtimes["pi"] == str(fake)

    def test_detect_runtimes_honours_explicit_pi_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """OUROBOROS_PI_CLI_PATH wins over the bare PATH lookup."""
        explicit = tmp_path / "from-env-pi"
        explicit.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setenv("OUROBOROS_PI_CLI_PATH", str(explicit))

        on_path = tmp_path / "from-path-pi"
        on_path.write_text("#!/bin/sh\n", encoding="utf-8")

        def fake_which(name: str) -> str | None:
            if name == str(explicit):
                return str(explicit)
            if name == "pi":
                return str(on_path)
            return None

        with patch("shutil.which", side_effect=fake_which):
            runtimes = setup_cmd._detect_runtimes()

        assert runtimes["pi"] == str(explicit)

    def test_detect_runtimes_picks_up_omp_from_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """`_detect_runtimes()` should report omp when the binary is on PATH."""
        fake = tmp_path / "omp"
        fake.write_text("#!/bin/sh\n", encoding="utf-8")

        monkeypatch.delenv("OUROBOROS_OMP_CLI_PATH", raising=False)

        def fake_which(name: str) -> str | None:
            return str(fake) if name == "omp" else None

        with patch("shutil.which", side_effect=fake_which):
            runtimes = setup_cmd._detect_runtimes()

        assert runtimes["omp"] == str(fake)

    def test_detect_runtimes_honours_explicit_omp_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """OUROBOROS_OMP_CLI_PATH wins over the bare PATH lookup."""
        explicit = tmp_path / "from-env-omp"
        explicit.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setenv("OUROBOROS_OMP_CLI_PATH", str(explicit))

        def fake_which(name: str) -> str | None:
            if name == str(explicit):
                return str(explicit)
            if name == "omp":
                return str(tmp_path / "from-path-omp")
            return None

        with patch("shutil.which", side_effect=fake_which):
            runtimes = setup_cmd._detect_runtimes()

        assert runtimes["omp"] == str(explicit)

    def test_setup_pi_writes_runtime_without_switching_llm_backend(self, tmp_path: Path) -> None:
        """Pi setup preserves the existing LLM backend unless explicitly changed."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text(
            yaml.safe_dump(
                {
                    "orchestrator": {"runtime_backend": "claude"},
                    "llm": {"backend": "codex"},
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        with patch("pathlib.Path.home", return_value=tmp_path):
            setup_cmd._setup_pi("/opt/bin/pi")

        config_path = tmp_path / ".ouroboros" / "config.yaml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        bridge_path = tmp_path / ".pi" / "agent" / "extensions" / "ouroboros-ooo-bridge.ts"

        assert config["orchestrator"]["runtime_backend"] == "pi"
        assert config["orchestrator"]["pi_cli_path"] == "/opt/bin/pi"
        assert config["llm"]["backend"] == "codex"
        assert bridge_path.exists()
        bridge_source = bridge_path.read_text(encoding="utf-8")
        assert 'pi.registerCommand("ooo"' in bridge_source
        assert 'pi.on("input"' in bridge_source
        assert (
            '[...entry.args, "dispatch", "--runtime", "pi", "--cwd", ctx.cwd, text]'
            in bridge_source
        )
        assert "UNSUPPORTED_DISPATCH_EXIT_CODE = 78" in bridge_source
        assert 'return { action: handled ? "handled" : "continue" }' in bridge_source

    def test_setup_omp_writes_runtime_without_switching_llm_backend(self, tmp_path: Path) -> None:
        """OMP setup preserves the existing LLM backend unless explicitly changed."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text(
            yaml.safe_dump(
                {
                    "orchestrator": {"runtime_backend": "claude"},
                    "llm": {"backend": "codex"},
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        with patch("pathlib.Path.home", return_value=tmp_path):
            setup_cmd._setup_omp("/opt/bin/omp")

        config_path = tmp_path / ".ouroboros" / "config.yaml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        bridge_path = tmp_path / ".omp" / "agent" / "extensions" / "ouroboros-ooo-bridge.ts"

        assert config["orchestrator"]["runtime_backend"] == "omp"
        assert config["orchestrator"]["omp_cli_path"] == "/opt/bin/omp"
        assert config["llm"]["backend"] == "codex"
        assert bridge_path.exists()
        bridge_source = bridge_path.read_text(encoding="utf-8")
        assert 'omp.registerCommand("ooo"' in bridge_source
        assert 'omp.on("input"' in bridge_source
        assert (
            '[...entry.args, "dispatch", "--runtime", "omp", "--cwd", ctx.cwd, text]'
            in bridge_source
        )
        assert "UNSUPPORTED_DISPATCH_EXIT_CODE = 78" in bridge_source

    def test_setup_omp_fails_closed_when_bridge_install_fails(self, tmp_path: Path) -> None:
        """PR #2299 round 1: a failed bridge write must not commit omp config."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text(
            yaml.safe_dump(
                {"orchestrator": {"runtime_backend": "claude"}},
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "ouroboros.cli.commands.omp_setup.install_omp_ooo_bridge",
                return_value=False,
            ) as mock_install,
        ):
            result = setup_cmd._setup_omp("/opt/bin/omp")

        assert result is False
        mock_install.assert_called_once_with()
        config = yaml.safe_load((config_dir / "config.yaml").read_text(encoding="utf-8"))
        assert config["orchestrator"]["runtime_backend"] == "claude"
        assert "omp_cli_path" not in config["orchestrator"]
        assert not (tmp_path / ".omp" / "agent" / "extensions").exists()

    def test_setup_omp_commits_config_only_after_bridge_success(self, tmp_path: Path) -> None:
        """Config commit happens after bridge activation, in that order."""
        calls: list[str] = []

        def fake_install() -> bool:
            calls.append("bridge")
            # The config file must still describe the pre-OMP state when the
            # bridge installer runs.
            raw = (tmp_path / ".ouroboros" / "config.yaml").read_text(encoding="utf-8")
            assert "runtime_backend: omp" not in raw
            return True

        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text(
            yaml.safe_dump({"orchestrator": {"runtime_backend": "claude"}}, sort_keys=False),
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "ouroboros.cli.commands.omp_setup.install_omp_ooo_bridge",
                side_effect=fake_install,
            ),
        ):
            result = setup_cmd._setup_omp("/opt/bin/omp")

        assert result is True
        assert calls == ["bridge"]
        config = yaml.safe_load((config_dir / "config.yaml").read_text(encoding="utf-8"))
        assert config["orchestrator"]["runtime_backend"] == "omp"

    def test_install_omp_ooo_bridge_is_idempotent(self, tmp_path: Path) -> None:
        """The managed OMP bridge should not rewrite an already-current extension."""
        bridge_path = tmp_path / ".omp" / "agent" / "extensions" / "ouroboros-ooo-bridge.ts"

        with patch("pathlib.Path.home", return_value=tmp_path):
            assert setup_cmd._install_omp_ooo_bridge() is True
            first_mtime = bridge_path.stat().st_mtime_ns
            assert setup_cmd._install_omp_ooo_bridge() is True

        assert bridge_path.stat().st_mtime_ns == first_mtime

    def test_install_pi_ooo_bridge_is_idempotent(self, tmp_path: Path) -> None:
        """The managed Pi bridge should not rewrite an already-current extension."""
        bridge_path = tmp_path / ".pi" / "agent" / "extensions" / "ouroboros-ooo-bridge.ts"

        with patch("pathlib.Path.home", return_value=tmp_path):
            assert setup_cmd._install_pi_ooo_bridge() is True
            first_mtime = bridge_path.stat().st_mtime_ns
            assert setup_cmd._install_pi_ooo_bridge() is True

        assert bridge_path.stat().st_mtime_ns == first_mtime

    def test_pi_bridge_registers_argument_completions(self) -> None:
        """The managed bridge should TAB-complete `/ooo` arguments.

        `/ooo <TAB>` lists the dispatchable subcommands and `ooo run <TAB>`
        lists Seed files from `~/.ouroboros/seeds/` as `run <quoted-absolute-path>`
        so Pi's full-prefix replacement preserves the subcommand and the
        completed value executes regardless of the Pi session cwd.
        """
        bridge_source = setup_cmd._pi_ooo_bridge_source_text()

        assert "getArgumentCompletions: argumentCompletions" in bridge_source
        assert "function argumentCompletions(argumentPrefix: string)" in bridge_source
        assert 'path.join(homedir(), ".ouroboros", "seeds")' in bridge_source
        # Seed completion values carry `run <quoted path>` — Pi replaces the
        # entire argument prefix with the value, so `run` must be part of it.
        assert "value: `run ${quoted}`" in bridge_source
        assert ".sort()" in bridge_source
        assert 'tokens[0].toLowerCase() === "run"' in bridge_source

    def test_pi_bridge_completions_mirror_dispatchable_skills(self) -> None:
        """Completion subcommands must mirror dispatcher-eligible skills.

        Eligibility is derived through ``resolve_skill_dispatch`` itself —
        frontmatter loading, ``normalize_mcp_frontmatter`` validation, and
        template resolution — instead of a textual ``mcp_tool:`` grep, so a
        body example or an invalid/missing ``mcp_args`` mapping cannot make
        an undispatchable skill look dispatchable.
        """
        bridge_source = setup_cmd._pi_ooo_bridge_source_text()
        completed = set(re.findall(r'cmd: "([a-z0-9][a-z0-9_-]*)"', bridge_source))

        with resolve_packaged_skills_dir(anchor_file=Path(__file__)) as skills_dir:
            dispatchable = {
                skill_dir.name
                for skill_dir in skills_dir.iterdir()
                if (skill_dir / "SKILL.md").is_file()
                and isinstance(
                    resolve_skill_dispatch(
                        ResolveRequest(prompt=f"ooo {skill_dir.name}", cwd=Path.cwd())
                    ),
                    Resolved,
                )
            }

        assert completed == dispatchable

    async def test_pi_bridge_run_completion_value_round_trips_to_global_seed(
        self, tmp_path: Path
    ) -> None:
        """Absolute Seed completion values must stay executable end to end.

        The `run` dispatcher forwards ``seed_path`` unchanged and the seed
        resolver treats relative paths as session-cwd-relative, degrading a
        missing file to inline YAML. Completion therefore advertises
        ``run <quoted-absolute-path>``; this test pins that contract from the
        completed value through router resolution to seed loading.
        """
        seeds_dir = tmp_path / ".ouroboros" / "seeds"
        seeds_dir.mkdir(parents=True)
        (seeds_dir / "demo.yaml").write_text("goal: demo\n", encoding="utf-8")
        absolute_path = str(seeds_dir / "demo.yaml")
        session_cwd = tmp_path / "session"
        session_cwd.mkdir()

        # The completion value now carries `run <path>` — the full replacement
        # that Pi will substitute for the argument prefix.
        completed_value = f"run {absolute_path}"

        resolved = resolve_skill_dispatch(
            ResolveRequest(prompt=f"ooo {completed_value}", cwd=session_cwd)
        )
        assert isinstance(resolved, Resolved)
        assert resolved.mcp_tool == "ouroboros_execute_seed"
        assert resolved.mcp_args["seed_path"] == absolute_path

        with patch("pathlib.Path.home", return_value=tmp_path):
            loaded = await ExecuteSeedHandler._resolve_seed_content(
                arguments={"seed_path": absolute_path},
                resolved_cwd=session_cwd,
                tool_name="ouroboros_execute_seed",
            )
            bare_name = await ExecuteSeedHandler._resolve_seed_content(
                arguments={"seed_path": "demo.yaml"},
                resolved_cwd=session_cwd,
                tool_name="ouroboros_execute_seed",
            )

        assert loaded.is_ok
        assert loaded.value == "goal: demo\n"
        # The pre-fix completion shape: a bare name never reaches the global
        # store from a session cwd and degrades to inline YAML.
        assert bare_name.is_ok
        assert bare_name.value == "demo.yaml"

    def test_pi_completion_value_survives_combined_autocomplete_provider_replacement(
        self, tmp_path: Path
    ) -> None:
        """Seed completion values remain dispatchable after Pi's full-prefix replacement.

        Pi's ``CombinedAutocompleteProvider.applyCompletion`` replaces the
        entire argument prefix with ``item.value``. For example, when a user
        types ``/ooo run de<TAB>`` and selects a Seed completion, the argument
        text ``run de`` is replaced wholesale with the item's ``value``.

        Before the fix, ``value`` was just the absolute path, so the result
        was ``/ooo /path/to/demo.yaml`` — which the shared dispatcher could
        not recognize (no ``run`` subcommand). This regression test verifies
        that the completed value, when applied as Pi does, produces a
        dispatchable command.
        """
        seeds_dir = tmp_path / ".ouroboros" / "seeds"
        seeds_dir.mkdir(parents=True)
        (seeds_dir / "demo.yaml").write_text("goal: demo\n", encoding="utf-8")
        session_cwd = tmp_path / "session"
        session_cwd.mkdir()

        absolute_path = str(seeds_dir / "demo.yaml")

        # --- Simulate Pi's CombinedAutocompleteProvider semantics ---
        # The user types: /ooo run de
        # argumentPrefix passed to getArgumentCompletions: "run de"
        # The rendered bridge returns: { value: "run <absolute-path>", ... }
        # Pi applies the completion: replaces "run de" with item.value.
        # The final command dispatched: /ooo <item.value>

        # The completion value as the bridge would render it (without
        # shell-quoting since this path has no special chars):
        completed_item_value = f"run {absolute_path}"

        # Pi replaces the full argument prefix with item.value, making the
        # dispatched command: `ooo <completed_item_value>`
        dispatched_text = f"ooo {completed_item_value}"

        resolved = resolve_skill_dispatch(ResolveRequest(prompt=dispatched_text, cwd=session_cwd))
        assert isinstance(resolved, Resolved), (
            f"Pi full-prefix replacement produced undispatchable command: "
            f"{dispatched_text!r} -> {resolved!r}"
        )
        assert resolved.mcp_tool == "ouroboros_execute_seed"
        assert resolved.mcp_args["seed_path"] == absolute_path

        # --- Verify the OLD (broken) behavior would fail ---
        # Before the fix, value was just the absolute path (no 'run' prefix).
        broken_item_value = absolute_path
        broken_dispatched = f"ooo {broken_item_value}"
        broken_result = resolve_skill_dispatch(
            ResolveRequest(prompt=broken_dispatched, cwd=session_cwd)
        )
        # The absolute path alone is not a recognized skill subcommand
        assert not isinstance(broken_result, Resolved), (
            "Bare absolute path should NOT dispatch — it means Pi's replacement "
            "lost the 'run' subcommand"
        )

    def test_pi_completion_value_with_quoted_path_survives_replacement(
        self, tmp_path: Path
    ) -> None:
        """Every shlex-supported pathname character survives Pi replacement.

        The rendered bridge uses POSIX single-argument quoting, including the
        standard quote break for an embedded single quote. The dispatcher's
        ``shlex.split`` must recover the exact original path without expanding
        dollar signs or backticks or retaining escape characters.
        """
        seeds_dir = tmp_path / "my $projects`archive'\\data" / ".ouroboros" / "seeds"
        seeds_dir.mkdir(parents=True)
        (seeds_dir / "demo.yaml").write_text("goal: quoted\n", encoding="utf-8")
        session_cwd = tmp_path / "session"
        session_cwd.mkdir()

        absolute_path = str(seeds_dir / "demo.yaml")
        quoted_path = shlex.quote(absolute_path)
        completed_item_value = f"run {quoted_path}"

        dispatched_text = f"ooo {completed_item_value}"
        resolved = resolve_skill_dispatch(ResolveRequest(prompt=dispatched_text, cwd=session_cwd))
        assert isinstance(resolved, Resolved), (
            f"Quoted-path completion produced undispatchable command: "
            f"{dispatched_text!r} -> {resolved!r}"
        )
        assert resolved.mcp_tool == "ouroboros_execute_seed"
        assert resolved.mcp_args["seed_path"] == absolute_path


class TestRuntimeFlagDispatch:
    """`setup --runtime <name> --non-interactive` dispatch for the CLI-detected
    runtimes (kiro, pi, copilot) — both the found and the missing-binary case."""

    _EMPTY_DETECTION = {
        "claude": None,
        "codex": None,
        "opencode": None,
        "hermes": None,
        "gemini": None,
        "kiro": None,
        "copilot": None,
        "pi": None,
    }

    @pytest.mark.parametrize(
        ("runtime", "setup_attr", "cli_path", "expected_kwargs"),
        [
            pytest.param("kiro", "_setup_kiro", "/opt/bin/kiro-cli", {}, id="kiro"),
            pytest.param("pi", "_setup_pi", "/opt/bin/pi", {}, id="pi"),
            pytest.param(
                "copilot",
                "_setup_copilot",
                "/opt/bin/copilot",
                {"non_interactive": True},
                id="copilot",
            ),
        ],
    )
    def test_runtime_flag_runs_setup_path(
        self,
        tmp_path: Path,
        runtime: str,
        setup_attr: str,
        cli_path: str,
        expected_kwargs: dict,
    ) -> None:
        """The setup path runs with the detected binary and no user interaction."""
        detected = dict(self._EMPTY_DETECTION, **{runtime: cli_path})
        runner = CliRunner()
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.cli.commands.setup._detect_runtimes", return_value=detected),
            patch(f"ouroboros.cli.commands.setup.{setup_attr}") as mock_setup,
        ):
            result = runner.invoke(
                setup_cmd.app,
                ["--runtime", runtime, "--non-interactive"],
            )

        assert result.exit_code == 0, result.output
        mock_setup.assert_called_once_with(cli_path, **expected_kwargs)

    @pytest.mark.parametrize(
        ("runtime", "message"),
        [
            pytest.param("kiro", "Kiro CLI not found", id="kiro"),
            pytest.param("pi", "Pi CLI not found", id="pi"),
            pytest.param("copilot", "Copilot CLI not found", id="copilot"),
        ],
    )
    def test_missing_binary_errors_cleanly(
        self, tmp_path: Path, runtime: str, message: str
    ) -> None:
        """An explicit --runtime with no binary exits non-zero instead of
        crashing or silently succeeding."""
        runner = CliRunner()
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "ouroboros.cli.commands.setup._detect_runtimes",
                return_value=dict(self._EMPTY_DETECTION),
            ),
        ):
            result = runner.invoke(
                setup_cmd.app,
                ["--runtime", runtime, "--non-interactive"],
            )

        assert result.exit_code != 0
        assert message in result.output


class TestNonInteractiveAutoSelect:
    """`ouroboros setup --non-interactive` runtime auto-selection."""

    def _run(self, *, current_backend: str | None, available: dict[str, str]) -> str:
        """Invoke `setup --non-interactive` (no --runtime) and capture which
        backend the auto-select branch chose, by stubbing every `_setup_*`
        helper. Returns the backend name actually dispatched."""
        chosen: dict[str, str] = {}

        def _claude(path: str) -> bool:
            chosen["selected"] = "claude"
            return True

        def _codex(path: str, **kwargs) -> bool:
            chosen["selected"] = "codex"
            return True

        def _hermes(path: str) -> None:
            chosen["selected"] = "hermes"

        runner = CliRunner()
        with (
            patch.object(setup_cmd, "_get_current_backend", return_value=current_backend),
            patch.object(setup_cmd, "_detect_runtimes", return_value=available),
            patch.object(setup_cmd, "_setup_claude", side_effect=_claude),
            patch.object(setup_cmd, "_setup_codex", side_effect=_codex),
            patch.object(setup_cmd, "_setup_hermes", side_effect=_hermes),
        ):
            result = runner.invoke(setup_cmd.app, ["--non-interactive"])
        assert result.exit_code == 0, result.output
        return chosen.get("selected", "")

    def test_prefers_current_backend_over_claude_default(self) -> None:
        """Existing config = codex; multi-runtime; non-interactive
        must keep codex, not silently flip to claude."""
        selected = self._run(
            current_backend="codex",
            available={"claude": "/usr/bin/claude", "codex": "/usr/bin/codex"},
        )
        assert selected == "codex"

    def test_falls_back_to_claude_when_no_current_backend(self) -> None:
        """First-install scenario (no persisted backend) keeps the
        existing claude default for unattended pipe-mode flows."""
        selected = self._run(
            current_backend=None,
            available={"claude": "/usr/bin/claude", "codex": "/usr/bin/codex"},
        )
        assert selected == "claude"

    def test_falls_back_to_first_available_when_claude_missing(self) -> None:
        """No persisted backend, no claude on PATH — pick the first
        available CLI deterministically (codex here)."""
        selected = self._run(
            current_backend=None,
            available={"codex": "/usr/bin/codex", "hermes": "/usr/bin/hermes"},
        )
        assert selected == "codex"

    def test_claude_activation_failure_propagates_nonzero(self) -> None:
        runner = CliRunner()
        with (
            patch.object(setup_cmd, "_get_current_backend", return_value="codex"),
            patch.object(
                setup_cmd,
                "_detect_runtimes",
                return_value={"claude": "/usr/bin/claude"},
            ),
            patch.object(setup_cmd, "_setup_claude", return_value=False),
        ):
            result = runner.invoke(
                setup_cmd.app,
                ["--runtime", "claude", "--non-interactive"],
            )

        assert result.exit_code == 1

    def test_claude_cli_activation_failure_propagates_nonzero(self) -> None:
        runner = CliRunner()
        with (
            patch.object(setup_cmd, "_get_current_backend", return_value="codex"),
            patch.object(
                setup_cmd,
                "_detect_runtimes",
                return_value={"claude": "/usr/bin/claude"},
            ),
            patch.object(setup_cmd, "_setup_claude_cli", return_value=False),
        ):
            result = runner.invoke(
                setup_cmd.app,
                ["--runtime", "claude-cli", "--non-interactive"],
            )

        assert result.exit_code == 1


class TestSourceTreeDetection:
    """Tests for `_is_source_tree_ouroboros_build`.

    Every other case in this file mocks this function out, so its real
    behaviour had no direct coverage. It gates which Codex MCP command block
    setup writes, so a misread of `pyproject.toml` silently downgrades a dev
    install back to the published release.
    """

    @staticmethod
    def _build_tree(root: Path, pyproject_body: str) -> Path:
        """Lay out a fake source tree and return the fake `setup.py` path."""
        (root / "pyproject.toml").write_text(pyproject_body, encoding="utf-8")
        module_dir = root / "src" / "ouroboros" / "cli" / "commands"
        module_dir.mkdir(parents=True)
        module_path = module_dir / "setup.py"
        module_path.write_text("", encoding="utf-8")
        return module_path

    def _detect(self, monkeypatch: pytest.MonkeyPatch, root: Path, body: str) -> bool:
        module_path = self._build_tree(root, body)
        monkeypatch.setattr(setup_cmd, "__file__", str(module_path))
        return setup_cmd._is_source_tree_ouroboros_build()

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param('[project]\nname = "ouroboros-ai"\n', id="canonical"),
            pytest.param('[project]\nname="ouroboros-ai"\n', id="no-spaces"),
            pytest.param("[project]\nname = 'ouroboros-ai'\n", id="single-quotes"),
            pytest.param('[project]\nname  =   "ouroboros-ai"\n', id="extra-whitespace"),
            pytest.param('[project]\nname = """ouroboros-ai"""\n', id="multi-line-string"),
        ],
    )
    def test_valid_spellings_of_the_name_are_detected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: str
    ) -> None:
        """TOML does not guarantee one spelling; all of these declare the same name."""
        assert self._detect(monkeypatch, tmp_path, body) is True

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param(
                '[project]\nname = "something-else"\n# name = "ouroboros-ai"\n',
                id="only-in-comment",
            ),
            pytest.param(
                '[project]\nname = "something-else"\n\n[tool.whatever]\nname = "ouroboros-ai"\n',
                id="only-in-tool-table",
            ),
            pytest.param(
                '[project]\nname = "downstream"\ndependencies = ["ouroboros-ai==0.1"]\n',
                id="only-in-dependency-pin",
            ),
        ],
    )
    def test_the_literal_outside_project_name_is_not_detected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: str
    ) -> None:
        """A consumer project that merely depends on Ouroboros is not a source tree."""
        assert self._detect(monkeypatch, tmp_path, body) is False

    def test_malformed_toml_continues_the_walk_instead_of_raising(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Preserves the pre-existing control flow for unreadable metadata."""
        assert self._detect(monkeypatch, tmp_path, "[project\nname = broken") is False

    def test_missing_project_table_is_not_detected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        assert (
            self._detect(monkeypatch, tmp_path, '[tool.poetry]\nname = "ouroboros-ai"\n') is False
        )

    def test_module_outside_the_source_package_is_not_detected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An installed wheel sitting next to a pyproject.toml is not a source tree."""
        self._build_tree(tmp_path, '[project]\nname = "ouroboros-ai"\n')
        elsewhere = tmp_path / "site-packages" / "ouroboros" / "cli" / "commands"
        elsewhere.mkdir(parents=True)
        installed = elsewhere / "setup.py"
        installed.write_text("", encoding="utf-8")

        monkeypatch.setattr(setup_cmd, "__file__", str(installed))
        assert setup_cmd._is_source_tree_ouroboros_build() is False

    def test_real_repository_checkout_is_detected(self) -> None:
        """Guards the fixtures: the actual checkout running these tests must pass."""
        assert setup_cmd._is_source_tree_ouroboros_build() is True
