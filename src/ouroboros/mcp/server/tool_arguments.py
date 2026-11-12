"""Typed argument selection for MCP tool dispatch."""

from typing import Any


def int_argument(arguments: dict[str, Any], *names: str) -> int | None:
    """Return the first integer argument among *names*."""
    for name in names:
        value = arguments.get(name)
        if isinstance(value, int):
            return value
    return None
