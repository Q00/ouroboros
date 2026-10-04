"""Controller-run checks of the whole artifact: the base commit's tests, and the worker's.

Two checks run on the frozen candidate, chosen and executed by the controller
alone. Neither calls a model, reads criterion or claim text, or consults the
worker, and neither reads anything of the official grader (its test patch or
its lists of tests): every input is the base snapshot, the candidate tree and
the bytes of their files.

- **Base regression** (``ArtifactCheck.BASE_REGRESSION``). Behaviour the
  criteria do not ask to change must be the same on the base and on the
  candidate. The controller selects the base tree's test files that pair with
  a changed module (``test_<stem>.py``, ``<stem>_test.py``, ``test_<stem>s.py``,
  or a file under a test directory named ``<stem>``) or import it (its import
  statements, resolved structurally, name the module's dotted path, or a
  string in it names that path, as a patch target does). It runs them on two
  fresh copies of the base snapshot and on one copy of the candidate in which
  those files, and every ``conftest.py`` above them, are restored to their
  base bytes, so an edit the worker made to them is undone before the run.
  The worker controls no pytest configuration of that run either: every
  other ``conftest.py`` that differs from the base is deleted, the root
  ``pytest.ini``, ``pyproject.toml``, ``tox.ini`` and ``setup.cfg`` are
  restored to base bytes (or deleted when the base has none), and pytest runs
  with ``-c`` naming the base's configuration file (or an empty one the
  controller writes outside the tree), ``--rootdir`` the copy and
  ``-o addopts=``. A test that passed on both base runs (a flaky test passes
  on at most one), fails or errors on the candidate, and fails again when it
  alone is rerun there, or vanishes behind a collection error of its module,
  regressed; a candidate on which the runner dies before it writes a report
  fails every such test. The base result is kept per (base tree digest,
  selected files), so repeated attempts never rerun the base (a timed out or
  refused base run is retried once). A regressed test is exempt when its
  footprint (the changed functions its failing run entered,
  ``boundary/footprint.py``) is not empty and lies inside what the admitted
  oracle checks that passed on the candidate entered
  (``regressions_to_keep``): the oracle adjudicates that behaviour, and the
  old test pins what the criterion changed.
- **Worker tests** (``ArtifactCheck.WORKER_TESTS``). Every test file the
  candidate adds is run alone, in path order, on its own copy of the candidate, under the worker's own configuration. A run that exits 1
  with a report naming a failing test fails; exit 0 passes and proves
  nothing; any other exit (2 usage or interrupted, 5 nothing collected) is
  not a test result.

Both only fail, never accept: an executed failure fails every criterion the
admitted package left ``unverified`` or ``uncovered`` (an artifact-level
finding: no criterion is matched to a test by its text), through the existing
``fail`` route (``acceptance.CriterionVerdict.artifact_check``), and while the
worker runs the gate hands the failing test names to bounded repair. A
verified pass keeps the package's authority. With no admitted package every
criterion is uncovered, so an executed failure fails them all (no gate runs
then, so there is no repair turn). Anything else is no observation
and decides nothing, with its reason (``ArtifactCheckOutcome``): a timeout,
a base on which the runner wrote no report, no selected file, a project whose
own runner is not pytest, a run the sandbox could not confine, or a changed
module the run imported from outside its copy (an editable install of the
live workspace, for example: the run would not test the copy's code).

Any project, not only one pytest drives. Besides the Python test files
above, a change selects the test files paired with its other sources by path
(``target_commands.select_other_tests``: ``foo_test.go``, ``x.test.ts``,
``FooTest.java``, ``tests/foo.rs``). Each selected file is a target run by a
test command taken, as data, from the Seed's ``verify_command``, the
constructor's declared ``test_command``, the worker's own test invocations or
a built-in default, and used only once admitted on the base: it exits 0 on
two base copies and fails when the target is replaced with unparseable bytes
(``target_commands.judge_admission``). Its exit status decides per target
(the floor: 0 on both base runs, nonzero on the candidate and again on a
rerun), or its JUnit report per test when it writes one (``{report}``). A
Python target without an admitted declared command takes the product's own
pytest run, the JUnit tier's pytest instance (per-test results, the
configuration hardening below, and the footprint exemption, which is Python
only). A target nothing admitted observes is no observation. The pytest run
is started by the controller's bootstrap (``footprint.PYTEST_BOOTSTRAP``,
which sets ``sys.path`` as ``python -m pytest`` does). Every run uses the
run's pinned interpreter, on a throwaway copy, through the check execution
entry point (``check_env.check_command``): confined by the execution
sandbox, without network, under the run contract's per-check timeout. Its report and its record go to the run's scratch
directory, outside the copy. Per-test outcomes come from the runner's JUnit
XML report, never from its console text. A report is written by code the
candidate controls, so a forged report can only hide a failure; it can never
fail a correct artifact that the base's own tests do not. Every tree walk,
copy and diff runs off the event loop.
"""

from __future__ import annotations

import ast
import asyncio
from collections.abc import Collection, Iterable, Mapping, Sequence
from configparser import ConfigParser
from configparser import Error as ConfigError
from dataclasses import dataclass, field, replace
from enum import StrEnum
import os
from pathlib import Path, PurePosixPath
import posixpath
import secrets
import shutil
import stat
import tempfile
import tomllib
from typing import Literal
from xml.etree import ElementTree

from ouroboros import telemetry as usage_telemetry
from ouroboros.boundary.acceptance import ArtifactCheck, CriterionVerdict, PackageCriterionStatus
from ouroboros.boundary.admission import _run_in_environment
from ouroboros.boundary.binding import CheckTier
from ouroboros.boundary.check_env import (
    CheckInterpreter,
    CheckUnavailable,
    check_command,
    check_scratch,
)
from ouroboros.boundary.footprint import (
    PYTEST_BOOTSTRAP,
    PYTEST_PLAN,
    ChangedCode,
    FunctionKey,
    RunRecord,
    changed_code,
    pytest_plan,
    read_run_record,
    read_source,
    write_plan,
)
from ouroboros.boundary.target_commands import (
    CANARY,
    Admission,
    AdmissionCache,
    CommandRun,
    CommandSource,
    TargetCommand,
    Tier,
    changed_other_sources,
    command_set,
    default_commands,
    is_paired_test,
    judge_admission,
    select_other_tests,
)
from ouroboros.boundary.tree import (
    UNREADABLE,
    added_paths,
    changed_paths,
    copy_checkout,
    manifest_digest,
    tree_manifest,
)
from ouroboros.orchestrator.evidence.call_citation import is_test_path


class ArtifactCheckMode(StrEnum):
    """How an artifact check acts (``boundary.base_regression``, ``boundary.worker_test_gate``)."""

    DECIDE = "decide"
    """Run it and let an executed failure decide (the regression check: only
    with an admitted package; see ``decides``)."""
    RECORD = "record"
    """Run it and record what it would have decided; it decides nothing and
    sends no repair turn."""
    OFF = "off"
    """Do not run it."""


BASE_REGRESSION_DEFAULT: Literal["decide", "record", "off"] = "decide"
"""The base regression check's mode when nothing configures it. The one place it lives."""
WORKER_TEST_GATE_DEFAULT: Literal["decide", "record", "off"] = "record"
"""The worker-test gate's mode when nothing configures it. The one place it lives."""

