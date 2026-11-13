"""Provider keys are written to credentials.yaml (mode 600) and never sent back."""

from __future__ import annotations

from pathlib import Path
import stat

import pytest
import yaml

from ouroboros.gui import providers


def _credentials(home: Path) -> dict:
    return yaml.safe_load((home / ".ouroboros" / "credentials.yaml").read_text())


def test_save_writes_key_with_private_mode(isolated_home: Path, monkeypatch) -> None:
    monkeypatch.setattr(providers, "_ocx_running", lambda: None)
    providers.save("openrouter", {"api_key": "sk-or-1234567890"})
    data = _credentials(isolated_home)
    assert data["providers"]["openrouter"]["api_key"] == "sk-or-1234567890"
    mode = stat.S_IMODE((isolated_home / ".ouroboros" / "credentials.yaml").stat().st_mode)
    assert mode == 0o600


def test_view_shows_only_a_hint(isolated_home: Path, monkeypatch) -> None:
    monkeypatch.setattr(providers, "_ocx_running", lambda: None)
    providers.save("openrouter", {"api_key": "sk-or-1234567890"})
    row = next(r for r in providers.view()["providers"] if r["id"] == "openrouter")
    assert row["configured"] is True
    assert row["key_hint"] == "…7890"
    assert "sk-or" not in str(providers.view())


def test_local_provider_needs_no_key(isolated_home: Path) -> None:
    providers.save("ollama", {"base_url": ""})
    entry = _credentials(isolated_home)["providers"]["ollama"]
    assert entry["base_url"] == "http://localhost:11434"
    assert entry["api_key"] == "not-needed"


def test_cloud_provider_requires_a_key(isolated_home: Path) -> None:
    with pytest.raises(providers.ProviderError):
        providers.save("openai", {})


def test_bad_base_url_is_rejected(isolated_home: Path) -> None:
    with pytest.raises(providers.ProviderError):
        providers.save("openai", {"api_key": "sk-x", "base_url": "ftp://nope"})


def test_remove_keeps_other_providers(isolated_home: Path) -> None:
    providers.save("openai", {"api_key": "sk-openai-1"})
    providers.save("groq", {"api_key": "gsk-groq-1"})
    providers.remove("openai")
    data = _credentials(isolated_home)["providers"]
    assert "openai" not in data
    assert data["groq"]["api_key"] == "gsk-groq-1"
