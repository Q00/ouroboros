#!/usr/bin/env python3
"""Version check utility for Ouroboros.

Checks PyPI for the latest version and compares with the installed version.
Caches results for 24 hours to avoid spamming PyPI on every session start.

Used by: session-start.py (auto-check on session start)
         skills/update/SKILL.md (manual update command)
"""

import json
from pathlib import Path
import sys
import tempfile
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


def _is_prerelease(version_str: str) -> bool:
    """Check if a version string is a pre-release (PEP 440)."""
    try:
        from packaging.version import Version

        return Version(version_str).is_prerelease
    except Exception:
        # Fallback: check for common pre-release suffixes
        import re

        return bool(re.search(r"(a|b|rc|dev)\d*", version_str))


def _get_latest_from_pypi(
    *,
    include_prerelease: bool = False,
) -> str | None:
    """Fetch version from PyPI. If include_prerelease, scan all releases."""
    import ssl
    import urllib.request

    try:
        ctx = ssl.create_default_context()
    except Exception:
        print(
            "ouroboros: SSL certificate bundle unavailable, skipping update check",
            file=sys.stderr,
        )
        return None

    resp = urllib.request.urlopen(  # noqa: S310
        "https://pypi.org/pypi/ouroboros-ai/json", timeout=5, context=ctx
    )
    data = json.loads(resp.read())

    if not include_prerelease:
        return data["info"]["version"]

    # Scan all releases to find the latest pre-release
    from packaging.version import Version

    all_versions = [Version(v) for v in data.get("releases", {}) if data["releases"][v]]
    if not all_versions:
        return data["info"]["version"]
    return str(max(all_versions))


def get_latest_version(*, current: str | None = None) -> str | None:
    """Fetch the latest version from PyPI, with 24h cache.

    If the currently installed version is a pre-release, also check for
    newer pre-releases (PyPI info.version only returns latest stable).
    """
    include_pre = False
    if current:
        include_pre = _is_prerelease(current)

    cache_key = "latest_version_pre" if include_pre else "latest_version"

    # Check cache first
    try:
        if _CACHE_FILE.exists():
            cache = json.loads(_CACHE_FILE.read_text())
            if time.time() - cache.get("timestamp", 0) < _CACHE_TTL:
                cached = cache.get(cache_key)
                if cached:
                    return cached
    except Exception:
        pass

    # Fetch from PyPI
    try:
        latest = _get_latest_from_pypi(include_prerelease=include_pre)

        # Cache the result (atomic write to avoid race conditions)
        try:
            _CACHE_DIR.mkdir(parents=True, exist_ok=True)
            # Read existing cache to preserve other keys
            existing_cache: dict = {}
            try:
                if _CACHE_FILE.exists():
                    existing_cache = json.loads(_CACHE_FILE.read_text())
            except Exception:
                pass
            existing_cache[cache_key] = latest
            existing_cache["timestamp"] = time.time()
            cache_content = json.dumps(existing_cache)
            fd, tmp_path = tempfile.mkstemp(dir=_CACHE_DIR, suffix=".tmp")
            try:
                with open(fd, "w") as f:
                    f.write(cache_content)
                Path(tmp_path).replace(_CACHE_FILE)
            except Exception:
                Path(tmp_path).unlink(missing_ok=True)
                raise
        except Exception:
            print("ouroboros: failed to write version cache", file=sys.stderr)

        return latest
    except Exception:
        return None


def check_update() -> dict:
    """Check if an update is available.

    Returns:
        Dict with keys: update_available, current, latest, message
    """
    current = get_installed_version()
    latest = get_latest_version(current=current)

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
        # Version parsing failed — cannot determine ordering safely.
        # Return False rather than risking a false positive (e.g. downgrade).
        pass

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
    elif result["current"] and result["latest"]:
        print(f"Ouroboros v{result['current']} is up to date.")
    elif result["current"]:
        print(f"Ouroboros v{result['current']} installed (could not check for updates).")
    else:
        print("Ouroboros is not installed.")