REGRESSION_REASON = "base_regression"
"""``CriterionVerdict.reason`` of a criterion the base regression failed."""
WORKER_TESTS_REASON = "worker_tests_failed"
"""``CriterionVerdict.reason`` of a criterion a failing worker test failed."""

_TEST_DIRECTORIES = frozenset({"tests", "test", "testing"})
_PROJECT_RUNNERS = frozenset({"tests/runtests.py", "bin/test"})
"""Base-tree test runners this check does not drive (Django's and SymPy's own)."""
_PASS, _FAIL, _ERROR, _SKIP = "pass", "fail", "error", "skip"
_REPORT_LIMIT = 32 * 1024 * 1024
_REPAIR_NAMES = 10


MASS_BREAKAGE = 20
"""More regressed tests than this are never exempt: the change broke too much to adjudicate."""
_BASE_ATTEMPTS = 2
_ROOT_CONFIGS = ("pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg")


class ArtifactCheckOutcome(StrEnum):
    """What one artifact check observed on a run (closed set; telemetry reports it)."""

    REJECTED = "rejected"
    """An executed failure: the check failed the undecided criteria."""
    EXEMPTED = "exempted"
    """Every regression was exempt (``regressions_to_keep``): the finding is void."""
    PASSED = "passed"
    """The check ran and found nothing; it decides nothing."""
    TIMEOUT = "timeout"
    BASE_RUNNER_CRASH = "base_runner_crash"
    """The runner wrote no report on the base: nothing to compare against."""
    NO_SELECTED_FILES = "no_selected_files"
    """No base test file pairs with or imports a changed module, or no test file was added."""
    UNSUPPORTED_RUNNER = "unsupported_runner"
    """No command can run the selected targets (none declared, none built in)."""
    NOT_A_TEST_RESULT = "not_a_test_result"
    """A worker test file whose run exited with neither 0 nor 1."""
    UNAVAILABLE = "unavailable"
    """Nothing ran: the sandbox, the pinned interpreter or the base snapshot refused it."""
    IMPORTED_OUTSIDE_COPY = "imported_outside_copy"
    """A changed module was imported from outside the run's copy: the run did not test it."""
    NOT_RUN = "not_run"
    """The check's mode is ``off``."""
    UNCONFIRMED = "unconfirmed"
    """A failure whose rerun gave no result for it (neither a pass nor a second failure)."""
    NO_ADMITTED_COMMAND = "no_admitted_command"
    """Commands exist for the selected targets, but none passed admission on the base
    (``target_commands.judge_admission``): nothing observed the targets."""


class ArtifactEffect(StrEnum):
    """What a check's finding did to the run (closed set; telemetry reports it)."""

    DECIDED = "decided"
    """It failed the criteria the package left undecided."""
    RECORDED = "recorded"
    """``record`` mode: it would have decided, and only that was recorded."""
    UNADJUDICATED = "regression_unadjudicated"
    """``decide`` mode, no admitted package: a regression nothing adjudicates. The
    worker had one repair turn; the criteria stay unverified."""
    NONE = "none"
    """Nothing to decide: it found nothing, observed nothing, or did not run."""


def decides(finding: ArtifactFinding, mode: ArtifactCheckMode, *, admitted: bool) -> bool:
    """Whether ``finding`` fails criteria: ``decide`` mode, and a regression only with a package.

    About one task in five legitimately changes behaviour an existing test
    pins; only an admitted oracle can adjudicate that, so without a package a
    regression never fails anything.
    """
    return (
        finding.rejects
        and mode is ArtifactCheckMode.DECIDE
        and (admitted or finding.check is ArtifactCheck.WORKER_TESTS)
    )


def effect(finding: ArtifactFinding, mode: ArtifactCheckMode, *, admitted: bool) -> ArtifactEffect:
    """What ``finding`` did to the run under ``mode`` (``decides`` is the deciding rule)."""
    if not finding.rejects:
        return ArtifactEffect.NONE
    if decides(finding, mode, admitted=admitted):
        return ArtifactEffect.DECIDED
    if mode is ArtifactCheckMode.DECIDE:
        return ArtifactEffect.UNADJUDICATED
    return ArtifactEffect.RECORDED


class Exemption(StrEnum):
    """How the footprint exemption treated a base regression (closed set; telemetry reports it)."""

    APPLIED = "applied"
    """At least one regressed test was exempt."""
    NONE_INSIDE = "none_inside"
    """A passing oracle exists, but no regressed test's footprint lies inside its own."""
    NO_PASSING_ORACLE = "no_passing_oracle"
    """No admitted oracle passed on this candidate (with no package, always)."""
    MASS_BREAKAGE = "mass_breakage"
    """More than ``MASS_BREAKAGE`` tests regressed."""
    NO_CHANGED_FUNCTION = "no_changed_function"
    """The change touched no function (``C`` is empty): there is no footprint to compare."""
    NO_ORACLE_FOOTPRINT = "no_oracle_footprint"
    """The passing oracles entered no changed function (only a CLI oracle passed, say)."""
    CHANGE_OUTSIDE_FUNCTIONS = "change_outside_functions"
    """The change also touched code outside every function (module or class level, a
    deleted function, a removed import), which no function footprint covers."""


@dataclass(frozen=True, slots=True)
class ArtifactFinding:
    """One check's observation on one candidate tree."""

    check: ArtifactCheck
    outcome: ArtifactCheckOutcome
    failed: tuple[str, ...] = ()
    """The regressed test ids that count, or the worker test files that failed."""
    selected: tuple[str, ...] = ()
    footprints: Mapping[str, frozenset[FunctionKey]] = field(default_factory=dict)
    """Per regressed test, the changed functions its failing run entered; a test
    without an entry produced no footprint."""
    exempted: tuple[str, ...] = ()
    """Regressed tests the footprint exemption set aside."""
    exemption: Exemption | None = None
    changed: ChangedCode = field(default_factory=ChangedCode)
    """What the change touched (``C``), which the exemption needs."""
    candidate_digest: str | None = None
    """The tree digest of the candidate the check observed (the journal record cites it)."""

    @property
    def rejects(self) -> bool:
        return self.outcome is ArtifactCheckOutcome.REJECTED

    @property
    def reason(self) -> str:
        """The ``CriterionVerdict.reason`` of a criterion this check fails."""
        return _reason(self.check)


def _reason(check: ArtifactCheck) -> str:
    return REGRESSION_REASON if check is ArtifactCheck.BASE_REGRESSION else WORKER_TESTS_REASON


# --------------------------------------------------------------------------
# Selection: the base tree and the changed paths only.


def base_test_files(paths: Iterable[str]) -> tuple[str, ...]:
    """Python test files of a tree: named like a test, or ``tests.py`` under a test path."""
    out = []
    for path in paths:
        name = posixpath.basename(path)
        if not path.endswith(".py") or not is_test_path(path):
            continue
        if name.startswith("test") or name.endswith("_test.py"):
            out.append(path)
    return tuple(sorted(out))


def changed_sources(changed: Iterable[str]) -> tuple[str, ...]:
    """Changed or deleted base Python files that are not tests (an added file pairs with nothing)."""
    return tuple(sorted(p for p in changed if p.endswith(".py") and not is_test_path(p)))


def dotted_module(path: str) -> str:
    """The dotted module of a source path; a leading ``src/`` or ``lib/`` is a layout root."""
    module = path[:-3] if path.endswith(".py") else path
    for root in ("src/", "lib/"):
        if module.startswith(root):
            module = module[len(root) :]
    module = module.replace("/", ".")
    return module[: -len(".__init__")] if module.endswith(".__init__") else module


