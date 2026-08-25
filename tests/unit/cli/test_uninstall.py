"""Unit tests for the uninstall command."""

from __future__ import annotations

import json
from pathlib import Path
import tomllib
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

import ouroboros.cli.commands.setup as setup_cmd
from ouroboros.cli.commands.uninstall import (
    _remove_claude_mcp,
    _remove_claude_md_block,
    _remove_codex_artifacts,
    _remove_codex_mcp,
    _remove_data_dir,
    _remove_gjc_artifacts,
    _remove_opencode_bridge_plugin,
    app,
)
from ouroboros.gjc import gjc_ooo_bridge_source_text, install_gjc_skills

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
            'args = ["ouroboros", "mcp", "serve"]\n\n'
            "[mcp_servers.ouroboros.env]\n"
            'OUROBOROS_AGENT_RUNTIME = "codex"\n\n'
            'OUROBOROS_LLM_BACKEND = "codex"\n\n'
            "[other]\nfoo = 1\n"
        )

        with patch("pathlib.Path.home", return_value=tmp_path):
            result = _remove_codex_mcp(dry_run=False)

        assert result is True
        content = codex_config.read_text()
        assert "[mcp_servers.ouroboros]" not in content
        assert "[other]" in content
        assert 'model = "gpt-5"' in content

    def test_preserves_concurrent_codex_edit_before_atomic_removal(self, tmp_path: Path) -> None:
        """Uninstall must reject a stale transform rather than lose an operator edit."""
        from ouroboros.cli.commands import setup as setup_cmd

        codex_config = tmp_path / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text(
            'model = "gpt-5"\n\n'
            "[mcp_servers.ouroboros]\n"
            'command = "uvx"\n'
            'args = ["ouroboros", "mcp", "serve"]\n',
            encoding="utf-8",
        )
        operator_toml = 'model = "operator-edit"\n'
        original_write = setup_cmd._atomic_write_text_if_current_matches

        def _write(
            path: Path,
            content: str,
            expected_current: setup_cmd._PathSnapshot,
            *,
            mode: int | None = None,
        ) -> setup_cmd._PathSnapshot:
            codex_config.write_text(operator_toml, encoding="utf-8")
            return original_write(
                path,
                content,
                expected_current,
                mode=mode,
            )

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "ouroboros.cli.commands.setup._atomic_write_text_if_current_matches",
                side_effect=_write,
            ),
        ):
            result = _remove_codex_mcp(dry_run=False)

        assert result is False
        assert codex_config.read_text(encoding="utf-8") == operator_toml

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
            'args = ["ouroboros", "mcp", "serve"]\n\n'
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

    def test_preserves_following_table_with_trailing_comment_header(
        self,
        tmp_path: Path,
    ) -> None:
        """A valid following table header with a trailing comment stops MCP removal."""
        codex_config = tmp_path / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text(
            "[mcp_servers.ouroboros]\n"
            'command = "uvx"\n'
            'args = ["ouroboros", "mcp", "serve"]\n'
            "\n"
            "[operator] # keep this table\n"
            'value = "preserved"\n',
            encoding="utf-8",
        )

        with patch("pathlib.Path.home", return_value=tmp_path):
            result = _remove_codex_mcp(dry_run=False)

        assert result is True
        content = codex_config.read_text(encoding="utf-8")
        parsed = tomllib.loads(content)

        assert "ouroboros" not in parsed.get("mcp_servers", {})
        assert parsed["operator"]["value"] == "preserved"
        assert "[operator] # keep this table" in content

    def test_removes_managed_inline_mcp_entry(
        self,
        tmp_path: Path,
    ) -> None:
        """Semantic ownership must remove inline parent-table MCP entries too."""
        codex_config = tmp_path / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text(
            "[mcp_servers]\n"
            'other = { command = "other" }\n'
            'ouroboros = { command = "uvx", args = ["--from", '
            '"ouroboros-ai", "ouroboros", "mcp", "serve"] }\n',
            encoding="utf-8",
        )

        with patch("pathlib.Path.home", return_value=tmp_path):
            result = _remove_codex_mcp(dry_run=False)

        content = codex_config.read_text(encoding="utf-8")
        parsed = tomllib.loads(content)

        assert result is True
        assert "ouroboros" not in parsed.get("mcp_servers", {})
        assert parsed["mcp_servers"]["other"]["command"] == "other"
        assert "ouroboros = {" not in content

    def test_removes_managed_root_dotted_mcp_entry(self, tmp_path: Path) -> None:
        """Semantic ownership must remove root dotted MCP assignments too."""
        codex_config = tmp_path / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text(
            'mcp_servers.other = { command = "other" }\n'
            'mcp_servers.ouroboros = { command = "uvx", args = ["--from", '
            '"ouroboros-ai", "ouroboros", "mcp", "serve"] }\n',
            encoding="utf-8",
        )

        with patch("pathlib.Path.home", return_value=tmp_path):
            result = _remove_codex_mcp(dry_run=False)

        content = codex_config.read_text(encoding="utf-8")
        parsed = tomllib.loads(content)

        assert result is True
        assert "ouroboros" not in parsed.get("mcp_servers", {})
        assert parsed["mcp_servers"]["other"]["command"] == "other"
        assert "mcp_servers.ouroboros =" not in content

    def test_removes_managed_multiline_root_dotted_mcp_entry(self, tmp_path: Path) -> None:
        """Semantic ownership must remove whole multiline dotted MCP assignments."""
        codex_config = tmp_path / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text(
            'mcp_servers.other = { command = "other" }\n'
            'mcp_servers.ouroboros.command = "uvx"\n'
            "mcp_servers.ouroboros.args = [\n"
            '  "--from",\n'
            '  "ouroboros-ai",\n'
            '  "ouroboros",\n'
            '  "mcp",\n'
            '  "serve",\n'
            "]\n",
            encoding="utf-8",
        )

        with patch("pathlib.Path.home", return_value=tmp_path):
            result = _remove_codex_mcp(dry_run=False)

        content = codex_config.read_text(encoding="utf-8")
        parsed = tomllib.loads(content)

        assert result is True
        assert "ouroboros" not in parsed.get("mcp_servers", {})
        assert parsed["mcp_servers"]["other"]["command"] == "other"
        assert "--from" not in content

    def test_removes_managed_root_inline_mcp_entry(self, tmp_path: Path) -> None:
        """Semantic ownership must remove root inline MCP mappings too."""
        codex_config = tmp_path / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text(
            'mcp_servers = { other = { command = "other" }, ouroboros = { command = "uvx", '
            'args = ["--from", "ouroboros-ai", "ouroboros", "mcp", "serve"] } }\n',
            encoding="utf-8",
        )

        with patch("pathlib.Path.home", return_value=tmp_path):
            result = _remove_codex_mcp(dry_run=False)

        content = codex_config.read_text(encoding="utf-8")
        parsed = tomllib.loads(content)

        assert result is True
        assert "ouroboros" not in parsed.get("mcp_servers", {})
        assert parsed["mcp_servers"]["other"]["command"] == "other"
        assert "mcp_servers = {" not in content

    def test_removal_preserves_mcp_like_multiline_text(self, tmp_path: Path) -> None:
        """Uninstall removes the real entry without rewriting string contents."""
        codex_config = tmp_path / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        instructions = (
            'operator notes\n[mcp_servers]\nouroboros = { command = "do-not-remove", args = [] }'
        )
        codex_config.write_text(
            'instructions = """operator notes\n[mcp_servers]\n'
            'ouroboros = { command = "do-not-remove", args = [] }"""\n\n'
            '[mcp_servers.ouroboros]\ncommand = "uvx"\n'
            'args = ["--from", "ouroboros-ai", "ouroboros", "mcp", "serve"]\n',
            encoding="utf-8",
        )

        with patch("pathlib.Path.home", return_value=tmp_path):
            result = _remove_codex_mcp(dry_run=False)

        parsed = tomllib.loads(codex_config.read_text(encoding="utf-8"))

        assert result is True
        assert parsed["instructions"] == instructions
        assert "ouroboros" not in parsed.get("mcp_servers", {})

    def test_removal_preserves_repeated_newlines_inside_multiline_text(
        self, tmp_path: Path
    ) -> None:
        """Formatting cleanup must not normalize newlines inside TOML values."""
        codex_config = tmp_path / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        instructions = "first\n\n\nsecond\n"
        codex_config.write_text(
            'instructions = """\nfirst\n\n\nsecond\n"""\n\n'
            '[mcp_servers.ouroboros]\ncommand = "uvx"\n'
            'args = ["--from", "ouroboros-ai", "ouroboros", "mcp", "serve"]\n',
            encoding="utf-8",
        )

        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _remove_codex_mcp(dry_run=False) is True

        parsed = tomllib.loads(codex_config.read_text(encoding="utf-8"))

        assert parsed["instructions"] == instructions

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
            'args = ["ouroboros", "mcp", "serve"]\n\n'
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
            'args = ["ouroboros", "mcp", "serve"]\n'
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

    def test_removes_managed_table_structurally_around_inner_comments(self, tmp_path: Path) -> None:
        """Comments inside the managed table must not leave orphaned TOML keys."""
        codex_config = tmp_path / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text(
            "[mcp_servers.ouroboros]\n"
            'command = "uvx"\n'
            "# keep local context\n"
            'args = ["ouroboros", "mcp", "serve"]\n'
            "\n"
            "[other]\nfoo = 1\n"
        )

        with patch("pathlib.Path.home", return_value=tmp_path):
            result = _remove_codex_mcp(dry_run=False)

        assert result is True
        content = codex_config.read_text()
        assert "[mcp_servers.ouroboros]" not in content
        assert 'args = ["ouroboros", "mcp", "serve"]' not in content
        assert "# keep local context" in content
        assert "[other]" in content
        tomllib.loads(content)

    def test_preserves_user_managed_url_entry(self, tmp_path: Path) -> None:
        """Uninstall must not delete a custom MCP entry setup auto would preserve."""
        codex_config = tmp_path / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        original = (
            '[mcp_servers.ouroboros]\nurl = "http://127.0.0.1:12000/mcp"\n\n[other]\nfoo = 1\n'
        )
        codex_config.write_text(original)

        with patch("pathlib.Path.home", return_value=tmp_path):
            result = _remove_codex_mcp(dry_run=False)

        assert result is False
        assert codex_config.read_text() == original

    def test_no_ouroboros_returns_false(self, tmp_path: Path) -> None:
        codex_config = tmp_path / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text("[other]\nfoo = 1\n")

        with patch("pathlib.Path.home", return_value=tmp_path):
            result = _remove_codex_mcp(dry_run=False)
        assert result is False


