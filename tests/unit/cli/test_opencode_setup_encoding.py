"""OpenCode setup reads and writes the same UTF-8 config as the loader."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from ouroboros.cli.commands import setup


@pytest.mark.parametrize("mode", ["subprocess", "plugin"])
@pytest.mark.parametrize("codepage", ["cp949", "cp1252"])
def test_opencode_setup_preserves_utf8_config_under_legacy_locale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, codepage: str
) -> None:
    config_dir = tmp_path / ".ouroboros"
    config_dir.mkdir()
    path = config_dir / "config.yaml"
    path.write_text(
        "orchestrator:\n  runtime_backend: codex\nproject: 한글 문서\n", encoding="utf-8"
    )
    original_open = Path.open
    original_read = Path.read_text

    def locale_open(self: Path, *args: object, **kwargs: object):
        if self == path and kwargs.get("encoding") is None:
            kwargs["encoding"] = codepage
        return original_open(self, *args, **kwargs)

    def locale_read(self: Path, *args: object, **kwargs: object):
        if self == path and not args and kwargs.get("encoding") is None:
            kwargs["encoding"] = codepage
        return original_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", locale_open)
    monkeypatch.setattr(Path, "read_text", locale_read)
    with (
        patch("ouroboros.config.loader.ensure_config_dir", return_value=config_dir),
        patch.object(setup, "plugin_version_error", return_value=None),
        patch.object(setup, "_cleanup_plugin_artifacts"),
        patch.object(setup, "_install_runtime_instruction_artifact"),
        patch.object(setup, "_install_opencode_bridge_plugin", return_value=True),
        patch.object(setup, "_ensure_opencode_mcp_entry", return_value=True),
        patch.object(setup, "_ensure_opencode_plugin_entry", return_value=True),
    ):
        assert setup._setup_opencode("/usr/bin/opencode", mode=mode)
    result = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert result["project"] == "한글 문서"
    assert result["orchestrator"]["runtime_backend"] == "opencode"
    assert result["orchestrator"]["opencode_mode"] == mode


def test_detect_existing_backend_from_utf8_config_under_cp949(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / ".ouroboros"
    config_dir.mkdir()
    path = config_dir / "config.yaml"
    path.write_text(
        "orchestrator:\n  runtime_backend: codex\nproject: 한글 문서\n", encoding="utf-8"
    )
    original_read = Path.read_text

    def locale_read(self: Path, *args: object, **kwargs: object):
        if self == path and not args and kwargs.get("encoding") is None:
            kwargs["encoding"] = "cp949"
        return original_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", locale_read)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert setup._get_current_backend() == "codex"
