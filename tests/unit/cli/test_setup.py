"""Unit tests for the setup command."""

import json
from pathlib import Path
from unittest.mock import patch

import yaml

import ouroboros.cli.commands.setup as setup_cmd


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

    def test_setup_codex_removes_legacy_claude_timeout_override(self, tmp_path: Path) -> None:
        """Codex setup should clear the legacy 600s Claude MCP timeout override."""
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
                            "args": ["--from", "ouroboros-ai", "ouroboros", "mcp", "serve"],
                            "timeout": 600,
                        },
                        "other": {
                            "command": "node",
                        },
                    }
                }
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

        claude_mcp = json.loads(claude_config.read_text(encoding="utf-8"))
        assert "timeout" not in claude_mcp["mcpServers"]["ouroboros"]
        assert claude_mcp["mcpServers"]["other"]["command"] == "node"


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
                            "args": ["--from", "ouroboros-ai", "ouroboros", "mcp", "serve"],
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
        assert config_dict["orchestrator"]["runtime_backend"] == "claude"
        assert config_dict["llm"]["backend"] == "claude"
