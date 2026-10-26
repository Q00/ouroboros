"""OpenCode config integration must preserve files it cannot decode."""

from pathlib import Path
from unittest.mock import patch

import pytest

from ouroboros.cli.commands import setup, uninstall


@pytest.mark.parametrize("suffix", [".json", ".jsonc"])
@pytest.mark.parametrize(
    "helper", ["mcp_setup", "plugin_setup", "mode_cleanup", "mcp_uninstall", "plugin_uninstall"]
)
def test_invalid_utf8_config_is_preserved(tmp_path: Path, suffix: str, helper: str) -> None:
    config = tmp_path / ("opencode" + suffix)
    original = b'{"mcp": {}, "plugin": [], "label": "\xff"}'
    config.write_bytes(original)
    with (
        patch.object(setup, "_find_opencode_config", return_value=config),
        patch.object(setup, "find_opencode_config", return_value=config),
        patch.object(setup, "opencode_config_dir", return_value=tmp_path),
        patch.object(uninstall, "_find_opencode_config", return_value=config),
        patch.object(uninstall, "find_opencode_config", return_value=config),
        patch.object(uninstall, "opencode_config_dir", return_value=tmp_path),
        patch.object(
            setup, "_detect_opencode_mcp_command", return_value={"command": ["ooo", "mcp", "serve"]}
        ),
    ):
        if helper == "mcp_setup":
            assert setup._ensure_opencode_mcp_entry() is False
        elif helper == "plugin_setup":
            assert setup._ensure_opencode_plugin_entry() is False
        elif helper == "mode_cleanup":
            setup._cleanup_plugin_artifacts()
        elif helper == "mcp_uninstall":
            assert uninstall._remove_opencode_mcp(dry_run=False) is False
        else:
            assert uninstall._remove_opencode_bridge_plugin(dry_run=False) is False
    assert config.read_bytes() == original


@pytest.mark.parametrize("helper", ["mode_cleanup", "plugin_uninstall"])
def test_cleanup_reads_utf8_independently_of_locale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, helper: str
) -> None:
    import json

    config = tmp_path / "opencode.json"
    config.write_text(
        json.dumps(
            {"label": "한글", "plugin": ["/old/plugins/ouroboros-bridge/ouroboros-bridge.ts"]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    original_read = Path.read_text

    def locale_read(path: Path, encoding=None, errors=None, **kwargs):
        return original_read(path, encoding=encoding or "cp1252", errors=errors, **kwargs)

    monkeypatch.setattr(Path, "read_text", locale_read)
    with (
        patch.object(setup, "find_opencode_config", return_value=config),
        patch.object(setup, "opencode_config_dir", return_value=tmp_path),
        patch.object(uninstall, "find_opencode_config", return_value=config),
        patch.object(uninstall, "opencode_config_dir", return_value=tmp_path),
    ):
        if helper == "mode_cleanup":
            setup._cleanup_plugin_artifacts()
        else:
            uninstall._remove_opencode_bridge_plugin(dry_run=False)

    result = json.loads(config.read_bytes().decode("utf-8"))
    assert result["label"] == "한글"
    assert result["plugin"] == []
