"""Typed argument selection for MCP tool dispatch."""

from typing import Any


def string_argument(arguments: dict[str, Any], *names: str) -> str | None:
    """Return the first non-empty string argument among *names*."""
    for name in names:
        value = arguments.get(name)
        if isinstance(value, str) and value:
            return value
    return None


def int_argument(arguments: dict[str, Any], *names: str) -> int | None:
    """Return the first integer argument among *names*."""
    for name in names:
        value = arguments.get(name)
        if isinstance(value, int):
            return value
    return None
