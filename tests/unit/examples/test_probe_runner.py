"""Probe runs on fixture trees: real wrapper processes through ``check_command``."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import sys
import time

import pytest

from ouroboros.boundary.admission import _Completed
from ouroboros.boundary.check_env import (
    INTERPRETER_UNAVAILABLE,
    CheckInterpreter,
    CheckUnavailable,
    pin_interpreter,
)
from ouroboros.examples import probe_runner
from ouroboros.examples.disagreement import DropReason, find_disagreements
from ouroboros.examples.probe_runner import (
    COPY_FAILED,
    LAUNCH_FAILED,
    STEP_DEADLINE,
    Probe,
    ProbeKind,
    ProbePoolRun,
    run_probe,
    run_probe_pool,
    wrapper_record,
)
from ouroboros.examples.probe_wrapper import STREAM_LIMIT
from ouroboros.examples.record import BASE_TREE_ID, OutcomeRecord, ProbeStatus

PY = sys.executable


@pytest.fixture
def python() -> CheckInterpreter:
    return pin_interpreter(PY, "test")


def _tree(root: Path, name: str, body: str) -> Path:
    tree = root / name
    tree.mkdir()
    (tree / "mod.py").write_text(body)
    return tree


@pytest.fixture
def trees(tmp_path: Path) -> dict[str, Path]:
    """A base and two candidates that read one input differently."""
    return {
        BASE_TREE_ID: _tree(
            tmp_path, "base", "def parse(text):\n    raise ValueError('unsupported unit')\n"
        ),
        "cand-a": _tree(tmp_path, "a", "def parse(text):\n    return 5400\n"),
        "cand-b": _tree(tmp_path, "b", "def parse(text):\n    return 90\n"),
    }


def call(body: str) -> Probe:
    return Probe(ProbeKind.CALL, source=body)


def command(*argv: str, stdin: str = "") -> Probe:
    return Probe(ProbeKind.COMMAND, argv=argv, stdin=stdin)


PARSE = call("from mod import parse\n\ndef probe():\n    return parse('1h30')\n")


def _records(run: ProbePoolRun, tree_id: str) -> list[OutcomeRecord]:
    return [record for record in run.records if record.tree_id == tree_id]


async def _once(
    probe: Probe, tree: Path, python: CheckInterpreter, tmp_path: Path, **kw
) -> OutcomeRecord:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    run = await run_probe(
        probe, tree, tree_id="cand", repeat=0, interpreter=python, work_dir=work, **kw
    )
    assert run.indeterminate is None
    return run.record


# ---------------------------------------------------------------- the Probe


def test_probe_id_is_the_digest_of_its_kind_and_inputs() -> None:
    assert PARSE.probe_id == call(PARSE.source).probe_id
    assert len(PARSE.probe_id) == 64
    assert command("echo", "x").probe_id != command("echo", "y").probe_id
    assert command("cat", stdin="a").probe_id != command("cat", stdin="b").probe_id
    assert command("x").canonical_bytes() == b'{"argv":["x"],"kind":"command","stdin":""}'


@pytest.mark.parametrize(
    "kwargs",
    [
        {"kind": ProbeKind.CALL},
        {"kind": ProbeKind.CALL, "source": "x", "argv": ("a",)},
        {"kind": ProbeKind.CALL, "source": "x", "stdin": "a"},
        {"kind": ProbeKind.COMMAND},
        {"kind": ProbeKind.COMMAND, "argv": ("a",), "source": "x"},
        {"kind": ProbeKind.COMMAND, "argv": ("a", 1)},
        {"kind": "files", "argv": ("a",)},
    ],
)
def test_a_malformed_probe_is_refused(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        Probe(**kwargs)


# ------------------------------------------------------- one run, call kind


async def test_a_returned_value(
    trees: dict[str, Path], python: CheckInterpreter, tmp_path: Path
) -> None:
    record = await _once(PARSE, trees["cand-a"], python, tmp_path)
    assert record.status is ProbeStatus.RETURNED
    assert record.payload() == {"status": "returned", "value": 5400}
    assert (record.probe_id, record.tree_id, record.repeat) == (PARSE.probe_id, "cand", 0)


async def test_a_raised_exception_is_qualified(
    trees: dict[str, Path], python: CheckInterpreter, tmp_path: Path
) -> None:
    record = await _once(PARSE, trees[BASE_TREE_ID], python, tmp_path)
    assert record.payload() == {
        "status": "raised",
        "exception_type": "builtins.ValueError",
        "message": "unsupported unit",
    }


async def test_a_probe_that_does_not_import_is_not_run(
    trees: dict[str, Path], python: CheckInterpreter, tmp_path: Path
) -> None:
    probe = call("from mod import missing_name\n\ndef probe():\n    return 1\n")
    record = await _once(probe, trees["cand-a"], python, tmp_path)
    assert record.status is ProbeStatus.NOT_RUN
    assert record.fields["exception_type"] == "builtins.ImportError"


async def test_a_probe_without_a_probe_function_is_not_run(
    trees: dict[str, Path], python: CheckInterpreter, tmp_path: Path
) -> None:
    record = await _once(call("value = 1\n"), trees["cand-a"], python, tmp_path)
    assert record.status is ProbeStatus.NOT_RUN
    assert record.fields["exception_type"] == "builtins.AttributeError"


async def test_a_syntax_error_is_not_run(
    trees: dict[str, Path], python: CheckInterpreter, tmp_path: Path
) -> None:
    record = await _once(call("def probe(:\n"), trees["cand-a"], python, tmp_path)
    assert record.status is ProbeStatus.NOT_RUN
    assert record.fields["exception_type"] == "builtins.SyntaxError"


async def test_what_a_probe_prints_never_reaches_the_record(
    trees: dict[str, Path], python: CheckInterpreter, tmp_path: Path
) -> None:
    probe = call(
        "import sys, os\n"
        "def probe():\n"
        "    print('noise')\n"
        "    sys.stderr.write('more noise')\n"
        "    os.write(1, b'raw noise\\n')\n"
        "    return 'ok'\n"
    )
    record = await _once(probe, trees["cand-a"], python, tmp_path)
    assert record.payload() == {"status": "returned", "value": "ok"}


async def test_system_exit_inside_probe_is_raised(
    trees: dict[str, Path], python: CheckInterpreter, tmp_path: Path
) -> None:
    probe = call("import sys\ndef probe():\n    sys.exit(4)\n")
    record = await _once(probe, trees["cand-a"], python, tmp_path)
    assert record.status is ProbeStatus.RAISED
    assert record.fields["exception_type"] == "builtins.SystemExit"


async def test_a_process_that_dies_without_a_record_exited(
    trees: dict[str, Path], python: CheckInterpreter, tmp_path: Path
) -> None:
    probe = call("import os\ndef probe():\n    os._exit(7)\n")
    record = await _once(probe, trees["cand-a"], python, tmp_path)
    assert record.payload() == {"status": "exited", "exit_code": 7}


async def test_a_slow_probe_times_out(
    trees: dict[str, Path], python: CheckInterpreter, tmp_path: Path
) -> None:
    probe = call("import time\ndef probe():\n    time.sleep(60)\n")
    started = time.monotonic()
    record = await _once(probe, trees["cand-a"], python, tmp_path, timeout=2)
    assert record.payload() == {"status": "timed_out"}
    assert time.monotonic() - started < 30


async def test_each_run_gets_a_fresh_copy_and_the_tree_is_untouched(
    trees: dict[str, Path], python: CheckInterpreter, tmp_path: Path
) -> None:
    probe = call(
        "import os\n"
        "def probe():\n"
        "    seen = sorted(os.listdir('.'))\n"
        "    open('written.txt', 'w').write('x')\n"
        "    os.remove('mod.py')\n"
        "    return seen\n"
    )
    tree = trees["cand-a"]
    first = await _once(probe, tree, python, tmp_path)
    second = await _once(probe, tree, python, tmp_path)
    # The probe file lives outside the copy; the copy is the tree only.
    assert first.payload() == second.payload() == {"status": "returned", "value": ["mod.py"]}
    assert sorted(path.name for path in tree.iterdir()) == ["mod.py"]
    assert list((tmp_path / "work").iterdir()) == []


async def test_the_tree_src_directory_is_importable(
    tmp_path: Path, python: CheckInterpreter
) -> None:
    tree = tmp_path / "layout"
    (tree / "src" / "pkg").mkdir(parents=True)
    (tree / "src" / "pkg" / "__init__.py").write_text("ANSWER = 42\n")
    probe = call("import pkg\ndef probe():\n    return pkg.ANSWER\n")
    record = await _once(probe, tree, python, tmp_path)
    assert record.payload() == {"status": "returned", "value": 42}


# ---------------------------------------------------- one run, command kind


async def test_a_command_reports_exit_code_stdout_and_stderr(
    trees: dict[str, Path], python: CheckInterpreter, tmp_path: Path
) -> None:
    script = (
        "import sys\n"
        "data = sys.stdin.read()\n"
        "sys.stdout.write(data.upper())\n"
        "sys.stderr.write('warn')\n"
        "sys.exit(3)\n"
    )
    probe = command(PY, "-c", script, stdin="hello\n")
    record = await _once(probe, trees["cand-a"], python, tmp_path)
    assert record.status is ProbeStatus.EXITED
    assert record.fields["exit_code"] == 3
    assert record.fields["stdout"].replace("\r\n", "\n") == "HELLO\n"
    assert record.fields["stderr"] == "warn"


async def test_a_command_runs_in_the_copy(
    trees: dict[str, Path], python: CheckInterpreter, tmp_path: Path
) -> None:
    probe = command(PY, "-c", "import os; print(sorted(os.listdir('.')))")
    record = await _once(probe, trees["cand-a"], python, tmp_path)
    assert record.fields["stdout"].strip() == "['mod.py']"


async def test_a_missing_program_is_not_run(
    trees: dict[str, Path], python: CheckInterpreter, tmp_path: Path
) -> None:
    probe = command(str(tmp_path / "no-such-program"))
    record = await _once(probe, trees["cand-a"], python, tmp_path)
    assert record.status is ProbeStatus.NOT_RUN
    assert record.fields["exception_type"] == "builtins.FileNotFoundError"


@pytest.mark.parametrize("argv", [("x\x00y",), (PY, "-c", "print(1)", "a\x00b")])
async def test_an_argv_item_with_a_nul_byte_is_not_run(
    argv: tuple[str, ...], trees: dict[str, Path], python: CheckInterpreter, tmp_path: Path
) -> None:
    record = await _once(command(*argv), trees["cand-a"], python, tmp_path)
    assert record.status is ProbeStatus.NOT_RUN
    assert record.fields["exception_type"] == "builtins.ValueError"


async def test_a_child_holding_the_pipes_cannot_turn_an_exit_into_a_timeout(
    trees: dict[str, Path], python: CheckInterpreter, tmp_path: Path
) -> None:
    """The command prints, exits 0, and leaves a child holding stdout and stderr."""
    script = (
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(8)'])\n"
        "print('done')\n"
    )
    run = await run_probe(
        command(PY, "-c", script),
        trees["cand-a"],
        tree_id="cand",
        repeat=0,
        interpreter=python,
        work_dir=tmp_path,
        timeout=4,
    )
    assert run.indeterminate == "output_incomplete"
    assert run.record.status is ProbeStatus.NOT_RUN


async def test_command_output_is_cut_at_the_stream_limit(
    trees: dict[str, Path], python: CheckInterpreter, tmp_path: Path
) -> None:
    probe = command(PY, "-c", f"import sys; sys.stdout.write('z' * {STREAM_LIMIT * 3})")
    run = await run_probe(
        probe, trees["cand-a"], tree_id="cand", repeat=0, interpreter=python, work_dir=tmp_path
    )
    assert run.indeterminate == "output_incomplete"
    assert run.record.status is ProbeStatus.NOT_RUN


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
async def test_undecodable_output_cannot_collide_with_literal_escape(
    stream: str, trees: dict[str, Path], python: CheckInterpreter, tmp_path: Path
) -> None:
    invalid = command(PY, "-c", f"import sys; sys.{stream}.buffer.write(bytes([255]))")
    literal = command(PY, "-c", f"import sys; sys.{stream}.buffer.write(b'\\\\xff')")
    run = await run_probe(
        invalid, trees["cand-a"], tree_id="cand", repeat=0, interpreter=python, work_dir=tmp_path
    )
    valid = await _once(literal, trees["cand-a"], python, tmp_path)
    assert run.indeterminate == "output_undecodable"
    assert run.record.status is ProbeStatus.NOT_RUN
    assert valid.status is ProbeStatus.EXITED
    assert valid.fields[stream] == r"\xff"


async def test_undecodable_candidate_output_makes_the_pool_indeterminate(
    trees: dict[str, Path], python: CheckInterpreter
) -> None:
    trees["cand-a"].joinpath("mod.py").write_text("value = bytes([255])\n")
    trees["cand-b"].joinpath("mod.py").write_text("value = b'\\\\xff'\n")
    probe = command(PY, "-c", "import mod, sys; sys.stdout.buffer.write(mod.value)")
    pool = await run_probe_pool([probe], trees, interpreter=python)
    assert pool.indeterminate == {probe.probe_id: "output_undecodable"}
    assert all(record.status is ProbeStatus.NOT_RUN for record in pool.records)


# ------------------------------------------------------------- indeterminate


async def test_a_refused_interpreter_is_indeterminate_and_runs_nothing(
    trees: dict[str, Path], tmp_path: Path
) -> None:
    marker = tmp_path / "ran"
    probe = call(f"def probe():\n    open({str(marker)!r}, 'w').write('x')\n")
    missing = pin_interpreter(str(tmp_path / "no-such-python"), "test")
    run = await run_probe_pool([probe], trees, interpreter=missing)
    assert run.indeterminate == {probe.probe_id: INTERPRETER_UNAVAILABLE}
    assert {record.status for record in run.records} == {ProbeStatus.NOT_RUN}
    assert all(record.payload() == {"status": "not_run"} for record in run.records)
    assert not marker.exists()
    report = find_disagreements(run.records, ["cand-a", "cand-b"])
    assert [(item.probe_id, item.reason) for item in report.dropped] == [
        (probe.probe_id, DropReason.NOT_RUN)
    ]


async def test_one_unavailable_run_blanks_only_its_probe(
    trees: dict[str, Path], python: CheckInterpreter, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = call("def probe():\n    return 1\n")
    real = probe_runner.check_command

    def refuse_on_b(argv, **kwargs):
        # The sandbox refuses the parse probe on candidate b's copy only.
        probe_file = Path(argv[-1])
        on_b = (kwargs["cwd"] / "mod.py").read_text().endswith("return 90\n")
        if on_b and probe_file.read_text() == PARSE.source:
            return CheckUnavailable("sandbox_unavailable")
        return real(argv, **kwargs)

    monkeypatch.setattr(probe_runner, "check_command", refuse_on_b)
    run = await run_probe_pool([PARSE, other], trees, interpreter=python, parallel=1)
    assert run.indeterminate == {PARSE.probe_id: "sandbox_unavailable"}
    parse_records = [record for record in run.records if record.probe_id == PARSE.probe_id]
    assert len(parse_records) == 6
    assert all(record.status is ProbeStatus.NOT_RUN for record in parse_records)
    other_records = [record for record in run.records if record.probe_id == other.probe_id]
    assert {record.payload()["value"] for record in other_records} == {1}


async def test_a_launch_failure_is_indeterminate(
    trees: dict[str, Path],
    python: CheckInterpreter,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def no_launch(command, timeout):
        return _Completed(None, b"", b"", False, "OSError: boom", 0.0)

    monkeypatch.setattr(probe_runner, "_run_in_environment", no_launch)
    run = await run_probe(
        PARSE, trees["cand-a"], tree_id="cand-a", repeat=1, interpreter=python, work_dir=tmp_path
    )
    assert run.indeterminate == LAUNCH_FAILED
    assert run.record == OutcomeRecord(PARSE.probe_id, "cand-a", 1, ProbeStatus.NOT_RUN)


async def test_a_flooding_run_exited_even_with_a_record(
    trees: dict[str, Path],
    python: CheckInterpreter,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def flooded(command, timeout):
        nonce = command.argv[-2]
        line = f"{nonce} " + '{"status":"returned","value":1}'
        return _Completed(-9, line.encode(), b"", False, None, 0.1, output_overflow=True)

    monkeypatch.setattr(probe_runner, "_run_in_environment", flooded)
    run = await run_probe(
        PARSE, trees["cand-a"], tree_id="cand-a", repeat=0, interpreter=python, work_dir=tmp_path
    )
    assert run.indeterminate is None
    assert run.record.payload() == {"status": "exited", "exit_code": -9}


async def test_a_tree_that_cannot_be_copied_is_indeterminate(
    trees: dict[str, Path], python: CheckInterpreter, tmp_path: Path
) -> None:
    broken = dict(trees, **{"cand-b": tmp_path / "missing"})
    run = await run_probe_pool([PARSE], broken, interpreter=python)
    assert run.indeterminate == {PARSE.probe_id: f"{COPY_FAILED}:FileNotFoundError"}
    assert all(record.status is ProbeStatus.NOT_RUN for record in run.records)


async def test_no_run_starts_past_the_step_deadline(
    trees: dict[str, Path], python: CheckInterpreter, tmp_path: Path
) -> None:
    marker = tmp_path / "ran"
    probe = call(f"def probe():\n    open({str(marker)!r}, 'w').write('x')\n")
    deadline = time.monotonic() + probe_runner.PROBE_TIMEOUT_SECONDS / 2
    run = await run_probe_pool([probe], trees, interpreter=python, deadline=deadline)
    assert run.indeterminate == {probe.probe_id: STEP_DEADLINE}
    assert len(run.records) == 6 and not marker.exists()


async def test_a_slow_copy_that_eats_the_step_budget_starts_no_process(
    trees: dict[str, Path],
    python: CheckInterpreter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deadline is checked again once the copy is ready, before the launch."""
    marker = tmp_path / "ran"
    probe = call(f"def probe():\n    open({str(marker)!r}, 'w').write('x')\n")
    real_copy = probe_runner.copy_checkout

    def slow_copy(source: Path, destination: Path) -> None:
        time.sleep(1.2)
        real_copy(source, destination)

    monkeypatch.setattr(probe_runner, "copy_checkout", slow_copy)
    deadline = time.monotonic() + 2.1  # fits 1 s plus the reap margin before the copy only
    run = await run_probe_pool(
        [probe], trees, interpreter=python, timeout=1, parallel=4, deadline=deadline
    )
    assert run.indeterminate == {probe.probe_id: STEP_DEADLINE}
    assert all(record.payload() == {"status": "not_run"} for record in run.records)
    assert not marker.exists()


