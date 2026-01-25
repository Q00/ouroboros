"""MCP Resources package.

This package provides resource handlers for the MCP server.

Public API:
    OUROBOROS_RESOURCES: List of available resource definitions
    Resource handlers for seeds, sessions, and events
"""

from ouroboros.mcp.resources.handlers import (
    OUROBOROS_RESOURCES,
    events_handler,
    seeds_handler,
    sessions_handler,
)

__all__ = [
    "OUROBOROS_RESOURCES",
    "seeds_handler",
    "sessions_handler",
    "events_handler",
]
