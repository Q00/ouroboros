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
import os
from pathlib import Path
import subprocess
import sys
import threading
from typing import Any

from ouroboros.mcp.job_manager import JobManager, JobSnapshot
from ouroboros.persistence.event_store import EventStore

_STARTUP_TIMEOUT_SECONDS = 20.0
_POLL_INTERVAL_SECONDS = 0.05
_STATE_DIR_NAME = "detached-jobs"


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
    threading.Thread(
        target=process.wait,
        name=f"ooo-detached-reaper-{process.pid}",
        daemon=True,
    ).start()
    return process


async def launch_detached_job(
    *,
    job_manager: JobManager,
    event_store: EventStore,
    request: DetachedJobRequest,
    startup_timeout_seconds: float = _STARTUP_TIMEOUT_SECONDS,
) -> JobSnapshot:
    """Spawn a durable owner and wait for its persisted acceptance receipt."""
    if request.database_url != event_store.database_url:
        raise ValueError("Detached worker request must use the accepting EventStore database URL")

    request_path = request_path_for(request.job_id)
    status_path = status_path_for(request_path)
    for stale in (request_path, status_path):
        try:
            stale.unlink()
        except FileNotFoundError:
            pass
    try:
        write_private_json(request_path, asdict(request), exclusive=True)
        process = _spawn_worker(request_path, cwd=request.cwd)
    except BaseException:
        job_manager.abandon_reserved_job_id(request.job_id)
        try:
            request_path.unlink()
        except OSError:
            pass
        raise

    deadline = asyncio.get_running_loop().time() + max(0.1, startup_timeout_seconds)
    while True:
        try:
            return await job_manager.get_snapshot(request.job_id)
        except ValueError:
            pass

        status = read_status(status_path)
        if status is not None and status.get("state") == "failed":
            error = str(status.get("error") or "detached worker failed before job acceptance")
            job_manager.abandon_reserved_job_id(request.job_id)
            for artifact in (request_path, status_path):
                try:
                    artifact.unlink()
                except OSError:
                    pass
            raise RuntimeError(error)

        if process.poll() is not None:
            # The reaper may already have consumed the exact return code; the
            # worker status is authoritative when present.
            status = read_status(status_path)
            error = (
                str(status.get("error"))
                if status is not None and status.get("error")
                else "detached worker exited before persisting job acceptance"
            )
            job_manager.abandon_reserved_job_id(request.job_id)
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
    "launch_detached_job",
    "read_status",
    "request_path_for",
    "status_path_for",
    "write_private_json",
]
