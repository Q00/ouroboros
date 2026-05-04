#!/usr/bin/env python3
"""Drift Monitor for Ouroboros.

Monitors file changes (Write/Edit tool calls) and checks
if there's an active Ouroboros session that may be drifting.

Hook: PostToolUse (Write|Edit)
Output: Advisory message if active session detected

This is a lightweight check - actual drift measurement
requires calling /ouroboros:status with the MCP server.
"""

from pathlib import Path
import sys
import time


def _configure_utf8_stdio() -> None:
    """Keep hook output safe on non-UTF-8 Windows locales."""
    for stream in (sys.stdout, sys.stderr):
        encoding = getattr(stream, "encoding", None)
        reconfigure = getattr(stream, "reconfigure", None)
        if encoding and encoding.lower().replace("-", "") != "utf8" and reconfigure:
            reconfigure(encoding="utf-8", errors="replace")


_configure_utf8_stdio()


def check_active_session() -> dict:
    """Check for active Ouroboros interview sessions."""
    ouroboros_dir = Path.home() / ".ouroboros" / "data"

    if not ouroboros_dir.exists():
        return {"active": False}

    try:
        files = [
            f
            for f in ouroboros_dir.iterdir()
            if f.suffix == ".json"
            and not f.name.endswith(".lock")
            and f.name.startswith("interview_")
        ]

        if not files:
            return {"active": False}

        # Find the most recent session
        newest = max(files, key=lambda f: f.stat().st_mtime)
        newest_time = newest.stat().st_mtime

        # Only consider sessions modified in the last hour
        one_hour_ago = time.time() - 3600
        if newest_time < one_hour_ago:
            return {"active": False}

        return {"active": True, "session_file": newest.name}
    except Exception:
        return {"active": False}


def main() -> None:
    session = check_active_session()

    if session["active"]:
        print(
            f"Ouroboros session active ({session['session_file']}). "
            f"Use /ouroboros:status to check drift."
        )
    else:
        print("Success")


if __name__ == "__main__":
    main()
