"""Two-phase generation claim handoff for background evolve jobs."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Protocol, cast

from ouroboros.core.types import Result
from ouroboros.evolution import loop_support
from ouroboros.mcp.errors import MCPServerError, MCPToolError
from ouroboros.mcp.job_manager import JobLinks
from ouroboros.mcp.tools.background import WorkFn
from ouroboros.mcp.types import MCPToolResult

_CLAIM_PREPARATION_TIMEOUT_SECONDS = 1.0


def evolve_lineage_busy_error(lineage_id: str) -> MCPToolError:
    """Return the bounded Start-surface conflict for an owned lineage."""
    return MCPToolError(
        (
            f"Lineage {lineage_id!r} already has an active evolve request. "
            "Wait for that owner to finish, then retry instead of starting a duplicate."
        ),
        tool_name="ouroboros_start_evolve_step",
        error_code="evolve_lineage_busy",
        is_retriable=True,
        details={"lineage_id": lineage_id},
    )


class ClaimableEvolveHandler(Protocol):
    """Handler surface needed to hold work at its durable core claim."""

    async def handle(
        self,
        arguments: dict[str, object],
        *,
        on_generation_claimed: Callable[[int], Awaitable[None]] | None = None,
    ) -> Result[MCPToolResult, MCPServerError]: ...


class PreparedEvolveClaim:
    """Hold an evolve request until its exact generation link is persisted."""

    def __init__(
        self,
        handler: ClaimableEvolveHandler,
        arguments: dict[str, object],
        lineage_id: str,
    ) -> None:
        self._handler = handler
        self._arguments = arguments
        self._lineage_id = lineage_id
        self._task: asyncio.Task[Result[MCPToolResult, MCPServerError]] | None = None
        self._release = asyncio.Event()

    async def prepare(self) -> tuple[JobLinks, WorkFn]:
        """Start work through claim acquisition and return its exact job link."""
        claimed: asyncio.Future[int] = asyncio.get_running_loop().create_future()

        async def publish_claim(generation_number: int) -> None:
            if not claimed.done():
                claimed.set_result(generation_number)
            await self._release.wait()

        self._task = asyncio.create_task(
            self._handler.handle(
                self._arguments,
                on_generation_claimed=publish_claim,
            )
        )
        waiters = {
            cast(asyncio.Future[object], claimed),
            cast(asyncio.Future[object], self._task),
        }
        done, _pending = await asyncio.wait(
            waiters,
            return_when=asyncio.FIRST_COMPLETED,
            timeout=_CLAIM_PREPARATION_TIMEOUT_SECONDS,
        )
        if not done:
            await self.abort()
            raise evolve_lineage_busy_error(self._lineage_id)
        if self._task in done and not claimed.done():
            result = self._task.result()
            if result.is_err:
                raise result.error
            raise RuntimeError("evolve_step completed before claiming a generation")

        execution_id = loop_support.generation_execution_id(
            self._lineage_id,
            claimed.result(),
        )
        return JobLinks(lineage_id=self._lineage_id, execution_id=execution_id), self.run

    async def run(self, _handle: object) -> MCPToolResult:
        """Release the claimed request only after the job is durably queued."""
        self._release.set()
        if self._task is None:  # pragma: no cover - start pipeline contract.
            raise RuntimeError("Prepared evolve claim has not been started")
        result = await self._task
        if result.is_err:
            raise RuntimeError(str(result.error))
        return result.value

    async def abort(self) -> None:
        """Cancel held work so durable handler/core claims are released."""
        if self._task is None or self._task.done():
            return
        self._task.cancel()
        cleanup_cancellation: asyncio.CancelledError | None = None
        owner = asyncio.current_task()
        initial_cancels = owner.cancelling() if owner is not None else 0
        while not self._task.done():
            try:
                await asyncio.shield(self._task)
            except asyncio.CancelledError as exc:
                # The held handler is expected to finish cancelled. A separate
                # cancellation of this cleanup owner must not be forwarded as
                # a second cancellation into the handler while its durable
                # claim transaction is still settling.
                if owner is not None and owner.cancelling() > initial_cancels:
                    cleanup_cancellation = exc
                continue
        with suppress(asyncio.CancelledError):
            self._task.result()
        if cleanup_cancellation is not None:
            raise cleanup_cancellation

    async def abort_on_failure(self, _error: BaseException) -> None:
        """Adapt abort cleanup to the background enqueue-failure hook."""
        await self.abort()
