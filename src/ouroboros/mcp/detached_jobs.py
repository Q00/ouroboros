"""Durable process ownership for background MCP jobs.

An MCP stdio server belongs to a client turn, not to the work it starts.  A
Codex or Claude client can close stdin as soon as that turn ends, which tears
down every asyncio task owned by the server.  This module moves each Start*
job into a small detached worker process that opens the same EventStore and
re-enters the same handler.  The MCP process remains a control client only.

The request file is mode 0600 because tool arguments may contain source code,
requirements, or other project data.  It is deleted by the worker immediately
after loading.  A short-lived status file covers failures before the worker can
create the durable ``mcp.job.created`` event.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import asdict, dataclass
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
from typing import Any

from ouroboros.mcp.errors import MCPToolError
from ouroboros.mcp.job_manager import JobManager, JobSnapshot
from ouroboros.persistence.event_store import EventStore

_STARTUP_TIMEOUT_SECONDS = 20.0
_POLL_INTERVAL_SECONDS = 0.05
_STATE_DIR_NAME = "detached-jobs"
_TOOL_ERROR_REJECTION_PROTOCOL = "ouroboros.detached.mcp-tool-error"
_TOOL_ERROR_REJECTION_VERSION = 1
_TOOL_ERROR_REJECTION_FIELDS = frozenset(
    {
        "protocol",
        "version",
        "message",
        "server_name",
        "tool_name",
        "error_code",
        "is_retriable",
        "details",
    }
)
_REJECTED_STATUS_FIELDS = frozenset(
    {"state", "worker_pid", "job_id", "request_tool_name", "rejection"}
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DetachedJobRequest:
    """Serializable instruction consumed by :mod:`detached_worker`."""

    job_id: str
    tool_name: str
    arguments: dict[str, Any]
    database_url: str
    cwd: str
    runtime_backend: str | None = None
    llm_backend: str | None = None
    opencode_mode: str | None = None

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> DetachedJobRequest:
        arguments = payload.get("arguments")
        if not isinstance(arguments, dict):
            raise ValueError("Detached job arguments must be a JSON object")
        required_values = (
            ("job_id", payload.get("job_id")),
            ("tool_name", payload.get("tool_name")),
            ("database_url", payload.get("database_url")),
            ("cwd", payload.get("cwd")),
        )
        for name, value in required_values:
            if not isinstance(value, str) or not value:
                raise ValueError(f"Detached job request requires non-empty {name}")
        job_id = str(payload["job_id"])
        tool_name = str(payload["tool_name"])
        database_url = str(payload["database_url"])
        cwd = str(payload["cwd"])
        return cls(
            job_id=job_id,
            tool_name=tool_name,
            arguments=dict(arguments),
            database_url=database_url,
            cwd=cwd,
            runtime_backend=_optional_string(payload.get("runtime_backend")),
            llm_backend=_optional_string(payload.get("llm_backend")),
            opencode_mode=_optional_string(payload.get("opencode_mode")),
        )


class DetachedJobAcceptanceTimeout(TimeoutError):
    """A live worker did not persist acceptance before the client deadline."""

    def __init__(self, *, job_id: str, worker_pid: int, timeout_seconds: float) -> None:
        self.job_id = job_id
        self.worker_pid = worker_pid
        self.timeout_seconds = timeout_seconds
        self.receipt: dict[str, Any] = {
            "status": "acceptance_pending",
            "job_id": job_id,
            "worker_pid": worker_pid,
            "startup_timeout_seconds": timeout_seconds,
            "worker_continues": True,
            "retry_start_allowed": False,
            "status_check": {
                "tool": "ouroboros_job_status",
                "arguments": {"job_id": job_id},
            },
        }
        super().__init__(
            "Detached worker did not persist job acceptance within "
            f"{timeout_seconds:g}s (job_id={job_id}, pid={worker_pid})"
        )


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def detached_jobs_dir() -> Path:
    """Return the private request/status directory, creating it if needed."""
    path = Path.home() / ".ouroboros" / _STATE_DIR_NAME
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):
        path.chmod(0o700)
    return path


def request_path_for(job_id: str) -> Path:
    return detached_jobs_dir() / f"{job_id}.json"


def status_path_for(request_path: Path) -> Path:
    return request_path.with_suffix(".status.json")


def write_private_json(path: Path, payload: dict[str, Any], *, exclusive: bool) -> None:
    """Write JSON with owner-only permissions and atomic replacement."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    if exclusive:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, encoded)
        finally:
            os.close(fd)
        return

    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, encoded)
    finally:
        os.close(fd)
    os.replace(temp, path)


