"""Console jobs: what a job records from its output outlives its bounded log."""

from __future__ import annotations

import re
import sys
import time

from ouroboros.gui import jobs


def _wait_done(job: jobs.Job, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while job.status == "running" and time.monotonic() < deadline:
        time.sleep(0.02)
    assert job.status != "running"


def test_watched_values_survive_the_bounded_log() -> None:
    script = (
        "print('Execution ID: exec_first00001')\n"
        "for i in range(2100): print(f'line {i}')\n"
        "print('again exec_second0002 at the end')\n"
    )
    job = jobs.JobRegistry().start(
        kind="auto",
        title="t",
        argv=[sys.executable, "-c", script],
        watch={"execution_id": re.compile(r"\b(exec_[A-Za-z0-9_-]{6,})\b")},
    )
    _wait_done(job)

    snap = job.snapshot()
    assert len(job.lines) == jobs._MAX_LINES
    assert not any("exec_first00001" in line for line in job.lines)
    assert snap["found"] == {"execution_id": "exec_second0002"}  # the latest match


def test_the_command_echo_is_not_read_as_output() -> None:
    job = jobs.JobRegistry().start(
        kind="auto",
        title="t",
        argv=[sys.executable, "-c", "pass", "exec_fromthegoal"],
        watch={"execution_id": re.compile(r"\b(exec_[A-Za-z0-9_-]{6,})\b")},
    )
    _wait_done(job)
    assert job.lines[0].startswith("$ ") and "exec_fromthegoal" in job.lines[0]
    assert job.snapshot()["found"] == {}


def test_a_job_without_watches_records_nothing() -> None:
    job = jobs.JobRegistry().start(
        kind="setup", title="t", argv=[sys.executable, "-c", "print('exec_abcdef123')"]
    )
    _wait_done(job)
    assert job.snapshot()["found"] == {}