def paired_tests(tests: Sequence[str], source: str) -> tuple[str, ...]:
    """Test files named after ``source``, or under a test directory named after it."""
    stem = posixpath.splitext(posixpath.basename(source))[0]
    if stem == "__init__":
        return ()
    names = {f"test_{stem}.py", f"{stem}_test.py", f"test_{stem}s.py"}
    out = []
    for path in tests:
        directories = path.split("/")[:-1]
        roots = [index for index, part in enumerate(directories) if part in _TEST_DIRECTORIES]
        under_root = [part for index, part in enumerate(directories) if roots and index > roots[0]]
        if posixpath.basename(path) in names or stem in under_root:
            out.append(path)
    return tuple(sorted(out))


def imported_names(text: str, path: str) -> frozenset[str]:
    """Every dotted name a file's import statements or string constants name.

    ``from . import x`` resolves against the file's own package. A file that
    does not parse names nothing.
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return frozenset()
    package = dotted_module(path).split(".")[:-1]
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            parts = node.module.split(".") if node.module else []
            if node.level:
                if node.level - 1 > len(package):
                    continue
                parts = package[: len(package) - (node.level - 1)] + parts
            base = ".".join(parts)
            names.add(base)
            names.update(f"{base}.{alias.name}" if base else alias.name for alias in node.names)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            names.add(node.value)
    return frozenset(names)


def imports_module(names: Collection[str], source: str) -> bool:
    """Whether ``names`` reach ``source``: a package by its own name, a module or anything in it."""
    dotted = dotted_module(source)
    if posixpath.basename(source) == "__init__.py":
        return dotted in names
    return any(name == dotted or name.startswith(dotted + ".") for name in names)


def select_tests(
    base_paths: Iterable[str],
    changed: Iterable[str],
    read_base_text: Mapping[str, str] | None = None,
    *,
    base_root: Path | None = None,
) -> tuple[str, ...]:
    """The base test files this diff selects: paired with, or importing, a changed module.

    The text of a test file comes from ``read_base_text`` or, for a real
    snapshot, from ``base_root``; a file that cannot be read imports nothing.
    """
    tests = base_test_files(base_paths)
    sources = changed_sources(changed)
    selected: set[str] = set()
    names: dict[str, frozenset[str]] = {}
    for source in sources:
        selected.update(paired_tests(tests, source))
        for path in tests:
            if path not in names:
                names[path] = imported_names(_text(path, read_base_text, base_root), path)
            if imports_module(names[path], source):
                selected.add(path)
    return tuple(sorted(selected))


def _text(path: str, given: Mapping[str, str] | None, root: Path | None) -> str:
    if given is not None:
        return given.get(path, "")
    if root is None:
        return ""
    try:
        return (root / path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def worker_test_files(added: Iterable[str]) -> tuple[str, ...]:
    """Test files the candidate adds (never a ``conftest.py`` or ``__init__.py``)."""
    return base_test_files(added)


def regressions_to_keep(
    regressed: Sequence[str],
    footprints: Mapping[str, frozenset[FunctionKey]],
    oracle_entered: frozenset[FunctionKey] | None,
    changed: ChangedCode,
) -> tuple[tuple[str, ...], Exemption]:
    """The regressed tests that count against the candidate, and how the exemption went.

    A regressed test T is exempt when it produced a footprint, the changed
    functions its failing run entered (T_C) are not none, and every one of
    them was also entered by an admitted oracle check that passed on this
    candidate (``oracle_entered``, O_C): the oracle then adjudicates the
    behaviour T reaches, and T pins what the criterion changed. A test with
    no footprint, or one that failed before it reached changed code, is kept:
    only a missing or forged footprint could claim that. Nothing is exempt
    without a passing oracle (``oracle_entered`` ``None``, always so with no
    package), when more than ``MASS_BREAKAGE`` tests regressed, when the
    change touched no function, when it also changed code outside every
    function (no function footprint covers it), or when the passing oracles
    entered no changed function.
    """
    every = tuple(regressed)
    if len(regressed) > MASS_BREAKAGE:
        return every, Exemption.MASS_BREAKAGE
    if not changed.functions:
        return every, Exemption.NO_CHANGED_FUNCTION
    if changed.outside_functions:
        return every, Exemption.CHANGE_OUTSIDE_FUNCTIONS
    if oracle_entered is None:
        return every, Exemption.NO_PASSING_ORACLE
    if not oracle_entered:
        return every, Exemption.NO_ORACLE_FOOTPRINT
    kept = tuple(
        test
        for test in regressed
        if not footprints.get(test) or not footprints[test] <= oracle_entered
    )
    return kept, Exemption.APPLIED if len(kept) < len(regressed) else Exemption.NONE_INSIDE


def exempt(
    finding: ArtifactFinding, oracle_entered: frozenset[FunctionKey] | None
) -> ArtifactFinding:
    """``finding`` with the footprint exemption applied (a worker-test finding is unchanged).

    When every regression is exempt the finding is void (``EXEMPTED``) and
    the package's decision stands.
    """
    if finding.check is not ArtifactCheck.BASE_REGRESSION or not finding.rejects:
        return finding
    kept, how = regressions_to_keep(
        finding.failed, finding.footprints, oracle_entered, finding.changed
    )
    exempted = tuple(test for test in finding.failed if test not in kept)
    outcome = ArtifactCheckOutcome.REJECTED if kept else ArtifactCheckOutcome.EXEMPTED
    return replace(finding, outcome=outcome, failed=kept, exempted=exempted, exemption=how)


# --------------------------------------------------------------------------
# Runs and their reports.


@dataclass(frozen=True, slots=True)
class _Run:
    statuses: dict[str, str] | None
    """Per-test status from the report; ``None`` when the runner wrote no usable report."""
    return_code: int | None
    timed_out: bool = False
    unavailable: bool = False
    record: RunRecord = field(default_factory=RunRecord)


def parse_junit(data: bytes) -> dict[str, str] | None:
    """``{test id: status}`` from a JUnit XML report, or ``None`` when it is not one.

    A test id is ``<classname>::<name>``; a collection error is the module's
    dotted name with an empty class name and the status ``error``.
    """
    try:
        root = ElementTree.fromstring(data)
    except (ElementTree.ParseError, ValueError):
        return None
    if root.tag not in ("testsuites", "testsuite"):
        return None
    statuses: dict[str, str] = {}
    for case in root.iter("testcase"):
        name = case.get("name") or ""
        classname = case.get("classname") or ""
        status = _PASS
        for child in case:
            if child.tag == "failure":
                status = _FAIL
            elif child.tag == "error":
                status = _ERROR
            elif child.tag == "skipped" and status == _PASS:
                status = _SKIP
        statuses[f"{classname}::{name}" if classname else name] = status
    return statuses


async def _pytest(
    root: Path,
    files: Sequence[str],
    interpreter: CheckInterpreter,
    timeout: int,
    *,
    past_collection_errors: bool = False,
    watched: frozenset[FunctionKey] | None = None,
    modules: Iterable[str] = (),
    config: Path | None = None,
) -> _Run:
    """Run ``files`` with pytest in ``root`` (a throwaway copy) and read its report and record.

    ``past_collection_errors`` runs the other files when one fails to collect
    (the regression runs, where that error is a finding); pytest then exits 1
    for the collection error alone, so a single worker test file never uses it
    and a file that cannot be collected keeps pytest's own exit 2. ``config``
    is the only configuration file pytest reads (``-c``, with ``--rootdir`` the
    copy and ``addopts`` cleared); without it the copy's own configuration
    applies (the worker-test gate). The bootstrap's plan names ``watched``
    (the changed functions, whose per-test footprint it records) and
    ``modules`` (whose import location it records). Report, plan and record
    live in the run's scratch directory, outside the copy; like the report,
    the record is written by code the candidate controls, so it can only ever
    exempt.
    """
    # Resolved: pytest names tests relative to its root, and an unresolved
    # root (a temp directory behind a link) would name them from elsewhere.
    root = root.resolve()
    controlled = (
        ("-o", "addopts=", f"--rootdir={root}", "-c", str(config.resolve()))
        if config is not None
        else ()
    )
    with check_scratch(root.parent) as scratch:
        token = secrets.token_hex(8)
        report = scratch / f"report-{token}.xml"
        record = scratch / f"record-{token}.jsonl"
        write_plan(scratch / PYTEST_PLAN, pytest_plan(root, record, watched, modules))
        argv = (
            "python",
            "-c",
            PYTEST_BOOTSTRAP,
            "-p",
            "no:cacheprovider",
            "-q",
            *controlled,
            *(("--continue-on-collection-errors",) if past_collection_errors else ()),
            f"--junitxml={report}",
            "--",
            *files,
        )
        command = check_command(
            argv, cwd=root, writable_root=root, interpreter=interpreter, scratch=scratch
        )
        if isinstance(command, CheckUnavailable):
            return _Run(None, None, unavailable=True)
        completed = await _run_in_environment(command, timeout)
        if completed.unavailable is not None or completed.launch_error is not None:
            return _Run(None, None, unavailable=True)
        if completed.timed_out:
            return _Run(None, completed.return_code, timed_out=True)
        if completed.output_overflow:
            # Killed for flooding its output: whatever it would have reported is unknown.
            return _Run(None, None, unavailable=True)
        statuses = None
        try:
            status = os.lstat(report)
            if stat.S_ISREG(status.st_mode) and status.st_size <= _REPORT_LIMIT:
                statuses = parse_junit(report.read_bytes())
        except OSError:
            statuses = None
        return _Run(statuses, completed.return_code, record=read_run_record(record, watched))


def controller_config(root: Path, work: Path) -> Path:
    """The one pytest configuration file of a controller run in ``root``.

    The root file pytest itself would pick (``pytest.ini``, then a
    ``pyproject.toml`` with ``[tool.pytest.ini_options]``, a ``tox.ini`` with
    ``[pytest]``, a ``setup.cfg`` with ``[tool:pytest]``), read safely;
    without one, an empty ``pytest.ini`` written in ``work``, outside the tree.
    """
    for name in _ROOT_CONFIGS:
        text = read_source(root / name)
        if text is None:
            continue
        if name == "pytest.ini":
            return root / name
        if name == "pyproject.toml":
            try:
                tool = tomllib.loads(text).get("tool", {})
            except tomllib.TOMLDecodeError:
                continue
            if isinstance(tool, dict) and isinstance(tool.get("pytest"), dict):
                if "ini_options" in tool["pytest"]:
                    return root / name
            continue
        parser = ConfigParser(interpolation=None)
        try:
            parser.read_string(text)
        except ConfigError:
            continue
        if parser.has_section("pytest" if name == "tox.ini" else "tool:pytest"):
            return root / name
    empty = work / f"pytest-{secrets.token_hex(4)}.ini"
    empty.write_text("[pytest]\n", encoding="utf-8")
    return empty


def neutralize_config(
    copy_root: Path,
    base: Path,
    base_manifest: Mapping[str, str],
    candidate_manifest: Mapping[str, str],
) -> bool:
    """Take every pytest configuration of the worker's out of a candidate copy.

    Every ``conftest.py`` that differs from the base (or the base lacks) is
    deleted, and each root configuration file is restored to its base bytes
    or deleted when the base has none. ``False`` when a path cannot be
    handled without writing through a link.
    """
    for path, digest in candidate_manifest.items():
        if posixpath.basename(path) == "conftest.py" and base_manifest.get(path) != digest:
            if not _remove(copy_root, path):
                return False
    for name in _ROOT_CONFIGS:
        if base_manifest.get(name) == candidate_manifest.get(name):
            continue
        if name in base_manifest:
            if not restore_base_bytes(copy_root, base, (name,)):
                return False
        elif not _remove(copy_root, name):
            return False
    return True


def _remove(copy_root: Path, relative: str) -> bool:
    directory = _real_directory(copy_root, posixpath.dirname(relative))
    if directory is None:
        return False
    target = directory / posixpath.basename(relative)
    if os.path.lexists(target):
        if stat.S_ISDIR(os.lstat(target).st_mode):
            return False
        os.unlink(target)
    return True


def _stable_passes(runs: Sequence[_Run]) -> tuple[str, ...]:
    """Tests that passed on every base run."""
    first = runs[0].statuses or {}
    return tuple(
        sorted(
            t
            for t, s in first.items()
            if s == _PASS and all((r.statuses or {}).get(t) == _PASS for r in runs)
        )
    )


def regressions(stable: Sequence[str], candidate: Mapping[str, str] | None) -> tuple[str, ...]:
    """Stable base passes that fail, error, or vanish behind a collection error on the candidate.

    ``candidate`` ``None`` is a runner that died before writing its report: it
    fails every stable test.
    """
    if candidate is None:
        return tuple(sorted(stable))
    broken = [test for test, status in candidate.items() if status == _ERROR and "::" not in test]
    out = []
    for test in stable:
        status = candidate.get(test)
        if status in (_FAIL, _ERROR):
            out.append(test)
        elif status is None:
            classname = test.partition("::")[0]
            if any(classname == module or classname.startswith(module + ".") for module in broken):
                out.append(test)
    return tuple(sorted(out))


def _real_directory(root: Path, relative: str) -> Path | None:
    """``root/relative`` with every component a real directory, created when missing.

    ``None`` when a component is a link or a file: nothing is written through it.
    """
    current = root
    for part in PurePosixPath(relative).parts:
        current = current / part
        if not os.path.lexists(current):
            current.mkdir()
        elif not stat.S_ISDIR(os.lstat(current).st_mode):
            return None
    return current


def restore_base_bytes(copy_root: Path, base: Path, paths: Iterable[str]) -> bool:
    """Write each base file over the candidate copy (an edit is undone, a deletion comes back).

    Writes only through real directories inside the copy; ``False`` when a
    path the controller must write through is a link or not a directory.
    """
    for relative in paths:
        directory = _real_directory(copy_root, posixpath.dirname(relative))
        if directory is None:
            return False
        target = directory / posixpath.basename(relative)
        if os.path.lexists(target):
            if stat.S_ISDIR(os.lstat(target).st_mode):
                return False
            os.unlink(target)
        shutil.copyfile(base / relative, target, follow_symlinks=False)
    return True


def _conftests_above(paths: Iterable[str], base_paths: Collection[str]) -> tuple[str, ...]:
    """Base ``conftest.py`` files in the directories that hold ``paths``, up to the root."""
    out: set[str] = set()
    for path in paths:
        directory = PurePosixPath(path).parent
        while True:
            conftest = (directory / "conftest.py").as_posix()
            if conftest.startswith("./"):
                conftest = conftest[2:]
            if conftest in base_paths:
                out.add(conftest)
            if directory == directory.parent:
                break
            directory = directory.parent
    return tuple(sorted(out))


@dataclass(frozen=True, slots=True)
class _Base:
    """The base side of a selection: its stable passes, or why there is none."""

    stable: tuple[str, ...] = ()
    outcome: ArtifactCheckOutcome | None = None


class ArtifactChecks:
    """Both checks for one run, with the base snapshot pinned by the admission's digest.

    ``findings`` is cached per candidate tree digest; the base runs are cached
    per (base tree digest, selected files). Calls are serialized, so parallel
    attempts on the same tree share one result.
    """

    def __init__(
        self,
        *,
        base: Path | None,
        base_digest: str | None,
        interpreter: CheckInterpreter,
        timeout_seconds: int,
        run_regression: bool = True,
        run_worker_tests: bool = True,
        seed_commands: Sequence[str] = (),
        constructor_command: str | None = None,
    ) -> None:
        self._run_regression = run_regression
        self._run_worker_tests = run_worker_tests
        self._seed_commands = tuple(seed_commands)
        self._constructor_command = constructor_command
        self._admissions = AdmissionCache()
        self._base = base
        self._base_digest = base_digest
        self._interpreter = interpreter
        self._timeout = timeout_seconds
        self._base_manifest: dict[str, str] | None = None
        self._base_runs: dict[tuple[str, tuple[str, ...]], _Base] = {}
        self._base_attempts: dict[tuple[str, tuple[str, ...]], int] = {}
        self._findings: dict[
            tuple[str, tuple[str, ...]], tuple[ArtifactFinding, ArtifactFinding]
        ] = {}
        self._changed: dict[str, ChangedCode] = {}
        self._lock = asyncio.Lock()

    async def changed_functions(self, candidate: Path) -> frozenset[FunctionKey] | None:
        """``C`` for ``candidate`` as it is now (``footprint.changed_code``), or ``None``.

        ``None`` when the base cannot be used, or when no function footprint
        can be compared (no changed function, or a change outside every
        function); the caller then records no oracle footprint.
        """
        async with self._lock:
            try:
                base = await asyncio.to_thread(self._pinned_base)
                if base is None:
                    return None
                candidate = candidate.resolve()
                manifest = await asyncio.to_thread(tree_manifest, candidate)
                changed = await self._changed_code(base, candidate, manifest)
            except Exception:  # noqa: BLE001 - an optional observation never fails the decision
                return None
        if not changed.functions or changed.outside_functions:
            return None
        return changed.functions

    async def _changed_code(
        self, base: Path, candidate: Path, manifest: Mapping[str, str]
    ) -> ChangedCode:
        """What the change did to the non-test Python files it touched or added.

        Only regular files count: a symbolic link or an unreadable entry on
        either side is a change outside every function.
        """
        digest = manifest_digest(manifest)
        if digest not in self._changed:
            assert self._base_manifest is not None
            base_manifest = self._base_manifest
            touched = [
                path
                for path in (
                    *changed_paths(base_manifest, manifest),
                    *added_paths(base_manifest, manifest),
                )
                if path.endswith(".py") and not is_test_path(path)
            ]
            others = changed_other_sources(
                (*changed_paths(base_manifest, manifest), *added_paths(base_manifest, manifest))
            )
            if others or any(
                not _regular(manifest.get(path)) or not _regular(base_manifest.get(path, ""))
                for path in touched
                if path in manifest
            ):
                # A changed source no Python footprint covers (another
                # language, a link, an unreadable file) is outside every function.
                self._changed[digest] = ChangedCode(outside_functions=True)
            else:
                self._changed[digest] = await asyncio.to_thread(
                    changed_code,
                    base,
                    candidate,
                    [path for path in touched if path in manifest and path in base_manifest],
                    [path for path in touched if path not in base_manifest],
                )
        return self._changed[digest]

    async def findings(
        self, candidate: Path, transcript: Sequence[str] = ()
    ) -> tuple[ArtifactFinding, ArtifactFinding]:
        """The base regression and worker-test findings for ``candidate`` as it is now.

        ``transcript`` holds the worker's recorded test invocations
        (``target_commands.transcript_commands``), one source of commands.
        """
        async with self._lock:
            try:
                return await self._findings_for(candidate.resolve(), tuple(transcript))
            except Exception:  # noqa: BLE001 - an optional check never fails the decision
                return _unavailable()

    async def _findings_for(
        self, candidate: Path, transcript: tuple[str, ...]
    ) -> tuple[ArtifactFinding, ArtifactFinding]:
        base = await asyncio.to_thread(self._pinned_base)
        if base is None:
            return _unavailable()
        manifest = await asyncio.to_thread(tree_manifest, candidate)
        digest = manifest_digest(manifest)
        cached = self._findings.get((digest, transcript))
        if cached is not None:
            return cached
        assert self._base_manifest is not None
        if UNREADABLE in manifest.values():
            return _unavailable()
        changed = await self._changed_code(base, candidate, manifest)
        not_run = ArtifactCheckOutcome.NOT_RUN
        project_runner = bool(_PROJECT_RUNNERS & set(self._base_manifest))
        if not self._run_worker_tests:
            worker = ArtifactFinding(ArtifactCheck.WORKER_TESTS, not_run)
        elif project_runner:
            # The worker-test gate runs added files with pytest only.
            worker = ArtifactFinding(
                ArtifactCheck.WORKER_TESTS, ArtifactCheckOutcome.UNSUPPORTED_RUNNER
            )
        else:
            worker = await self._worker_tests(candidate, added_paths(self._base_manifest, manifest))
        found = (
            replace(
                await self._regression(base, candidate, manifest, changed, transcript)
                if self._run_regression
                else ArtifactFinding(ArtifactCheck.BASE_REGRESSION, not_run),
                candidate_digest=digest,
            ),
            replace(worker, candidate_digest=digest),
        )
        if await asyncio.to_thread(tree_manifest, candidate) == manifest:
            # Kept only for the tree it observed: a workspace that changed
            # while the checks ran is checked again on its next call.
            self._findings[(digest, transcript)] = found
        return found

    def _pinned_base(self) -> Path | None:
        """The base snapshot while it still has the admission's tree digest."""
        if self._base is None or self._base_digest is None:
            return None
        if self._base_manifest is None:
            manifest = tree_manifest(self._base)
            if manifest_digest(manifest) != self._base_digest or UNREADABLE in manifest.values():
                return None
            self._base_manifest = manifest
        return self._base

    async def _regression(
        self,
        base: Path,
        candidate: Path,
        manifest: Mapping[str, str],
        changed: ChangedCode,
        transcript: tuple[str, ...] = (),
    ) -> ArtifactFinding:
        """Every selected target, through the product's pytest run or an admitted command.

        A Python target takes an admitted declared command first (Seed,
        constructor, transcript), then the product's pytest run (unless the
        project has its own runner pytest cannot drive), then a built-in
        command. Any other target takes an admitted command. A target nothing
        admitted observes is no observation, never a decision.
        """
        check = ArtifactCheck.BASE_REGRESSION
        base_manifest = self._base_manifest
        assert base_manifest is not None
        paths = changed_paths(base_manifest, manifest)
        python = await asyncio.to_thread(select_tests, base_manifest, paths, base_root=base)
        others = select_other_tests(base_manifest, paths)
        selected = tuple(sorted({*python, *others}))
        if not selected:
            return ArtifactFinding(check, ArtifactCheckOutcome.NO_SELECTED_FILES)
        project_runner = bool(_PROJECT_RUNNERS & set(base_manifest))
        commands = command_set(
            seed_commands=self._seed_commands,
            constructor_command=self._constructor_command,
            transcript=transcript,
            test_files={*base_test_files(base_manifest), *filter(is_paired_test, base_manifest)},
            defaults=default_commands(
                base_manifest,
                lambda path: read_source(base / path),
                python_targets=project_runner,
            ),
        )
        admitted: dict[str, Admission] = {}
        tried: set[str] = set()
        for target in selected:
            candidates = commands.for_target(target)
            if target in python and not project_runner:
                # The product's pytest run is the default for a Python target.
                candidates = tuple(c for c in candidates if c.source is not CommandSource.DEFAULT)
            if candidates:
                tried.add(target)
            for command in candidates:
                admission = await self._admit(base, command, target)
                if admission.admitted:
                    admitted[target] = admission
                    break
        pytest_targets = () if project_runner else tuple(t for t in python if t not in admitted)
        regressed: list[str] = []
        footprints: dict[str, frozenset[FunctionKey]] = {}
        unobserved: list[ArtifactCheckOutcome] = []
        observed = False
        if pytest_targets:
            outcome, found, prints = await self._pytest_regression(
                base, candidate, manifest, changed, pytest_targets, paths
            )
            if outcome is None:
                observed = True
                regressed.extend(found)
                footprints.update(prints)
            else:
                unobserved.append(outcome)
        for target, admission in sorted(admitted.items()):
            outcome, found = await self._command_regression(
                base, candidate, manifest, selected, target, admission
            )
            if outcome is None:
                observed = True
                regressed.extend(found)
            else:
                unobserved.append(outcome)
        left = [t for t in selected if t not in admitted and t not in pytest_targets]
        if left:
            unobserved.append(
                ArtifactCheckOutcome.NO_ADMITTED_COMMAND
                if any(t in tried for t in left)
                else ArtifactCheckOutcome.UNSUPPORTED_RUNNER
            )
        if regressed:
            return ArtifactFinding(
                check,
                ArtifactCheckOutcome.REJECTED,
                tuple(sorted(regressed)),
                selected,
                footprints,
                changed=changed,
            )
        if observed or not unobserved:
            return ArtifactFinding(check, ArtifactCheckOutcome.PASSED, selected=selected)
        return ArtifactFinding(check, unobserved[0], selected=selected)

    async def _pytest_regression(
        self,
        base: Path,
        candidate: Path,
        manifest: Mapping[str, str],
        changed: ChangedCode,
        selected: tuple[str, ...],
        paths: Sequence[str],
    ) -> tuple[ArtifactCheckOutcome | None, tuple[str, ...], dict[str, frozenset[FunctionKey]]]:
        """The product's pytest run of ``selected``: ``(why unobserved, regressed, footprints)``."""
        assert self._base_manifest is not None
        modules = tuple(dotted_module(path) for path in changed_sources(paths))
        side = await self._base_side(base, selected, modules)
        if side.outcome is not None:
            return side.outcome, (), {}
        watched = changed.functions if changed.functions and not changed.outside_functions else None
        restored = (*selected, *_conftests_above(selected, self._base_manifest))
        with tempfile.TemporaryDirectory(prefix="ouroboros-regression-") as work:
            copy_root = Path(work) / "candidate"
            await asyncio.to_thread(copy_checkout, candidate, copy_root)
            prepared = await asyncio.to_thread(
                _prepare_candidate_copy, copy_root, base, self._base_manifest, manifest, restored
            )
            if not prepared:
                return ArtifactCheckOutcome.UNAVAILABLE, (), {}
            config = controller_config(copy_root, Path(work))
            run = await _pytest(
                copy_root,
                selected,
                self._interpreter,
                self._timeout,
                past_collection_errors=True,
                watched=watched,
                modules=modules,
                config=config,
            )
            failing = _unobserved(run)
            if failing is not None:
                return failing, (), {}
            if run.record.imported_outside:
                return ArtifactCheckOutcome.IMPORTED_OUTSIDE_COPY, (), {}
            failing = regressions(side.stable, run.statuses)
            if not failing:
                return None, (), {}
            # A regression is a second observed failure: each failing test runs
            # once more, alone where its node id is known, else with the whole
            # selection (the copy still holds the base bytes). A rerun that
            # times out, is refused, or gives no result for a test leaves that
            # test unobserved, never a regression.
            nodeids = {
                test: run.record.nodeids[test] for test in failing if test in run.record.nodeids
            }
            rest = tuple(test for test in failing if test not in nodeids)
            regressed: tuple[str, ...] = ()
            reasons: list[ArtifactCheckOutcome] = []
            for tests, files in (
                (tuple(nodeids), tuple(nodeids.values())),
                (rest, selected),
            ):
                if not tests:
                    continue
                await asyncio.to_thread(restore_base_bytes, copy_root, base, restored)
                rerun = await _pytest(
                    copy_root,
                    files,
                    self._interpreter,
                    self._timeout,
                    past_collection_errors=True,
                    modules=modules,
                    config=config,
                )
                reason = _unobserved(rerun)
                if reason is not None:
                    reasons.append(reason)
                    continue
                again, no_result = _confirmed(tests, rerun.statuses, rerun.return_code)
                regressed += again
                if no_result:
                    reasons.append(ArtifactCheckOutcome.UNCONFIRMED)
            if not regressed and reasons:
                return reasons[0], (), {}
        footprints = {
            test: run.record.footprints[test] for test in regressed if test in run.record.footprints
        }
        return None, regressed, footprints

    async def _admit(self, base: Path, command: TargetCommand, target: str) -> Admission:
        """Admission of ``command`` for ``target`` on the base, cached per base digest."""
        assert self._base_digest is not None
        key = (self._base_digest, command.template, target)
        cached = self._admissions.get(key)
        if cached is not None:
            return cached
        runs = []
        for canary in (False, False, True):
            with tempfile.TemporaryDirectory(prefix="ouroboros-admission-") as work:
                copy_root = Path(work) / "base"
                await asyncio.to_thread(copy_checkout, base, copy_root)
                if canary and not await asyncio.to_thread(_write_canary, copy_root, target):
                    runs.append(CommandRun(None, unavailable=True))
                    continue
                runs.append(
                    await _run_command(copy_root, command, target, self._interpreter, self._timeout)
                )
        admission = judge_admission(command, runs[:2], runs[2])
        self._admissions.put(key, admission)
        return admission

    async def _command_regression(
        self,
        base: Path,
        candidate: Path,
        manifest: Mapping[str, str],
        selected: tuple[str, ...],
        target: str,
        admission: Admission,
    ) -> tuple[ArtifactCheckOutcome | None, tuple[str, ...]]:
        """An admitted command's run of ``target`` on the candidate, rerun once when it fails.

        On the exit tier a nonzero exit regresses the target (its id is the
        target path); on the JUnit tier each stable base test that fails or
        is missing regresses.
        """
        assert self._base_manifest is not None and admission.command is not None
        restored = (*selected, *_conftests_above(selected, self._base_manifest))
        with tempfile.TemporaryDirectory(prefix="ouroboros-regression-") as work:
            copy_root = Path(work) / "candidate"
            await asyncio.to_thread(copy_checkout, candidate, copy_root)
            prepared = await asyncio.to_thread(
                _prepare_candidate_copy, copy_root, base, self._base_manifest, manifest, restored
            )
            if not prepared:
                return ArtifactCheckOutcome.UNAVAILABLE, ()
            regressed: tuple[str, ...] = ()
            for attempt in range(2):
                run = await _run_command(
                    copy_root, admission.command, target, self._interpreter, self._timeout
                )
                if not run.observed:
                    # The first run, or the rerun that must confirm it, saw
                    # nothing: the target is no observation, not a regression.
                    return (
                        ArtifactCheckOutcome.TIMEOUT
                        if run.timed_out
                        else ArtifactCheckOutcome.UNAVAILABLE
                    ), ()
                if attempt == 0:
                    if admission.tier is Tier.JUNIT:
                        regressed = tuple(
                            test
                            for test in admission.stable
                            if (run.statuses or {}).get(test) != _PASS
                        )
                    else:
                        regressed = (target,) if run.exit_code != 0 else ()
                    if not regressed:
                        break
                    await asyncio.to_thread(restore_base_bytes, copy_root, base, restored)
                elif admission.tier is Tier.JUNIT:
                    # A second observed failure of the same test, or nothing.
                    again, no_result = _confirmed(regressed, run.statuses, run.exit_code)
                    if not again and no_result:
                        return ArtifactCheckOutcome.UNCONFIRMED, ()
                    regressed = again
                else:
                    regressed = regressed if run.exit_code != 0 else ()
        return None, regressed

    async def _base_side(
        self, base: Path, selected: tuple[str, ...], modules: Sequence[str]
    ) -> _Base:
        """The base side of ``selected``, run once per base digest (a timeout or refusal twice)."""
        assert self._base_digest is not None
        key = (self._base_digest, selected)
        side = self._base_runs.get(key)
        transient = (ArtifactCheckOutcome.TIMEOUT, ArtifactCheckOutcome.UNAVAILABLE)
        if side is not None and (
            side.outcome not in transient or self._base_attempts[key] >= _BASE_ATTEMPTS
        ):
            return side
        self._base_attempts[key] = self._base_attempts.get(key, 0) + 1
        side = self._base_runs[key] = await self._run_base(base, selected, modules)
        return side

    async def _run_base(
        self, base: Path, selected: tuple[str, ...], modules: Sequence[str]
    ) -> _Base:
        """Two runs of ``selected`` on fresh base copies; a test must pass on both."""
        runs = []
        for _ in range(2):
            with tempfile.TemporaryDirectory(prefix="ouroboros-regression-") as work:
                copy_root = Path(work) / "base"
                await asyncio.to_thread(copy_checkout, base, copy_root)
                runs.append(
                    await _pytest(
                        copy_root,
                        selected,
                        self._interpreter,
                        self._timeout,
                        past_collection_errors=True,
                        modules=modules,
                        config=controller_config(copy_root, Path(work)),
                    )
                )
        if any(run.unavailable for run in runs):
            return _Base(outcome=ArtifactCheckOutcome.UNAVAILABLE)
        if any(run.timed_out for run in runs):
            return _Base(outcome=ArtifactCheckOutcome.TIMEOUT)
        if any(run.record.imported_outside for run in runs):
            return _Base(outcome=ArtifactCheckOutcome.IMPORTED_OUTSIDE_COPY)
        if any(run.statuses is None for run in runs):
            return _Base(outcome=ArtifactCheckOutcome.BASE_RUNNER_CRASH)
        return _Base(_stable_passes(runs))

    async def _worker_tests(self, candidate: Path, added: Sequence[str]) -> ArtifactFinding:
        check = ArtifactCheck.WORKER_TESTS
        files = worker_test_files(added)
        if not files:
            return ArtifactFinding(check, ArtifactCheckOutcome.NO_SELECTED_FILES)
        failed: list[str] = []
        unobserved: list[ArtifactCheckOutcome] = []
        for path in files:
            with tempfile.TemporaryDirectory(prefix="ouroboros-worker-tests-") as work:
                copy_root = Path(work) / "candidate"
                await asyncio.to_thread(copy_checkout, candidate, copy_root)
                run = await _pytest(copy_root, (path,), self._interpreter, self._timeout)
            if run.unavailable:
                unobserved.append(ArtifactCheckOutcome.UNAVAILABLE)
            elif run.timed_out:
                unobserved.append(ArtifactCheckOutcome.TIMEOUT)
            elif run.return_code == 1 and any(
                status in (_FAIL, _ERROR) for status in (run.statuses or {}).values()
            ):
                failed.append(path)
            elif run.return_code != 0:
                unobserved.append(ArtifactCheckOutcome.NOT_A_TEST_RESULT)
        if failed:
            return ArtifactFinding(check, ArtifactCheckOutcome.REJECTED, tuple(failed), files)
        outcome = unobserved[0] if unobserved else ArtifactCheckOutcome.PASSED
        return ArtifactFinding(check, outcome, selected=files)


