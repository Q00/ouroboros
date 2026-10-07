"""Ouroboros CLI module.

This module provides the command-line interface for the Ouroboros system,
built with Typer for CLI framework and Rich for beautiful output.
"""

from typing import Any

__all__ = ["app"]


def __getattr__(name: str) -> Any:
    """Load the full CLI app only when a caller explicitly requests it."""
    if name == "app":
        from ouroboros.cli.main import app

        return app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
