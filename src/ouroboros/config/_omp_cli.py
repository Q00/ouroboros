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


def _runnable_omp_candidate(candidate: str | None) -> str | None:
    """Return the expanded candidate when it resolves to a runnable executable."""
    if not candidate:
        return None
    expanded = str(Path(candidate).expanduser())
    return expanded if shutil.which(expanded) else None


def resolve_omp_cli_path() -> str | None:
    """Resolve a runnable OMP CLI path (canonical validated precedence).

    This is the single owner of OMP executable selection; every OMP
    construction path (setup detection, ``config backend omp``, the
    orchestrator runtime factory, the provider factory/adapter, and catalog
    discovery) resolves through it (PR #2299 review rounds 4-6).

    Each source is validated independently in declared order, so a stale
    higher-priority candidate falls through to the next source instead of
    masking it:

        1. OUROBOROS_OMP_CLI_PATH environment variable, when runnable
        2. config.yaml orchestrator.omp_cli_path, when runnable
        3. ``omp`` on PATH

    The environment and configuration candidates are deliberately read and
    validated separately — never collapsed into one value before validation —
    so a stale environment override cannot hide a runnable configured CLI.
    Returns None when no source resolves.
    """
    env_candidate = _runnable_omp_candidate(os.environ.get("OUROBOROS_OMP_CLI_PATH", "").strip())
    if env_candidate:
        return env_candidate

    try:
        configured = load_config().orchestrator.omp_cli_path
    except Exception:
        configured = None
    config_candidate = _runnable_omp_candidate(configured)
    if config_candidate:
        return config_candidate

    return shutil.which("omp")


__all__ = ["get_omp_cli_path", "resolve_omp_cli_path"]
