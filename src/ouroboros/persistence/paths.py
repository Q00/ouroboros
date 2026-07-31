"""Canonical persistence path resolution.

All process surfaces that open the shared SQLite store resolve its location
through this module so runtime writers and recovery readers cannot silently
diverge.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml


def event_store_path_from_config(data: Mapping[str, Any], config_path: Path) -> Path:
    """Resolve the EventStore path from already-loaded configuration data.

    Existing installations may still have the legacy database beside
    ``config.yaml``. Prefer that file until the configured target exists so an
    upgrade never strands durable history merely because the default config
    now names ``data/ouroboros.db``.
    """
    persistence = data.get("persistence")
    if persistence is not None and not isinstance(persistence, Mapping):
        raise ValueError("config section 'persistence' must be a mapping")

    configured = persistence.get("database_path") if persistence else None
    legacy_path = config_path.parent / "ouroboros.db"
    if not configured:
        return legacy_path

    configured_path = Path(str(configured)).expanduser()
    if not configured_path.is_absolute():
        configured_path = config_path.parent / configured_path
    if configured_path.exists() or not legacy_path.exists():
        return configured_path
    return legacy_path


def resolve_event_store_path(config_path: Path | None = None) -> Path:
    """Resolve the authoritative EventStore path for runtime and CLI readers."""
    if config_path is None:
        from ouroboros.config.models import get_config_dir

        config_path = get_config_dir() / "config.yaml"

    if not config_path.exists():
        return config_path.parent / "ouroboros.db"

    try:
        loaded = yaml.safe_load(config_path.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot read EventStore configuration {config_path}: {exc}") from exc
    if not isinstance(loaded, Mapping):
        raise ValueError(
            f"invalid config format in {config_path} "
            f"(expected mapping, got {type(loaded).__name__})"
        )
    return event_store_path_from_config(loaded, config_path)