def read_status(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def encode_tool_error_rejection(error: MCPToolError) -> dict[str, Any]:
    """Encode a pre-acceptance tool rejection without serializing a class name.

    Detached status files are data, never an exception-object transport.  The
    fixed protocol discriminator and explicit scalar fields keep decoding
    closed to arbitrary class construction while preserving the public
    ``MCPToolError`` contract across the process boundary.
    """
    details = error.details
    if details is not None:
        if not isinstance(details, dict):
            raise ValueError("Detached tool-error details must be a JSON object")
        try:
            normalized_details = json.loads(
                json.dumps(details, ensure_ascii=False, allow_nan=False)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("Detached tool-error details must be JSON-safe") from exc
        if not isinstance(normalized_details, dict):  # pragma: no cover - guarded above.
            raise ValueError("Detached tool-error details must be a JSON object")
        if normalized_details != details:
            raise ValueError("Detached tool-error details must use canonical JSON values")
    else:
        normalized_details = None
    return {
        "protocol": _TOOL_ERROR_REJECTION_PROTOCOL,
        "version": _TOOL_ERROR_REJECTION_VERSION,
        "message": error.message,
        "server_name": error.server_name,
        "tool_name": error.tool_name,
        "error_code": error.error_code,
        "is_retriable": error.is_retriable,
        "details": normalized_details,
    }


def decode_tool_error_rejection(payload: object) -> MCPToolError:
    """Validate and reconstruct one detached pre-acceptance rejection."""
    if not isinstance(payload, dict):
        raise ValueError("Detached tool-error rejection must be a JSON object")
    if set(payload) != _TOOL_ERROR_REJECTION_FIELDS:
        raise ValueError("Detached tool-error rejection has unexpected fields")
    if payload.get("protocol") != _TOOL_ERROR_REJECTION_PROTOCOL:
        raise ValueError("Detached tool-error rejection has an unknown protocol")
    version = payload.get("version")
    if type(version) is not int or version != _TOOL_ERROR_REJECTION_VERSION:
        raise ValueError("Detached tool-error rejection has an unsupported version")

    message = payload.get("message")
    if not isinstance(message, str):
        raise ValueError("Detached tool-error rejection requires a string message")
    server_name = payload.get("server_name")
    if server_name is not None and not isinstance(server_name, str):
        raise ValueError("Detached tool-error rejection has an invalid server_name")
    tool_name = payload.get("tool_name")
    if tool_name is not None and not isinstance(tool_name, str):
        raise ValueError("Detached tool-error rejection has an invalid tool_name")
    error_code = payload.get("error_code")
    if error_code is not None and not isinstance(error_code, str):
        raise ValueError("Detached tool-error rejection has an invalid error_code")
    is_retriable = payload.get("is_retriable")
    if not isinstance(is_retriable, bool):
        raise ValueError("Detached tool-error rejection requires boolean is_retriable")
    details = payload.get("details")
    if details is not None and not isinstance(details, dict):
        raise ValueError("Detached tool-error rejection details must be a JSON object")
    try:
        json.dumps(details, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("Detached tool-error rejection details must be JSON-safe") from exc

    return MCPToolError(
        message,
        server_name=server_name,
        tool_name=tool_name,
        error_code=error_code,
        is_retriable=is_retriable,
        details=details,
    )


def _preacceptance_rejection(
    status: dict[str, Any] | None,
    *,
    job_id: str,
    request_tool_name: str,
    worker_pid: int,
) -> MCPToolError | None:
    if status is None or status.get("state") != "rejected":
        return None
    if set(status) != _REJECTED_STATUS_FIELDS:
        raise RuntimeError("Detached worker rejection has unexpected fields")
    if status.get("job_id") != job_id:
        raise RuntimeError("Detached worker rejection has an unexpected job id")
    if status.get("request_tool_name") != request_tool_name:
        raise RuntimeError("Detached worker rejection has an unexpected request tool")
    if status.get("worker_pid") != worker_pid:
        raise RuntimeError("Detached worker rejection has an unexpected worker pid")
    try:
        return decode_tool_error_rejection(status.get("rejection"))
    except ValueError as exc:
        raise RuntimeError(f"Malformed detached worker rejection: {exc}") from exc


def _spawn_worker(request_path: Path, *, cwd: str) -> subprocess.Popen[bytes]:
    env = os.environ.copy()
    env["OUROBOROS_DETACHED_JOB_WORKER"] = "1"
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "cwd": cwd,
        "env": env,
        "close_fds": True,
    }
    if os.name == "posix":
        kwargs["start_new_session"] = True
    else:  # pragma: no cover - exercised on Windows CI/hosts only
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0
        )
    process = subprocess.Popen(  # noqa: S603 - fixed interpreter/module argv
        [sys.executable, "-m", "ouroboros.mcp.detached_worker", str(request_path)],
        **kwargs,
    )
    # Reap the child if this MCP process remains alive.  If the MCP process
    # exits first, the detached worker is reparented and the OS reaps it.
    try:
        threading.Thread(
            target=process.wait,
            name=f"ooo-detached-reaper-{process.pid}",
            daemon=True,
        ).start()
    except BaseException:
        # Popen already transferred ownership to an independent process. A
        # best-effort local reaper failure must not report definitive launch
        # rejection and allow a duplicate job to start.
        try:
            logger.warning(
                "detached worker reaper registration failed after spawn",
                extra={"worker_pid": process.pid},
                exc_info=True,
            )
        except BaseException:
            pass
    return process


async def launch_detached_job(
    *,
    job_manager: JobManager,
    event_store: EventStore,
    request: DetachedJobRequest,
    startup_timeout_seconds: float = _STARTUP_TIMEOUT_SECONDS,
) -> JobSnapshot:
    """Spawn a durable owner and wait for its persisted acceptance receipt."""
    request_path: Path | None = None
    status_path: Path | None = None
    try:
        if request.database_url != event_store.database_url:
            raise ValueError(
                "Detached worker request must use the accepting EventStore database URL"
            )
        request_path = request_path_for(request.job_id)
        status_path = status_path_for(request_path)
        for stale in (request_path, status_path):
            try:
                stale.unlink()
            except FileNotFoundError:
                pass
        write_private_json(request_path, asdict(request), exclusive=True)
        process = _spawn_worker(request_path, cwd=request.cwd)
    except BaseException:
        job_manager.abandon_reserved_job_id(request.job_id)
        for artifact in (request_path, status_path):
            if artifact is None:
                continue
            try:
                artifact.unlink()
            except OSError:
                pass
        raise

    # From successful Popen onward, the worker may accept independently. No
    # ordinary receipt/read failure below is definitive launch rejection.
    assert request_path is not None
    assert status_path is not None

    deadline = asyncio.get_running_loop().time() + max(0.1, startup_timeout_seconds)
    while True:
        try:
            return await job_manager.get_snapshot(request.job_id)
        except ValueError:
            pass

        status = read_status(status_path)
        try:
            rejection = _preacceptance_rejection(
                status,
                job_id=request.job_id,
                request_tool_name=request.tool_name,
                worker_pid=process.pid,
            )
        except RuntimeError:
            job_manager.abandon_reserved_job_id(request.job_id)
            cleanup_worker_artifacts(request_path)
            raise
        if rejection is not None:
            job_manager.abandon_reserved_job_id(request.job_id)
            cleanup_worker_artifacts(request_path)
            raise rejection
        if process.poll() is not None:
            # A failed status can precede the worker's fallback created receipt,
            # and a created commit can race the initial read immediately before
            # process exit. Once the child is dead no further writes are
            # possible, so only this final durable read can prove rejection.
            try:
                snapshot = await job_manager.get_snapshot(request.job_id)
            except ValueError:
                pass
            else:
                cleanup_worker_artifacts(request_path)
                return snapshot
            status = read_status(status_path)
            try:
                rejection = _preacceptance_rejection(
                    status,
                    job_id=request.job_id,
                    request_tool_name=request.tool_name,
                    worker_pid=process.pid,
                )
            except RuntimeError:
                job_manager.abandon_reserved_job_id(request.job_id)
                cleanup_worker_artifacts(request_path)
                raise
            if rejection is not None:
                job_manager.abandon_reserved_job_id(request.job_id)
                cleanup_worker_artifacts(request_path)
                raise rejection
            error = (
                str(status.get("error"))
                if status is not None and status.get("error")
                else "detached worker exited before persisting job acceptance"
            )
            job_manager.abandon_reserved_job_id(request.job_id)
            cleanup_worker_artifacts(request_path)
            raise RuntimeError(error)

        if asyncio.get_running_loop().time() >= deadline:
            # Do not kill a live worker: doing so can turn an accepted request
            # into exactly the interrupted state this boundary exists to avoid.
            # Surface the stable job id so a caller can retry/status-check; the
            # request remains owned by the live process.
            raise DetachedJobAcceptanceTimeout(
                job_id=request.job_id,
                worker_pid=process.pid,
                timeout_seconds=startup_timeout_seconds,
            )
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)


def cleanup_worker_artifacts(request_path: Path) -> None:
    """Best-effort cleanup after a worker reaches a terminal state."""
    for artifact in (request_path, status_path_for(request_path)):
        try:
            artifact.unlink()
        except OSError:
            pass


__all__ = [
    "DetachedJobAcceptanceTimeout",
    "DetachedJobRequest",
    "cleanup_worker_artifacts",
    "decode_tool_error_rejection",
    "encode_tool_error_rejection",
    "launch_detached_job",
    "read_status",
    "request_path_for",
    "status_path_for",
    "write_private_json",
]
