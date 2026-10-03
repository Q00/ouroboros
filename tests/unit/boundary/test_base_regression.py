"""Controller-run checks of the whole artifact (boundary/base_regression.py)."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
import sys
from typing import Any

import pytest

from ouroboros.boundary import base_regression as br
from ouroboros.boundary.acceptance import (
    ArtifactCheck,
    CriterionVerdict,
    ExistingOutcome,
    Governor,
    PackageCriterionStatus,
    reconcile_acceptance,
)
from ouroboros.boundary.base_regression import ArtifactCheckOutcome as Outcome
from ouroboros.boundary.binding import CheckTier
from ouroboros.boundary.check_env import pin_interpreter
from ouroboros.boundary.tree import tree_digest

BASE_FILES = [
    "pkg/__init__.py",
    "pkg/export.py",
    "pkg/tests/__init__.py",
    "pkg/tests/test_export.py",
    "pkg/tests/test_tree.py",
    "pkg/tests/test_relative.py",
    "pkg/tests/test_patches.py",
    "pkg/tests/test_mentions.py",
    "pkg/utils/tests/test_validation.py",
    "src/_pytest/logging.py",
    "testing/logging/test_reporting.py",
    "testing/test_skipping.py",
    "conftest.py",
]
TEXT = {
    "pkg/tests/test_export.py": "from pkg.export import export_text\n",
    "pkg/tests/test_tree.py": "from pkg import Tree\n",
    "pkg/tests/test_relative.py": "from .. import export\n",
    "pkg/tests/test_patches.py": "from unittest import mock\n@mock.patch('pkg.export.render')\n"
    "def test_a(render):\n    pass\n",
    # A comment or a longer name is not an import: selection is structural.
    "pkg/tests/test_mentions.py": "# pkg.export is changed often\nimport pkg.exporter\n",
    "pkg/utils/tests/test_validation.py": "import pkg.export as exported\n",
    "testing/logging/test_reporting.py": "import logging\n",
    "testing/test_skipping.py": "from _pytest.skipping import evaluate_skip_marks\n",
}


# --------------------------------------------------------------------------
# Selection


def test_selection_pairs_by_stem_and_by_a_test_directory_named_after_the_module() -> None:
    tests = br.base_test_files(BASE_FILES)
    assert br.paired_tests(tests, "pkg/export.py") == ("pkg/tests/test_export.py",)
    # A test directory named after the changed module counts as paired.
    assert br.paired_tests(tests, "src/_pytest/logging.py") == (
        "testing/logging/test_reporting.py",
    )


def test_selection_follows_imports_not_text() -> None:
    assert br.select_tests(BASE_FILES, ["pkg/export.py"], TEXT) == (
        "pkg/tests/test_export.py",
        "pkg/tests/test_patches.py",
        "pkg/tests/test_relative.py",
        "pkg/utils/tests/test_validation.py",
    )


def test_a_package_init_selects_only_the_tests_that_import_the_package_itself() -> None:
    assert br.select_tests(BASE_FILES, ["pkg/__init__.py"], TEXT) == (
        "pkg/tests/test_relative.py",
        "pkg/tests/test_tree.py",
    )


def test_added_files_and_edited_tests_select_nothing() -> None:
    changed = ["test_repro.py", "pkg/tests/test_export.py", "README.md"]
    assert br.select_tests(BASE_FILES, changed, TEXT) == ()
    added = ["test_repro.py", "scripts/helper.py", "tests/test_new.py", "tests/conftest.py"]
    assert br.worker_test_files(added) == ("test_repro.py", "tests/test_new.py")


def test_the_footprint_seam_keeps_every_regressed_test_for_now() -> None:
    assert br.regressions_to_keep(("t::a", "t::b"), ("pkg.export.render",)) == ("t::a", "t::b")


# --------------------------------------------------------------------------
# Reports and regressions

JUNIT = b"""<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest">
<testcase classname="pkg.tests.test_export" name="test_a"/>
<testcase classname="pkg.tests.test_export" name="test_b"><failure message="x"/></testcase>
<testcase classname="pkg.tests.test_export.TestK" name="test_c"><skipped message="s"/></testcase>
<testcase classname="pkg.tests.test_export" name="test_d"><error message="e"/></testcase>
<testcase classname="" name="pkg.tests.test_tree"><error message="collection failure"/></testcase>
</testsuite></testsuites>"""


def test_the_report_gives_per_test_status() -> None:
    assert br.parse_junit(JUNIT) == {
        "pkg.tests.test_export::test_a": "pass",
        "pkg.tests.test_export::test_b": "fail",
        "pkg.tests.test_export.TestK::test_c": "skip",
        "pkg.tests.test_export::test_d": "error",
        "pkg.tests.test_tree": "error",
    }
    assert br.parse_junit(b"not xml") is None
    assert br.parse_junit(b"<html/>") is None


def test_a_regression_is_a_stable_base_pass_that_fails_or_vanishes() -> None:
    stable = ("m.test_a::a", "m.test_a::b", "m.test_b.K::c", "m.test_c::d")
    candidate = {
        "m.test_a::a": "pass",
        "m.test_a::b": "fail",
        "m.test_b": "error",  # collection error: its tests vanished
    }
    assert br.regressions(stable, candidate) == ("m.test_a::b", "m.test_b.K::c")
    # A runner that died before writing its report fails every stable test.
    assert br.regressions(stable, None) == stable


# --------------------------------------------------------------------------
# The checks on real trees, with the runner replaced


def _tree(root: Path, files: dict[str, str]) -> Path:
    for relative, text in files.items():
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text(text)
    return root


BASE_TREE = {
    "calc/__init__.py": "",
    "calc/ops.py": "def add(a, b):\n    return a - b\n",
    "calc/tests/__init__.py": "",
    "calc/tests/conftest.py": "",
    "calc/tests/test_ops.py": "from calc.ops import add\ndef test_zero():\n    assert add(0, 0) == 0\n",
    "README.md": "calc\n",
}
TEST_FILE = "calc/tests/test_ops.py"


class _Runner:
    """Stands in for ``_pytest``: records each run and returns the next scripted result."""

    def __init__(self, base: list[Any], candidate: list[Any]) -> None:
        self.base, self.candidate = list(base), list(candidate)
        self.calls: list[tuple[str, tuple[str, ...], str | None]] = []

    async def __call__(
        self, root: Path, files: Any, interpreter: Any, timeout: int, **_options: Any
    ) -> Any:
        kind = root.name
        seen = (root / TEST_FILE).read_text() if (root / TEST_FILE).exists() else None
        self.calls.append((kind, tuple(files), seen))
        script = self.base if kind == "base" else self.candidate
        return script.pop(0)


def _run(statuses: dict[str, str] | None, code: int | None = 0, **flags: Any) -> Any:
    return br._Run(statuses, code, **flags)


def _checks(base: Path) -> br.ArtifactChecks:
    return br.ArtifactChecks(
        base=base,
        base_digest=tree_digest(base),
        interpreter=pin_interpreter(sys.executable, "test"),
        timeout_seconds=30,
    )


@pytest.fixture
def trees(tmp_path: Path) -> tuple[Path, Path]:
    base = _tree(tmp_path / "base", BASE_TREE)
    candidate = _tree(tmp_path / "work", BASE_TREE)
    (candidate / "calc/ops.py").write_text("def add(a, b):\n    return a + b + 1\n")
    return base, candidate


PASSING = {"calc.tests.test_ops::test_zero": "pass", "calc.tests.test_ops::test_one": "pass"}


async def test_a_worker_edit_to_a_selected_test_is_undone_before_the_run(
    trees: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    base, candidate = trees
    (candidate / TEST_FILE).write_text("def test_zero():\n    pass\n")
    (candidate / "calc/tests/conftest.py").write_text("collect_ignore = ['test_ops.py']\n")
    failing = {**PASSING, "calc.tests.test_ops::test_zero": "fail"}
    runner = _Runner([_run(PASSING), _run(PASSING)], [_run(failing, 1)])
    monkeypatch.setattr(br, "_pytest", runner)

    regression, _worker = await _checks(base).findings(candidate)

    assert regression.outcome is Outcome.REJECTED
    assert regression.failed == ("calc.tests.test_ops::test_zero",)
    assert regression.selected == (TEST_FILE,)
    # Base twice, candidate once; the candidate ran the base bytes of the test file.
    assert [call[0] for call in runner.calls] == ["base", "base", "candidate"]
    assert {call[2] for call in runner.calls} == {BASE_TREE[TEST_FILE]}
    # The worker's workspace itself is never touched.
    assert (candidate / TEST_FILE).read_text() == "def test_zero():\n    pass\n"


async def test_a_test_that_passed_on_one_base_run_only_is_never_a_regression(
    trees: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    base, candidate = trees
    flaky = {**PASSING, "calc.tests.test_ops::test_one": "fail"}
    failing = dict.fromkeys(PASSING, "fail")
    runner = _Runner([_run(PASSING), _run(flaky, 1)], [_run(failing, 1)])
    monkeypatch.setattr(br, "_pytest", runner)

    regression, _worker = await _checks(base).findings(candidate)

    assert regression.failed == ("calc.tests.test_ops::test_zero",)


async def test_the_base_runs_once_per_selection_across_attempts(
    trees: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    base, candidate = trees
    runner = _Runner([_run(PASSING), _run(PASSING)], [_run(PASSING), _run(PASSING)])
    monkeypatch.setattr(br, "_pytest", runner)
    checks = _checks(base)

    first, _ = await checks.findings(candidate)
    again, _ = await checks.findings(candidate)  # the same tree: cached
    (candidate / "calc/ops.py").write_text("def add(a, b):\n    return a + b\n")
    repaired, _ = await checks.findings(candidate)  # a new tree: the candidate only

    assert first.outcome is again.outcome is repaired.outcome is Outcome.PASSED
    assert [call[0] for call in runner.calls] == ["base", "base", "candidate", "candidate"]


@pytest.mark.parametrize(
    ("base_runs", "candidate_runs", "expected"),
    [
        ([_run(PASSING), _run(PASSING)], [_run(None, None, timed_out=True)], Outcome.TIMEOUT),
        ([_run(None, None, timed_out=True), _run(PASSING)], [], Outcome.TIMEOUT),
        ([_run(None, 2), _run(None, 2)], [], Outcome.BASE_RUNNER_CRASH),
        ([_run(None, None, unavailable=True), _run(PASSING)], [], Outcome.UNAVAILABLE),
    ],
    ids=["candidate_timeout", "base_timeout", "base_runner_crash", "sandbox_unavailable"],
)
async def test_a_run_without_an_observation_decides_nothing(
    trees: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    base_runs: list[Any],
    candidate_runs: list[Any],
    expected: Outcome,
) -> None:
    base, candidate = trees
    monkeypatch.setattr(br, "_pytest", _Runner(base_runs, candidate_runs))

    regression, _worker = await _checks(base).findings(candidate)

    assert regression.outcome is expected and not regression.rejects
    verdicts = {"k0": _verdict("k0", PackageCriterionStatus.UNVERIFIED)}
    assert br.apply_findings(verdicts, ["k0", "k1"], (regression,)) == verdicts


async def test_a_dead_runner_on_the_candidate_fails_every_stable_test(
    trees: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    base, candidate = trees
    runner = _Runner([_run(PASSING), _run(PASSING)], [_run(None, 4)])
    monkeypatch.setattr(br, "_pytest", runner)

    regression, _worker = await _checks(base).findings(candidate)

    assert regression.outcome is Outcome.REJECTED and regression.failed == tuple(sorted(PASSING))


async def test_no_selected_file_and_an_unsupported_runner_are_reasons(
    tmp_path: Path, trees: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    base, candidate = trees
    monkeypatch.setattr(br, "_pytest", _Runner([], []))
    (candidate / "calc/ops.py").write_text(BASE_TREE["calc/ops.py"])
    (candidate / "README.md").write_text("changed\n")
    regression, worker = await _checks(base).findings(candidate)
    assert (regression.outcome, worker.outcome) == (Outcome.NO_SELECTED_FILES,) * 2

    runner_base = _tree(tmp_path / "django", {**BASE_TREE, "tests/runtests.py": ""})
    regression, worker = await _checks(runner_base).findings(candidate)
    assert (regression.outcome, worker.outcome) == (Outcome.UNSUPPORTED_RUNNER,) * 2


async def test_a_base_snapshot_that_changed_is_not_used(
    trees: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    base, candidate = trees
    monkeypatch.setattr(br, "_pytest", _Runner([], []))
    checks = _checks(base)
    (base / "calc/ops.py").write_text("tampered\n")
    regression, worker = await checks.findings(candidate)
    assert (regression.outcome, worker.outcome) == (Outcome.UNAVAILABLE,) * 2


# --------------------------------------------------------------------------
# The worker-test gate: exit status 1 with a failing test fails; 2, 5 and the rest are no result


@pytest.mark.parametrize(
    ("run", "expected"),
    [
        (_run({"t::a": "fail"}, 1), Outcome.REJECTED),
        (_run({"t::a": "pass"}, 0), Outcome.PASSED),
        (_run({}, 5), Outcome.NOT_A_TEST_RESULT),
        (_run({"t": "error"}, 2), Outcome.NOT_A_TEST_RESULT),
        # Exit 1 without a report (no runner in the environment) is no test result.
        (_run(None, 1), Outcome.NOT_A_TEST_RESULT),
        (_run(None, None, timed_out=True), Outcome.TIMEOUT),
    ],
    ids=["exit_1", "exit_0", "exit_5", "exit_2", "exit_1_no_report", "timeout"],
)
async def test_the_worker_test_gate_reads_the_exit_status(
    trees: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, run: Any, expected: Outcome
) -> None:
    base, candidate = trees
    (candidate / "calc/ops.py").write_text(BASE_TREE["calc/ops.py"])
    (candidate / "calc/tests/test_new.py").write_text("def test_new():\n    assert False\n")
    monkeypatch.setattr(br, "_pytest", _Runner([], [run]))

    _regression, worker = await _checks(base).findings(candidate)

    assert worker.outcome is expected
    assert worker.selected == ("calc/tests/test_new.py",)
    assert worker.failed == (("calc/tests/test_new.py",) if expected is Outcome.REJECTED else ())


# --------------------------------------------------------------------------
# What a finding decides


def _verdict(key: str, status: PackageCriterionStatus) -> CriterionVerdict:
    tier = CheckTier.U if status.is_unverified else CheckTier.A
    return CriterionVerdict(key, status, tier, status.value, declared_binding_pass=False)


REJECTED = br.ArtifactFinding(
    ArtifactCheck.BASE_REGRESSION, Outcome.REJECTED, ("m::test_a",), ("m.py",)
)


def test_a_rejection_fails_only_what_the_package_left_undecided() -> None:
    keys = ["pass", "unverified", "uncovered", "fail", "indeterminate", "absent"]
    verdicts = {key: _verdict(key, PackageCriterionStatus(key)) for key in keys[:5]}

    out = br.apply_findings(verdicts, keys, (REJECTED,))

    for key in ("pass", "fail", "indeterminate"):
        assert out[key] == verdicts[key]  # a verified pass keeps the package's authority
    for key in ("unverified", "uncovered", "absent"):
        assert out[key].status is PackageCriterionStatus.FAIL
        assert out[key].artifact_check is ArtifactCheck.BASE_REGRESSION
        assert out[key].reason == br.REGRESSION_REASON
    passed = br.ArtifactFinding(ArtifactCheck.BASE_REGRESSION, Outcome.PASSED)
    assert br.apply_findings(verdicts, keys, (passed,)) == verdicts


def test_a_rejection_flows_through_the_fail_route_and_the_journal_admits_it() -> None:
    keys = ["k0", "k1"]
    verdicts = br.apply_findings(
        {
            "k0": _verdict("k0", PackageCriterionStatus.PASS),
            "k1": _verdict("k1", PackageCriterionStatus.UNVERIFIED),
        },
        keys,
        (REJECTED,),
    )
    legacy = {
        index: ExistingOutcome(index, "succeeded", "accepted", "completed") for index in (0, 1)
    }
    reconciliation = reconcile_acceptance(
        keys, verdicts, legacy, existing_run_accepted=True, legacy_decides_unverified=True
    )
    first, second = reconciliation.decisions
    assert first.accepted and first.artifact_check is None
    assert not second.accepted and second.governed_by is Governor.CHECK_PACKAGE
    assert second.artifact_check is ArtifactCheck.BASE_REGRESSION
    assert reconciliation.run_accepted is False
    payload = reconciliation.to_payload()
    assert payload.criteria[1].artifact_check == "base_regression"


def test_the_repair_names_the_failing_tests_and_never_the_selection() -> None:
    worker = br.ArtifactFinding(
        ArtifactCheck.WORKER_TESTS, Outcome.REJECTED, ("tests/test_new.py",), ("tests/test_new.py",)
    )
    message = br.repair_message((REJECTED, worker))
    assert message is not None
    assert "m::test_a" in message and "tests/test_new.py" in message
    assert "m.py" not in message.replace("tests/test_new.py", "")
    assert (
        br.repair_message((br.ArtifactFinding(ArtifactCheck.BASE_REGRESSION, Outcome.PASSED),))
        is None
    )


def test_never_reads_the_grader_or_the_criteria() -> None:
    """The decision has no input but trees: no grader material, no criterion or claim text."""
    tree = ast.parse(inspect.getsource(br))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if ast.get_docstring(node) is not None:
                node.body = node.body[1:] or [ast.Pass()]
    code = ast.unparse(tree)
    for forbidden in (
        "FAIL_TO_PASS",
        "PASS_TO_PASS",
        "test_patch",
        "gold",
        "acceptance_criteria",
        "ac_content",
        "criterion_text",
        "typed_evidence",
        "claim",
        "Seed",
    ):
        assert forbidden not in code, forbidden
    imported = {
        node.module
        for node in ast.walk(ast.parse(inspect.getsource(br)))
        if isinstance(node, ast.ImportFrom)
    }
    assert not {"ouroboros.core.seed", "ouroboros.boundary.package"} & imported
    # The functions that decide take trees, paths and findings: never a criterion's text.
    for function in (br.select_tests, br.apply_findings, br.repair_message):
        assert not {"seed", "criteria", "text", "content"} & set(
            inspect.signature(function).parameters
        )


# --------------------------------------------------------------------------
# End to end with the real runner (the sandbox is switched off for unit tests)


async def test_the_real_runner_finds_a_regression_and_keeps_a_fixed_test(
    tmp_path: Path,
) -> None:
    pytest.importorskip("pytest")
    files = {
        **BASE_TREE,
        TEST_FILE: "from calc.ops import add\n"
        "def test_pins_subtraction():\n    assert add(5, 3) == 2\n"
        "def test_zero():\n    assert add(0, 0) == 0\n",
    }
    base = _tree(tmp_path / "base", files)
    candidate = _tree(tmp_path / "work", files)
    (candidate / "calc/ops.py").write_text("def add(a, b):\n    return a + b\n")
    (candidate / "calc/tests/test_added.py").write_text(
        "from calc.ops import add\ndef test_new():\n    assert add(1, 1) == 3\n"
    )

    regression, worker = await _checks(base).findings(candidate)

    assert regression.outcome is Outcome.REJECTED
    assert regression.failed == ("calc.tests.test_ops::test_pins_subtraction",)
    assert worker.outcome is Outcome.REJECTED
    assert worker.failed == ("calc/tests/test_added.py",)


# --------------------------------------------------------------------------
# Inside the runner: bounded repair while the worker runs, the fail route at the end


@pytest.fixture
async def store():
    from ouroboros.persistence.event_store import EventStore

    event_store = EventStore("sqlite+aiosqlite:///:memory:")
    await event_store.initialize()
    yield event_store
    await event_store.close()


async def _calc_authority(store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    from ouroboros.boundary import run_wiring
    from ouroboros.boundary.authority import CheckPackageAuthority
    from ouroboros.boundary.run_wiring import CheckPackageSettings, prepare_check_package

    from .calc_fixtures import BUGFIX_SCRIPT, _package, _seed
    from .fake_constructors import FakeConstructor, _ok

    repo = _tree(
        tmp_path / "repo",
        {
            "calc.py": "def add(a, b):\n    return a - b\n",
            "tests/test_calc.py": "from calc import add\n"
            "def test_pins_subtraction():\n    assert add(5, 3) == 2\n",
        },
    )
    monkeypatch.setattr(
        run_wiring, "resolve_check_interpreter", lambda _base: pin_interpreter(sys.executable, "t")
    )
    seed = _seed("add(2, 3) returns 5")
    settings = CheckPackageSettings(enabled=True, base_regression=True)
    state = await prepare_check_package(
        seed,
        event_store=store,
        constructor=FakeConstructor(_ok(_package(seed, "repro_add", BUGFIX_SCRIPT))),
        execution_id="exec_regression",
        base_checkout=repo,
        worker_workspace=repo,
        runtime_label="codex",
        settings=settings,
        store_dir=tmp_path / "store",
    )
    assert state.base_snapshot is not None and state.contract.base_regression
    authority = CheckPackageAuthority(state, settings, event_store=store, candidate_checkout=repo)
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    return seed, authority


def _succeeded() -> Any:
    from ouroboros.orchestrator.parallel_executor_models import (
        ACExecutionOutcome,
        ACExecutionResult,
        ParallelExecutionResult,
    )

    result = ACExecutionResult(
        ac_index=0, ac_content="criterion 0", success=True, outcome=ACExecutionOutcome.SUCCEEDED
    )
    return result, ParallelExecutionResult(
        results=(result,), success_count=1, failure_count=0, externally_satisfied_count=0
    )


async def test_the_gate_sends_a_regression_back_with_the_failing_test_names(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ouroboros.orchestrator.parallel_executor_models import package_repair

    seed, authority = await _calc_authority(store, tmp_path, monkeypatch)
    result, _parallel = _succeeded()

    decided = await authority.gate(
        seed=seed, ac_index=0, result=result, execution_id="exec_regression"
    )

    assert decided.success is False
    repair = package_repair(decided)
    assert repair is not None and "tests.test_calc::test_pins_subtraction" in repair
    assert authority.gate.artifact_repairs == 1


async def test_the_final_decision_fails_an_unverified_criterion_and_the_journal_records_it(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ouroboros.boundary.authority import ARTIFACT_CHECK_ERROR
    from ouroboros.boundary.events import ACCEPTANCE_RECONCILED, BOUNDARY_AGGREGATE_TYPE

    seed, authority = await _calc_authority(store, tmp_path, monkeypatch)
    _result, parallel = _succeeded()

    decided = await authority(seed=seed, execution_id="exec_regression", parallel_result=parallel)

    assert authority.outcome is not None and authority.outcome.error is None
    (decision,) = authority.outcome.reconciliation.decisions
    assert decision.package_status is PackageCriterionStatus.FAIL
    assert decision.artifact_check is ArtifactCheck.BASE_REGRESSION
    assert decided.results[0].error == f"{ARTIFACT_CHECK_ERROR} (base_regression)"
    example = authority.outcome.verdict.counterexamples[-1]
    assert (example.check_id, example.role) == ("base_regression", "preservation")
    assert example.output_tail == "tests.test_calc::test_pins_subtraction"
    events = await store.replay(BOUNDARY_AGGREGATE_TYPE, "exec_regression/check_package/v1")
    assert events[-1].type == ACCEPTANCE_RECONCILED
    assert events[-1].data["criteria"][0]["artifact_check"] == "base_regression"


async def test_a_decided_run_reports_what_the_artifact_checks_observed_once(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ouroboros import telemetry
    from ouroboros.boundary.run_control import CheckPackageRun

    captured: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        telemetry, "capture", lambda event, properties=None: captured.append((event, properties))
    )
    seed, authority = await _calc_authority(store, tmp_path, monkeypatch)
    _result, parallel = _succeeded()
    await authority(seed=seed, execution_id="exec_regression", parallel_result=parallel)
    run = CheckPackageRun(authority.settings, state=authority.state, authority=authority)
    run.attempted, run.runtime_backend = True, "codex"

    run.finish("failed", surface="cli_run")
    run.finish("failed", surface="cli_run")

    rows = [props for event, props in captured if event == "acceptance_artifact_checks"]
    assert rows == [
        {
            "base_regression": "rejected",
            "worker_tests": "no_selected_files",
            "failed_criteria": 1,
            "criterion_count": 1,
            "repairs": 0,
            "surface": "cli_run",
            "runtime_backend": "codex",
        }
    ]


async def test_an_added_test_file_that_cannot_be_collected_is_no_test_result(
    tmp_path: Path,
) -> None:
    base = _tree(tmp_path / "base", BASE_TREE)
    candidate = _tree(tmp_path / "work", BASE_TREE)
    (candidate / "calc/tests/test_added.py").write_text(
        "import no_such_module\ndef test_new():\n    pass\n"
    )

    _regression, worker = await _checks(base).findings(candidate)

    assert worker.outcome is Outcome.NOT_A_TEST_RESULT and worker.failed == ()


async def test_a_check_that_breaks_leaves_the_package_decision_intact(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("runner exploded")

    seed, authority = await _calc_authority(store, tmp_path, monkeypatch)
    monkeypatch.setattr(br, "_pytest", broken)
    _result, parallel = _succeeded()

    decided = await authority(seed=seed, execution_id="exec_regression", parallel_result=parallel)

    assert authority.outcome is not None and authority.outcome.error is None
    assert {finding.outcome for finding in authority.artifact_findings} == {Outcome.UNAVAILABLE}
    (decision,) = authority.outcome.reconciliation.decisions
    assert decision.artifact_check is None and decision.reason == "script_check_advisory"
    assert decided.all_succeeded
