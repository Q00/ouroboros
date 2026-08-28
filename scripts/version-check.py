#!/usr/bin/env python3
"""Version check utility for Ouroboros.

Checks PyPI for the latest version and compares with the installed version.
Caches results for 24 hours to avoid spamming PyPI on every session start.

Used by: session-start.py (auto-check on session start)
         skills/update/SKILL.md (manual update command)
"""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time

_CACHE_DIR = Path.home() / ".ouroboros"
_CACHE_FILE = _CACHE_DIR / "version-check-cache.json"
_CACHE_TTL = 86400  # 24 hours


@contextmanager
def _cache_write_lock():
    """Serialize cache updates with the MCP serve-side producer."""
    lock_path = _CACHE_FILE.with_name(f"{_CACHE_FILE.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            if handle.seek(0, 2) == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


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


_VERSION_RE = re.compile(
    r"^\s*v?(?P<release>\d+(?:\.\d+)*)"
    r"(?:(?P<pre_kind>a|b|rc)(?P<pre_num>\d+))?"
    r"(?:\.post(?P<post_num>\d+))?"
    r"(?:\.dev(?P<dev_num>\d+))?"
    r"(?:\+[0-9A-Za-z.-]+)?\s*$",
    re.IGNORECASE,
)


def _version_key(version: str) -> tuple[tuple[int, ...], int, int] | None:
    """Return a sortable key for the version forms Ouroboros publishes.

    SessionStart runs under the first host ``python3`` on PATH, so this
    standalone hook cannot depend on the package environment containing
    ``packaging``. Supported ordering is dev < alpha < beta < rc < final < post.
    Local suffixes do not make a same-public-version update available.
    """
    match = _VERSION_RE.fullmatch(version)
    if match is None:
        return None
    release_parts = [int(part) for part in match.group("release").split(".")]
    while len(release_parts) > 1 and release_parts[-1] == 0:
        release_parts.pop()
    release = tuple(release_parts)
    pre_kind = match.group("pre_kind")
    if match.group("dev_num") is not None and (
        pre_kind is not None or match.group("post_num") is not None
    ):
        return None
    if match.group("dev_num") is not None:
        phase, number = 0, int(match.group("dev_num"))
    elif pre_kind is not None:
        phase = {"a": 1, "b": 2, "rc": 3}[pre_kind.lower()]
        number = int(match.group("pre_num") or 0)
    elif match.group("post_num") is not None:
        phase, number = 5, int(match.group("post_num"))
    else:
        phase, number = 4, 0
    return release, phase, number


def _compare_versions(left: str, right: str) -> int | None:
    """Compare supported versions, returning None when either is invalid."""
    left_key = _version_key(left)
    right_key = _version_key(right)
    if left_key is None or right_key is None:
        return None
    return (left_key > right_key) - (left_key < right_key)


def _is_prerelease(version_str: str) -> bool:
    """Check whether a supported Ouroboros version is a pre-release."""
    key = _version_key(version_str)
    return key is not None and key[1] < 4


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

    candidates = [
        (key, version)
        for version, files in data.get("releases", {}).items()
        if files and (key := _version_key(version)) is not None
    ]
    if not candidates:
        return data["info"]["version"]
    return max(candidates)[1]


def get_latest_version(*, current: str | None = None) -> str | None:
    """Fetch the latest version from PyPI, with 24h cache.

    If the currently installed version is a pre-release, also check for
    newer pre-releases (PyPI info.version only returns latest stable).
    """
    include_pre = False
    if current:
        include_pre = _is_prerelease(current)

    cache_key = "latest_version_pre" if include_pre else "latest_version"

    # Check cache first. Freshness is attested per channel (#2066): the
    # shared timestamp cannot vouch for a channel another refresh did not
    # touch, so once any per-channel stamp exists, an unstamped channel is
    # stale. A cache with no stamps at all predates them and keeps the
    # shared-timestamp semantics for one TTL window.
    try:
        if _CACHE_FILE.exists():
            cache = json.loads(_CACHE_FILE.read_text())
            stamp = cache.get(f"{cache_key}_checked_at")
            if stamp is None:
                stamped_keys = ("latest_version", "latest_version_pre")
                if any(cache.get(f"{key}_checked_at") is not None for key in stamped_keys):
                    stamp = 0
                else:
                    stamp = cache.get("timestamp", 0)
            age = time.time() - stamp
            if 0 <= age < _CACHE_TTL:
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
            with _cache_write_lock():
                # Read only after acquiring the shared lock: another producer
                # may have refreshed the other channel while this process was
                # waiting on PyPI.
                existing_cache: dict = {}
                try:
                    if _CACHE_FILE.exists():
                        loaded_cache = json.loads(_CACHE_FILE.read_text())
                        if isinstance(loaded_cache, dict):
                            existing_cache = loaded_cache
                except Exception:
                    pass
                now = time.time()
                existing_cache[cache_key] = latest
                # Per-channel freshness stamp (#2066); the shared timestamp is
                # kept for readers that predate the stamps.
                existing_cache[f"{cache_key}_checked_at"] = now
                existing_cache["timestamp"] = now
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


def consume_update_notice(*, current: str | None = None) -> bool:
    """Claim the once-per-24h SessionStart notice for this release channel."""
    include_pre = bool(current and _is_prerelease(current))
    notice_key = "latest_version_pre_notified_at" if include_pre else "latest_version_notified_at"
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with _cache_write_lock():
            cache: dict = {}
            if _CACHE_FILE.exists():
                loaded = json.loads(_CACHE_FILE.read_text())
                if isinstance(loaded, dict):
                    cache = loaded
            now = time.time()
            stamp = cache.get(notice_key, 0)
            if isinstance(stamp, (int, float)) and 0 <= now - stamp < _CACHE_TTL:
                return False
            cache[notice_key] = now
            payload = json.dumps(cache)
            fd, tmp_path = tempfile.mkstemp(dir=_CACHE_DIR, suffix=".tmp")
            try:
                with open(fd, "w") as handle:
                    handle.write(payload)
                Path(tmp_path).replace(_CACHE_FILE)
            except Exception:
                Path(tmp_path).unlink(missing_ok=True)
                raise
        return True
    except Exception:
        # Delivery is advisory. Fail closed so a damaged cache cannot spam
        # every new host session.
        return False


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

    comparison = _compare_versions(latest, current)
    if comparison is not None and comparison > 0:
        return {
            "update_available": True,
            "current": current,
            "latest": latest,
            "message": (
                f"Ouroboros update available: v{current} → v{latest}. Run `ooo update` to upgrade."
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
    elif result["current"] and result["latest"]:
        print(f"Ouroboros v{result['current']} is up to date.")
    elif result["current"]:
        print(f"Ouroboros v{result['current']} installed (could not check for updates).")
    else:
        print("Ouroboros is not installed.")
