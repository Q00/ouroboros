"""Resolve the effective Codex configuration directory."""

from __future__ import annotations

import os
from pathlib import Path


def resolve_codex_home(codex_home: str | Path | None = None) -> Path:
    """Return an explicit or environment-selected Codex home directory.

    Codex uses ``$CODEX_HOME`` when it is set, otherwise ``~/.codex``. Setup
    and settings hints must use that same location as the spawned CLI.
    """
    if codex_home is not None:
        return Path(codex_home).expanduser()
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"
