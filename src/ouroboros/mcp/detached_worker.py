"""Detached background-job worker entrypoint.

This process owns exactly one accepted top-level Start* job.  It re-enters the
normal handler with a one-shot forced job id, then remains alive until that job
has a durable terminal event.  Nested jobs are detached normally, so an auto
worker may finish after handing off a run without killing that run.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
from typing import Any

from ouroboros.mcp.detached_jobs import (
    DetachedJobRequest,
    cleanup_worker_artifacts,
    status_path_for,
    write_private_json,
)
from ouroboros.mcp.job_manager import JobLinks, JobManager
from ouroboros.mcp.server.adapter import create_ouroboros_server
from ouroboros.mcp.tools.background import start_background_tool_job
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.persistence.event_store import EventStore

_TERMINAL_POLL_SECONDS = 0.1
_TASK_RELEASE_GRACE_SECONDS = 5.0


def _load_request(path: Path) -> DetachedJobRequest:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Detached job request must be a JSON object")
    request = DetachedJobRequest.from_json(payload)
    path.unlink(missing_ok=True)
    return request


def _status(path: Path, state: str, **extra: Any) -> None:
    write_private_json(
        status_path_for(path),
        {"state": state, "worker_pid": os.getpid(), **extra},
        exclusive=False,
    )


async def _record_start_failure(
    manager: JobManager,
    request: DetachedJobRequest,
    error: str,
) -> None:
    """Create a pollable failed job when a handler rejects inside the worker."""

    async def _failed() -> MCPToolResult:
        return MCPToolResult(
            content=(MCPContentItem(type=ContentType.TEXT, text=error),),
            is_error=True,
            meta={"status": "failed", "detached_worker_start_failure": True},
        )

    try:
        snapshot = await manager.get_snapshot(request.job_id)
    except ValueError:
        await manager.start_job(
            job_type=request.tool_name.removeprefix("ouroboros_start_") or "detached_job",
            initial_message="Detached worker start failed",
            runner=_failed(),
            links=JobLinks(),
            job_id=request.job_id,
        )
        return
    if not snapshot.is_terminal:
        await manager.drain(grace_seconds=0.5)


async def _run_probe(manager: JobManager, request: DetachedJobRequest) -> None:
    """Internal process-lifetime probe used by integration tests."""
    delay_raw = request.arguments.get("delay_seconds", 0.2)
    delay = float(delay_raw) if isinstance(delay_raw, int | float) else 0.2

    async def _probe() -> MCPToolResult:
        await asyncio.sleep(max(0.0, delay))
        return MCPToolResult(
            content=(MCPContentItem(type=ContentType.TEXT, text="detached probe complete"),),
            is_error=False,
            meta={"status": "completed", "probe": True},
        )

    await manager.start_job(
        job_type="detached_probe",
        initial_message="Queued detached ownership probe",
        runner=_probe(),
        links=JobLinks(),
        job_id=await manager.allocate_job_id(),
    )


async def _run_nested_probe(
    manager: JobManager,
    event_store: EventStore,
    request: DetachedJobRequest,
) -> None:
    """Internal proof that a worker's nested job gets a new durable owner."""
    delay_raw = request.arguments.get("nested_delay_seconds", 2.0)
    delay = float(delay_raw) if isinstance(delay_raw, int | float) else 2.0

    async def _outer() -> MCPToolResult:
        async def _unused_inline_work(_handle: object) -> MCPToolResult:
            raise AssertionError("nested durable probe unexpectedly ran inline")

        nested = await start_background_tool_job(
            job_manager=manager,
            event_store=event_store,
            job_type="detached_probe",
            intent="detached_probe",
            process_scope="detached_probe:nested",
            initial_message="Queued nested detached ownership probe",
            links=JobLinks(),
            work_fn=_unused_inline_work,
            cancelled_text="Nested detached probe cancelled before work began.",
            detached_tool_name="__detached_probe__",
            detached_arguments={"delay_seconds": delay},
        )
        return MCPToolResult(
            content=(MCPContentItem(type=ContentType.TEXT, text="nested probe accepted"),),
            is_error=False,
            meta={"status": "completed", "nested_job_id": nested.job_id},
        )

    top_job_id = await manager.allocate_job_id()
    manager.claim_forced_inline_allocation(top_job_id)
    await manager.start_job(
        job_type="detached_nested_probe",
        initial_message="Queued nested ownership probe",
        runner=_outer(),
        links=JobLinks(),
        job_id=top_job_id,
    )


