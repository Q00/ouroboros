#!/usr/bin/env python3
"""Session Start Hook for Ouroboros.

Checks for available updates on session start (cached, max once per 24h).

Hook: SessionStart
"""

from pathlib import Path
import sys

# Add scripts directory to path so we can import version-check
sys.path.insert(0, str(Path(__file__).parent))


def main() -> None:
    try:
        from importlib.machinery import SourceFileLoader

        checker = SourceFileLoader(
            "version_check",
            str(Path(__file__).parent / "version-check.py"),
        ).load_module()

        result = checker.check_update()
        if result.get("update_available") and result.get("message"):
            # Print update notice — Claude Code shows this as hook output
            print(result["message"])
            return
    except Exception:
        pass

    print("Success")


if __name__ == "__main__":
    main()
