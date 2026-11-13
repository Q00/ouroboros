"""Background CLI jobs the console starts on the user's behalf.

The console never re-implements a CLI flow. Connecting a tool runs
``ouroboros setup --runtime <id> --non-interactive`` in a child process —
the exact command the docs and the ``ooo setup`` skill run — and streams its
output to the page. Jobs live in memory only; the files the command writes are
the record.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import os
import re
import subprocess
import sys
import threading
import time
from typing import Any
import uuid

_MAX_LINES = 2000
_MAX_JOBS = 50
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")


def ouroboros_command(*args: str) -> list[str]:
    """Run the same Ouroboros install that serves the console."""
    return [sys.executable, "-m", "ouroboros", *args]


def display_command(argv: list[str]) -> str:
    """Render an argv the way a user would type it."""
    prefix = ouroboros_command()
    if argv[: len(prefix)] == prefix:
        return " ".join(["ouroboros", *argv[len(prefix) :]])
    return " ".join(argv)


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    # Plain text: the page renders lines itself, and rich must not wrap them.
    env.update({"NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "120", "PYTHONUNBUFFERED": "1"})
    env.pop("FORCE_COLOR", None)
    return env


@dataclass(slots=True)
class Job:
    """One child process and everything it printed."""

    id: str
    kind: str
    title: str
    argv: list[str]
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    exit_code: int | None = None
    status: str = "running"  # running | succeeded | failed
    lines: deque[str] = field(default_factory=lambda: deque(maxlen=_MAX_LINES))
    meta: dict[str, Any] = field(default_factory=dict)
    _cursor: int = 0  # total lines ever appended, for incremental reads
    _proc: subprocess.Popen[str] | None = None
    cancelled: bool = False

    def cancel(self) -> None:
        """Ask the child to stop (SIGTERM); the reader thread records the exit."""
        self.cancelled = True
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()

    def snapshot(self, since: int = 0) -> dict[str, Any]:
        first = self._cursor - len(self.lines)
        start = max(since, first)
        new_lines = list(self.lines)[start - first :]
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "command": display_command(self.argv),
            "status": self.status,
            "exit_code": self.exit_code,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "cursor": self._cursor,
            "lines": new_lines,
            "meta": self.meta,
        }

    def append(self, line: str) -> None:
        self.lines.append(line)
        self._cursor += 1


class JobRegistry:
    """Start child processes and keep their output for the page to poll."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def start(
        self,
        *,
        kind: str,
        title: str,
        argv: list[str],
        meta: dict[str, Any] | None = None,
        on_exit: Any = None,
        cwd: str | None = None,
    ) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, title=title, argv=argv, meta=meta or {})
        with self._lock:
            self._jobs[job.id] = job
            self._trim()
        thread = threading.Thread(
            target=self._run, args=(job, on_exit, cwd), name=f"gui-job-{job.id}", daemon=True
        )
        thread.start()
        return job

    def _trim(self) -> None:
        if len(self._jobs) <= _MAX_JOBS:
            return
        finished = sorted(
            (j for j in self._jobs.values() if j.status != "running"),
            key=lambda j: j.started_at,
        )
        for job in finished[: len(self._jobs) - _MAX_JOBS]:
            self._jobs.pop(job.id, None)

    def _run(self, job: Job, on_exit: Any, cwd: str | None) -> None:
        job.append("$ " + display_command(job.argv))
        try:
            proc = subprocess.Popen(  # noqa: S603 - argv built from fixed tokens
                job.argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=_child_env(),
                cwd=cwd,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as exc:
            job.append(f"Could not start: {exc}")
            job.status, job.exit_code, job.finished_at = "failed", -1, time.time()
            return
        job._proc = proc
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = _ANSI.sub("", raw.rstrip("\n"))
            job.append(line)
        job.exit_code = proc.wait()
        job._proc = None
        if job.cancelled:
            job.append("Stopped.")
        job.status = "succeeded" if job.exit_code == 0 else "failed"
        if on_exit is not None:
            try:
                note = on_exit(job)
            except Exception as exc:  # report, never crash the worker thread
                note = f"Follow-up step failed: {exc}"
                job.status = "failed"
            if note:
                job.append(note)
        job.finished_at = time.time()

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.started_at, reverse=True)
        return [{**j.snapshot(since=j._cursor), "lines": []} for j in jobs]

    def running_for(self, kind: str, key: str) -> Job | None:
        with self._lock:
            for job in self._jobs.values():
                if job.kind == kind and job.status == "running" and job.meta.get("key") == key:
                    return job
        return None


__all__ = ["Job", "JobRegistry", "ouroboros_command"]