async def _run_command(
    root: Path,
    command: TargetCommand,
    target: str,
    interpreter: CheckInterpreter,
    timeout: int,
) -> CommandRun:
    """Run an admitted (or candidate for admission) command for ``target`` in ``root``.

    Confined like every check process, without a shell; a report it writes
    to ``{report}`` (in the scratch directory, outside the copy) is read for
    per-test results. Its console output is never read.
    """
    root = root.resolve()
    with check_scratch(root.parent) as scratch:
        report = scratch / f"report-{secrets.token_hex(8)}.xml"
        argv = command.argv(target, str(report))
        if argv is None:
            return CommandRun(None, unavailable=True)
        prepared = check_command(
            argv, cwd=root, writable_root=root, interpreter=interpreter, scratch=scratch
        )
        if isinstance(prepared, CheckUnavailable):
            return CommandRun(None, unavailable=True)
        completed = await _run_in_environment(prepared, timeout)
        if completed.unavailable is not None or completed.output_overflow:
            return CommandRun(None, unavailable=True)
        if completed.launch_error is not None:
            # The program does not exist here: not a command for this host.
            return CommandRun(None, unavailable=True)
        if completed.timed_out:
            return CommandRun(completed.return_code, timed_out=True)
        statuses = None
        if command.writes_report:
            try:
                status = os.lstat(report)
                if stat.S_ISREG(status.st_mode) and status.st_size <= _REPORT_LIMIT:
                    statuses = parse_junit(report.read_bytes())
            except OSError:
                statuses = None
        return CommandRun(completed.return_code, statuses)