# ── _remove_codex_artifacts ──────────────────────────────────────


class TestRemoveCodexArtifacts:
    """Tests for _remove_codex_artifacts helper."""

    def test_removes_namespaced_codex_artifacts(self, tmp_path: Path) -> None:
        rules_dir = tmp_path / ".codex" / "rules"
        skills_dir = tmp_path / ".codex" / "skills"
        rules_dir.mkdir(parents=True)
        skills_dir.mkdir(parents=True)
        (rules_dir / "ouroboros.md").write_text("rules", encoding="utf-8")
        (rules_dir / "ouroboros-extra.md").write_text("extra", encoding="utf-8")
        (rules_dir / "other.md").write_text("other", encoding="utf-8")
        (skills_dir / "ouroboros-interview").mkdir()
        (skills_dir / "ouroboros-run").mkdir()
        (skills_dir / "other").mkdir()

        with patch("pathlib.Path.home", return_value=tmp_path):
            result = _remove_codex_artifacts(dry_run=False)

        assert result is True
        assert not (rules_dir / "ouroboros.md").exists()
        assert (rules_dir / "ouroboros-extra.md").exists()
        assert (rules_dir / "other.md").exists()
        assert not (skills_dir / "ouroboros-interview").exists()
        assert not (skills_dir / "ouroboros-run").exists()
        assert (skills_dir / "other").exists()


