"""Target output is read under a hard byte cap while it streams.

A target that writes more than 1 GiB never makes the controller hold more
than the cap: it is killed at the first byte past it, and the case fails as
oversized output (the base is undecided). Nothing allocates the full size:
the flood is generated in 1 MiB writes by the target itself.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
import time
import tracemalloc

from ouroboros.boundary import admission as admission_module
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
from ouroboros.boundary.oracle_run import _CLI_OUTPUT_LIMIT, run_oracle_check
from ouroboros.boundary.package import AssertionLink, CheckRole, CheckSpec
from ouroboros.boundary.receipts import CheckStatus

MIB = 1024 * 1024
FLOOD_MIB = 1100  # 1.1 GiB in total, more than 1 GiB
MALFORMED = "observed malformed or oversized output"
# ``echo.py --n N``: prints N; with N == 2 it floods stdout with 1.1 GiB and
# only then would write ``finished`` (it never gets there).
TOOL = (
    "import sys\n"
    "n = sys.argv[sys.argv.index('--n') + 1]\n"
    "if n == '2':\n"
    "    block = b'x' * (1024 * 1024)\n"
    f"    for _ in range({FLOOD_MIB}):\n"
    "        sys.stdout.buffer.write(block)\n"
    "        sys.stdout.buffer.flush()\n"
    "    open('finished', 'w').close()\n"
    "print(n)\n"
)


def _spec() -> OracleSpec:
    return OracleSpec(
        criterion_key="ac_echo",
        check_id="oracle_1",
        call_kind=CallKind.CLI,
        params=("n",),
        default_binding=Binding(criterion_key="ac_echo", symbol="echo.py", call_kind=CallKind.CLI),
        cases=(
            OracleCase(
                case_id="c1", args={"n": 1}, expect=OracleExpectation(kind="cli", stdout="1")
            ),
            OracleCase(
                case_id="c2",
                args={"n": 2},
                expect=OracleExpectation(kind="cli", stdout="2"),
                held_out=True,
            ),
        ),
    )


def _tool_dir(tmp_path: Path) -> Path:
    root = tmp_path / "cli"
    root.mkdir()
    (root / "echo.py").write_text(TOOL)
    return root


async def _run(root: Path, *, on_base: bool):
    spec = _spec()
    files = {
        ORACLE_HARNESS_PATH: ORACLE_HARNESS_SOURCE,
        ORACLE_DATA_PATH: oracle_data_text([spec]),
    }
    tracemalloc.start()
    started = time.monotonic()
    try:
        run = await run_oracle_check(
            files,
            spec,
            root,
            timeout_seconds=60,
            on_base=on_base,
            env=dict(os.environ),
            interpreter=pin_interpreter(sys.executable, "test"),
            binding=None,
        )
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return run, peak, time.monotonic() - started


async def test_a_flooding_cli_target_fails_its_case_with_bounded_memory(tmp_path: Path) -> None:
    root = _tool_dir(tmp_path)
    run, peak, elapsed = await _run(root, on_base=False)
    assert run.result is not None
    cases = {case["case_id"]: case for case in run.result["cases"]}
    assert cases["c1"]["passed"] is True
    assert cases["c2"]["passed"] is False
    assert cases["c2"]["detail"].endswith(MALFORMED)
    assert run.return_code == 1 and not run.timed_out
    # Killed at the cap: the target never finished its 1.1 GiB, and the
    # controller never held more than a few times the 1 MiB cap.
    assert not (root / "finished").exists()
    assert peak < 16 * MIB, peak
    assert elapsed < 30


async def test_a_flooding_cli_target_leaves_the_base_undecided(tmp_path: Path) -> None:
    root = _tool_dir(tmp_path)
    run, peak, _elapsed = await _run(root, on_base=True)
    assert run.return_code == 3 and run.result is not None
    assert run.result["resolve"] == "frame_malformed"
    assert peak < 16 * MIB, peak


async def test_output_below_the_cap_is_read_whole(tmp_path: Path) -> None:
    root = tmp_path / "cli"
    root.mkdir()
    size = _CLI_OUTPUT_LIMIT - 10
    (root / "echo.py").write_text("import sys\nsys.stdout.write('y' * " + str(size) + ")\n")
    run, _peak, _elapsed = await _run(root, on_base=False)
    assert run.result is not None
    # Both cases ran to completion and were compared (wrong output, no oversize).
    details = [case["detail"] for case in run.result["cases"]]
    assert all(MALFORMED not in detail for detail in details)


async def test_a_flooding_script_check_is_killed_at_the_cap(tmp_path: Path) -> None:
    (tmp_path / "flood.py").write_text(
        TOOL.replace("n = sys.argv[sys.argv.index('--n') + 1]", "n = '2'")
    )
    tracemalloc.start()
    try:
        completed = await admission_module._run_argv([sys.executable, "flood.py"], tmp_path, 60)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert completed.output_overflow and not completed.timed_out
    assert len(completed.stdout) == admission_module._SCRIPT_OUTPUT_LIMIT
    assert not (tmp_path / "finished").exists()
    assert peak < 64 * MIB, peak
    check = CheckSpec(
        check_id="flood",
        role=CheckRole.PRESERVATION,
        argv=("python3", "flood.py"),
        assertions=(AssertionLink(assertion_id="a", criterion_key="k"),),
    )
    candidate = admission_module._classify(
        check, completed, mutated=False, signature_seen=False, on_base=False
    )
    base = admission_module._classify(
        check, completed, mutated=False, signature_seen=False, on_base=True
    )
    assert candidate == (CheckStatus.VIOLATED, "output_oversized")
    assert base == (CheckStatus.INDETERMINATE, "output_oversized")


# a target that fakes its ``resolved`` frame at import and never reads
# stdin cannot stall the controller on a large input write.
STALL = (
    "import os, stat, sys, time\n"
    "for fd in range(3, 64):\n"
    "    try:\n"
    "        if stat.S_ISFIFO(os.fstat(fd).st_mode):\n"
    "            os.write(fd, ('\\n' + sys.argv[2] + ' {\"phase\": \"resolved\", '\n"
    '                          \'"resolve": "ok", "detail": ""}\\n\').encode())\n'
    "            break\n"
    "    except OSError:\n"
    "        pass\n"
    "time.sleep(60)\n"
    "def f(s):\n"
    "    return 1\n"
)


async def test_a_target_that_never_reads_its_input_times_out_on_the_case_budget(
    tmp_path: Path,
) -> None:
    root = tmp_path / "py"
    root.mkdir()
    (root / "slow.py").write_text(STALL)
    spec = OracleSpec(
        criterion_key="ac_slow",
        check_id="oracle_1",
        call_kind=CallKind.FUNCTION,
        params=("s",),
        default_binding=Binding(
            criterion_key="ac_slow", symbol="slow.f", call_kind=CallKind.FUNCTION
        ),
        cases=(
            OracleCase(
                case_id="c1",
                args={"s": "z" * (2 * MIB)},
                expect=OracleExpectation(kind="returns", value=1),
                held_out=True,
            ),
        ),
    )
    files = {ORACLE_HARNESS_PATH: ORACLE_HARNESS_SOURCE, ORACLE_DATA_PATH: oracle_data_text([spec])}
    started = time.monotonic()
    run = await run_oracle_check(
        files,
        spec,
        root,
        timeout_seconds=2,
        on_base=False,
        env=dict(os.environ),
        interpreter=pin_interpreter(sys.executable, "test"),
        binding=None,
    )
    elapsed = time.monotonic() - started
    assert run.result is not None
    (case,) = run.result["cases"]
    assert case["passed"] is False and case["detail"].endswith("observed timeout")
    assert elapsed < 15, elapsed


# A child that leaves the process group (setsid) and keeps the pipes open.
_SURVIVOR = """
import os, sys, time
if os.fork() == 0:
    os.setsid()
    open(sys.argv[1], "w").write(str(os.getpid()))
    time.sleep(60)
    os._exit(0)
