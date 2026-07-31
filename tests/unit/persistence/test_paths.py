"""Tests for canonical EventStore path selection."""

from pathlib import Path

from ouroboros.persistence.paths import resolve_event_store_path


def _write_config(config_path: Path, database_path: str) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(f"persistence:\n  database_path: {database_path}\n")


def test_resolve_event_store_path_uses_configured_target_for_new_install(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, "data/events.db")

    assert resolve_event_store_path(config_path) == tmp_path / "data" / "events.db"


def test_resolve_event_store_path_preserves_existing_legacy_database(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    legacy_path = tmp_path / "ouroboros.db"
    _write_config(config_path, "data/events.db")
    legacy_path.touch()

    assert resolve_event_store_path(config_path) == legacy_path


def test_resolve_event_store_path_prefers_existing_configured_database(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    configured_path = tmp_path / "data" / "events.db"
    _write_config(config_path, "data/events.db")
    configured_path.parent.mkdir()
    configured_path.touch()
    (tmp_path / "ouroboros.db").touch()

    assert resolve_event_store_path(config_path) == configured_path
