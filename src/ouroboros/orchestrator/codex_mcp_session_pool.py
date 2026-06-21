"""Persistent per-worker MCP session actor — enables multi-turn codex workers.

``codex mcp-server`` sessions are PROCESS-BOUND (in-memory): a ``threadId`` is
only addressable by the ``codex-reply`` of the SAME server process that created
it. To support multi-turn resume we must keep that process (and its
``ClientSession``) alive across turns.

The hard constraint is anyio: an MCP ``stdio_client`` / ``ClientSession`` uses
TASK-BOUND cancel scopes — opening it in one task and using/closing it from
another raises "cancel scope in a different task". The leader (ParallelExecutor)
drives turns of an AC across separate ``execute_task`` calls, potentially in
different tasks.

Solution — the ACTOR pattern: each session is owned by ONE dedicated background
task that opens, uses, and closes the connection entirely within itself. Callers
from any task communicate via a queue + per-call futures (both task-agnostic), so
the connection never crosses a task boundary. An idle-TTL closes the connection
if no turn arrives, bounding zombie ``codex mcp-server`` processes (see
``mcp-zombie-lifecycle-audit``).
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from ouroboros.core.types import Result
from ouroboros.mcp.client.adapter import create_mcp_client
from ouroboros.mcp.types import MCPServerConfig, MCPToolResult
from ouroboros.observability.logging import get_logger

log = get_logger(__name__)

_CLOSE = object()  # close sentinel placed on the request queue

# Default seconds an idle session connection stays warm before self-closing.
DEFAULT_SESSION_IDLE_TIMEOUT = 120.0


class MCPSessionActor:
    """Owns ONE MCP server connection in a dedicated task; serves tool calls via a queue."""

    def __init__(
        self,
        server_config: MCPServerConfig,
        *,
        idle_timeout: float = DEFAULT_SESSION_IDLE_TIMEOUT,
    ) -> None:
        self._config = server_config
        self._idle_timeout = idle_timeout
        self._requests: asyncio.Queue[Any] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._start_error: str | None = None
        self._closing = False

    @property
    def is_alive(self) -> bool:
        return self._task is not None and not self._task.done() and not self._closing

    async def _run(self) -> None:
        try:
            async with create_mcp_client(self._config) as client:
                self._ready.set()
                while True:
                    try:
                        item = await asyncio.wait_for(self._requests.get(), self._idle_timeout)
                    except TimeoutError:
                        return  # idle → close the connection (inside THIS task)
                    if item is _CLOSE:
                        return
                    tool, args, fut = item
                    if fut.done():  # caller already gave up
                        continue
                    try:
                        result = await client.call_tool(tool, args)
                        if not fut.done():
                            fut.set_result(result)
                    except Exception as exc:  # noqa: BLE001 — propagate to the caller's future
                        if not fut.done():
                            fut.set_exception(exc)
        except Exception as exc:  # noqa: BLE001 — connection/startup failure
            self._start_error = str(exc)
            log.warning("codex_mcp_session.start_failed", error=str(exc))
        finally:
            self._ready.set()
            self._fail_pending()

    def _fail_pending(self) -> None:
        err = ConnectionError(self._start_error or "codex mcp session closed")
        while not self._requests.empty():
            try:
                item = self._requests.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover
                break
            if item is _CLOSE:
                continue
            _, _, fut = item
            if not fut.done():
                fut.set_exception(err)

    async def call(self, tool: str, arguments: dict[str, Any]) -> Result[MCPToolResult, str]:
        """Run one tool call on this session's persistent connection."""
        if self._closing:
            return Result.err("codex mcp session is closing")
        if self._task is None:
            self._task = asyncio.create_task(self._run())
        await self._ready.wait()
        if self._start_error is not None:
            return Result.err(self._start_error)
        if self._task is None or self._task.done():
            return Result.err("codex mcp session is no longer alive")

        fut: asyncio.Future[Result[MCPToolResult, Any]] = asyncio.get_running_loop().create_future()
        await self._requests.put((tool, arguments, fut))

        # Backstop the idle-close race: if the actor task finishes (idle timeout)
        # without resolving our future, fail the call rather than hang forever.
        waitables: set[asyncio.Future[Any]] = {fut, self._task}
        done, _ = await asyncio.wait(waitables, return_when=asyncio.FIRST_COMPLETED)
        if fut not in done and not fut.done():
            return Result.err("codex mcp session closed before the request was served")

        try:
            result = await fut
        except Exception as exc:  # noqa: BLE001
            return Result.err(str(exc))
        if result.is_err:
            return Result.err(result.error.message)
        return Result.ok(result.value)

    async def aclose(self) -> None:
        """Close the session connection (cross-task safe: the close happens in the actor task)."""
        self._closing = True
        if self._task is None:
            return
        if not self._task.done():
            with contextlib.suppress(Exception):
                self._requests.put_nowait(_CLOSE)
        with contextlib.suppress(Exception):
            await self._task


__all__ = ["DEFAULT_SESSION_IDLE_TIMEOUT", "MCPSessionActor"]
