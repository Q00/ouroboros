"""Atomic persistence for validated mechanical detector commands."""

from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path
from typing import Any


def persist_mechanical_toml(
    target: Path,
    commands: Any,
    render_toml: Callable[[Any], str],
    command_keys: tuple[str, ...],
    log: Any,
) -> bool:
    """Atomically persist validated detector output without losing old config."""
    # Write a sibling file first; replace only after the complete TOML is ready.
    temp = target.with_name(target.name + ".tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp.write_text(render_toml(commands), encoding="utf-8")
        os.replace(temp, target)
    except OSError as exc:
        log.warning("detector.write_failed", path=str(target), error=str(exc))
        try:
            if temp.exists():
                temp.unlink()
        except OSError:
            pass
        return False

    log.info(
        "detector.toml_written",
        path=str(target),
        commands={k: getattr(commands, k) for k in command_keys},
    )
    return True
