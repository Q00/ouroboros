"""Explicit startup and shutdown ownership for MCP server resources."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import structlog

from ouroboros.orchestrator.control_bus import ControlBusDrainError

log = structlog.get_logger(__name__)


class ServerResourceLifecycle:
    """Initialize and close server-owned resources exactly once."""

    def __init__(self, *, server_name: str) -> None:
        self._server_name = server_name
        self._owned: list[Any] = []
        self._startup_owned: list[Any] = []
        self._lock = asyncio.Lock()
        self._startup_complete = False
        self._startup_error: Exception | None = None
        self._shutdown_complete = False

    def register(self, resource: Any, *, initialize_on_startup: bool = False) -> None:
        """Register a resource before startup begins."""
        if self._startup_complete or self._startup_error is not None:
            raise RuntimeError("Cannot register resources after server startup")
        if self._shutdown_complete:
            raise RuntimeError("Cannot register resources after server shutdown")
        if initialize_on_startup and not callable(getattr(resource, "initialize", None)):
            raise TypeError("Startup resources must provide initialize()")
        self._owned.append(resource)
        if initialize_on_startup:
            self._startup_owned.append(resource)

    @property
    def owned_resources(self) -> list[Any]:
        """Return the live owned-resource list for adapter compatibility."""
        return self._owned

    @property
    def startup_resources(self) -> list[Any]:
        """Return the live startup-resource list for adapter compatibility."""
        return self._startup_owned

    async def startup(self) -> None:
        """Initialize startup-owned resources before request work."""
        async with self._lock:
            if self._shutdown_complete:
                raise RuntimeError("Cannot start a shut down MCP server")
            if self._startup_complete:
                return
            if self._startup_error is not None:
                raise self._startup_error
            try:
                for resource in self._startup_owned:
                    initialized = resource.initialize()
                    if inspect.isawaitable(initialized):
                        await initialized
            except Exception as exc:
                self._startup_error = exc
                raise
            self._startup_complete = True

    async def shutdown(self) -> None:
        """Close owned resources once, preserving control-bus drain failures."""
        async with self._lock:
            if self._shutdown_complete:
                return
            log.info("mcp.server.shutdown", name=self._server_name)
            for resource in self._owned:
                close_fn = getattr(resource, "close", None)
                if not callable(close_fn):
                    continue
                try:
                    await close_fn()
                except ControlBusDrainError:
                    log.error(
                        "mcp.server.control_bus_close_failed",
                        resource=type(resource).__name__,
                    )
                    raise
                except Exception as exc:
                    log.warning(
                        "mcp.server.resource_close_failed",
                        resource=type(resource).__name__,
                        error=str(exc),
                    )
            self._owned.clear()
            self._startup_owned.clear()
            self._shutdown_complete = True
