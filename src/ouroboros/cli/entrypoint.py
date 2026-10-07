"""Public CLI entry point with a side-effect-free runtime doctor fast path."""

from __future__ import annotations

import sys


def main() -> None:
    """Dispatch ``doctor-runtime`` before importing the full CLI application."""
    arguments = sys.argv[1:]
    if arguments[:2] == ["mcp", "doctor-runtime"]:
        from ouroboros.cli.runtime_doctor import app

        app(args=arguments[2:])
        return

    from ouroboros.cli.main import app

    app()
