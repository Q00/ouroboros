"""Runner-agnostic regression checks: test commands as data, and their admission."""

from __future__ import annotations

from pathlib import Path
import shlex
import sys
from typing import Any

import pytest

from ouroboros.boundary import base_regression as br
from ouroboros.boundary import target_commands as tc
from ouroboros.boundary.base_regression import ArtifactCheckOutcome as Outcome
from ouroboros.boundary.check_env import pin_interpreter
from ouroboros.boundary.tree import tree_digest


def _tree(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for relative, text in files.items():
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text(text)
    return root


def _checks(base: Path, **options: Any) -> br.ArtifactChecks:
    return br.ArtifactChecks(
        base=base,
        base_digest=tree_digest(base),
        interpreter=pin_interpreter(sys.executable, "test"),
        timeout_seconds=60,
        run_worker_tests=False,
        **options,
    )


# --------------------------------------------------------------------------
# Templates


def test_a_template_is_a_direct_command_that_names_its_target() -> None:
    assert tc.parse_template("python -m unittest {module}") == (
        "python",
        "-m",
        "unittest",
        "{module}",
    )
    assert tc.parse_template("CI=1 npm test -- {target}") == (
        "env",
        "CI=1",
        "npm",
        "test",
        "--",
        "{target}",
    )
    for refused in ("make test", "pytest {target} | tail", "sh -c 'run $1' {target}", ""):
        assert tc.parse_template(refused) is None
    assert tc.declared_test_command({"test_command": "go test {gopkg}"}) == "go test {gopkg}"
    assert tc.declared_test_command({"test_command": "make test"}) is None
    assert tc.declared_test_command(["not", "a", "reply"]) is None


def test_a_concrete_command_becomes_a_template_on_the_test_file_it_names() -> None:
    files = {"tests/test_calc.py", "tests/test_other.py"}
    assert tc.template_from_command("python tests/test_calc.py -v", files) == (
        "python",
        "{target}",
        "-v",
    )
    assert tc.template_from_command("pytest tests/test_calc.py::test_a -q", files) == (
        "pytest",
        "{target}",
        "-q",
    )
    assert tc.template_from_command("python run.py", files) is None
    assert tc.template_from_command("pytest tests/test_calc.py tests/test_other.py", files) is None
    assert tc.runs_pytest(("python3", "-m", "pytest", "{target}"))
    assert tc.runs_pytest(("env", "A=1", "pytest", "{target}"))
    assert not tc.runs_pytest(("python", "-m", "unittest", "{module}"))


def test_placeholders_take_values_only_where_they_mean_something() -> None:
    go = tc.TargetCommand(("go", "test", "{gopkg}"), tc.CommandSource.DEFAULT)
    assert go.argv("pkg/calc_test.go", "r") == ("go", "test", "./pkg")
    assert go.argv("tests/test_calc.py", "r") is None
    django = tc.TargetCommand(("python", "tests/runtests.py", "{label}"), tc.CommandSource.DEFAULT)
    assert django.argv("tests/app/test_x.py", "r") == ("python", "tests/runtests.py", "app.test_x")
    assert django.argv("app/tests/test_x.py", "r") is None


def test_sources_keep_their_priority_and_pytest_goes_to_the_products_own_run() -> None:
    files = {"tests/test_calc.py"}
    commands = tc.command_set(
        seed_commands=("pytest tests/test_calc.py", "python tests/test_calc.py"),
        constructor_command="python -m unittest {module}",
        transcript=("python tests/test_calc.py",),
        test_files=files,
        defaults=tc.default_commands({"go.mod"}, lambda _path: None, python_targets=False),
    )
    assert commands.pytest_declared
    assert [(c.source, c.template) for c in commands.commands] == [
        (tc.CommandSource.SEED, ("python", "{target}")),
        (tc.CommandSource.CONSTRUCTOR, ("python", "-m", "unittest", "{module}")),
        (tc.CommandSource.DEFAULT, ("go", "test", "{gopkg}")),
    ]


def test_built_in_defaults_come_from_the_trees_structure() -> None:
    read = {"package.json": '{"scripts": {"test": "jest"}}'}.get
    paths = {"package.json", "go.mod", "Cargo.toml", "tests/runtests.py", "bin/test"}
    templates = [c.template for c in tc.default_commands(paths, read, python_targets=True)]
    assert templates == [
        ("python", "tests/runtests.py", "--parallel", "1", "{label}"),
        ("python", "bin/test", "{target}"),
        ("npm", "test", "--", "{target}"),
        ("go", "test", "{gopkg}"),
        ("cargo", "test", "--test", "{stem}"),
    ]
    no_script = tc.default_commands(
        {"package.json"}, {"package.json": "{}"}.get, python_targets=False
    )
    assert no_script == ()


def test_other_languages_pair_by_path_only() -> None:
    base = {
        "pkg/calc.go",
        "pkg/calc_test.go",
        "other/calc_test.go",
        "src/widget.ts",
        "src/widget.test.ts",
        "src/__tests__/widget.tsx",
        "web/widget.spec.js",
        "src/main/java/Foo.java",
        "src/test/java/FooTest.java",
        "src/lib.rs",
        "tests/lib.rs",
    }
    changed = ["pkg/calc.go", "src/widget.ts", "src/main/java/Foo.java", "src/lib.rs", "README.md"]
    assert tc.select_other_tests(base, changed) == (
        "pkg/calc_test.go",
        "src/__tests__/widget.tsx",
        "src/test/java/FooTest.java",
        "src/widget.test.ts",
        "tests/lib.rs",
        "web/widget.spec.js",
    )
    assert tc.changed_other_sources(["pkg/calc_test.go", "src/widget.test.ts"]) == ()


# --------------------------------------------------------------------------
# Admission: the canary


SCRIPT_BASE = {
    "calc/__init__.py": "",
    "calc/ops.py": "def add(a, b):\n    return a - b\n",
    "tests/__init__.py": "",
    "tests/test_calc.py": "import os, sys\nsys.path.insert(0, os.getcwd())\n"
    "from calc.ops import add\nassert add(5, 3) == 2\n",
    "tests/test_other.py": "print('always fine')\n",
}


async def _admission(tmp_path: Path, text: str, target: str = "tests/test_calc.py") -> Any:
    base = _tree(tmp_path / "base", SCRIPT_BASE)
    checks = _checks(base)
    assert checks._pinned_base() is not None
    template = tc.parse_template(text)
    assert template is not None
    return await checks._admit(base, tc.TargetCommand(template, tc.CommandSource.SEED), target)


@pytest.mark.parametrize(
    "template",
    ["python -c pass {target}", "python tests/test_other.py {target}"],
    ids=["no_op", "wrong_target"],
)
async def test_the_canary_refuses_a_command_that_does_not_run_the_target(
    tmp_path: Path, template: str
) -> None:
    admission = await _admission(tmp_path, template)
    assert not admission.admitted and not admission.transient


async def test_the_canary_admits_a_command_that_runs_the_target(tmp_path: Path) -> None:
    admission = await _admission(tmp_path, "python {target}")
    assert admission.admitted and admission.tier is tc.Tier.EXIT


async def test_admission_is_cached_per_base_command_and_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = _tree(tmp_path / "base", SCRIPT_BASE)
    checks = _checks(base)
    checks._pinned_base()
    calls: list[str] = []
    real = br._run_command

    async def counting(*args: Any, **kwargs: Any) -> Any:
        calls.append(args[2])
        return await real(*args, **kwargs)

    monkeypatch.setattr(br, "_run_command", counting)
    command = tc.TargetCommand(("python", "{target}"), tc.CommandSource.SEED)
    await checks._admit(base, command, "tests/test_calc.py")
    await checks._admit(base, command, "tests/test_calc.py")
    assert calls == ["tests/test_calc.py"] * 3


# --------------------------------------------------------------------------
# Non-pytest runners end to end


UNITTEST_BASE = {
    "calc/__init__.py": "",
    "calc/ops.py": "def add(a, b):\n    return a - b\n",
    "tests/__init__.py": "",
    "tests/test_calc.py": "import unittest\nfrom calc.ops import add\n\n\n"
    "class Pins(unittest.TestCase):\n    def test_pins(self):\n        self.assertEqual(add(5, 3), 2)\n",
}


def _fixed(candidate: Path) -> Path:
    (candidate / "calc/ops.py").write_text("def add(a, b):\n    return a + b\n")
    return candidate


async def test_a_unittest_command_finds_a_regression_by_exit_status(tmp_path: Path) -> None:
    base = _tree(tmp_path / "base", UNITTEST_BASE)
    candidate = _fixed(_tree(tmp_path / "work", UNITTEST_BASE))

    regression, _worker = await _checks(
        base, constructor_command="python -m unittest {module}"
    ).findings(candidate)

    assert regression.outcome is Outcome.REJECTED
    assert regression.failed == ("tests/test_calc.py",)


async def test_a_plain_script_that_exits_by_result_finds_a_regression(tmp_path: Path) -> None:
    files = {**SCRIPT_BASE, "run_one.sh": 'PYTHONPATH="$PWD" exec python "$1"\n'}
    base = _tree(tmp_path / "base", files)
    candidate = _fixed(_tree(tmp_path / "work", files))

    regression, _worker = await _checks(
        base, seed_commands=("sh run_one.sh tests/test_calc.py",)
    ).findings(candidate)

    assert regression.outcome is Outcome.REJECTED
    assert regression.failed == ("tests/test_calc.py",)
    # The same target, unchanged on the candidate, is no regression.
    passing, _worker = await _checks(
        base, seed_commands=("sh run_one.sh tests/test_calc.py",)
    ).findings(_tree(tmp_path / "same", {**files, "README.md": "x\n"}))
    assert passing.outcome is Outcome.NO_SELECTED_FILES


JUNIT_RUNNER = """import importlib.util, sys, xml.sax.saxutils as x
sys.path.insert(0, ".")
spec = importlib.util.spec_from_file_location("target", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
cases, failed = [], False
for name in sorted(n for n in dir(module) if n.startswith("test_")):
    try:
        getattr(module, name)()
        cases.append('<testcase classname="t" name="%s"/>' % name)
    except Exception:
        failed = True
        cases.append('<testcase classname="t" name="%s"><failure/></testcase>' % name)
if len(sys.argv) > 2:
    with open(sys.argv[2], "w") as handle:
        handle.write("<testsuite>%s</testsuite>" % "".join(cases))
sys.exit(1 if failed else 0)
"""
# ``bin/test`` marks a project runner pytest cannot drive, so the product's own
# pytest run does not stand in for the commands under test.
JUNIT_BASE = {
    "bin/test": "",
    "calc/__init__.py": "",
    "calc/ops.py": "def add(a, b):\n    return a - b\n",
    "junit_run.py": JUNIT_RUNNER,
    "tests/test_calc.py": "from calc.ops import add\n\n"
    "def test_pins():\n    assert add(5, 3) == 2\n\n"
    "def test_broken_on_base():\n    assert False\n",
}


async def test_a_base_failure_drops_the_file_on_the_floor_and_not_on_the_junit_tier(
    tmp_path: Path,
) -> None:
    base = _tree(tmp_path / "base", JUNIT_BASE)
    candidate = _fixed(_tree(tmp_path / "work", JUNIT_BASE))

    floor, _worker = await _checks(
        base, constructor_command="python junit_run.py {target}"
    ).findings(candidate)
    junit, _worker = await _checks(
        base, constructor_command="python junit_run.py {target} {report}"
    ).findings(candidate)

    # The file fails on the base, so its exit status says nothing.
    assert floor.outcome is Outcome.NO_ADMITTED_COMMAND
    # Per test, the stable base pass that now fails is a regression.
    assert junit.outcome is Outcome.REJECTED and junit.failed == ("t::test_pins",)


async def test_no_admitted_command_is_no_observation_and_no_command_is_unsupported(
    tmp_path: Path,
) -> None:
    files = {
        "src/widget.ts": "export const w = 1;\n",
        "src/widget.test.ts": "import { w } from './widget';\n",
    }
    base = _tree(tmp_path / "base", files)
    candidate = _tree(tmp_path / "work", {**files, "src/widget.ts": "export const w = 2;\n"})

    refused, _worker = await _checks(base, constructor_command="python -c pass {target}").findings(
        candidate
    )
    nothing, _worker = await _checks(base).findings(candidate)

    assert refused.outcome is Outcome.NO_ADMITTED_COMMAND and not refused.rejects
    assert nothing.outcome is Outcome.UNSUPPORTED_RUNNER and not nothing.rejects


def test_a_change_in_another_language_leaves_nothing_exempt(tmp_path: Path) -> None:
    import asyncio

    files = {"src/widget.ts": "export const w = 1;\n", "calc.py": "def f():\n    return 1\n"}
    base = _tree(tmp_path / "base", files)
    candidate = _tree(
        tmp_path / "work",
        {"src/widget.ts": "export const w = 2;\n", "calc.py": "def f():\n    return 2\n"},
    )
    checks = _checks(base)
    assert asyncio.run(checks.changed_functions(candidate)) is None


# --------------------------------------------------------------------------
# Sources: the constructor's reply and the worker's transcript


async def test_the_constructors_declared_command_reaches_the_run_state(
    tmp_path: Path,
) -> None:
    from ouroboros.boundary.constructor import ConstructionOutcome
    from ouroboros.boundary.run_wiring import CheckPackageSettings, prepare_check_package
    from ouroboros.persistence.event_store import EventStore

    from .calc_fixtures import _seed
    from .fake_constructors import FakeConstructor

    store = EventStore("sqlite+aiosqlite:///:memory:")
    await store.initialize()
    try:
        repo = _tree(tmp_path / "repo", {"calc.py": "def add(a, b):\n    return a - b\n"})
        outage = ConstructionOutcome(
            None, "constructor_timeout", "1" * 64, "fake", test_command="go test {gopkg}"
        )
        state = await prepare_check_package(
            _seed("add(2, 3) returns 5"),
            event_store=store,
            constructor=FakeConstructor(outage, outage),
            execution_id="exec_declared",
            base_checkout=repo,
            worker_workspace=repo,
            runtime_label="codex",
            settings=CheckPackageSettings(enabled=True),
            store_dir=tmp_path / "store",
        )
    finally:
        await store.close()
    assert state.test_command == "go test {gopkg}"


def test_the_workers_test_invocations_are_read_from_its_transcript() -> None:
    from types import SimpleNamespace

    from ouroboros.orchestrator.adapter import AgentMessage

    def bash(command: str, call_id: str) -> tuple[AgentMessage, AgentMessage]:
        wrapped = "/bin/bash -lc " + shlex.quote(command)
        call = AgentMessage(
            type="assistant",
            content=f"Calling tool: Bash: {wrapped}",
            tool_name="Bash",
            data={"tool_input": {"command": wrapped}, "tool_call_id": call_id},
        )
        result = AgentMessage(
            type="tool_result",
            content="",
            data={
                "tool_call_id": call_id,
                "exit_code": 0,
                "tool_result": {"is_error": False, "meta": {"tool_call_id": call_id}},
            },
        )
        return call, result

    messages = (*bash("ls -la", "c1"), *bash("python -m pytest tests/test_calc.py -q", "c2"))
    commands = tc.transcript_commands([SimpleNamespace(messages=messages)], "/work")
    assert commands == ("python -m pytest tests/test_calc.py -q",)
    assert tc.template_from_command(commands[0], {"tests/test_calc.py"}) == (
        "python",
        "-m",
        "pytest",
        "{target}",
        "-q",
    )


# --------------------------------------------------------------------------
# A regression is a second observed failure, on both tiers


def _scripted(monkeypatch: pytest.MonkeyPatch, runs: list[tc.CommandRun]) -> None:
    script = list(runs)

    async def run(*_args: Any, **_kwargs: Any) -> tc.CommandRun:
        return script.pop(0)

    monkeypatch.setattr(br, "_run_command", run)


SECOND = [
    (tc.CommandRun(None, timed_out=True), Outcome.TIMEOUT),
    (tc.CommandRun(None, unavailable=True), Outcome.UNAVAILABLE),
    (tc.CommandRun(0), Outcome.PASSED),
]


@pytest.mark.parametrize(
    ("rerun", "expected", "failed"),
    [
        *((run, outcome, ()) for run, outcome in SECOND),
        (tc.CommandRun(1), Outcome.REJECTED, ("tests/test_calc.py",)),
    ],
    ids=["then_timeout", "then_unavailable", "then_pass", "then_fail"],
)
async def test_the_exit_floor_counts_only_a_second_observed_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rerun: tc.CommandRun,
    expected: Outcome,
    failed: tuple[str, ...],
) -> None:
    base = _tree(tmp_path / "base", {**UNITTEST_BASE, "bin/test": ""})
    candidate = _fixed(_tree(tmp_path / "work", {**UNITTEST_BASE, "bin/test": ""}))
    # Admission (two base passes, a failing canary), the candidate's failure, the rerun.
    _scripted(
        monkeypatch, [tc.CommandRun(0), tc.CommandRun(0), tc.CommandRun(1), tc.CommandRun(1), rerun]
    )

    regression, _worker = await _checks(
        base, constructor_command="python -m unittest {module}"
    ).findings(candidate)

    assert (regression.outcome, regression.failed) == (expected, failed)


