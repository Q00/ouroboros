"""Every console test runs against a throwaway HOME (config, credentials, client configs)."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    for name in (
        "OUROBOROS_AGENT_RUNTIME",
        "OUROBOROS_RUNTIME",
        "OUROBOROS_LLM_BACKEND",
        "OUROBOROS_MODEL",
        "OUROBOROS_CLARIFICATION_MODEL",
        "OUROBOROS_EXECUTION_MODEL",
        "OUROBOROS_SEMANTIC_MODEL",
        "OUROBOROS_REFLECT_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)
    return home
