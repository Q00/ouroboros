"""OMP CLI path resolution (private split of ``config.loader``).

``config/loader.py`` is a module-size-ratcheted grandfathered module, so the
OMP backend's path getter lives here and is re-exported from the loader (the
backends model catalog resolves CLI-path getters as loader attributes).
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil

from ouroboros.config.loader import ConfigError, load_config


def get_omp_cli_path() -> str | None:
    """Get OMP CLI path from environment variable or config file.

    Priority:
        1. OUROBOROS_OMP_CLI_PATH environment variable
        2. config.yaml orchestrator.omp_cli_path
        3. None (resolve from PATH at runtime)

    Returns:
        Path to OMP CLI binary or None.

    The result is the *configured* candidate only — it is not validated.
    Construction paths must use :func:`resolve_omp_cli_path`, which owns
    the validated env/config/PATH precedence.
    """
    env_path = os.environ.get("OUROBOROS_OMP_CLI_PATH", "").strip()
    if env_path:
        return str(Path(env_path).expanduser())

    try:
        config = load_config()
        if config.orchestrator.omp_cli_path:
            return config.orchestrator.omp_cli_path
    except ConfigError:
        pass

    return None


def resolve_omp_cli_path() -> str | None:
    """Resolve a runnable OMP CLI path (canonical validated precedence).

    This is the single owner of OMP executable selection; every OMP
    construction path (setup detection, ``config backend omp``, the
    orchestrator runtime factory, and the provider factory/adapter) resolves
    through it (PR #2299 review rounds 4-5).

    Priority:
        1. OUROBOROS_OMP_CLI_PATH environment variable, when runnable
        2. config.yaml orchestrator.omp_cli_path, when runnable
        3. ``omp`` on PATH

    A configured candidate that does not resolve is skipped in favor of the
    next source, so a stale configured path never shadows a valid PATH
    installation. Returns None when no source resolves.
    """
    try:
        candidate = get_omp_cli_path()
    except Exception:
        candidate = None
    return (candidate if candidate and shutil.which(candidate) else None) or shutil.which("omp")


__all__ = ["get_omp_cli_path", "resolve_omp_cli_path"]
