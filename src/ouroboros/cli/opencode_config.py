"""Shared helpers for locating the OpenCode configuration file.

Both :mod:`~ouroboros.cli.commands.setup` and
:mod:`~ouroboros.cli.commands.uninstall` need to find the same config file;
centralising the logic here avoids duplication and keeps the ``PermissionError``
/ ``OSError`` guard in one place.
"""

from __future__ import annotations

from pathlib import Path


def find_opencode_config(*, allow_default: bool = True) -> Path | None:
    """Locate the existing OpenCode config file.

    OpenCode checks (in order): ``opencode.jsonc``, ``opencode.json`` —
    both inside ``~/.config/opencode/``.

    Args:
        allow_default: When ``True`` (setup path), return
            ``~/.config/opencode/opencode.json`` as a default for new
            installations if neither file exists.  When ``False``
            (uninstall path), return ``None`` so the caller can skip
            cleanly when no config is present.

    Returns:
        The first existing config path, the default path (when
        *allow_default* is ``True``), or ``None``.
    """
    config_dir = Path.home() / ".config" / "opencode"
    for name in ("opencode.jsonc", "opencode.json"):
        candidate = config_dir / name
        try:
            if candidate.exists():
                return candidate
        except OSError:
            continue
    return (config_dir / "opencode.json") if allow_default else None