async def test_a_slow_preparation_that_eats_the_step_budget_starts_no_process(
    trees: dict[str, Path],
    python: CheckInterpreter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The last deadline check is after ``check_command``, right before the launch."""
    marker = tmp_path / "ran"
    probe = call(f"def probe():\n    open({str(marker)!r}, 'w').write('x')\n")
    real = probe_runner.check_command

    def slow_check_command(argv, **kwargs):
        time.sleep(1.25)
        return real(argv, **kwargs)

    monkeypatch.setattr(probe_runner, "check_command", slow_check_command)
    deadline = time.monotonic() + 2.1
    run = await run_probe_pool(
        [probe], trees, interpreter=python, timeout=1, parallel=4, deadline=deadline
    )
    assert run.indeterminate == {probe.probe_id: STEP_DEADLINE}
    assert not marker.exists()


def test_fits_counts_the_timeout_and_the_reap_margin() -> None:
    now = time.monotonic()
    margin = probe_runner.REAP_MARGIN_SECONDS
    assert probe_runner.fits(None, 20)
    assert probe_runner.fits(now + 20 + margin + 5, 20)
    assert not probe_runner.fits(now + 20 + margin - 0.5, 20)


# ------------------------------------------------------------------ the pool


async def test_the_pool_feeds_the_disagreement_core(
    trees: dict[str, Path], python: CheckInterpreter
) -> None:
    same = call("from mod import parse\ndef probe():\n    return 'unrelated'\n")
    run = await run_probe_pool([PARSE, same, PARSE], trees, interpreter=python)
    assert run.indeterminate == {}
    assert len(run.records) == 2 * 3 * 2  # probes x trees x repeats, duplicate dropped
    keys = [(r.probe_id, r.tree_id, r.repeat) for r in run.records]
    assert keys == sorted(keys)
    report = find_disagreements(run.records, ["cand-a", "cand-b"])
    assert [item.probe_id for item in report.disagreements] == [PARSE.probe_id]
    values = sorted(group.outcome["value"] for group in report.disagreements[0].groups)
    assert values == [90, 5400]
    assert [item.probe_id for item in report.unchanged] == [same.probe_id]


async def test_a_flaky_value_is_masked_as_unstable(
    trees: dict[str, Path], python: CheckInterpreter
) -> None:
    flaky = call("import os\ndef probe():\n    return os.urandom(8).hex()\n")
    run = await run_probe_pool([flaky], trees, interpreter=python)
    report = find_disagreements(run.records, ["cand-a", "cand-b"])
    comparison = report.unchanged[0]
    assert comparison.unstable_fields == ("value",)
    assert not report.disagreements


async def test_a_probe_not_run_on_every_candidate_is_dropped(
    trees: dict[str, Path], python: CheckInterpreter
) -> None:
    probe = call("from mod import not_there\ndef probe():\n    return 1\n")
    run = await run_probe_pool([probe], trees, interpreter=python, parallel=2)
    assert run.indeterminate == {}
    assert {record.status for record in _records(run, "cand-a")} == {ProbeStatus.NOT_RUN}
    report = find_disagreements(run.records, ["cand-a", "cand-b"])
    assert [item.reason for item in report.dropped] == [DropReason.NOT_RUN]


async def test_the_pool_uses_and_keeps_a_given_work_dir(
    trees: dict[str, Path], python: CheckInterpreter, tmp_path: Path
) -> None:
    work = tmp_path / "given"
    work.mkdir()
    await run_probe_pool([PARSE], trees, interpreter=python, work_dir=work)
    assert work.is_dir() and list(work.iterdir()) == []


@pytest.mark.parametrize(
    "bad",
    [
        {"cand-a": Path(".")},
        {BASE_TREE_ID: Path(".")},
        {BASE_TREE_ID: Path("."), "": Path(".")},
    ],
)
async def test_the_pool_refuses_malformed_trees(bad: Mapping[str, Path]) -> None:
    with pytest.raises(ValueError):
        await run_probe_pool([PARSE], bad)


async def test_the_pool_refuses_zero_parallel(trees: dict[str, Path]) -> None:
    with pytest.raises(ValueError):
        await run_probe_pool([PARSE], trees, parallel=0)


async def test_a_real_sandbox_run(
    real_check_isolation: None, trees: dict[str, Path], python: CheckInterpreter, tmp_path: Path
) -> None:
    from ouroboros.runtime.exec_sandbox import sandbox_unavailable_reason

    reason = sandbox_unavailable_reason(deny_network=True)
    if reason is not None:
        pytest.skip(f"execution sandbox unavailable on this host: {reason}")
    confined = pin_interpreter(PY, "test")
    record = await _once(PARSE, trees["cand-b"], confined, tmp_path)
    assert record.payload() == {"status": "returned", "value": 90}


# ------------------------------------------------- record line validation


def _line(nonce: str, body: str) -> bytes:
    return f"\n{nonce} {body}\n".encode()


@pytest.mark.parametrize(
    ("kind", "body", "ok"),
    [
        (ProbeKind.CALL, '{"status":"returned","value":[1]}', True),
        (ProbeKind.CALL, '{"status":"raised","exception_type":"builtins.E","message":"m"}', True),
        (ProbeKind.CALL, '{"status":"exited","exit_code":0,"stdout":"","stderr":""}', False),
        (ProbeKind.CALL, '{"status":"returned"}', False),
        (ProbeKind.CALL, '{"status":"returned","value":1,"stdout":""}', False),
        (ProbeKind.CALL, '{"status":"timed_out"}', False),
        (ProbeKind.CALL, '{"status":"raised","exception_type":1,"message":"m"}', False),
        (ProbeKind.CALL, '{"status":"bogus","value":1}', False),
        (ProbeKind.CALL, '{"value":1}', False),
        (ProbeKind.CALL, "[1]", False),
        (ProbeKind.CALL, '{"status":"returned","value":NaN}', False),
        (ProbeKind.CALL, '{"status":"returned","value":' + "9" * 2000 + "}", False),
        (ProbeKind.CALL, "not json", False),
        (ProbeKind.COMMAND, '{"status":"exited","exit_code":0,"stdout":"","stderr":""}', True),
        (ProbeKind.COMMAND, '{"status":"exited","exit_code":true,"stdout":"","stderr":""}', False),
        (ProbeKind.COMMAND, '{"status":"exited","exit_code":0,"stdout":1,"stderr":""}', False),
        (ProbeKind.COMMAND, '{"status":"returned","value":1}', False),
    ],
)
def test_the_record_line_is_checked_structurally(kind: ProbeKind, body: str, ok: bool) -> None:
    stdout = b"other output\n" + _line("n0", body)
    record = wrapper_record(stdout, "n0", kind, "p", "cand", 0)
    assert (record is not None) is ok


def test_two_record_lines_or_a_wrong_nonce_give_no_record() -> None:
    body = '{"status":"returned","value":1}'
    assert wrapper_record(_line("n0", body) * 2, "n0", ProbeKind.CALL, "p", "t", 0) is None
    assert wrapper_record(_line("n1", body), "n0", ProbeKind.CALL, "p", "t", 0) is None
    assert wrapper_record(b"", "n0", ProbeKind.CALL, "p", "t", 0) is None
    crlf = f"n0 {body}\r\n".encode()
    assert wrapper_record(crlf, "n0", ProbeKind.CALL, "p", "t", 0) is not None


<<<<<<< HEAD
async def test_output_cut_at_limit_is_indeterminate_for_entire_probe(
    trees: dict[str, Path], python: CheckInterpreter
) -> None:
    probe = command(
        PY,
        "-c",
        f"import sys; sys.stdout.write('x' * {STREAM_LIMIT} + open('mod.py').read())",
    )
    run = await run_probe_pool([probe], trees, interpreter=python, parallel=1)
    assert run.indeterminate == {probe.probe_id: "output_incomplete"}
    assert {item.status for item in run.records} == {ProbeStatus.NOT_RUN}


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize("extra", [0, 1])
async def test_command_stream_limit_boundary(
    stream: str, extra: int, trees: dict[str, Path], python: CheckInterpreter, tmp_path: Path
) -> None:
    probe = command(PY, "-c", f"import sys; sys.{stream}.write('x' * {STREAM_LIMIT + extra})")
    run = await run_probe(
        probe, trees["cand-a"], tree_id="cand", repeat=0, interpreter=python, work_dir=tmp_path
    )
    if extra:
        assert run.indeterminate == "output_incomplete"
        assert run.record.status is ProbeStatus.NOT_RUN
    else:
        assert run.indeterminate is None
        assert run.record.status is ProbeStatus.EXITED
        assert run.record.fields[stream] == "x" * STREAM_LIMIT
=======
# ------------------------------------------------------- owned file lifetimes


@pytest.fixture
async def file_lifetime(monkeypatch: pytest.MonkeyPatch):
    """Hold real file writers/removal at observable ownership transitions."""
    import asyncio
    import threading

    class FileLifetime:
        def __init__(self):
            self.release = threading.Event()
            self.fail = threading.Event()
            self.failed = threading.Event()
            self.started = threading.Event()
            self.finished = threading.Event()
            self.removing = threading.Event()
            self.removed = threading.Event()
            self.lock = threading.Lock()
            self.expected = 1
            self.copies = 0
            self.completed = 0
            self.active = set()
            self.roots = set()
            self.overlaps = []
            self.children = set()
            self.fail_first = False
            self.hold_removal = False
            self.removal_prefix = "probe-run-"

        def copy(self, source, destination):
            with self.lock:
                index = self.copies
                self.copies += 1
                self.active.add(destination)
                self.roots.add(destination.parent.parent)
                if self.copies >= self.expected:
                    self.started.set()
            try:
                if self.fail_first and index == 0:
                    assert self.fail.wait(5), "fixture failure barrier not released"
                    self.failed.set()
                    raise RuntimeError("owned copy failed")
                assert self.release.wait(5), "fixture copy barrier not released"
                real_copy(source, destination)
                (destination / "last-write").write_text("copy finished", encoding="utf-8")
            finally:
                with self.lock:
                    self.active.remove(destination)
                    self.completed += 1
                    if self.completed >= self.expected:
                        self.finished.set()

        def remove(self, path, *args, **kwargs):
            path = Path(path)
            with self.lock:
                self.overlaps.extend(dest for dest in self.active if dest.is_relative_to(path))
            # The copy's staging cleanup is a sibling of tree, not its owner.
            if self.hold_removal and path.name.startswith(self.removal_prefix):
                self.release.clear()
                self.removing.set()
                assert self.release.wait(5), "fixture removal barrier not released"
                try:
                    return real_remove(path, *args, **kwargs)
                finally:
                    self.removed.set()
            return real_remove(path, *args, **kwargs)

    state = FileLifetime()
    real_copy = probe_runner.copy_checkout
    real_remove = probe_runner.shutil.rmtree
    original_run = probe_runner.run_probe

    async def track_run(*args, **kwargs):
        # Track only for safe teardown of a failing baseline test.
        state.children.add(asyncio.current_task())
        return await original_run(*args, **kwargs)

    monkeypatch.setattr(probe_runner, "copy_checkout", state.copy)
    monkeypatch.setattr(probe_runner.shutil, "rmtree", state.remove)
    monkeypatch.setattr(probe_runner, "run_probe", track_run)
    monkeypatch.setattr(
        probe_runner, "check_command", lambda *_args, **_kwargs: CheckUnavailable("fixture_refusal")
    )
    yield state
    state.release.set()
    state.fail.set()
    if state.children:
        await asyncio.gather(*state.children, return_exceptions=True)
    if state.copies:
        assert await asyncio.to_thread(state.finished.wait, 5), "file writers did not settle"
    if state.removing.is_set():
        assert await asyncio.to_thread(state.removed.wait, 5), "file removal did not settle"


async def test_cancelled_probe_settles_copy_before_deleting_or_returning(
    tmp_path, python, file_lifetime
):
    import asyncio

    state = file_lifetime
    tree = _tree(tmp_path, "input", "value = 1\n")
    work = tmp_path / "work"
    work.mkdir()
    (work / "caller-file").write_text("keep", encoding="utf-8")
    task = asyncio.create_task(
        probe_runner.run_probe(
            PARSE, tree, tree_id="candidate", repeat=0, interpreter=python, work_dir=work
        )
    )
    try:
        assert await asyncio.to_thread(state.started.wait, 5)
        for _ in range(3):
            task.cancel()
            await asyncio.wait({task}, timeout=0.03)
            assert not task.done(), "cancelled API returned while its copy could still write"
            assert not state.overlaps, "cleanup deleted an active copy destination"
        state.release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert state.finished.is_set()
        assert list(work.iterdir()) == [work / "caller-file"]
        assert (work / "caller-file").read_text(encoding="utf-8") == "keep"
    finally:
        state.release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("pool_root", [False, True])
async def test_cancellation_settles_directory_removal_before_return(
    tmp_path, python, file_lifetime, pool_root
):
    import asyncio

    state = file_lifetime
    state.release.set()
    tree = _tree(tmp_path, "input", "value = 1\n")
    work = tmp_path / "work"
    work.mkdir()
    state.hold_removal = True
    if pool_root:
        state.expected = 4
        state.removal_prefix = "ouroboros-probes-"
        operation = run_probe_pool(
            [PARSE], {BASE_TREE_ID: tree, "candidate": tree}, interpreter=python, parallel=4
        )
    else:
        operation = probe_runner.run_probe(
            PARSE, tree, tree_id="candidate", repeat=0, interpreter=python, work_dir=work
        )
    task = asyncio.create_task(operation)
    try:
        assert await asyncio.to_thread(state.removing.wait, 5)
        for _ in range(3):
            task.cancel()
            await asyncio.wait({task}, timeout=0.03)
            assert not task.done(), "API returned before owned removal finished"
        state.release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert state.removed.is_set()
        assert list(work.iterdir()) == []
        if pool_root:
            assert state.roots and all(not root.exists() for root in state.roots)
    finally:
        state.release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("caller_work_dir", [True, False])
@pytest.mark.parametrize("child_fails", [True, False])
async def test_pool_joins_writers_and_sibling_cleanup_before_return(
    tmp_path, python, file_lifetime, caller_work_dir, child_fails
):
    import asyncio

    state = file_lifetime
    state.expected = 4  # two trees, each repeated twice
    state.fail_first = child_fails
    trees = {
        BASE_TREE_ID: _tree(tmp_path, "base", "value = 1\n"),
        "candidate": _tree(tmp_path, "candidate", "value = 2\n"),
    }
    work = tmp_path / "caller-work"
    work.mkdir()
    sentinel = work / "caller-file"
    sentinel.write_text("keep", encoding="utf-8")
    task = asyncio.create_task(
        run_probe_pool(
            [PARSE],
            trees,
            interpreter=python,
            parallel=4,
            work_dir=work if caller_work_dir else None,
        )
    )
    try:
        assert await asyncio.to_thread(state.started.wait, 5)
        if child_fails:
            state.fail.set()
            assert await asyncio.to_thread(state.failed.wait, 5)
        else:
            task.cancel()
        await asyncio.wait({task}, timeout=0.05)
        assert not task.done(), "pool returned with sibling file writers still active"
        assert not state.overlaps, "pool/run deletion raced a sibling copy"
        if not child_fails:
            task.cancel()
            await asyncio.wait({task}, timeout=0.03)
            assert not task.done(), "repeated cancellation detached sibling cleanup"
        state.release.set()
        if child_fails:
            with pytest.raises(RuntimeError, match="owned copy failed"):
                await asyncio.wait_for(task, 5)
        else:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
        assert state.finished.is_set()
        assert not state.active
        assert not state.overlaps
        assert sentinel.read_text(encoding="utf-8") == "keep"
        assert list(work.iterdir()) == [sentinel]
        if not caller_work_dir:
            assert state.roots and all(not root.exists() for root in state.roots)
    finally:
        state.fail.set()
        state.release.set()
        await asyncio.gather(task, return_exceptions=True)
>>>>>>> 48f8efd4050451a1125a747e987cdf11edc48553
