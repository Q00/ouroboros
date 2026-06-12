"""Validated batch writes for the settings GUI.

Writes go through the exact contract `ouroboros config set` enforces: the
schema key-path validator is shared (imported from the CLI command module,
not duplicated), the YAML serialization options match ``_save_config``, and
the post-write ``load_config()`` check rolls the whole batch back on any
Pydantic validation failure — the settings app never becomes a second,
unvalidated write path to ``~/.ouroboros/config.yaml``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import yaml

from ouroboros.config.models import get_config_dir


class ConfigWriteError(Exception):
    """A settings write was rejected; the file was left (or rolled back) unmodified."""


def load_raw_config() -> dict[str, Any]:
    """Read ``config.yaml`` as a plain dict (empty when the file is missing)."""
    config_path = get_config_dir() / "config.yaml"
    if not config_path.exists():
        return {}
    data = yaml.safe_load(config_path.read_text()) or {}
    if not isinstance(data, dict):
        msg = f"Invalid config format in {config_path} (expected mapping)"
        raise ConfigWriteError(msg)
    return data


def apply_config_values(values: Mapping[str, Any]) -> None:
    """Apply dot-notation ``{key: value}`` updates atomically.

    A ``None`` value deletes the key (used to clear a per-stage runtime
    override back to "inherit"). Raises :class:`ConfigWriteError` on an
    unknown key or when the resulting config fails schema validation; in
    the latter case the previous file content is restored byte-for-byte.
    """
    if not values:
        return

    # Shared with `config set` — the single schema-aware key validator.
    from ouroboros.cli.commands.config import _validate_key_path

    config_path = get_config_dir() / "config.yaml"
    original_text = config_path.read_text() if config_path.exists() else None
    data = load_raw_config()

    for key, value in values.items():
        keys = key.split(".")
        error = _validate_key_path(keys)
        if error:
            raise ConfigWriteError(error)
        target = data
        for part in keys[:-1]:
            node = target.setdefault(part, {})
            if not isinstance(node, dict):
                msg = f"Cannot set nested key: {key} ({part!r} is not a section)"
                raise ConfigWriteError(msg)
            target = node
        if value is None:
            target.pop(keys[-1], None)
        else:
            target[keys[-1]] = value

    config_path.parent.mkdir(parents=True, exist_ok=True)
    if original_text is not None:
        # One-step undo support: `ouroboros config undo` swaps this back in.
        (config_path.parent / "config.yaml.bak").write_text(original_text)
    config_path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))

    try:
        from ouroboros.config.loader import load_config

        load_config()
    except Exception as exc:
        if original_text is None:
            config_path.unlink(missing_ok=True)
        else:
            config_path.write_text(original_text)
        msg = f"Invalid value — rolled back. {exc}"
        raise ConfigWriteError(msg) from exc


__all__ = ["ConfigWriteError", "apply_config_values", "load_raw_config"]