time.sleep(0.3)
{tail}
"""


def _stop_survivor(pidfile: Path) -> None:
    import signal

    try:
        os.kill(int(pidfile.read_text()), signal.SIGKILL)
    except (OSError, ValueError):
        pass


async def test_a_flood_that_ends_in_the_timeout_is_still_oversized(tmp_path: Path) -> None:
    """A survivor holding the pipe sends the overflow to the timeout path."""
    pidfile = tmp_path / "pid"
    script = tmp_path / "flood.py"
    script.write_text(
        _SURVIVOR.format(
            tail="sys.stdout.buffer.write(b'x' * (9 * 1024 * 1024)); sys.stdout.flush()"
            "; time.sleep(60)"
        )
    )
    try:
        done = await admission_module._run_argv(
            [sys.executable, str(script), str(pidfile)], tmp_path, 3
        )
    finally:
        _stop_survivor(pidfile)
    assert done.timed_out and done.output_overflow
    check = CheckSpec(
        check_id="flood",
        role=CheckRole.PRESERVATION,
        argv=("python3", "flood.py"),
        assertions=(AssertionLink(assertion_id="flood.a", criterion_key="ac_x", locator="x"),),
    )
    status = admission_module._classify(
        check, done, mutated=False, signature_seen=False, on_base=False
    )
    assert status == (CheckStatus.VIOLATED, "output_oversized")


async def test_a_cancelled_script_check_does_not_wait_on_a_survivor(tmp_path: Path) -> None:
    """Cancelling (Ctrl-C, MCP cancel) is bounded like the timeout path."""
    import asyncio

    pidfile = tmp_path / "pid"
    script = tmp_path / "hold.py"
    script.write_text(_SURVIVOR.format(tail="time.sleep(60)"))
    task = asyncio.create_task(
        admission_module._run_argv([sys.executable, str(script), str(pidfile)], tmp_path, 60)
    )
    try:
        await asyncio.sleep(1.0)
        task.cancel()
        done, _pending = await asyncio.wait({task}, timeout=8)
    finally:
        _stop_survivor(pidfile)
    assert done == {task} and task.cancelled()
