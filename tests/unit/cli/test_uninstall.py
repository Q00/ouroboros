"""Unit tests for the uninstall command."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from ouroboros.cli.commands.uninstall import (
    _remove_claude_mcp,
    _remove_claude_md_block,
    _remove_codex_mcp,
    _remove_data_dir,
    _remove_opencode_bridge_plugin,
    app,
)

runner = CliRunner()


# ── _remove_claude_mcp ──────────────────────────────────────────


class TestRemoveClaudeMcp:
    """Tests for _remove_claude_mcp helper."""

    def test_removes_ouroboros_entry(self, tmp_path: Path) -> None:
        mcp_json = tmp_path / ".claude" / "mcp.json"
        mcp_json.parent.mkdir(parents=True)
        mcp_json.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "ouroboros": {"command": "uvx", "args": ["ouroboros", "mcp", "serve"]},
                        "other": {"command": "other"},
                    }
                }
            )
        )

        with patch("pathlib.Path.home", return_value=tmp_path):
            result = _remove_claude_mcp(dry_run=False)

        assert result is True
        data = json.loads(mcp_json.read_text())
        assert "ouroboros" not in data["mcpServers"]
        assert "other" in data["mcpServers"]

    def test_dry_run_does_not_modify(self, tmp_path: Path) -> None:
        mcp_json = tmp_path / ".claude" / "mcp.json"
        mcp_json.parent.mkdir(parents=True)
        original = json.dumps({"mcpServers": {"ouroboros": {"command": "uvx"}}})
        mcp_json.write_text(original)

        with patch("pathlib.Path.home", return_value=tmp_path):
            result = _remove_claude_mcp(dry_run=True)

        assert result is True
        assert mcp_json.read_text() == original

    def test_missing_file_returns_false(self, tmp_path: Path) -> None:
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = _remove_claude_mcp(dry_run=False)
        assert result is False

    def test_no_ouroboros_entry_returns_false(self, tmp_path: Path) -> None:
        mcp_json = tmp_path / ".claude" / "mcp.json"
        mcp_json.parent.mkdir(parents=True)
        mcp_json.write_text(json.dumps({"mcpServers": {"other": {}}}))

        with patch("pathlib.Path.home", return_value=tmp_path):
            result = _remove_claude_mcp(dry_run=False)
        assert result is False

    def test_malformed_json_returns_false(self, tmp_path: Path) -> None:
        mcp_json = tmp_path / ".claude" / "mcp.json"
        mcp_json.parent.mkdir(parents=True)
        mcp_json.write_text("{broken json")

        with patch("pathlib.Path.home", return_value=tmp_path):
            result = _remove_claude_mcp(dry_run=False)
        assert result is False


# ── _remove_codex_mcp ────────────────────────────────────────────


class TestRemoveCodexMcp:
    """Tests for _remove_codex_mcp helper."""

    def test_removes_ouroboros_section(self, tmp_path: Path) -> None:
        codex_config = tmp_path / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text(
            'model = "gpt-5"\n\n'
            "[mcp_servers.ouroboros]\n"
            'command = "uvx"\n\n'
            "[mcp_servers.ouroboros.env]\n"
            'OUROBOROS_AGENT_RUNTIME = "codex"\n\n'
            "[other]\nfoo = 1\n"
        )

        with patch("pathlib.Path.home", return_value=tmp_path):
            result = _remove_codex_mcp(dry_run=False)

        assert result is True
        content = codex_config.read_text()
        assert "[mcp_servers.ouroboros]" not in content
        assert "[other]" in content
        assert 'model = "gpt-5"' in content

    def test_preserves_user_comments_outside_managed_block(self, tmp_path: Path) -> None:
        """User comments outside the ouroboros section are preserved."""
        codex_config = tmp_path / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text(
            "# My custom comment at top\n"
            'model = "gpt-5"\n\n'
            "# Ouroboros MCP hookup for Codex CLI.\n"
            "# Keep Ouroboros runtime settings.\n"
            "\n"
            "[mcp_servers.ouroboros]\n"
            'command = "uvx"\n\n'
            "# Comment inside other section\n"
            "[other]\nfoo = 1\n"
        )

        with patch("pathlib.Path.home", return_value=tmp_path):
            result = _remove_codex_mcp(dry_run=False)

        assert result is True
        content = codex_config.read_text()
        assert "[mcp_servers.ouroboros]" not in content
        assert "Ouroboros MCP hookup" not in content
        assert "# My custom comment at top" in content
        assert "[other]" in content

    def test_managed_comment_block_only_removes_known_prefix(self, tmp_path: Path) -> None:
        """Comment block removal stops at blank lines (non-# lines)."""
        codex_config = tmp_path / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text(
            "# Ouroboros MCP hookup for Codex CLI.\n"
            "# Managed line 2\n"
            "\n"  # blank line breaks comment block
            "# Unrelated user comment\n"
            "[mcp_servers.ouroboros]\n"
            'command = "uvx"\n\n'
            "[other]\nfoo = 1\n"
        )

        with patch("pathlib.Path.home", return_value=tmp_path):
            result = _remove_codex_mcp(dry_run=False)

        assert result is True
        content = codex_config.read_text()
        assert "[mcp_servers.ouroboros]" not in content
        # Blank line broke the comment block, so this user comment is preserved
        assert "# Unrelated user comment" in content

    def test_preserves_trailing_comments_after_ouroboros_section(self, tmp_path: Path) -> None:
        """User comments placed after the ouroboros table should not be deleted."""
        codex_config = tmp_path / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text(
            "[mcp_servers.ouroboros]\n"
            'command = "uvx"\n'
            'args = ["ouroboros"]\n'
            "\n"
            "# User note about the next section\n"
            "[other]\nfoo = 1\n"
        )

        with patch("pathlib.Path.home", return_value=tmp_path):
            result = _remove_codex_mcp(dry_run=False)

        assert result is True
        content = codex_config.read_text()
        assert "[mcp_servers.ouroboros]" not in content
        assert "# User note about the next section" in content
        assert "[other]" in content

    def test_no_ouroboros_returns_false(self, tmp_path: Path) -> None:
        codex_config = tmp_path / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text("[other]\nfoo = 1\n")

        with patch("pathlib.Path.home", return_value=tmp_path):
            result = _remove_codex_mcp(dry_run=False)
        assert result is False


# ── _remove_claude_md_block ──────────────────────────────────────


class TestRemoveClaudeMdBlock:
    """Tests for _remove_claude_md_block helper."""

    def test_removes_ooo_block(self, tmp_path: Path) -> None:
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text(
            "# My Project\n\n"
            "<!-- ooo:START -->\nOuroboros stuff\n<!-- ooo:END -->\n\n"
            "Other content\n"
        )

        result = _remove_claude_md_block(tmp_path, dry_run=False)

        assert result is True
        content = claude_md.read_text()
        assert "ooo:START" not in content
        assert "Other content" in content
        assert "My Project" in content

    def test_no_block_returns_false(self, tmp_path: Path) -> None:
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("# My Project\n")

        result = _remove_claude_md_block(tmp_path, dry_run=False)
        assert result is False

    def test_no_file_returns_false(self, tmp_path: Path) -> None:
        result = _remove_claude_md_block(tmp_path, dry_run=False)
        assert result is False


# ── _remove_data_dir ─────────────────────────────────────────────


class TestRemoveDataDir:
    """Tests for _remove_data_dir helper."""

    def test_removes_directory(self, tmp_path: Path) -> None:
        data_dir = tmp_path / ".ouroboros"
        data_dir.mkdir()
        (data_dir / "config.yaml").write_text("test")

        with patch("pathlib.Path.home", return_value=tmp_path):
            result = _remove_data_dir(dry_run=False)

        assert result is True
        assert not data_dir.exists()

    def test_dry_run_preserves(self, tmp_path: Path) -> None:
        data_dir = tmp_path / ".ouroboros"
        data_dir.mkdir()

        with patch("pathlib.Path.home", return_value=tmp_path):
            result = _remove_data_dir(dry_run=True)

        assert result is True
        assert data_dir.exists()


# ── CLI integration ──────────────────────────────────────────────


class TestUninstallCLI:
    """Integration tests for the uninstall command."""

    def test_nothing_to_remove(self, tmp_path: Path) -> None:
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("pathlib.Path.cwd", return_value=tmp_path),
        ):
            result = runner.invoke(app, [])
        assert result.exit_code == 0
        assert "Nothing to remove" in result.output

    def test_dry_run_no_changes(self, tmp_path: Path) -> None:
        # Create mcp.json with ouroboros
        mcp_dir = tmp_path / ".claude"
        mcp_dir.mkdir()
        mcp_json = mcp_dir / "mcp.json"
        mcp_json.write_text(json.dumps({"mcpServers": {"ouroboros": {}}}))

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("pathlib.Path.cwd", return_value=tmp_path),
        ):
            result = runner.invoke(app, ["--dry-run"])

        assert result.exit_code == 0
        assert "Dry run" in result.output
        # File should still contain ouroboros
        data = json.loads(mcp_json.read_text())
        assert "ouroboros" in data["mcpServers"]

    def test_yes_flag_skips_prompt(self, tmp_path: Path) -> None:
        # Use separate dirs to avoid .ouroboros being both project and home
        home_dir = tmp_path / "home"
        home_dir.mkdir()
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        data_dir = home_dir / ".ouroboros"
        data_dir.mkdir()
        (data_dir / "config.yaml").write_text("test")

        with (
            patch("pathlib.Path.home", return_value=home_dir),
            patch("pathlib.Path.cwd", return_value=project_dir),
        ):
            result = runner.invoke(app, ["-y"])

        assert result.exit_code == 0
        assert "has been removed" in result.output
        assert not data_dir.exists()

    def test_keep_data_preserves_data_dir(self, tmp_path: Path) -> None:
        # Use separate dirs for home and project to avoid path overlap
        home_dir = tmp_path / "home"
        home_dir.mkdir()
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        data_dir = home_dir / ".ouroboros"
        data_dir.mkdir()
        (data_dir / "config.yaml").write_text("test")

        with (
            patch("pathlib.Path.home", return_value=home_dir),
            patch("pathlib.Path.cwd", return_value=project_dir),
        ):
            result = runner.invoke(app, ["--keep-data", "-y"])

        assert result.exit_code == 0
        assert data_dir.exists()