# ── _remove_gjc_artifacts ────────────────────────────────────────


class TestRemoveGjcArtifacts:
    def test_removes_managed_skills_and_preserves_other_skills(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent"
        source = tmp_path / "source" / "interview"
        source.mkdir(parents=True)
        (source / "SKILL.md").write_text(
            "---\nname: interview\ndescription: managed\n---\n",
            encoding="utf-8",
        )
        managed = install_gjc_skills(agent_dir=agent_dir, skills_dir=source.parent).skill_paths[0]
        other = agent_dir / "skills" / "my-skill"
        other.mkdir()
        with (
            patch.dict("os.environ", {"GJC_CODING_AGENT_DIR": str(agent_dir)}),
            patch("ouroboros.config.get_gjc_cli_path", return_value=None),
            patch("ouroboros.cli.commands.uninstall.shutil.which", return_value=None),
        ):
            assert _remove_gjc_artifacts(dry_run=False)

        assert not managed.exists()
        assert other.exists()

    def test_preserves_user_managed_mcp(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent"
        mcp_path = agent_dir / "mcp.json"
        mcp_path.parent.mkdir(parents=True)
        custom = {"type": "stdio", "command": "docker", "args": ["run"]}
        mcp_path.write_text(
            json.dumps({"mcpServers": {"ouroboros": custom}}),
            encoding="utf-8",
        )

        with patch.dict("os.environ", {"GJC_CODING_AGENT_DIR": str(agent_dir)}):
            assert not _remove_gjc_artifacts(dry_run=False)

        assert json.loads(mcp_path.read_text(encoding="utf-8"))["mcpServers"]["ouroboros"] == custom

    def test_removes_managed_routing_guide(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent"
        with (
            patch.dict("os.environ", {"GJC_CODING_AGENT_DIR": str(agent_dir)}),
            patch("ouroboros.config.get_gjc_cli_path", return_value=None),
            patch("ouroboros.cli.commands.uninstall.shutil.which", return_value=None),
        ):
            from ouroboros.runtime_instruction_artifacts import install_gjc_instruction_artifact

            guide = install_gjc_instruction_artifact().path
            assert _remove_gjc_artifacts(dry_run=False)

        assert not guide.exists()

    def test_removes_managed_mcp_without_current_launcher(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent"
        mcp_path = agent_dir / "mcp.json"
        mcp_path.parent.mkdir(parents=True)
        bridge_path = agent_dir / "ouroboros" / "mcp-bridge.yaml"
        bridge_path.parent.mkdir(parents=True)
        bridge_path.write_text(
            "# Managed by ouroboros setup --runtime gjc\nmcp_servers: []\n",
            encoding="utf-8",
        )
        managed = {
            "type": "stdio",
            "command": "uvx",
            "args": [
                "--isolated",
                "--python",
                ">=3.12",
                "--from",
                "ouroboros-ai[mcp]",
                "ouroboros",
                "mcp",
                "serve",
                "--runtime",
                "gjc",
            ],
            "env": {"OUROBOROS_MCP_CONFIG": str(bridge_path)},
            "sharing": "per-session",
            "timeout": 30000,
        }
        sibling = {"type": "stdio", "command": "other", "args": []}
        mcp_path.write_text(
            json.dumps({"mcpServers": {"ouroboros": managed, "other": sibling}}),
            encoding="utf-8",
        )

        with patch.dict("os.environ", {"GJC_CODING_AGENT_DIR": str(agent_dir)}):
            assert _remove_gjc_artifacts(dry_run=False)

        payload = json.loads(mcp_path.read_text(encoding="utf-8"))
        assert payload["mcpServers"] == {"other": sibling}
        assert not bridge_path.exists()

    def test_preserves_symlinked_mcp_config(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent"
        external = tmp_path / "operator-mcp.json"
        agent_dir.mkdir()
        mcp_path = agent_dir / "mcp.json"
        external.write_text('{"mcpServers": {"ouroboros": {}}}\n', encoding="utf-8")
        try:
            mcp_path.symlink_to(external)
        except OSError:
            pytest.skip("symlinks are not supported on this platform")

        from ouroboros.gjc import remove_persisted_gjc_mcp_server

        before = external.read_text(encoding="utf-8")
        assert not remove_persisted_gjc_mcp_server(mcp_path)
        assert external.read_text(encoding="utf-8") == before

    def test_persistent_cleanup_rejects_changed_generation(self, tmp_path: Path) -> None:
        from ouroboros.gjc import mcp as gjc_mcp

        mcp_path = tmp_path / "mcp.json"
        original = '{"mcpServers": {"ouroboros": {}}}\n'
        operator = '{"mcpServers": {"operator": {"command": "keep"}}}\n'
        mcp_path.write_text(operator, encoding="utf-8")

        with pytest.raises(OSError, match="changed concurrently"):
            gjc_mcp._atomic_replace_json(mcp_path, {"mcpServers": {}}, original)

        assert mcp_path.read_text(encoding="utf-8") == operator

    def test_disabled_setup_shaped_mcp_is_operator_owned(self, tmp_path: Path) -> None:
        from ouroboros.gjc import persisted_gjc_mcp_entry

        mcp_path = tmp_path / "mcp.json"
        mcp_path.write_text(
            json.dumps(
                {
                    "disabledServers": ["ouroboros"],
                    "mcpServers": {"ouroboros": {"command": "uvx"}},
                }
            ),
            encoding="utf-8",
        )

        assert persisted_gjc_mcp_entry(mcp_path) is None

    def test_removes_managed_compatibility_bridge(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent"
        bridge = agent_dir / "extensions" / "ouroboros-ooo-bridge" / "index.ts"
        bridge.parent.mkdir(parents=True)
        bridge.write_text(
            gjc_ooo_bridge_source_text("ouroboros", []),
            encoding="utf-8",
        )
        with (
            patch.dict("os.environ", {"GJC_CODING_AGENT_DIR": str(agent_dir)}),
            patch("ouroboros.config.get_gjc_cli_path", return_value=None),
            patch("ouroboros.cli.commands.uninstall.shutil.which", return_value=None),
        ):
            assert _remove_gjc_artifacts(dry_run=False)

        assert not bridge.exists()

    def test_preserves_modified_compatibility_bridge(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent"
        bridge = agent_dir / "extensions" / "ouroboros-ooo-bridge" / "index.ts"
        bridge.parent.mkdir(parents=True)
        bridge.write_text(
            gjc_ooo_bridge_source_text("ouroboros", []).replace(
                "6 * 60 * 60 * 1000", "30 * 1000", 1
            ),
            encoding="utf-8",
        )

        with patch.dict("os.environ", {"GJC_CODING_AGENT_DIR": str(agent_dir)}):
            from ouroboros.cli.gjc_setup import remove_legacy_gjc_bridge

            assert remove_legacy_gjc_bridge()

        assert bridge.exists()
        assert "30 * 1000" in bridge.read_text(encoding="utf-8")

    def test_bridge_config_only_failure_surfaces_in_cli_summary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed bridge-config removal must fail the command, not report success."""
        agent_dir = tmp_path / "agent"
        bridge_config = agent_dir / "ouroboros" / "mcp-bridge.yaml"
        bridge_config.parent.mkdir(parents=True)
        bridge_config.write_text(
            "# Managed by ouroboros setup --runtime gjc\nmcp_servers: []\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("GJC_CODING_AGENT_DIR", str(agent_dir))

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("pathlib.Path.cwd", return_value=tmp_path),
            patch(
                "ouroboros.cli.commands.uninstall._claim_remove_owned_file",
                return_value=False,
            ),
        ):
            result = runner.invoke(app, ["--yes", "--keep-data"])

        assert result.exit_code == 1
        assert "partially removed" in result.output
        assert bridge_config.exists()

    def test_preserves_guide_replaced_after_discovery(self, tmp_path: Path) -> None:
        """A stale ownership observation must not delete an operator file that
        replaced the guide between discovery and the destructive removal."""
        from ouroboros.gjc import is_setup_managed_gjc_instruction as real_check

        agent_dir = tmp_path / "agent"
        guide = agent_dir / "rules" / "ouroboros-skill-capability-guide.md"
        guide.parent.mkdir(parents=True)
        guide.write_text("operator rules\n", encoding="utf-8")

        def stale_for_canonical_path(path: Path) -> bool:
            if Path(path) == guide:
                return True
            return real_check(path)

        with (
            patch.dict("os.environ", {"GJC_CODING_AGENT_DIR": str(agent_dir)}),
            patch("ouroboros.config.get_gjc_cli_path", return_value=None),
            patch("ouroboros.cli.commands.uninstall.shutil.which", return_value=None),
            patch(
                "ouroboros.cli.commands.uninstall.is_setup_managed_gjc_instruction",
                side_effect=stale_for_canonical_path,
            ),
        ):
            assert not _remove_gjc_artifacts(dry_run=False)

        assert guide.read_text(encoding="utf-8") == "operator rules\n"

    def test_preserves_custom_routing_guide(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent"
        guide = agent_dir / "rules" / "ouroboros-skill-capability-guide.md"
        guide.parent.mkdir(parents=True)
        guide.write_text("my routing rules\n", encoding="utf-8")
        with (
            patch.dict("os.environ", {"GJC_CODING_AGENT_DIR": str(agent_dir)}),
            patch("ouroboros.config.get_gjc_cli_path", return_value=None),
            patch("ouroboros.cli.commands.uninstall.shutil.which", return_value=None),
        ):
            assert not _remove_gjc_artifacts(dry_run=False)

        assert guide.read_text(encoding="utf-8") == "my routing rules\n"


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

    def test_expected_cleanup_failure_exits_nonzero(self, tmp_path: Path) -> None:
        """Automation must distinguish partial uninstall from complete cleanup."""
        home_dir = tmp_path / "home"
        home_dir.mkdir()
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        codex_config = home_dir / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text("[mcp_servers.ouroboros]\ncommand = [\n", encoding="utf-8")

        with (
            patch("pathlib.Path.home", return_value=home_dir),
            patch("pathlib.Path.cwd", return_value=project_dir),
        ):
            result = runner.invoke(app, ["-y"])

        assert result.exit_code == 1
        assert "partially removed" in result.output
        assert "~/.codex/config.toml" in result.output


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


# ── Tail-match removal (PR #442 round-3 follow-up) ───────────────


class TestBridgeTailMatchRemoval:
    """Uninstall must remove bridge entries by tail-match, not exact path.

    Mirrors setup's dedupe logic so uninstall actually cleans stale entries
    from XDG reshuffles, root/sudo migrations, or path drift across installs.
    """

    def test_removes_stale_bridge_entry_from_legacy_path(self, tmp_path: Path) -> None:
        """Config holds a bridge entry from a *different* install path.

        Current-machine canonical path is `<tmp>/opencode/plugins/ouroboros-bridge/ouroboros-bridge.ts`
        but config lists a stale `/root/.config/opencode/plugins/ouroboros-bridge/ouroboros-bridge.ts`.
        Pre-fix: entry stays. Post-fix: tail match sweeps it.
        """
        config_dir = tmp_path / "opencode"
        config_dir.mkdir(parents=True)
        plugin_dir = config_dir / "plugins" / "ouroboros-bridge"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "ouroboros-bridge.ts").write_text("// bridge")

        stale = "/root/.config/opencode/plugins/ouroboros-bridge/ouroboros-bridge.ts"
        json_file = config_dir / "opencode.json"
        json_file.write_text(json.dumps({"plugin": [stale, "other-plugin"]}))

        with (
            patch("ouroboros.cli.commands.uninstall.opencode_config_dir", return_value=config_dir),
            patch("ouroboros.cli.opencode_config.opencode_config_dir", return_value=config_dir),
        ):
            result = _remove_opencode_bridge_plugin(dry_run=False)

        assert result is True
        data = json.loads(json_file.read_text())
        assert data["plugin"] == ["other-plugin"]

    def test_removes_multiple_stale_entries(self, tmp_path: Path) -> None:
        """Several bridge entries across XDG/root paths all get swept."""
        config_dir = tmp_path / "opencode"
        config_dir.mkdir(parents=True)
        plugin_dir = config_dir / "plugins" / "ouroboros-bridge"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "ouroboros-bridge.ts").write_text("// bridge")

        entries = [
            str(plugin_dir / "ouroboros-bridge.ts"),  # canonical
            "/home/x/.config/opencode/plugins/ouroboros-bridge/ouroboros-bridge.ts",
            "/root/.config/opencode/plugins/ouroboros-bridge/ouroboros-bridge.ts",
            "keep-me",
        ]
        json_file = config_dir / "opencode.json"
        json_file.write_text(json.dumps({"plugin": entries}))

        with (
            patch("ouroboros.cli.commands.uninstall.opencode_config_dir", return_value=config_dir),
            patch("ouroboros.cli.opencode_config.opencode_config_dir", return_value=config_dir),
        ):
            result = _remove_opencode_bridge_plugin(dry_run=False)

        assert result is True
        data = json.loads(json_file.read_text())
        assert data["plugin"] == ["keep-me"]

    def test_removes_windows_separator_entry(self, tmp_path: Path) -> None:
        """Windows-style backslash path also tail-matches."""
        config_dir = tmp_path / "opencode"
        config_dir.mkdir(parents=True)
        plugin_dir = config_dir / "plugins" / "ouroboros-bridge"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "ouroboros-bridge.ts").write_text("// bridge")

        win_entry = (
            r"C:\Users\x\AppData\Roaming\OpenCode\plugins\ouroboros-bridge\ouroboros-bridge.ts"
        )
        json_file = config_dir / "opencode.json"
        json_file.write_text(json.dumps({"plugin": [win_entry]}))

        with (
            patch("ouroboros.cli.commands.uninstall.opencode_config_dir", return_value=config_dir),
            patch("ouroboros.cli.opencode_config.opencode_config_dir", return_value=config_dir),
        ):
            result = _remove_opencode_bridge_plugin(dry_run=False)

        assert result is True
        data = json.loads(json_file.read_text())
        assert data["plugin"] == []

    def test_preserves_unrelated_plugins(self, tmp_path: Path) -> None:
        """Non-bridge entries must not be touched."""
        config_dir = tmp_path / "opencode"
        config_dir.mkdir(parents=True)
        plugin_dir = config_dir / "plugins" / "ouroboros-bridge"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "ouroboros-bridge.ts").write_text("// bridge")

        json_file = config_dir / "opencode.json"
        unrelated = [
            "plugins/other-thing/index.ts",
            "plugins/ouroboros-bridge/something-else.ts",  # wrong filename
            "plugins/not-bridge/ouroboros-bridge.ts",  # wrong subdir
        ]
        json_file.write_text(json.dumps({"plugin": unrelated}))

        with (
            patch("ouroboros.cli.commands.uninstall.opencode_config_dir", return_value=config_dir),
            patch("ouroboros.cli.opencode_config.opencode_config_dir", return_value=config_dir),
        ):
            # Dir removal still returns True even if config unchanged.
            _remove_opencode_bridge_plugin(dry_run=False)

        data = json.loads(json_file.read_text())
        assert data["plugin"] == unrelated
