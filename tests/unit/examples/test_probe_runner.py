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


async def test_command_output_is_cut_at_the_stream_limit(
    trees: dict[str, Path], python: CheckInterpreter, tmp_path: Path
) -> None:
    probe = command(PY, "-c", f"import sys; sys.stdout.write('z' * {STREAM_LIMIT * 3})")
    record = await _once(probe, trees["cand-a"], python, tmp_path)
    assert record.fields["exit_code"] == 0
    assert record.fields["stdout"] == "z" * STREAM_LIMIT


async def test_undecodable_output_is_escaped(
    trees: dict[str, Path], python: CheckInterpreter, tmp_path: Path
) -> None:
    probe = command(PY, "-c", "import sys; sys.stdout.buffer.write(b'a\\xffb')")
    record = await _once(probe, trees["cand-a"], python, tmp_path)
    assert record.fields["stdout"] == "a\\xffb"


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