def _write_canary(copy_root: Path, target: str) -> bool:
    """Replace ``target`` in a throwaway base copy with bytes no language parses."""
    directory = _real_directory(copy_root, posixpath.dirname(target))
    if directory is None:
        return False
    path = directory / posixpath.basename(target)
    if os.path.lexists(path):
        if stat.S_ISDIR(os.lstat(path).st_mode):
            return False
        os.unlink(path)
    path.write_bytes(CANARY)
    return True


def _confirmed(
    tests: Sequence[str], statuses: Mapping[str, str] | None, exit_code: int | None
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(failing again, no result)`` among ``tests`` after an observed rerun.

    A test fails again when its report has it failing or erroring, or it
    vanished behind a collection error of its module, or the runner died
    before it wrote a report (a nonzero exit with no report fails every test).
    A report that does not name a test, or no report with exit 0, is no
    result for it: it neither confirms the failure nor passes.
    """
    again = () if statuses is None and exit_code == 0 else regressions(tests, statuses)
    passed = {test for test, status in (statuses or {}).items() if status in (_PASS, _SKIP)}
    return again, tuple(test for test in tests if test not in again and test not in passed)


def _unobserved(run: _Run) -> ArtifactCheckOutcome | None:
    """Why a run observed nothing, or ``None`` when it did."""
    if run.unavailable:
        return ArtifactCheckOutcome.UNAVAILABLE
    if run.timed_out:
        return ArtifactCheckOutcome.TIMEOUT
    return None


def _regular(digest: str | None) -> bool:
    """A manifest entry that is a readable regular file (not a link, not unreadable)."""
    return digest is not None and digest != UNREADABLE and not digest.startswith("symlink:")


def _prepare_candidate_copy(
    copy_root: Path,
    base: Path,
    base_manifest: Mapping[str, str],
    manifest: Mapping[str, str],
    restored: Iterable[str],
) -> bool:
    """The candidate copy a regression run uses: no worker configuration, base test bytes."""
    return neutralize_config(copy_root, base, base_manifest, manifest) and restore_base_bytes(
        copy_root, base, restored
    )


def _unavailable() -> tuple[ArtifactFinding, ArtifactFinding]:
    unavailable = ArtifactCheckOutcome.UNAVAILABLE
    return (
        ArtifactFinding(ArtifactCheck.BASE_REGRESSION, unavailable),
        ArtifactFinding(ArtifactCheck.WORKER_TESTS, unavailable),
    )


# --------------------------------------------------------------------------
# What a finding decides.


def undecided_by_package(item: CriterionVerdict | None) -> bool:
    """A criterion the package left unverified or uncovered (no verdict: uncovered)."""
    return item is None or item.status.is_unverified


def apply_findings(
    verdicts: Mapping[str, CriterionVerdict],
    criterion_keys: Sequence[str],
    findings: Sequence[ArtifactFinding],
    attempted: Collection[str] | None = None,
) -> dict[str, CriterionVerdict]:
    """Fail every attempted criterion the package left undecided when a finding rejects.

    A verified pass, a package failure and an indeterminate criterion keep
    the package's verdict, and so does a criterion the worker never attempted
    (``attempted``: the keys of attempted criteria, ``None`` for all). The
    first rejecting finding names the check.
    """
    rejecting = next((finding for finding in findings if finding.rejects), None)
    if rejecting is None:
        return dict(verdicts)
    recorded = dict.fromkeys(criterion_keys, rejecting.check)
    return replay_recorded(verdicts, recorded, criterion_keys, attempted)


def replay_recorded(
    verdicts: Mapping[str, CriterionVerdict],
    recorded: Mapping[str, ArtifactCheck],
    criterion_keys: Collection[str],
    attempted: Collection[str] | None = None,
) -> dict[str, CriterionVerdict]:
    """Fail each criterion ``recorded`` names with its artifact check, where still undecided.

    The one rule of ``apply_findings``, and how a resumed run replays the
    artifact checks a journaled decision recorded without running them again:
    a criterion the package (or the resume) decided keeps its verdict, a key
    that is not one of ``criterion_keys`` is ignored, and a criterion the
    worker never attempted (outside ``attempted``) is left alone.
    """
    out = dict(verdicts)
    for key, check in recorded.items():
        prior = verdicts.get(key)
        if key not in criterion_keys or (attempted is not None and key not in attempted):
            continue
        if not undecided_by_package(prior):
            continue
        out[key] = CriterionVerdict(
            key,
            PackageCriterionStatus.FAIL,
            prior.tier if prior is not None else CheckTier.U,
            _reason(check),
            prior.check_ids if prior is not None else (),
            declared_binding_pass=False,
            artifact_check=check,
        )
    return out


def repair_message(findings: Sequence[ArtifactFinding]) -> str | None:
    """The counterexample bounded repair receives: failing test names only, or ``None``."""
    lines: list[str] = []
    for finding in findings:
        if not finding.rejects:
            continue
        if finding.check is ArtifactCheck.BASE_REGRESSION:
            lines.append("Existing tests that passed before your change fail on your workspace:")
        else:
            lines.append("Test files you added fail when run on your workspace:")
        lines.extend(f"- {name}" for name in finding.failed[:_REPAIR_NAMES])
        if len(finding.failed) > _REPAIR_NAMES:
            lines.append(f"- and {len(finding.failed) - _REPAIR_NAMES} more")
    if not lines:
        return None
    lines.append("Fix them without undoing what the criterion asks for.")
    return "\n".join(lines)


def report_artifact_checks(
    findings: Sequence[ArtifactFinding],
    *,
    modes: Mapping[ArtifactCheck, ArtifactCheckMode],
    effects: Mapping[ArtifactCheck, ArtifactEffect],
    would_fail: int,
    failed_criteria: int,
    criterion_count: int,
    repairs: int,
    surface: str | None,
    runtime_backend: str | None,
) -> None:
    """Send one ``acceptance_artifact_checks`` event for a decided run. Never raises.

    With each check's mode and what its finding did (``ArtifactEffect``), and
    how many criteria the findings that did not decide would have failed.
    """
    try:
        outcomes = {finding.check: finding.outcome.value for finding in findings}
        regression = next((f for f in findings if f.check is ArtifactCheck.BASE_REGRESSION), None)
        none = ArtifactEffect.NONE
        usage_telemetry.capture_acceptance_artifact_checks(
            base_regression_mode=modes[ArtifactCheck.BASE_REGRESSION].value,
            worker_test_gate_mode=modes[ArtifactCheck.WORKER_TESTS].value,
            base_regression_effect=effects.get(ArtifactCheck.BASE_REGRESSION, none).value,
            worker_tests_effect=effects.get(ArtifactCheck.WORKER_TESTS, none).value,
            would_fail_criteria=would_fail,
            base_regression=outcomes.get(ArtifactCheck.BASE_REGRESSION),
            worker_tests=outcomes.get(ArtifactCheck.WORKER_TESTS),
            failed_criteria=failed_criteria,
            criterion_count=criterion_count,
            repairs=repairs,
            surface=surface,
            runtime_backend=runtime_backend,
            exemption=(
                regression.exemption.value
                if regression is not None and regression.exemption is not None
                else None
            ),
            exempted_tests=len(regression.exempted) if regression is not None else 0,
        )
    except Exception:  # noqa: BLE001 - telemetry must never affect the run
        pass


__all__ = [
    "BASE_REGRESSION_DEFAULT",
    "REGRESSION_REASON",
    "WORKER_TESTS_REASON",
    "ArtifactCheckMode",
    "ArtifactCheckOutcome",
    "ArtifactEffect",
    "WORKER_TEST_GATE_DEFAULT",
    "decides",
    "effect",
    "ArtifactChecks",
    "ArtifactFinding",
    "Exemption",
    "MASS_BREAKAGE",
    "exempt",
    "apply_findings",
    "base_test_files",
    "controller_config",
    "neutralize_config",
    "imported_names",
    "imports_module",
    "paired_tests",
    "parse_junit",
    "regressions",
    "regressions_to_keep",
    "repair_message",
    "replay_recorded",
    "report_artifact_checks",
    "restore_base_bytes",
    "select_tests",
    "undecided_by_package",
    "worker_test_files",
]
