"""Shared helpers for locating the OpenCode configuration file.

Both :mod:`~ouroboros.cli.commands.setup` and
:mod:`~ouroboros.cli.commands.uninstall` need to find the same config file;
centralising the logic here avoids duplication and keeps the ``PermissionError``
/ ``OSError`` guard in one place.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys


def opencode_config_dir() -> Path:
    """Return the platform-specific OpenCode global config directory.

    Mirrors the lookup order used by OpenCode itself:

    * **Windows** – ``%APPDATA%\\OpenCode``
      (falls back to ``~\\AppData\\Roaming\\OpenCode`` when
      ``APPDATA`` is unset, which should not happen in practice).
    * **macOS** – ``~/Library/Application Support/OpenCode``
    * **Linux / other** – ``$XDG_CONFIG_HOME/opencode`` (defaults to
      ``~/.config/opencode`` when the env-var is not set).
    """
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(appdata) / "OpenCode"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "OpenCode"
    # Linux / BSD / other — honour XDG
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
