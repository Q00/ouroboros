#!/usr/bin/env python3
"""Version check utility for Ouroboros.

Checks PyPI for the latest version and compares with the installed version.
Caches results for 24 hours to avoid spamming PyPI on every session start.

Used by: session-start.py (auto-check on session start)
         skills/update/SKILL.md (manual update command)
"""

import json
from pathlib import Path
import time

_CACHE_DIR = Path.home() / ".ouroboros"
_CACHE_FILE = _CACHE_DIR / "version-check-cache.json"
_CACHE_TTL = 86400  # 24 hours


def get_installed_version() -> str | None:
    """Get the currently installed ouroboros version."""
    try:
        # Read from plugin.json first (works even without package installed)
        plugin_root = Path(__file__).parent.parent
        plugin_json = plugin_root / ".claude-plugin" / "plugin.json"
        if plugin_json.exists():
            data = json.loads(plugin_json.read_text())
            return data.get("version")
    except Exception:
        pass

    try:
        import importlib.metadata

        return importlib.metadata.version("ouroboros-ai")
    except Exception:
        pass

    return None


def get_latest_version() -> str | None:
    """Fetch the latest version from PyPI, with 24h cache."""
    # Check cache first
    try:
        if _CACHE_FILE.exists():
            cache = json.loads(_CACHE_FILE.read_text())
            if time.time() - cache.get("timestamp", 0) < _CACHE_TTL:
                return cache.get("latest_version")
    except Exception:
        pass

    # Fetch from PyPI
    try:
        import urllib.request

        resp = urllib.request.urlopen(  # noqa: S310
            "https://pypi.org/pypi/ouroboros-ai/json", timeout=5
        )
        data = json.loads(resp.read())
        latest = data["info"]["version"]

        # Cache the result
        try:
            _CACHE_DIR.mkdir(parents=True, exist_ok=True)
            _CACHE_FILE.write_text(json.dumps({"latest_version": latest, "timestamp": time.time()}))
        except Exception:
            pass

        return latest
    except Exception:
        return None


def check_update() -> dict:
    """Check if an update is available.

    Returns:
        Dict with keys: update_available, current, latest, message
    """
    current = get_installed_version()
    latest = get_latest_version()

    if not current or not latest:
        return {
            "update_available": False,
            "current": current,
            "latest": latest,
            "message": None,
        }

    if current == latest:
        return {
            "update_available": False,
            "current": current,
            "latest": latest,
            "message": None,
        }

    # Simple version comparison (works for semver)
    from packaging.version import Version

    try:
        if Version(latest) > Version(current):
            return {
                "update_available": True,
                "current": current,
                "latest": latest,
                "message": (
                    f"Ouroboros update available: v{current} → v{latest}. "
                    f"Run `ooo update` to upgrade."
                ),
            }
    except Exception:
        # Fallback: string comparison
        if latest != current:
            return {
                "update_available": True,
                "current": current,
                "latest": latest,
                "message": (
                    f"Ouroboros update available: v{current} → v{latest}. "
                    f"Run `ooo update` to upgrade."
                ),
            }

    return {
        "update_available": False,
        "current": current,
        "latest": latest,
        "message": None,
    }


if __name__ == "__main__":
    result = check_update()
    if result["message"]:
        print(result["message"])
    elif result["current"]:
        print(f"Ouroboros v{result['current']} is up to date.")
    else:
        print("Ouroboros is not installed.")
