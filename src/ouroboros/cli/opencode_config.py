"""Shared helpers for locating the OpenCode configuration file.

Both :mod:`~ouroboros.cli.commands.setup` and
:mod:`~ouroboros.cli.commands.uninstall` need to find the same config file;
centralising the logic here avoids duplication and keeps the ``PermissionError``
/ ``OSError`` guard in one place.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import yaml


def _expand_config_dir(raw: str) -> Path:
    """Expand an OpenCode config directory string into a :class:`Path`."""
    return Path(raw).expanduser()


def _configured_opencode_cli_path() -> Path | None:
    """Return the setup-selected OpenCode CLI path when one is persisted.

    Path discovery must be stable across setup, cleanup, and uninstall.  Do not
    fall back to whichever ``opencode`` happens to be on ``PATH`` here: machines
    can have several OpenCode installs, and uninstall should target the same
    install tree that setup recorded.
    """
    for key in ("OUROBOROS_OPENCODE_CLI_PATH", "OPENCODE_CLI_PATH"):
        raw = os.environ.get(key, "").strip()
        if raw:
            return Path(raw).expanduser()

    config_path = Path.home() / ".ouroboros" / "config.yaml"
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(config, dict):
        return None
    orchestrator = config.get("orchestrator")
    if not isinstance(orchestrator, dict):
        return None
    raw = orchestrator.get("opencode_cli_path")
    if not isinstance(raw, str) or not raw.strip():
        return None
    return Path(raw.strip()).expanduser()


def _debug_paths_config_dir() -> Path | None:
    """Return ``opencode debug paths`` config dir when the CLI reports one.

    Current OpenCode releases expose the authoritative runtime directories via
    ``opencode debug paths``. Querying it first avoids baking version-specific
    platform assumptions into Ouroboros. Any failure falls back to deterministic
    local resolution so setup still works on machines without OpenCode on PATH.
    """
    opencode = _configured_opencode_cli_path()
    if not opencode:
        return None
    try:
        result = subprocess.run(
            [str(opencode), "debug", "paths"],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0] == "config":
            return _expand_config_dir(parts[1])
    return None


def opencode_config_dir() -> Path:
    """Return the active OpenCode global config directory.

    Resolution order:

    1. ``OPENCODE_CONFIG_DIR`` when explicitly set.
    2. ``opencode debug paths`` ``config`` value when the CLI is available.
    3. XDG-style default (``$XDG_CONFIG_HOME/opencode`` or
       ``~/.config/opencode``) on macOS/Linux/other Unix platforms.
    4. Windows roaming config directory for Windows.

    Older Ouroboros releases wrote macOS config under
    ``~/Library/Application Support/OpenCode``. Current OpenCode releases use
    XDG config paths on macOS too, so that legacy directory is intentionally not
    the default unless OpenCode itself reports it through ``debug paths``.
    """
    explicit = os.environ.get("OPENCODE_CONFIG_DIR")
    if explicit:
        return _expand_config_dir(explicit)

    reported = _debug_paths_config_dir()
    if reported is not None:
        return reported

    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(appdata) / "OpenCode"

    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(xdg) / "opencode"


def find_opencode_config(*, allow_default: bool = True) -> Path | None:
    """Locate the existing OpenCode config file.

    OpenCode checks (in order): ``opencode.jsonc``, ``opencode.json`` —
    both inside :func:`opencode_config_dir`.

    Args:
        allow_default: When ``True`` (setup path), return
            ``<config_dir>/opencode.json`` as a default for new
            installations if neither file exists.  When ``False``
            (uninstall path), return ``None`` so the caller can skip
            cleanly when no config is present.

    Returns:
        The first existing config path, the default path (when
        *allow_default* is ``True``), or ``None``.
    """
    explicit_config = os.environ.get("OPENCODE_CONFIG")
    if explicit_config:
        config_path = Path(explicit_config).expanduser()
        if allow_default or config_path.exists():
            return config_path
        return None

    config_dir = opencode_config_dir()
    for name in ("opencode.jsonc", "opencode.json"):
        candidate = config_dir / name
        try:
            if candidate.exists():
                return candidate
        except OSError:
            continue
    return (config_dir / "opencode.json") if allow_default else None


# ── Bridge-plugin identity ───────────────────────────────────────
# Canonical subdirectory + filename of the OpenCode bridge plugin.
# Shared by setup (install + dedupe) and uninstall (tail-match removal)
# so both paths agree on what counts as "a bridge-plugin entry".
BRIDGE_PLUGIN_SUBDIR: tuple[str, str] = ("plugins", "ouroboros-bridge")
BRIDGE_PLUGIN_FILENAME: str = "ouroboros-bridge.ts"


def is_bridge_plugin_entry(entry: object) -> bool:
    """Return ``True`` when *entry* refers to any bridge-plugin install.

    Matches by directory-tail (``plugins/ouroboros-bridge``) + basename
    (``ouroboros-bridge.ts``) rather than exact string equality — catches
    stale entries from XDG reshuffles, Windows mixed separators, legacy
    install paths, and sudo/root migrations. Setup uses this for dedupe;
    uninstall uses it to remove stale entries, not just the exact
    canonical path for this machine's current config layout.
    """
    if not isinstance(entry, str) or not entry:
        return False
    # Normalise path separators so Windows entries (``\``) compare equal.
    normalised = entry.replace("\\", "/")
    parts = [p for p in normalised.split("/") if p]
    if len(parts) < 3:
        return False
    return (
        parts[-1] == BRIDGE_PLUGIN_FILENAME
        and parts[-2] == BRIDGE_PLUGIN_SUBDIR[1]
        and parts[-3] == BRIDGE_PLUGIN_SUBDIR[0]
    )
