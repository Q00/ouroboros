"""Unit tests for `ouroboros codex` helper commands."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from ouroboros.cli.commands.codex import app

runner = CliRunner()


def test_codex_refresh_rejects_symlinked_codex_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`codex refresh` must preserve the raw CODEX_HOME for symlink refusal."""
    real_home = tmp_path / "real-codex-home"
    real_home.mkdir()
    codex_home_link = tmp_path / "codex-home-link"
    try:
        codex_home_link.symlink_to(real_home, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are not supported on this platform")

    monkeypatch.setenv("CODEX_HOME", str(codex_home_link))

    result = runner.invoke(app, ["refresh"])

    assert result.exit_code == 1
    assert "Refusing to install Codex artifacts into a path with a symlinked" in result.output
    assert not (real_home / "rules").exists()
