"""One absolute deadline bounds a check process from launch to reaping.

A script check or an oracle target that leaves a process behind (in its own
process group, or escaped from it with ``setsid``) holding its output pipe
must not keep the controller past the check's timeout, and the process group
is killed whether or not its leader has already exited.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import signal
import sys
import time

import pytest

from ouroboros.boundary.admission import admit_check_package
from ouroboros.boundary.binding import Binding, CallKind
from ouroboros.boundary.check_env import pin_interpreter
from ouroboros.boundary.oracle import (
    ORACLE_DATA_PATH,
    ORACLE_HARNESS_PATH,
    ORACLE_HARNESS_SOURCE,
    OracleCase,
    OracleExpectation,
    OracleSpec,
    oracle_data_text,
)
from ouroboros.boundary.oracle_run import REAP_MARGIN_SECONDS, run_oracle_check
from ouroboros.boundary.receipts import CheckStatus

from .conftest import build_package

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")

TIMEOUT = 1
# Scheduling slack for a loaded test host (``-n 8``); far below the 10 s drain
# window and the 30 s child that the controller used to wait for.
SLACK = 2.0


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _gone_within(pid: int, seconds: float) -> bool:
    """Whether ``pid`` no longer exists within ``seconds`` (init reaps an orphan's zombie)."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


def _stop(pidfile: Path) -> None:
    try:
        os.kill(int(pidfile.read_text()), signal.SIGKILL)
    except (OSError, ValueError):
        pass


async def test_a_script_whose_child_outlives_it_ends_on_the_deadline(
    tmp_path: Path, seed, base_checkout
) -> None:
    # The leader exits at once; its child stays in the process group and
    # holds both output pipes for 30 s.
    pidfile = tmp_path / "child.pid"
    script = (
        "import subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        f"open({str(pidfile)!r}, 'w').write(str(child.pid))\n"
    )
    package = build_package(seed, preserve_script=script)
    started = time.monotonic()
    try:
        result = await asyncio.wait_for(
            admit_check_package(
                package, base_checkout, work_dir=tmp_path / "w", timeout_seconds=TIMEOUT
            ),
            timeout=60,
        )
        elapsed = time.monotonic() - started
        pid = int(pidfile.read_text())
        assert _gone_within(pid, 1.0), "the child the check left in its group is still alive"
    finally:
        _stop(pidfile)
    preserve = next(check for check in result.checks if check.check_id == "script_2_1")
    assert preserve.status is CheckStatus.INDETERMINATE
    assert preserve.reason == "timeout" and preserve.timed_out
    # Two checks run one after the other, each bounded by its own deadline.
    assert elapsed < 2 * (TIMEOUT + REAP_MARGIN_SECONDS) + SLACK, elapsed


def _holder_spec() -> OracleSpec:
    return OracleSpec(
        criterion_key="ac_inc",
        check_id="oracle_1",
        call_kind=CallKind.FUNCTION,
        params=("x",),
        default_binding=Binding(
            criterion_key="ac_inc", symbol="inc.inc", call_kind=CallKind.FUNCTION
        ),
        cases=(
            OracleCase(
                case_id="c1",
                args={"x": 1},
                expect=OracleExpectation(kind="returns", value=2),
                held_out=True,
            ),
        ),
    )


# The target returns the right value, but first leaves a process that escaped
# its group (``setsid``) and holds every pipe the target had for 5 s.
HOLDER = """import os, time


def inc(x):
    if os.fork() == 0:
        os.setsid()
        open({pidfile!r}, "w").write(str(os.getpid()))
        time.sleep(5)
        os._exit(0)
    time.sleep(0.2)
    return x + 1
"""


async def test_an_oracle_never_passes_past_its_deadline(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    pidfile = tmp_path / "holder.pid"
    (root / "inc.py").write_text(HOLDER.format(pidfile=str(pidfile)))
    spec = _holder_spec()
    files = {ORACLE_HARNESS_PATH: ORACLE_HARNESS_SOURCE, ORACLE_DATA_PATH: oracle_data_text([spec])}
    started = time.monotonic()
    try:
        run = await asyncio.wait_for(
            run_oracle_check(
                files,
                spec,
                root,
                timeout_seconds=TIMEOUT,
                on_base=False,
                env=dict(os.environ),
                interpreter=pin_interpreter(sys.executable, "test"),
                binding=None,
            ),
            timeout=60,
        )
        elapsed = time.monotonic() - started
    finally:
        _stop(pidfile)
    assert pidfile.exists(), "the target never forked its holder"
    # The run ends within its deadline plus the reap margin, whatever holds
    # the pipe, and a run that overran its deadline is never a clean pass.
    assert elapsed < TIMEOUT + REAP_MARGIN_SECONDS + SLACK, elapsed
    assert not (run.return_code == 0 and not run.timed_out and elapsed > TIMEOUT), (
        run.return_code,
        run.timed_out,
        elapsed,
    )


async def test_a_case_whose_reap_overran_the_deadline_is_a_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The target reports the right value in time, but ending it takes the
    # controller past the deadline: what it reported does not stand.
    from ouroboros.boundary import oracle_run

    root = tmp_path / "repo"
    root.mkdir()
    (root / "inc.py").write_text("def inc(x):\n    return x + 1\n")
    real_reap = oracle_run.reap_check_process

    async def slow_reap(process: oracle_run.CheckProcess, deadline: float) -> int | None:
        code = await real_reap(process, deadline)
        await asyncio.sleep(max(deadline - asyncio.get_running_loop().time(), 0) + 0.1)
        return code

    monkeypatch.setattr(oracle_run, "reap_check_process", slow_reap)
    spec = _holder_spec()
    files = {ORACLE_HARNESS_PATH: ORACLE_HARNESS_SOURCE, ORACLE_DATA_PATH: oracle_data_text([spec])}
    for on_base in (False, True):
        run = await run_oracle_check(
            files,
            spec,
            root,
            timeout_seconds=TIMEOUT,
            on_base=on_base,
            env=dict(os.environ),
            interpreter=pin_interpreter(sys.executable, "test"),
            binding=None,
        )
        assert run.return_code != 0, on_base
        assert run.result is not None
        if on_base:
            assert run.timed_out and run.result["resolve"] == "target_timeout"
        else:
            (case,) = run.result["cases"]
            assert case["passed"] is False and case["detail"].endswith("observed timeout")