# ── _remove_opencode_bridge_plugin ──────────────────────────────


class TestRemoveOpencodeBridgePlugin:
    """Tests for _remove_opencode_bridge_plugin helper.

    Reviewer suggestion #2: lock the file-precedence behavior to the same
    contract used by setup (opencode_config.py: .jsonc before .json).
    """

    def test_dual_config_prefers_jsonc_over_json(self, tmp_path: Path) -> None:
        """When both opencode.json and opencode.jsonc exist, .jsonc wins.

        This mirrors the precedence in opencode_config.py:find_opencode_config
        which checks .jsonc first. The uninstall must use the same file the
        setup wrote to — otherwise the plugin entry survives uninstall.
        """
        config_dir = tmp_path / "opencode"
        config_dir.mkdir(parents=True)
        plugin_dir = config_dir / "plugins" / "ouroboros-bridge"
        plugin_dir.mkdir(parents=True)
        plugin_path = str(plugin_dir / "ouroboros-bridge.ts")
        (plugin_dir / "ouroboros-bridge.ts").write_text("// bridge")

        # .jsonc has the plugin entry (this is the one setup writes to)
        jsonc = config_dir / "opencode.jsonc"
        jsonc.write_text(json.dumps({"plugin": [plugin_path]}))

        # .json does NOT have the plugin entry (stale / user-managed)
        json_file = config_dir / "opencode.json"
        json_file.write_text(json.dumps({"plugin": []}))

        with (
            patch("ouroboros.cli.commands.uninstall.opencode_config_dir", return_value=config_dir),
            patch("ouroboros.cli.opencode_config.opencode_config_dir", return_value=config_dir),
        ):
            result = _remove_opencode_bridge_plugin(dry_run=False)

        assert result is True
        # .jsonc should have the entry removed
        data = json.loads(jsonc.read_text())
        assert plugin_path not in data["plugin"]
        # .json should be untouched
        data_json = json.loads(json_file.read_text())
        assert data_json["plugin"] == []

    def test_only_json_present(self, tmp_path: Path) -> None:
        """When only opencode.json exists, use it."""
        config_dir = tmp_path / "opencode"
        config_dir.mkdir(parents=True)
        plugin_dir = config_dir / "plugins" / "ouroboros-bridge"
        plugin_dir.mkdir(parents=True)
        plugin_path = str(plugin_dir / "ouroboros-bridge.ts")
        (plugin_dir / "ouroboros-bridge.ts").write_text("// bridge")

        json_file = config_dir / "opencode.json"
        json_file.write_text(json.dumps({"plugin": [plugin_path]}))

        with (
            patch("ouroboros.cli.commands.uninstall.opencode_config_dir", return_value=config_dir),
            patch("ouroboros.cli.opencode_config.opencode_config_dir", return_value=config_dir),
        ):
            result = _remove_opencode_bridge_plugin(dry_run=False)

        assert result is True
        data = json.loads(json_file.read_text())
        assert plugin_path not in data["plugin"]

    def test_no_config_files_returns_true_if_dir_removed(self, tmp_path: Path) -> None:
        """When no config file exists, still returns True if plugin dir was removed."""
        config_dir = tmp_path / "opencode"
        config_dir.mkdir(parents=True)
        plugin_dir = config_dir / "plugins" / "ouroboros-bridge"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "ouroboros-bridge.ts").write_text("// bridge")

        with (
            patch("ouroboros.cli.commands.uninstall.opencode_config_dir", return_value=config_dir),
            patch("ouroboros.cli.opencode_config.opencode_config_dir", return_value=config_dir),
        ):
            result = _remove_opencode_bridge_plugin(dry_run=False)

        assert result is True
        assert not plugin_dir.exists()

    def test_jsonc_with_comments_parsed_correctly(self, tmp_path: Path) -> None:
        """JSONC files with single-line comments should be parsed."""
        config_dir = tmp_path / "opencode"
        config_dir.mkdir(parents=True)
        plugin_dir = config_dir / "plugins" / "ouroboros-bridge"
        plugin_dir.mkdir(parents=True)
        plugin_path = str(plugin_dir / "ouroboros-bridge.ts")

        jsonc = config_dir / "opencode.jsonc"
        jsonc.write_text(f'// OpenCode config\n{{\n  "plugin": ["{plugin_path}"]\n}}\n')

        with (
            patch("ouroboros.cli.commands.uninstall.opencode_config_dir", return_value=config_dir),
            patch("ouroboros.cli.opencode_config.opencode_config_dir", return_value=config_dir),
        ):
            result = _remove_opencode_bridge_plugin(dry_run=False)

        assert result is True
        data = json.loads(jsonc.read_text())  # rewritten without comments
        assert plugin_path not in data.get("plugin", [])
