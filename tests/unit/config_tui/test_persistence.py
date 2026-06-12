"""Tests for the validated batch write path (#1413).

The contract under test: writes share `config set`'s key validator, pass
the full Pydantic load check after writing, and roll back byte-for-byte on
validation failure.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ouroboros.config_tui import persistence


@pytest.fixture
def config_dir(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(persistence, "get_config_dir", lambda: tmp_path)
    # The post-write validation reads the same canonical path.
    from ouroboros.config import loader as config_loader
    from ouroboros.config import models as config_models

    monkeypatch.setattr(config_models, "get_config_dir", lambda: tmp_path)
    monkeypatch.setattr(config_loader, "get_config_dir", lambda: tmp_path)
    return tmp_path


def _read(config_dir: Path) -> dict:
    return yaml.safe_load((config_dir / "config.yaml").read_text()) or {}


def test_apply_valid_values_persists(config_dir: Path) -> None:
    persistence.apply_config_values(
        {
            "orchestrator.runtime_backend": "codex",
            "orchestrator.runtime_profile.stages.execute": "hermes",
            "clarification.default_model": "my-model",
        }
    )
    data = _read(config_dir)
    assert data["orchestrator"]["runtime_backend"] == "codex"
    assert data["orchestrator"]["runtime_profile"]["stages"]["execute"] == "hermes"
    assert data["clarification"]["default_model"] == "my-model"


def test_apply_none_deletes_stage_override(config_dir: Path) -> None:
    persistence.apply_config_values({"orchestrator.runtime_profile.stages.execute": "codex"})
    persistence.apply_config_values({"orchestrator.runtime_profile.stages.execute": None})
    data = _read(config_dir)
    assert "execute" not in data["orchestrator"]["runtime_profile"]["stages"]


def test_unknown_key_rejected_without_writing(config_dir: Path) -> None:
    with pytest.raises(persistence.ConfigWriteError, match="Unknown config key"):
        persistence.apply_config_values({"orchestrator.not_a_real_key_xyz": "value"})
    assert not (config_dir / "config.yaml").exists()


def test_invalid_value_rolls_back_file(config_dir: Path) -> None:
    persistence.apply_config_values({"orchestrator.runtime_backend": "codex"})
    before = (config_dir / "config.yaml").read_text()

    with pytest.raises(persistence.ConfigWriteError, match="rolled back"):
        persistence.apply_config_values({"orchestrator.runtime_backend": "not-a-backend"})

    assert (config_dir / "config.yaml").read_text() == before


def test_invalid_stage_backend_rolls_back(config_dir: Path) -> None:
    # The key path is structurally valid (validator cannot drill past the
    # Optional runtime_profile), so rejection must come from the Pydantic
    # load check — proving the post-write gate carries real weight.
    with pytest.raises(persistence.ConfigWriteError, match="rolled back"):
        persistence.apply_config_values(
            {"orchestrator.runtime_profile.stages.execute": "not-a-backend"}
        )
    assert not (config_dir / "config.yaml").exists()


def test_invalid_value_on_fresh_file_removes_it(config_dir: Path) -> None:
    with pytest.raises(persistence.ConfigWriteError):
        persistence.apply_config_values({"llm.backend": "not-a-backend"})
    assert not (config_dir / "config.yaml").exists()


def test_empty_batch_is_noop(config_dir: Path) -> None:
    persistence.apply_config_values({})
    assert not (config_dir / "config.yaml").exists()


def test_load_raw_config_missing_file_returns_empty(config_dir: Path) -> None:
    assert persistence.load_raw_config() == {}


def test_apply_writes_backup_for_undo(config_dir: Path) -> None:
    persistence.apply_config_values({"orchestrator.runtime_backend": "codex"})
    before = (config_dir / "config.yaml").read_text()
    persistence.apply_config_values({"orchestrator.runtime_backend": "hermes"})
    assert (config_dir / "config.yaml.bak").read_text() == before
