"""Exercise uninstall with UTF-8 configuration under non-UTF-8 locales."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ouroboros.cli.commands.uninstall import _remove_claude_mcp, _remove_codex_mcp, app


@pytest.mark.parametrize("locale", ["cp949", "cp1252"])
@pytest.mark.parametrize("kind", ["claude", "codex"])
def test_uninstall_preserves_utf8_config(tmp_path, monkeypatch, locale, kind):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    monkeypatch.chdir(tmp_path)
    if kind == "claude":
        config = tmp_path / ".claude" / "mcp.json"
        config.parent.mkdir()
        config.write_text(
            json.dumps(
                {"mcpServers": {"ouroboros": {"command": "uvx"}, "other": {"command": "한글"}}},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        remove = _remove_claude_mcp
    else:
        config = tmp_path / ".codex" / "config.toml"
        config.parent.mkdir()
        config.write_text(
            'title = "한글"\n[mcp_servers.ouroboros]\ncommand = "uvx"\nargs = ["ouroboros", "mcp", "serve"]\n',
            encoding="utf-8",
        )
        remove = _remove_codex_mcp

    read_text = Path.read_text
    write_text = Path.write_text

    def locale_read(path, encoding=None, errors=None):
        return read_text(
            path,
            encoding=locale if path == config and encoding is None else encoding,
            errors=errors,
        )

    def locale_write(path, data, encoding=None, errors=None, newline=None):
        return write_text(
            path,
            data,
            encoding=locale if path == config and encoding is None else encoding,
            errors=errors,
            newline=newline,
        )

    monkeypatch.setattr(Path, "read_text", locale_read)
    monkeypatch.setattr(Path, "write_text", locale_write)

    result = CliRunner().invoke(app, ["--dry-run"])
    assert result.exit_code == 0, result.exception
    assert remove(dry_run=False)
    content = read_text(config, encoding="utf-8")
    if kind == "claude":
        assert json.loads(content)["mcpServers"]["other"]["command"] == "한글"
    else:
        assert "한글" in content


@pytest.mark.parametrize("kind", ["claude", "codex"])
def test_invalid_utf8_config_is_preserved(tmp_path, monkeypatch, kind):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    monkeypatch.chdir(tmp_path)
    config = tmp_path / (".claude/mcp.json" if kind == "claude" else ".codex/config.toml")
    config.parent.mkdir()
    config.write_bytes(b"\xffinvalid")
    result = CliRunner().invoke(app, ["--dry-run"])
    assert result.exit_code == 0, result.exception
    assert not (_remove_claude_mcp if kind == "claude" else _remove_codex_mcp)(False)
    assert config.read_bytes() == b"\xffinvalid"