async def _wait_for_terminal(manager: JobManager, job_id: str) -> None:
    while True:
        snapshot = await manager.get_snapshot(job_id)
        if snapshot.is_terminal:
            break
        await asyncio.sleep(_TERMINAL_POLL_SECONDS)

    # The terminal row is persisted before _run_job releases monitors and its
    # durability backstop.  Give that local cleanup a bounded chance to finish
    # before closing the worker's EventStore.
    deadline = asyncio.get_running_loop().time() + _TASK_RELEASE_GRACE_SECONDS
    while manager.has_live_job_task(job_id) and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(_TERMINAL_POLL_SECONDS)


async def run_worker(request_path: Path) -> int:
    request: DetachedJobRequest | None = None
    event_store: EventStore | None = None
    server = None
    manager: JobManager | None = None
    cleanup_artifacts = False
    try:
        request = _load_request(request_path)
        os.chdir(request.cwd)
        event_store = EventStore(request.database_url)
        server = create_ouroboros_server(
            event_store=event_store,
            runtime_backend=request.runtime_backend,
            llm_backend=request.llm_backend,
            opencode_mode=request.opencode_mode,
            durable_jobs=True,
            forced_inline_job_id=request.job_id,
        )
        manager = server.job_manager
        if not isinstance(manager, JobManager):
            raise RuntimeError("Detached worker composition did not provide a JobManager")

        if request.tool_name == "__detached_probe__":
            await _run_probe(manager, request)
        elif request.tool_name == "__detached_nested_probe__":
            await _run_nested_probe(manager, event_store, request)
        else:
            handler = server._tool_handlers.get(request.tool_name)  # noqa: SLF001
            if handler is None:
                raise ValueError(f"Unknown detached Start tool: {request.tool_name}")
            result = await handler.handle(dict(request.arguments))
            if result.is_err:
                error = result.error.message
                await _record_start_failure(manager, request, error)
            else:
                returned_job_id = result.value.meta.get("job_id")
                if returned_job_id != request.job_id:
                    raise RuntimeError(
                        "Detached Start handler returned an unexpected job id: "
                        f"expected {request.job_id}, got {returned_job_id!r}"
                    )

        # Acceptance becomes externally visible only after mcp.job.created is
        # durable.  The parent polls that event; this status gives diagnostics
        # for launch races and records the actual owner PID.
        await manager.get_snapshot(request.job_id)
        _status(request_path, "accepted", job_id=request.job_id)
        await _wait_for_terminal(manager, request.job_id)
        _status(request_path, "completed", job_id=request.job_id)
        cleanup_artifacts = True
        return 0
    except BaseException as exc:
        error = f"{type(exc).__name__}: {exc}"
        try:
            _status(request_path, "failed", error=error)
        except Exception:
            pass
        if manager is not None and request is not None:
            try:
                await _record_start_failure(manager, request, error)
                await _wait_for_terminal(manager, request.job_id)
            except Exception:
                pass
        return 1
    finally:
        if server is not None:
            try:
                await server.shutdown()
            except Exception:
                pass
        elif event_store is not None:
            try:
                await event_store.close()
            except Exception:
                pass
        if request is not None and cleanup_artifacts:
            cleanup_worker_artifacts(request_path)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: python -m ouroboros.mcp.detached_worker REQUEST.json", file=sys.stderr)
        return 2
    return asyncio.run(run_worker(Path(args[0]).expanduser().resolve()))


if __name__ == "__main__":  # pragma: no cover - exercised through subprocess tests
    raise SystemExit(main())
