"""Unit tests for the setup command."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tomllib
from unittest.mock import AsyncMock, patch

import pytest
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
from ouroboros.codex import CodexArtifactInstallResult
from ouroboros.config._model_defaults import DEFAULT_OPUS_MODEL
from ouroboros.config.models import OuroborosConfig, get_default_config
from ouroboros.providers.base import CompletionConfig
from ouroboros.providers.profiles import resolve_completion_profile

# ── Codex setup tests ────────────────────────────────────────────


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
        assert 'args = ["--from", "ouroboros-ai[mcp]", "ouroboros", "mcp", "serve"]' in contents

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
        """Earlier setup-owned direct executable configs should not become stuck."""
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
                    ('args = ["mcp", "serve", "--runtime", "codex", "--llm-backend", "codex"]'),
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
        """Generated legacy defaults are not user pins that suppress Codex roles."""
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
        role_profiles = config_dict["llm_role_profiles"]
        assert role_profiles == setup_cmd._CODEX_DEFAULT_LLM_ROLE_PROFILES
        for profile_name in set(role_profiles.values()):
            effort = config_dict["llm_profiles"][profile_name]["providers"]["codex"][
                "reasoning_effort"
            ]
            assert effort in {"low", "medium", "high", "xhigh"}

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

    def test_legacy_claude_mcp_registration_shim_is_fail_closed(self, tmp_path: Path) -> None:
        """Older plugin callers cannot reactivate the incompatible MCP path."""
        with patch("pathlib.Path.home", return_value=tmp_path):
            setup_cmd._ensure_claude_mcp_entry()

        assert not (tmp_path / ".claude" / "mcp.json").exists()

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
            setup_cmd._setup_claude("/usr/local/bin/claude")

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
            setup_cmd._setup_claude("/usr/local/bin/claude")

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
            setup_cmd._setup_claude("/usr/local/bin/claude")

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
            setup_cmd._setup_claude("/usr/local/bin/claude")

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
            setup_cmd._setup_claude("/usr/local/bin/claude")

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
            setup_cmd._setup_claude("/usr/local/bin/claude")

        # File should not be rewritten when nothing changed
        assert claude_config.stat().st_mtime == mtime_before


class TestIsolatedMCPLaunchers:
    """MCP 2 registrations must never inherit an arbitrary host environment."""

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

    def test_renders_without_error(self, capsys) -> None:
        """Table renders without raising for typical repo data."""
        repos = [
            {"path": "/home/user/proj", "name": "proj", "desc": "A project", "is_default": True},
            {"path": "/home/user/other", "name": "other", "desc": "", "is_default": False},
        ]
        # Should not raise
        _display_repos_table(repos)

    def test_renders_empty_list(self) -> None:
        """Empty list renders without error."""
        _display_repos_table([])

    def test_renders_without_default_column(self) -> None:
        """Can hide the default column."""
        repos = [{"path": "/p", "name": "n", "desc": "d", "is_default": False}]
        _display_repos_table(repos, show_default=False)


class TestPromptRepoSelection:
    """Tests for _prompt_repo_selection interactive input."""

    def test_valid_number_selection(self) -> None:
        """Selecting a valid number returns 0-based index."""
        repos = [
            {"path": "/a", "name": "a"},
            {"path": "/b", "name": "b"},
            {"path": "/c", "name": "c"},
        ]
        with patch("ouroboros.cli.commands.setup.Prompt.ask", return_value="2"):
            result = _prompt_repo_selection(repos)
        assert result == 1  # 0-based

    def test_skip_returns_none(self) -> None:
        """Typing 'skip' returns None."""
        repos = [{"path": "/a", "name": "a"}]
        with patch("ouroboros.cli.commands.setup.Prompt.ask", return_value="skip"):
            result = _prompt_repo_selection(repos)
        assert result is None

    def test_invalid_input_returns_none(self) -> None:
        """Invalid input (non-number) returns None."""
        repos = [{"path": "/a", "name": "a"}]
        with patch("ouroboros.cli.commands.setup.Prompt.ask", return_value="abc"):
            result = _prompt_repo_selection(repos)
        assert result is None

    def test_out_of_range_returns_none(self) -> None:
        """Number out of range returns None."""
        repos = [{"path": "/a", "name": "a"}]
        with patch("ouroboros.cli.commands.setup.Prompt.ask", return_value="5"):
            result = _prompt_repo_selection(repos)
        assert result is None

    def test_first_repo_selection(self) -> None:
        """Selecting 1 returns index 0."""
        repos = [{"path": "/a", "name": "a"}, {"path": "/b", "name": "b"}]
        with patch("ouroboros.cli.commands.setup.Prompt.ask", return_value="1"):
            result = _prompt_repo_selection(repos)
        assert result == 0


# ── Brownfield async core logic tests ─────────────────────────────


class TestScanAndRegisterRepos:
    """Tests for _scan_and_register_repos async function."""

    @pytest.mark.asyncio
    async def test_returns_repo_dicts(self) -> None:
        """Returns list of dicts from scan_and_register."""
        from ouroboros.persistence.brownfield import BrownfieldRepo

        mock_repos = [
            BrownfieldRepo(path="/home/user/proj", name="proj", desc="A project", is_default=True),
            BrownfieldRepo(path="/home/user/lib", name="lib", desc="", is_default=False),
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

        assert len(result) == 2
        assert result[0]["name"] == "proj"
        assert result[0]["is_default"] is True
        assert result[1]["name"] == "lib"
        assert result[1]["desc"] == ""

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
        assert result[1]["desc"] == ""
        assert result[1]["is_default"] is False

    @pytest.mark.asyncio
    async def test_store_closed_even_on_scan_error(self) -> None:
        """Store is closed even if scan_and_register raises."""
        mock_store = AsyncMock()
        mock_store.initialize = AsyncMock()
        mock_store.close = AsyncMock()

        with (
            patch(
                "ouroboros.cli.commands.setup.BrownfieldStore",
                return_value=mock_store,
            ),
            patch(
                "ouroboros.cli.commands.setup.scan_and_register",
                new_callable=AsyncMock,
                side_effect=RuntimeError("DB locked"),
            ),
        ):
            with pytest.raises(RuntimeError, match="DB locked"):
                await _scan_and_register_repos()

        mock_store.close.assert_awaited_once()

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

    def test_jsonc_comments_preserved(self, tmp_path: Path) -> None:
        """JSONC with line and block comments parses without crashing and preserves non-MCP keys."""
        config_dir = tmp_path / "opencode"
        config_dir.mkdir()
        config_path = config_dir / "opencode.json"
        config_path.write_text(
            '{\n  // line comment\n  /* block comment */\n  "theme": "dark",\n  "mcp": {}\n}\n',
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
        assert "theme" in data
        assert data["theme"] == "dark"
        assert "ouroboros" in data["mcp"]

    def test_jsonc_trailing_commas_preserved(self, tmp_path: Path) -> None:
        """JSONC with trailing commas parses correctly and preserves keys."""
        config_dir = tmp_path / "opencode"
        config_dir.mkdir()
        config_path = config_dir / "opencode.json"
        config_path.write_text(
            '{\n  "editor": "vim",\n  "mcp": {},\n}\n',
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
        assert data["editor"] == "vim"
        assert "ouroboros" in data["mcp"]

    def test_existing_keys_survive_setup(self, tmp_path: Path) -> None:
        """Non-MCP keys like $schema and plugin survive _ensure_opencode_mcp_entry."""
        config_dir = tmp_path / "opencode"
        config_dir.mkdir()
        config_path = config_dir / "opencode.json"
        config_path.write_text(
            json.dumps(
                {"$schema": "https://example.com/schema.json", "plugin": ["foo"], "mcp": {}}
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
        assert data["$schema"] == "https://example.com/schema.json"
        assert data["plugin"] == ["foo"]
        assert "ouroboros" in data["mcp"]

    def test_mcp_as_non_dict_is_replaced(self, tmp_path: Path) -> None:
        """If mcp is a list instead of a dict, setup replaces it with a valid dict."""
        config_dir = tmp_path / "opencode"
        config_dir.mkdir()
        config_path = config_dir / "opencode.json"
        config_path.write_text(
            json.dumps({"mcp": ["invalid"]}),
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
        assert isinstance(data["mcp"], dict)
        assert "ouroboros" in data["mcp"]

    def test_ouroboros_entry_as_non_dict_is_replaced(self, tmp_path: Path) -> None:
        """If mcp.ouroboros is a string, setup replaces it with a proper entry."""
        config_dir = tmp_path / "opencode"
        config_dir.mkdir()
        config_path = config_dir / "opencode.json"
        config_path.write_text(
            json.dumps({"mcp": {"ouroboros": "disabled"}}),
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
        assert isinstance(data["mcp"]["ouroboros"], dict)
        assert data["mcp"]["ouroboros"]["type"] == "local"

    def test_quoted_slashes_in_config_values_survive(self, tmp_path: Path) -> None:
        """URLs and patterns containing // or /* */ inside values are preserved."""
        config_dir = tmp_path / "opencode"
        config_dir.mkdir()
        config_path = config_dir / "opencode.json"
        config_path.write_text(
            '{\n  "$schema": "https://opencode.ai/config.json",\n  "mcp": {}\n}\n',
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
        assert data["$schema"] == "https://opencode.ai/config.json"
        assert "ouroboros" in data["mcp"]

    def test_environment_as_string_is_replaced(self, tmp_path: Path) -> None:
        """If mcp.ouroboros.environment is a string, setup replaces it with a valid dict."""
        config_dir = tmp_path / "opencode"
        config_dir.mkdir()
        config_path = config_dir / "opencode.json"
        config_path.write_text(
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
        env = data["mcp"]["ouroboros"]["environment"]
        assert isinstance(env, dict)

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

    def test_command_as_bare_string_replaced_with_array(self, tmp_path: Path) -> None:
        """A hand-edited command: "ouroboros" string must be replaced with array."""
        config_dir = tmp_path / "opencode"
        config_dir.mkdir()
        config_path = config_dir / "opencode.json"
        config_path.write_text(
            json.dumps(
                {
                    "mcp": {
                        "ouroboros": {
                            "type": "local",
                            "command": "ouroboros mcp serve",
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

    def test_empty_list_command_replaced(self, tmp_path: Path) -> None:
        """An empty command array must be replaced with the detected launcher."""
        config_dir = tmp_path / "opencode"
        config_dir.mkdir()
        config_path = config_dir / "opencode.json"
        config_path.write_text(
            json.dumps(
                {
                    "mcp": {
                        "ouroboros": {
                            "type": "local",
                            "command": [],
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
        assert data["mcp"]["ouroboros"]["command"] == ["ouroboros", "mcp", "serve"]

    def test_non_string_first_element_replaced(self, tmp_path: Path) -> None:
        """A command array with non-string first element must be replaced."""
        config_dir = tmp_path / "opencode"
        config_dir.mkdir()
        config_path = config_dir / "opencode.json"
        config_path.write_text(
            json.dumps(
                {
                    "mcp": {
                        "ouroboros": {
                            "type": "local",
                            "command": [123, "mcp", "serve"],
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
        assert data["mcp"]["ouroboros"]["command"] == ["ouroboros", "mcp", "serve"]

    def test_none_first_element_replaced(self, tmp_path: Path) -> None:
        """A command array with null first element must be replaced."""
        config_dir = tmp_path / "opencode"
        config_dir.mkdir()
        config_path = config_dir / "opencode.json"
        config_path.write_text(
            json.dumps(
                {
                    "mcp": {
                        "ouroboros": {
                            "type": "local",
                            "command": [None, "mcp", "serve"],
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

    def test_mode_plugin_persisted(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, "plugin")
        assert result["orchestrator"]["opencode_mode"] == "plugin"
        # Plugin mode sets runtime_backend=opencode so the MCP server's
        # should_dispatch_via_plugin() gate recognises the OpenCode context.
        assert result["orchestrator"]["runtime_backend"] == "opencode"

    def test_mode_subprocess_persisted(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, "subprocess")
        assert result["orchestrator"]["opencode_mode"] == "subprocess"
        assert result["orchestrator"]["runtime_backend"] == "opencode"


# ── JSONC config file detection tests ────────────────────────────


class TestFindOpencodeConfig:
    """Tests for _find_opencode_config — .jsonc/.json detection logic.

    Patches ``opencode_config_dir`` directly so tests are platform-agnostic
    (no reliance on Linux-specific ``~/.config/opencode`` paths).
    """

    _OCD = "ouroboros.cli.opencode_config.opencode_config_dir"

    def test_prefers_jsonc_over_json(self, tmp_path: Path) -> None:
        """When both opencode.jsonc and opencode.json exist, .jsonc wins."""
        config_dir = tmp_path / "opencode"
        config_dir.mkdir()
        (config_dir / "opencode.jsonc").write_text("{}", encoding="utf-8")
        (config_dir / "opencode.json").write_text("{}", encoding="utf-8")

        with patch(self._OCD, return_value=config_dir):
            result = _find_opencode_config()

        assert result.name == "opencode.jsonc"

    def test_falls_back_to_json(self, tmp_path: Path) -> None:
        """When only opencode.json exists, it is returned."""
        config_dir = tmp_path / "opencode"
        config_dir.mkdir()
        (config_dir / "opencode.json").write_text("{}", encoding="utf-8")

        with patch(self._OCD, return_value=config_dir):
            result = _find_opencode_config()

        assert result.name == "opencode.json"

    def test_returns_json_default_when_neither_exists(self, tmp_path: Path) -> None:
        """When no config exists, returns opencode.json as default for creation."""
        config_dir = tmp_path / "opencode"
        config_dir.mkdir()

        with patch(self._OCD, return_value=config_dir):
            result = _find_opencode_config()

        assert result.name == "opencode.json"
        assert not result.exists()

    def test_only_jsonc_exists(self, tmp_path: Path) -> None:
        """When only opencode.jsonc exists, it is returned."""
        config_dir = tmp_path / "opencode"
        config_dir.mkdir()
        (config_dir / "opencode.jsonc").write_text("{}", encoding="utf-8")

        with patch(self._OCD, return_value=config_dir):
            result = _find_opencode_config()

        assert result.name == "opencode.jsonc"


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
        data = json.loads(jsonc_path.read_text(encoding="utf-8"))
        assert "ouroboros" in data["mcp"]
        assert data["theme"] == "dark"
        assert not (config_dir / "opencode.json").exists()

    def test_setup_does_not_create_json_when_jsonc_exists(self, tmp_path: Path) -> None:
        """No stray opencode.json should be created when .jsonc is present."""
        config_dir = tmp_path / "opencode"
        config_dir.mkdir()
        jsonc_path = config_dir / "opencode.jsonc"
        jsonc_path.write_text('{"mcp": {}}', encoding="utf-8")

        with (
            patch(self._OCD, return_value=config_dir),
            patch(
                "ouroboros.cli.commands.setup._detect_opencode_mcp_command",
                return_value={"command": ["ouroboros", "mcp", "serve"]},
            ),
        ):
            _ensure_opencode_mcp_entry()

        assert jsonc_path.exists()
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


class TestAntigravitySetup:
    """Tests for the runtime-only Antigravity (agy) setup path."""

    def test_setup_antigravity_writes_runtime_only_config(self, tmp_path: Path) -> None:
        """Setup records the runtime + CLI path but leaves llm.backend alone
        (antigravity is runtime-only), and the written config validates."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text("{}", encoding="utf-8")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
        ):
            setup_cmd._setup_antigravity("/opt/bin/agy")

        data = yaml.safe_load((config_dir / "config.yaml").read_text(encoding="utf-8"))
        assert data["orchestrator"]["runtime_backend"] == "antigravity"
        assert data["orchestrator"]["antigravity_cli_path"] == "/opt/bin/agy"
        # Runtime-only: the completion-only llm.backend is never set to it.
        assert data.get("llm", {}).get("backend") != "antigravity"
        # The persisted config must round-trip through schema validation.
        from ouroboros.config.models import OuroborosConfig

        OuroborosConfig.model_validate(data)

    def test_detect_runtimes_includes_antigravity(self) -> None:
        with (
            patch(
                "ouroboros.cli.commands.setup.shutil.which",
                side_effect=lambda name: "/usr/bin/agy" if name == "agy" else None,
            ),
            patch("ouroboros.config.get_antigravity_cli_path", return_value=None),
        ):
            detected = setup_cmd._detect_runtimes()

        assert detected["antigravity"] == "/usr/bin/agy"

    def test_setup_runtime_antigravity_dispatches_not_unsupported(self, tmp_path: Path) -> None:
        """`setup --runtime antigravity --non-interactive` configures the backend
        rather than failing with 'Unsupported runtime' (the prior contract gap)."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text("{}", encoding="utf-8")
        runner = CliRunner()
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.commands.setup._detect_runtimes",
                return_value={"antigravity": "/opt/bin/agy"},
            ),
        ):
            result = runner.invoke(setup_cmd.app, ["--runtime", "antigravity", "--non-interactive"])

        assert "Unsupported runtime" not in result.output
        assert result.exit_code == 0, result.output
        data = yaml.safe_load((config_dir / "config.yaml").read_text(encoding="utf-8"))
        assert data["orchestrator"]["runtime_backend"] == "antigravity"


class TestGrokSetup:
    """Tests for the runtime-only Grok Build (grok) setup path."""

    def test_setup_grok_writes_runtime_only_config(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text("{}", encoding="utf-8")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
        ):
            setup_cmd._setup_grok("/opt/bin/grok")

        data = yaml.safe_load((config_dir / "config.yaml").read_text(encoding="utf-8"))
        assert data["orchestrator"]["runtime_backend"] == "grok"
        assert data["orchestrator"]["grok_cli_path"] == "/opt/bin/grok"
        assert data.get("llm", {}).get("backend") != "grok"
        from ouroboros.config.models import OuroborosConfig

        OuroborosConfig.model_validate(data)

    def test_detect_runtimes_includes_grok(self) -> None:
        with (
            patch(
                "ouroboros.cli.commands.setup.shutil.which",
                side_effect=lambda name: "/usr/bin/grok" if name == "grok" else None,
            ),
            patch("ouroboros.config.get_grok_cli_path", return_value=None),
        ):
            detected = setup_cmd._detect_runtimes()

        assert detected["grok"] == "/usr/bin/grok"

    def test_setup_runtime_grok_dispatches_not_unsupported(self, tmp_path: Path) -> None:
        """`setup --runtime grok --non-interactive` configures the backend rather
        than failing with 'Unsupported runtime' (the prior contract gap)."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text("{}", encoding="utf-8")
        runner = CliRunner()
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.commands.setup._detect_runtimes",
                return_value={"grok": "/opt/bin/grok"},
            ),
        ):
            result = runner.invoke(setup_cmd.app, ["--runtime", "grok", "--non-interactive"])

        assert "Unsupported runtime" not in result.output
        assert result.exit_code == 0, result.output
        data = yaml.safe_load((config_dir / "config.yaml").read_text(encoding="utf-8"))
        assert data["orchestrator"]["runtime_backend"] == "grok"


class TestZcodeSetup:
    """Tests for the runtime-only Zcode setup path."""

    def test_setup_zcode_writes_runtime_only_config(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text("{}", encoding="utf-8")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
        ):
            setup_cmd._setup_zcode("/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs")

        data = yaml.safe_load((config_dir / "config.yaml").read_text(encoding="utf-8"))
        assert data["orchestrator"]["runtime_backend"] == "zcode"
        assert data["orchestrator"]["zcode_cli_path"].endswith("zcode.cjs")
        assert data.get("llm", {}).get("backend") != "zcode"
        from ouroboros.config.models import OuroborosConfig

        OuroborosConfig.model_validate(data)

    def test_detect_runtimes_includes_zcode(self) -> None:
        with (
            patch(
                "ouroboros.cli.commands.setup.shutil.which",
                side_effect=lambda name: "/usr/local/bin/zcode" if name == "zcode" else None,
            ),
            patch("ouroboros.config.get_zcode_cli_path", return_value=None),
        ):
            detected = setup_cmd._detect_runtimes()

        assert detected["zcode"] == "/usr/local/bin/zcode"

    def test_setup_runtime_zcode_dispatches_not_unsupported(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text("{}", encoding="utf-8")
        runner = CliRunner()
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch(
                "ouroboros.cli.commands.setup._detect_runtimes",
                return_value={"zcode": "/usr/local/bin/zcode"},
            ),
        ):
            result = runner.invoke(setup_cmd.app, ["--runtime", "zcode", "--non-interactive"])

        assert "Unsupported runtime" not in result.output
        assert result.exit_code == 0, result.output
        data = yaml.safe_load((config_dir / "config.yaml").read_text(encoding="utf-8"))
        assert data["orchestrator"]["runtime_backend"] == "zcode"


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

    def test_setup_cli_with_runtime_kiro_flag(self, tmp_path: Path) -> None:
        """`ouroboros setup --runtime kiro --non-interactive` runs the kiro
        setup path without requiring user interaction."""
        runner = CliRunner()
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "ouroboros.cli.commands.setup._detect_runtimes",
                return_value={
                    "claude": None,
                    "codex": None,
                    "opencode": None,
                    "hermes": None,
                    "gemini": None,
                    "kiro": "/opt/bin/kiro-cli",
                },
            ),
            patch("ouroboros.cli.commands.setup._setup_kiro") as mock_setup,
        ):
            result = runner.invoke(
                setup_cmd.app,
                ["--runtime", "kiro", "--non-interactive"],
            )

        assert result.exit_code == 0, result.output
        mock_setup.assert_called_once_with("/opt/bin/kiro-cli")

    def test_setup_cli_kiro_missing_binary_errors_cleanly(
        self,
        tmp_path: Path,
    ) -> None:
        """Explicit --runtime kiro with no kiro-cli should exit non-zero
        instead of crashing or silently succeeding."""
        runner = CliRunner()
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "ouroboros.cli.commands.setup._detect_runtimes",
                return_value={
                    "claude": None,
                    "codex": None,
                    "opencode": None,
                    "hermes": None,
                    "gemini": None,
                    "kiro": None,
                },
            ),
        ):
            result = runner.invoke(
                setup_cmd.app,
                ["--runtime", "kiro", "--non-interactive"],
            )

        assert result.exit_code != 0
        assert "Kiro CLI not found" in result.output

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

    def test_setup_copilot_aborts_on_non_mapping_sections(self, tmp_path: Path) -> None:
        """Malformed sections must not be clobbered or crash setup."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        original = yaml.safe_dump(
            {
                "orchestrator": ["keep", "me"],
                "llm": {"backend": "claude_code"},
            },
            sort_keys=False,
        )
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

    def test_setup_copilot_aborts_on_non_mapping_model_sections(self, tmp_path: Path) -> None:
        """Model-default sections are validated before rewrite."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        original = yaml.safe_dump(
            {
                "orchestrator": {"runtime_backend": "claude"},
                "llm": {"backend": "claude_code"},
                "consensus": ["keep", "me"],
            },
            sort_keys=False,
        )
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

    def test_setup_copilot_aborts_on_non_mapping_ouroboros_config(self, tmp_path: Path) -> None:
        """Malformed config.yaml must not be clobbered or partially rewritten."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        original = "- not-a-mapping\n- keep-me\n"
        config_path.write_text(original, encoding="utf-8")

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

        assert config_path.read_text(encoding="utf-8") == original
        mock_register.assert_not_called()

    def test_setup_copilot_warns_when_discovery_used_fallback(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
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
        ):
            setup_cmd._setup_copilot("/opt/bin/copilot", non_interactive=True)

        out = capsys.readouterr().out
        assert "fallback" in out.lower() or "gh auth" in out.lower()

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

    def test_install_pi_ooo_bridge_is_idempotent(self, tmp_path: Path) -> None:
        """The managed Pi bridge should not rewrite an already-current extension."""
        bridge_path = tmp_path / ".pi" / "agent" / "extensions" / "ouroboros-ooo-bridge.ts"

        with patch("pathlib.Path.home", return_value=tmp_path):
            assert setup_cmd._install_pi_ooo_bridge() is True
            first_mtime = bridge_path.stat().st_mtime_ns
            assert setup_cmd._install_pi_ooo_bridge() is True

        assert bridge_path.stat().st_mtime_ns == first_mtime

    def test_setup_cli_with_runtime_pi_flag(self, tmp_path: Path) -> None:
        """`ouroboros setup --runtime pi --non-interactive` runs the Pi setup path."""
        runner = CliRunner()
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "ouroboros.cli.commands.setup._detect_runtimes",
                return_value={
                    "claude": None,
                    "codex": None,
                    "opencode": None,
                    "hermes": None,
                    "gemini": None,
                    "kiro": None,
                    "copilot": None,
                    "pi": "/opt/bin/pi",
                },
            ),
            patch("ouroboros.cli.commands.setup._setup_pi") as mock_setup,
        ):
            result = runner.invoke(
                setup_cmd.app,
                ["--runtime", "pi", "--non-interactive"],
            )

        assert result.exit_code == 0, result.output
        mock_setup.assert_called_once_with("/opt/bin/pi")

    def test_setup_cli_pi_missing_binary_errors_cleanly(self, tmp_path: Path) -> None:
        """Explicit --runtime pi with no pi binary should exit non-zero."""
        runner = CliRunner()
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "ouroboros.cli.commands.setup._detect_runtimes",
                return_value={
                    "claude": None,
                    "codex": None,
                    "opencode": None,
                    "hermes": None,
                    "gemini": None,
                    "kiro": None,
                    "copilot": None,
                    "pi": None,
                },
            ),
        ):
            result = runner.invoke(
                setup_cmd.app,
                ["--runtime", "pi", "--non-interactive"],
            )

        assert result.exit_code != 0
        assert "Pi CLI not found" in result.output

    def test_setup_cli_with_runtime_copilot_flag(self, tmp_path: Path) -> None:
        """`ouroboros setup --runtime copilot --non-interactive` runs the
        copilot setup path without requiring user interaction."""
        runner = CliRunner()
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "ouroboros.cli.commands.setup._detect_runtimes",
                return_value={
                    "claude": None,
                    "codex": None,
                    "opencode": None,
                    "hermes": None,
                    "gemini": None,
                    "kiro": None,
                    "copilot": "/opt/bin/copilot",
                },
            ),
            patch("ouroboros.cli.commands.setup._setup_copilot") as mock_setup,
        ):
            result = runner.invoke(
                setup_cmd.app,
                ["--runtime", "copilot", "--non-interactive"],
            )

        assert result.exit_code == 0, result.output
        mock_setup.assert_called_once_with("/opt/bin/copilot", non_interactive=True)

    def test_setup_cli_copilot_missing_binary_errors_cleanly(self, tmp_path: Path) -> None:
        """Explicit --runtime copilot with no copilot binary should exit non-zero."""
        runner = CliRunner()
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "ouroboros.cli.commands.setup._detect_runtimes",
                return_value={
                    "claude": None,
                    "codex": None,
                    "opencode": None,
                    "hermes": None,
                    "gemini": None,
                    "kiro": None,
                    "copilot": None,
                },
            ),
        ):
            result = runner.invoke(
                setup_cmd.app,
                ["--runtime", "copilot", "--non-interactive"],
            )

        assert result.exit_code != 0
        assert "Copilot CLI not found" in result.output


class TestNonInteractiveAutoSelect:
    """`ouroboros setup --non-interactive` runtime auto-selection."""

    def _run(self, *, current_backend: str | None, available: dict[str, str]) -> str:
        """Invoke `setup --non-interactive` (no --runtime) and capture which
        backend the auto-select branch chose, by stubbing every `_setup_*`
        helper. Returns the backend name actually dispatched."""
        chosen: dict[str, str] = {}

        def _claude(path: str) -> None:
            chosen["selected"] = "claude"

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
