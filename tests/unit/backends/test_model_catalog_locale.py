"""Model hints should read standard UTF-8 host configuration on any locale."""

from pathlib import Path

import pytest

from ouroboros.backends.model_catalog import configured_default_model
from ouroboros.cli.commands.config import _display_stage_model
from ouroboros.cli.commands.run import _execution_model_status


@pytest.mark.parametrize("backend", ["codex", "hermes"])
def test_utf8_host_config_with_korean_comment_keeps_model_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    codex_home = tmp_path / ".codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    config_path = (
        codex_home / "config.toml" if backend == "codex" else tmp_path / ".hermes/config.yaml"
    )
    config_path.parent.mkdir()
    content = (
        '# 한글 주석\nmodel = "gpt-configured"\n'
        if backend == "codex"
        else "# 한글 주석\nmodel:\n  default: gpt-configured\n"
    )
    config_path.write_text(content, encoding="utf-8")
    original_read = Path.read_text

    def locale_read(path: Path, encoding=None, errors=None):
        return original_read(
            path,
            encoding="cp949" if path == config_path and encoding is None else encoding,
            errors=errors,
        )

    monkeypatch.setattr(Path, "read_text", locale_read)
    if backend == "codex":
        assert "config.toml: gpt-configured" in _execution_model_status("codex", None)
        assert "config.toml: gpt-configured" in _display_stage_model("default", "test", "codex")[0]
    assert configured_default_model(backend) == "gpt-configured"


@pytest.mark.parametrize("backend", ["codex", "hermes"])
def test_invalid_utf8_host_config_is_ignored(tmp_path: Path, monkeypatch, backend: str) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    codex_home = tmp_path / ".codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    path = codex_home / "config.toml" if backend == "codex" else tmp_path / ".hermes/config.yaml"
    path.parent.mkdir()
    path.write_bytes(b"\xffinvalid")
    assert configured_default_model(backend) is None
    assert path.read_bytes() == b"\xffinvalid"
