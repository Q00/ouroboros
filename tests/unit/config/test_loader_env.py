"""Tests for stdlib .env loading helpers."""

from __future__ import annotations

import os
from pathlib import Path

from ouroboros.config.loader import _load_env_file


def test_load_env_file_sets_missing_values(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("export FIRST=value\nSECOND='two words'\nTHIRD=three # trailing comment\n")

    monkeypatch.delenv("FIRST", raising=False)
    monkeypatch.delenv("SECOND", raising=False)
    monkeypatch.delenv("THIRD", raising=False)

    _load_env_file(env_file)

    assert os.environ["FIRST"] == "value"
    assert os.environ["SECOND"] == "two words"
    assert os.environ["THIRD"] == "three"


def test_load_env_file_does_not_override_existing_values(
    tmp_path: Path,
    monkeypatch,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("FIRST=from-file\n")
    monkeypatch.setenv("FIRST", "existing")

    _load_env_file(env_file)

    assert os.environ["FIRST"] == "existing"


def test_load_env_file_skips_template_placeholders(tmp_path: Path, monkeypatch) -> None:
    """Template placeholders should not block later env values from loading."""
    repo_env = tmp_path / "repo.env"
    home_env = tmp_path / "home.env"

    repo_env.write_text("OPENROUTER_API_KEY=YOUR_OPENROUTER_API_KEY")
    home_env.write_text("OPENROUTER_API_KEY=real-key")

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    _load_env_file(repo_env)
    _load_env_file(home_env)

    assert os.environ["OPENROUTER_API_KEY"] == "real-key"
