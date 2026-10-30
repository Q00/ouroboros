"""Megacode harness CLI path resolution (private split of ``config.loader``).

``config/loader.py`` is a module-size-ratcheted module, so the megacode
backend's path getter lives here and is re-exported from the loader (the
backends model catalog resolves CLI-path getters as loader attributes).
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil

from ouroboros.config.loader import ConfigError, load_config


def get_megacode_cli_path() -> str | None:
    """Get the megacode harness CLI path (``harness``) from environment or config.

    Priority:
        1. OUROBOROS_MEGACODE_CLI_PATH environment variable
        2. config.yaml orchestrator.megacode_cli_path
        3. None (resolve from PATH at runtime)

    This is the configured candidate only. Launch and detection paths must
    use :func:`resolve_megacode_cli_path` to validate it.
    """
    env_path = os.environ.get("OUROBOROS_MEGACODE_CLI_PATH", "").strip()
    if env_path:
        return env_path

    try:
        config = load_config()
        if config.orchestrator.megacode_cli_path:
            return config.orchestrator.megacode_cli_path
    except ConfigError:
        pass

    return None


def resolve_megacode_cli_path(cli_path: str | Path | None = None) -> str | None:
    """Resolve one runnable absolute executable, independently of project cwd.

    Explicit constructor/factory input wins over the env/config getter, then
    the default is ``harness``. A selected invalid candidate fails closed.
    Relative paths containing a directory component are unsupported. Bare
    names search only absolute PATH entries; empty, dot and relative entries
    can never select an executable from the current project.
    """
    candidate = str(cli_path) if cli_path is not None else get_megacode_cli_path()
    value = candidate if candidate is not None else "harness"
    expanded = Path(value).expanduser()
    if expanded.is_absolute():
        return shutil.which(str(expanded))
    if expanded.drive or not value or os.sep in value or (os.altsep and os.altsep in value):
        return None
    for entry in os.get_exec_path():
        directory = Path(entry)
        if directory.is_absolute():
            executable = shutil.which(str(directory / value))
            if executable:
                return executable
    return None


__all__ = ["get_megacode_cli_path", "resolve_megacode_cli_path"]
