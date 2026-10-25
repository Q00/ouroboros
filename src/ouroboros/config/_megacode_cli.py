"""Megacode harness CLI path resolution (private split of ``config.loader``).

``config/loader.py`` is a module-size-ratcheted module, so the megacode
backend's path getter lives here and is re-exported from the loader (the
backends model catalog resolves CLI-path getters as loader attributes).
"""

from __future__ import annotations

import os
from pathlib import Path

from ouroboros.config.loader import ConfigError, load_config


def get_megacode_cli_path() -> str | None:
    """Get the megacode harness CLI path (``harness``) from environment or config.

    Priority:
        1. OUROBOROS_MEGACODE_CLI_PATH environment variable
        2. config.yaml orchestrator.megacode_cli_path
        3. None (resolve from PATH at runtime)

    Returns:
        Path to the ``harness`` binary or None.
    """
    env_path = os.environ.get("OUROBOROS_MEGACODE_CLI_PATH", "").strip()
    if env_path:
        return str(Path(env_path).expanduser())

    try:
        config = load_config()
        if config.orchestrator.megacode_cli_path:
            return config.orchestrator.megacode_cli_path
    except ConfigError:
        pass

    return None


__all__ = ["get_megacode_cli_path"]
