"""Unit tests for the setup command."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml

import ouroboros.cli.commands.setup as setup_cmd
from ouroboros.cli.commands.setup import (
    _display_repos_table,
    _ensure_opencode_mcp_entry,
    _find_opencode_config,
    _list_repos,
    _prompt_repo_selection,
    _scan_and_register_repos,
    _set_default_repo,
)

# ── Codex setup tests ────────────────────────────────────────────


class TestCodexSetup:
    """Tests for Codex-specific setup behavior."""

    def test_register_codex_mcp_server_writes_guidance_comment(self, tmp_path: Path) -> None:
        """The generated Codex config should explain the config file split."""
        with patch("pathlib.Path.home", return_value=tmp_path):
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

    def test_register_codex_mcp_server_rewrites_existing_block_without_timeout(
        self,
        tmp_path: Path,
    ) -> None:
        """Re-running setup should replace legacy Codex blocks instead of skipping them."""
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
        assert 'OUROBOROS_AGENT_RUNTIME = "codex"' in contents
        assert 'OUROBOROS_LLM_BACKEND = "codex"' in contents
        assert "tool_timeout_sec" not in contents

    def test_install_codex_artifacts_installs_rules_and_skills(self, tmp_path: Path) -> None:
        """Codex setup should install both managed rules and managed skills."""
        rules_path = tmp_path / ".codex" / "rules"
        skill_paths = [tmp_path / ".codex" / "skills" / "evaluate"]

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.codex.install_codex_rules", return_value=rules_path) as mock_rules,
            patch("ouroboros.codex.install_codex_skills", return_value=skill_paths) as mock_skills,
            patch("ouroboros.cli.commands.setup.print_success") as mock_success,
        ):
            setup_cmd._install_codex_artifacts()

        mock_rules.assert_called_once()
        mock_skills.assert_called_once()
        success_messages = [call.args[0] for call in mock_success.call_args_list]
        assert any("Installed Codex rules" in message for message in success_messages)
        assert any("Installed 1 Codex skills" in message for message in success_messages)

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
            patch("ouroboros.cli.commands.setup.print_info") as mock_info,
        ):
            setup_cmd._setup_codex("/usr/local/bin/codex")

        config_dict = yaml.safe_load(config_path.read_text(encoding="utf-8"))

        assert config_dict["orchestrator"]["runtime_backend"] == "codex"
        assert config_dict["orchestrator"]["codex_cli_path"] == "/usr/local/bin/codex"
        assert config_dict["llm"]["backend"] == "codex"
        mock_install.assert_called_once_with()
        mock_register.assert_called_once_with()

        info_messages = [call.args[0] for call in mock_info.call_args_list]
        assert any("Config saved to" in message for message in info_messages)
        assert any("Configure Ouroboros runtime" in message for message in info_messages)
        assert any("Codex MCP/env hookup" in message for message in info_messages)

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


class TestClaudeSetup:
    """Tests for Claude-specific setup behavior."""

    def test_setup_claude_removes_legacy_timeout_override(self, tmp_path: Path) -> None:
        """Claude setup should no longer persist the legacy 600s MCP timeout."""
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
        ):
            setup_cmd._setup_claude("/usr/local/bin/claude")

        claude_mcp = json.loads(claude_config.read_text(encoding="utf-8"))
        config_dict = yaml.safe_load(config_path.read_text(encoding="utf-8"))

        assert "timeout" not in claude_mcp["mcpServers"]["ouroboros"]
        # Stale args (ouroboros-ai without [claude]) should be updated
        assert claude_mcp["mcpServers"]["ouroboros"]["args"] == [
            "--from",
            "ouroboros-ai[mcp,claude]",
            "ouroboros",
            "mcp",
            "serve",
        ]
        assert config_dict["orchestrator"]["runtime_backend"] == "claude"
        assert config_dict["llm"]["backend"] == "claude"

    @pytest.mark.parametrize(
        "which_side_effect, expected_cmd, expected_args",
        [
            # uvx available → uvx entry with [claude] extras
            (
                lambda cmd: "/usr/local/bin/uvx" if cmd == "uvx" else None,
                "uvx",
                ["--from", "ouroboros-ai[mcp,claude]", "ouroboros", "mcp", "serve"],
            ),
            # no uvx, ouroboros binary available → binary entry
            (
                lambda cmd: "/usr/local/bin/ouroboros" if cmd == "ouroboros" else None,
                "ouroboros",
                ["mcp", "serve"],
            ),
            # no uvx, no binary → python3 -m fallback
            (
                lambda _cmd: None,
                "python3",
                ["-m", "ouroboros", "mcp", "serve"],
            ),
        ],
        ids=["uvx", "pipx-binary", "pip-fallback"],
    )
    def test_setup_claude_creates_new_entry_per_install_method(
        self,
        tmp_path: Path,
        which_side_effect,
        expected_cmd: str,
        expected_args: list[str],
    ) -> None:
        """New MCP entry command/args should match the detected install method."""
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
        ):
            setup_cmd._setup_claude("/usr/local/bin/claude")

        claude_mcp = json.loads(claude_config.read_text(encoding="utf-8"))
        entry = claude_mcp["mcpServers"]["ouroboros"]
        assert entry["command"] == expected_cmd
        assert entry["args"] == expected_args

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
        ):
            setup_cmd._setup_claude("/usr/local/bin/claude")

        claude_mcp = json.loads(claude_config.read_text(encoding="utf-8"))
        # Custom command (docker) should be left untouched
        assert claude_mcp["mcpServers"]["ouroboros"]["command"] == "docker"
        assert claude_mcp["mcpServers"]["ouroboros"]["args"] == custom_args

    def test_setup_claude_updates_stale_standard_entry(self, tmp_path: Path) -> None:
        """Stale standard entry (e.g. python3) should be updated to detected method."""
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
        # Should be updated from python3 to uvx
        assert claude_mcp["mcpServers"]["ouroboros"]["command"] == "uvx"
        assert "ouroboros-ai[mcp,claude]" in str(claude_mcp["mcpServers"]["ouroboros"]["args"])

    def test_setup_claude_skips_write_when_args_already_current(self, tmp_path: Path) -> None:
        """No file write when args are already up to date."""
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text("{}", encoding="utf-8")

        current_args = ["--from", "ouroboros-ai[mcp,claude]", "ouroboros", "mcp", "serve"]
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
        ):
            setup_cmd._setup_claude("/usr/local/bin/claude")

        # File should not be rewritten when nothing changed
        assert claude_config.stat().st_mtime == mtime_before


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
        mock_register.assert_called_once_with()

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

        async def fake_scan(store):
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

        async def capture_store(store):
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
    """Tests for OpenCode JSONC config handling in _ensure_opencode_mcp_entry."""

    def test_jsonc_comments_preserved(self, tmp_path: Path) -> None:
        """JSONC with line and block comments parses without crashing and preserves non-MCP keys."""
        config_dir = tmp_path / ".config" / "opencode"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "opencode.json"
        config_path.write_text(
            '{\n  // line comment\n  /* block comment */\n  "theme": "dark",\n  "mcp": {}\n}\n',
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
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
        config_dir = tmp_path / ".config" / "opencode"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "opencode.json"
        config_path.write_text(
            '{\n  "editor": "vim",\n  "mcp": {},\n}\n',
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
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
        config_dir = tmp_path / ".config" / "opencode"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "opencode.json"
        config_path.write_text(
            json.dumps(
                {"$schema": "https://example.com/schema.json", "plugin": ["foo"], "mcp": {}}
            ),
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
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
        config_dir = tmp_path / ".config" / "opencode"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "opencode.json"
        config_path.write_text(
            json.dumps({"mcp": ["invalid"]}),
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
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
        config_dir = tmp_path / ".config" / "opencode"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "opencode.json"
        config_path.write_text(
            json.dumps({"mcp": {"ouroboros": "disabled"}}),
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
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
        config_dir = tmp_path / ".config" / "opencode"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "opencode.json"
        config_path.write_text(
            '{\n  "$schema": "https://opencode.ai/config.json",\n  "mcp": {}\n}\n',
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
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
        config_dir = tmp_path / ".config" / "opencode"
        config_dir.mkdir(parents=True)
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
            patch("pathlib.Path.home", return_value=tmp_path),
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
        config_dir = tmp_path / ".config" / "opencode"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "opencode.json"
        original_content = '{"theme": "dark", BROKEN JSON HERE}'
        config_path.write_text(original_content, encoding="utf-8")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
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
        config_dir = tmp_path / ".config" / "opencode"
        config_dir.mkdir(parents=True)
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
            patch("pathlib.Path.home", return_value=tmp_path),
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
        config_dir = tmp_path / ".config" / "opencode"
        config_dir.mkdir(parents=True)
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
            patch("pathlib.Path.home", return_value=tmp_path),
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
        config_dir = tmp_path / ".config" / "opencode"
        config_dir.mkdir(parents=True)
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
            patch("pathlib.Path.home", return_value=tmp_path),
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
        config_dir = tmp_path / ".config" / "opencode"
        config_dir.mkdir(parents=True)
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
            patch("pathlib.Path.home", return_value=tmp_path),
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
        config_dir = tmp_path / ".config" / "opencode"
        config_dir.mkdir(parents=True)
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
            patch("pathlib.Path.home", return_value=tmp_path),
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
        config_dir = tmp_path / ".config" / "opencode"
        config_dir.mkdir(parents=True)
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
            patch("pathlib.Path.home", return_value=tmp_path),
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
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._ensure_opencode_mcp_entry"),
            patch("ouroboros.cli.commands.setup._ensure_claude_mcp_entry"),
        ):
            from ouroboros.cli.commands.setup import _setup_opencode

            _setup_opencode("/usr/bin/opencode", mode="subprocess")

        result = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert isinstance(result, dict)
        assert result["orchestrator"]["runtime_backend"] == "opencode"
        assert result["llm"]["backend"] == "opencode"

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
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._ensure_opencode_mcp_entry"),
            patch("ouroboros.cli.commands.setup._ensure_claude_mcp_entry"),
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
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._ensure_opencode_mcp_entry"),
            patch("ouroboros.cli.commands.setup._ensure_opencode_plugin_entry"),
            patch("ouroboros.cli.commands.setup._install_opencode_bridge_plugin"),
            patch("ouroboros.cli.commands.setup._ensure_claude_mcp_entry") as mock_claude,
        ):
            from ouroboros.cli.commands.setup import _setup_opencode

            _setup_opencode("/usr/bin/opencode")

        result = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        mock_claude.assert_not_called()
        assert result["orchestrator"]["opencode_mode"] == "plugin"


class TestOpenCodeModePersisted:
    """_setup_opencode persists orchestrator.opencode_mode in both branches."""

    def _run(self, tmp_path: Path, mode: str) -> dict:
        config_dir = tmp_path / ".ouroboros"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
            patch("ouroboros.cli.commands.setup._ensure_opencode_mcp_entry"),
            patch("ouroboros.cli.commands.setup._ensure_claude_mcp_entry"),
        ):
            from ouroboros.cli.commands.setup import _setup_opencode

            _setup_opencode("/usr/bin/opencode", mode=mode)
        return yaml.safe_load(config_path.read_text(encoding="utf-8"))

    def test_mode_plugin_persisted(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, "plugin")
        assert result["orchestrator"]["opencode_mode"] == "plugin"
        # Plugin branch intentionally does NOT set runtime_backend — plugin
        # runs in-process inside OpenCode; runtime stays at caller default.

    def test_mode_subprocess_persisted(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, "subprocess")
        assert result["orchestrator"]["opencode_mode"] == "subprocess"
        assert result["orchestrator"]["runtime_backend"] == "opencode"


# ── JSONC config file detection tests ────────────────────────────


class TestFindOpencodeConfig:
    """Tests for _find_opencode_config — .jsonc/.json detection logic."""

    def test_prefers_jsonc_over_json(self, tmp_path: Path) -> None:
        """When both opencode.jsonc and opencode.json exist, .jsonc wins."""
        config_dir = tmp_path / ".config" / "opencode"
        config_dir.mkdir(parents=True)
        (config_dir / "opencode.jsonc").write_text("{}", encoding="utf-8")
        (config_dir / "opencode.json").write_text("{}", encoding="utf-8")

        with patch("pathlib.Path.home", return_value=tmp_path):
            result = _find_opencode_config()

        assert result.name == "opencode.jsonc"

    def test_falls_back_to_json(self, tmp_path: Path) -> None:
        """When only opencode.json exists, it is returned."""
        config_dir = tmp_path / ".config" / "opencode"
        config_dir.mkdir(parents=True)
        (config_dir / "opencode.json").write_text("{}", encoding="utf-8")

        with patch("pathlib.Path.home", return_value=tmp_path):
            result = _find_opencode_config()

        assert result.name == "opencode.json"

    def test_returns_json_default_when_neither_exists(self, tmp_path: Path) -> None:
        """When no config exists, returns opencode.json as default for creation."""
        config_dir = tmp_path / ".config" / "opencode"
        config_dir.mkdir(parents=True)

        with patch("pathlib.Path.home", return_value=tmp_path):
            result = _find_opencode_config()

        assert result.name == "opencode.json"
        assert not result.exists()

    def test_only_jsonc_exists(self, tmp_path: Path) -> None:
        """When only opencode.jsonc exists, it is returned."""
        config_dir = tmp_path / ".config" / "opencode"
        config_dir.mkdir(parents=True)
        (config_dir / "opencode.jsonc").write_text("{}", encoding="utf-8")

        with patch("pathlib.Path.home", return_value=tmp_path):
            result = _find_opencode_config()

        assert result.name == "opencode.jsonc"


class TestSetupJsoncDetection:
    """Tests for _ensure_opencode_mcp_entry picking up .jsonc files."""

    def test_setup_reads_existing_jsonc(self, tmp_path: Path) -> None:
        """Setup should read and update an existing opencode.jsonc file."""
        config_dir = tmp_path / ".config" / "opencode"
        config_dir.mkdir(parents=True)
        jsonc_path = config_dir / "opencode.jsonc"
        jsonc_path.write_text(
            '{\n  // user comment\n  "theme": "dark",\n  "mcp": {}\n}\n',
            encoding="utf-8",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
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
        config_dir = tmp_path / ".config" / "opencode"
        config_dir.mkdir(parents=True)
        jsonc_path = config_dir / "opencode.jsonc"
        jsonc_path.write_text('{"mcp": {}}', encoding="utf-8")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "ouroboros.cli.commands.setup._detect_opencode_mcp_command",
                return_value={"command": ["ouroboros", "mcp", "serve"]},
            ),
        ):
            _ensure_opencode_mcp_entry()

        assert jsonc_path.exists()
        assert not (config_dir / "opencode.json").exists()