@pytest.mark.parametrize(
    ("rerun", "expected", "failed"),
    [
        *((run, outcome, ()) for run, outcome in SECOND[:2]),
        (tc.CommandRun(0, {"t::test_pins": "pass"}), Outcome.PASSED, ()),
        (tc.CommandRun(1, {"t::other": "pass"}), Outcome.UNCONFIRMED, ()),
        (tc.CommandRun(1, {"t::test_pins": "fail"}), Outcome.REJECTED, ("t::test_pins",)),
    ],
    ids=["then_timeout", "then_unavailable", "then_pass", "then_missing", "then_fail"],
)
async def test_the_junit_tier_counts_only_a_second_observed_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rerun: tc.CommandRun,
    expected: Outcome,
    failed: tuple[str, ...],
) -> None:
    base = _tree(tmp_path / "base", JUNIT_BASE)
    candidate = _fixed(_tree(tmp_path / "work", JUNIT_BASE))
    passing = tc.CommandRun(1, {"t::test_pins": "pass", "t::test_broken_on_base": "fail"})
    _scripted(
        monkeypatch,
        [passing, passing, tc.CommandRun(1), tc.CommandRun(1, {"t::test_pins": "fail"}), rerun],
    )

    regression, _worker = await _checks(
        base, constructor_command="python junit_run.py {target} {report}"
    ).findings(candidate)

    assert (regression.outcome, regression.failed) == (expected, failed)
