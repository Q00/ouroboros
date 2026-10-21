"""The pinned interpreter is the one that runs, whatever its path names at launch.

``check_command`` verifies the pin when it prepares a command, but a process
starts later. A pinned virtualenv ``python3`` link switched to another
program between the two (and possibly switched back afterwards) must never
run: the check is indeterminate (``interpreter_changed``) instead.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
from typing import Any

import pytest

from ouroboros.boundary import admission, check_env, oracle_run
from ouroboros.boundary.binding import Binding, CallKind
from ouroboros.boundary.check_env import (
    INTERPRETER_CHANGED,
    pin_interpreter,
)
from ouroboros.boundary.oracle import (
    ORACLE_DATA_PATH,
    ORACLE_HARNESS_PATH,
    ORACLE_HARNESS_SOURCE,
    OracleCase,
    OracleExpectation,
    OracleSpec,
    oracle_data_text,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")


def _venv(root: Path) -> Path:
    """A virtualenv whose ``bin/python3`` links to this interpreter."""
    python = root / "bin" / "python3"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    home = os.path.dirname(os.path.realpath(sys.executable))
    (root / "pyvenv.cfg").write_text(f"home = {home}\ninclude-system-site-packages = false\n")
    return python


def _swap_at_launch(
    monkeypatch: pytest.MonkeyPatch, python: Path, replacement: Path, *, restore: bool
) -> None:
    """Switch ``python`` to ``replacement`` after the command was prepared, before it starts."""
    spawn = check_env.spawn_check_process
    original = os.readlink(python)

    def relink(target: str) -> None:
        staged = python.with_name("staged")
        staged.symlink_to(target)
        os.replace(staged, python)

    async def restore_after(process: Any) -> None:
        await process.wait()
        relink(original)  # the pinned link is back: a later pin check sees nothing

    async def swapping_spawn(*args: Any, **kwargs: Any) -> Any:
        relink(str(replacement))
        process = await spawn(*args, **kwargs)
        if restore:
            restoring.append(asyncio.ensure_future(restore_after(process)))
        return process

    restoring: list[Any] = []

    for module in (check_env, admission, oracle_run):
        monkeypatch.setattr(module, "spawn_check_process", swapping_spawn, raising=False)


def _replacement(tmp_path: Path) -> tuple[Path, Path]:
    """A program that leaves ``marker`` behind if it ever runs."""
    marker = tmp_path / "replacement-ran"
    program = tmp_path / "replacement"
    program.write_text(f"#!/bin/sh\ntouch {marker}\nexit 0\n")
    program.chmod(0o755)
    return program, marker


def _sandbox(mode: str, request: pytest.FixtureRequest) -> None:
    if mode == "sandbox_on":
        from ouroboros.runtime.exec_sandbox import sandbox_unavailable_reason

        request.getfixturevalue("real_check_isolation")
        reason = sandbox_unavailable_reason(deny_network=True)
        if reason is not None:
            pytest.skip(f"execution sandbox unavailable on this host: {reason}")


@pytest.mark.parametrize("mode", ["sandbox_off", "sandbox_on"])
@pytest.mark.parametrize("restore", [False, True], ids=["left", "restored"])
async def test_a_script_check_never_runs_a_replaced_interpreter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restore: bool,
    mode: str,
    request: pytest.FixtureRequest,
) -> None:
    _sandbox(mode, request)
    python = _venv(tmp_path / "venv")
    pinned = pin_interpreter(str(python), "project_venv")
    program, marker = _replacement(tmp_path)
    _swap_at_launch(monkeypatch, python, program, restore=restore)

    completed = await admission._run_argv(
        ["python3", "-c", "pass"], tmp_path, 30, interpreter=pinned
    )

    assert not marker.exists(), "the replaced interpreter ran"
    assert completed.unavailable == INTERPRETER_CHANGED


def _spec(call_kind: CallKind, symbol: str) -> OracleSpec:
    expect = (
        OracleExpectation(kind="cli", stdout="2")
        if call_kind is CallKind.CLI
        else OracleExpectation(kind="returns", value=2)
    )
    return OracleSpec(
        criterion_key="ac_inc",
        check_id="oracle_1",
        call_kind=call_kind,
        params=("x",),
        default_binding=Binding(
            criterion_key="ac_inc",
            symbol=symbol,
            call_kind=call_kind,
            arg_map={"x": 0} if call_kind is CallKind.CLI else {},
        ),
        cases=(OracleCase(case_id="c1", args={"x": 1}, expect=expect, held_out=True),),
    )


@pytest.mark.parametrize(
    ("call_kind", "symbol"),
    [(CallKind.FUNCTION, "inc.inc"), (CallKind.CLI, "inc.py")],
    ids=["function", "cli"],
)
@pytest.mark.parametrize("restore", [False, True], ids=["left", "restored"])
async def test_an_oracle_target_never_runs_a_replaced_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, call_kind: CallKind, symbol: str, restore: bool
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "inc.py").write_text(
        "import sys\ndef inc(x):\n    return x + 1\n"
        "if __name__ == '__main__':\n    print(inc(int(sys.argv[1])))\n"
    )
    python = _venv(tmp_path / "venv")
    pinned = pin_interpreter(str(python), "project_venv")
    program, marker = _replacement(tmp_path)
    _swap_at_launch(monkeypatch, python, program, restore=restore)
    spec = _spec(call_kind, symbol)

    run = await oracle_run.run_oracle_check(
        {ORACLE_HARNESS_PATH: ORACLE_HARNESS_SOURCE, ORACLE_DATA_PATH: oracle_data_text([spec])},
        spec,
        root,
        timeout_seconds=30,
        on_base=False,
        env=dict(os.environ),
        interpreter=pinned,
        binding=None,
    )

    assert not marker.exists(), "the replaced interpreter ran"
    assert run.unavailable == INTERPRETER_CHANGED
    assert run.return_code is None


@pytest.mark.parametrize("mode", ["sandbox_off", "sandbox_on"])
async def test_the_pinned_virtualenv_keeps_its_venv(
    tmp_path: Path, mode: str, request: pytest.FixtureRequest
) -> None:
    # The pinned binary runs with the virtualenv it was pinned through.
    _sandbox(mode, request)
    venv = tmp_path / "venv"
    python = _venv(venv)
    report = tmp_path / "work" / "prefix.txt"
    report.parent.mkdir()
    completed = await admission._run_argv(
        [
            "python3",
            "-c",
            f"import sys; open({str(report)!r}, 'w').write(sys.prefix + '\\n' + sys.executable)",
        ],
        report.parent,
        30,
        interpreter=pin_interpreter(str(python), "project_venv"),
    )
    assert completed.return_code == 0, completed
    assert report.read_text().splitlines() == [str(venv), str(python)]
