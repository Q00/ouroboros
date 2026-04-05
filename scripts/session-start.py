#!/usr/bin/env python3
"""Session Start Hook for Ouroboros.

Checks for available updates on session start (cached, max once per 24h).

Hook: SessionStart
"""

import importlib.util
from pathlib import Path
import sys


def _load_version_checker():
    script_path = Path(__file__).parent / "version-check.py"
    spec = importlib.util.spec_from_file_location("version_check", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load version checker from {script_path}")
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    return checker


def main() -> None:
    try:
        checker = _load_version_checker()
        result = checker.check_update()
        if result.get("update_available") and result.get("message"):
            # SessionStart stdout is consumed as Claude context, so keep the
            # success path silent and send notices to stderr instead.
            print(result["message"], file=sys.stderr)
            return
    except Exception as e:
        print(f"ouroboros: update check failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
