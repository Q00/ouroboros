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
  base bytes, so an edit the worker made to them is undone before the run. A
  test that passed on both base runs (a flaky test passes on at most one) and
  fails or errors on the candidate, or vanishes behind a collection error of
  its module, regressed; a candidate on which the runner dies before it writes
  a report fails every such test. The base result is kept per (base tree
  digest, selected files), so repeated attempts never rerun the base.
- **Worker tests** (``ArtifactCheck.WORKER_TESTS``). Each test file the
  candidate adds is run alone on a copy of the candidate. A run that exits 1
  with a report naming a failing test fails; exit 0 passes and proves
  nothing; any other exit (2 usage or interrupted, 5 nothing collected) is
  not a test result.

Both only fail, never accept: an executed failure fails every criterion the
admitted package left ``unverified`` or ``uncovered`` (an artifact-level
finding: no criterion is matched to a test by its text), through the existing
``fail`` route (``acceptance.CriterionVerdict.artifact_check``), and while the
worker runs the gate hands the failing test names to bounded repair. A
verified pass keeps the package's authority. Anything else is no observation
and decides nothing, with its reason (``ArtifactCheckOutcome``): a timeout,
a base on which the runner wrote no report, no selected file, a project whose
own runner is not pytest, or a run the sandbox could not confine.

Every run is ``python -m pytest`` with the run's pinned interpreter, on a
throwaway copy, through the check execution entry point
(``admission._run_argv``, ``check_env.check_command``): confined by the
execution sandbox, without network, under the run contract's per-check
timeout. Per-test outcomes come from the runner's JUnit XML report, never from
its console text. A report is written by code the candidate controls, so a
forged report can only hide a failure; it can never fail a correct artifact
that the base's own tests do not.
"""

from __future__ import annotations

import ast
import asyncio
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
import os
from pathlib import Path, PurePosixPath
import posixpath
import secrets
import shutil
import stat
import tempfile
from xml.etree import ElementTree

from ouroboros import telemetry as usage_telemetry
from ouroboros.boundary.acceptance import ArtifactCheck, CriterionVerdict, PackageCriterionStatus
from ouroboros.boundary.admission import _run_argv
from ouroboros.boundary.binding import CheckTier
from ouroboros.boundary.check_env import CheckInterpreter
from ouroboros.boundary.tree import (
    UNREADABLE,
    added_paths,
    changed_paths,
    copy_checkout,
    manifest_digest,
    tree_manifest,
)
from ouroboros.orchestrator.evidence.call_citation import is_test_path

BASE_REGRESSION_DEFAULT = True
"""Whether the artifact checks run when nothing configures them (``boundary.base_regression``).

The one place the default lives: flip it here to turn both checks off by default."""

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


class ArtifactCheckOutcome(StrEnum):
    """What one artifact check observed on a run (closed set; telemetry reports it)."""

    REJECTED = "rejected"
    """An executed failure: the check failed the undecided criteria."""
    PASSED = "passed"
    """The check ran and found nothing; it decides nothing."""
    TIMEOUT = "timeout"
    BASE_RUNNER_CRASH = "base_runner_crash"
    """The runner wrote no report on the base: nothing to compare against."""
    NO_SELECTED_FILES = "no_selected_files"
    """No base test file pairs with or imports a changed module, or no test file was added."""
    UNSUPPORTED_RUNNER = "unsupported_runner"
    NOT_A_TEST_RESULT = "not_a_test_result"
    """A worker test file whose run exited with neither 0 nor 1."""
    UNAVAILABLE = "unavailable"
    """Nothing ran: the sandbox, the pinned interpreter or the base snapshot refused it."""


@dataclass(frozen=True, slots=True)
class ArtifactFinding:
    """One check's observation on one candidate tree."""

    check: ArtifactCheck
    outcome: ArtifactCheckOutcome
    failed: tuple[str, ...] = ()
    """The regressed test ids, or the worker test files that failed."""
    selected: tuple[str, ...] = ()

    @property
    def rejects(self) -> bool:
        return self.outcome is ArtifactCheckOutcome.REJECTED

    @property
    def reason(self) -> str:
        """The ``CriterionVerdict.reason`` of a criterion this check fails."""
        if self.check is ArtifactCheck.BASE_REGRESSION:
            return REGRESSION_REASON
        return WORKER_TESTS_REASON


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
    regressed: Sequence[str], changed_functions: Collection[str]
) -> tuple[str, ...]:
    """The regressed tests that count against the candidate; today every one of them.

    The seam of the planned footprint exemption: a regressed test is exempt
    when every changed function its failing run entered was also entered by a
    passing admitted oracle, that is, the test pins behaviour the package
    itself verified the fix to change. ``changed_functions`` will carry the
    functions the diff changed; the follow-up records which of them each
    failing test and each passing oracle entered, and drops exempt tests here.
    """
    del changed_functions
    return tuple(regressed)


# --------------------------------------------------------------------------
# Runs and their reports.


@dataclass(frozen=True, slots=True)
class _Run:
    statuses: dict[str, str] | None
    """Per-test status from the report; ``None`` when the runner wrote no usable report."""
    return_code: int | None
    timed_out: bool = False
    unavailable: bool = False


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
    root: Path, files: Sequence[str], interpreter: CheckInterpreter, timeout: int
) -> _Run:
    """Run ``files`` with pytest in ``root`` (a throwaway copy) and read its report."""
    report = root / f".ouroboros-report-{secrets.token_hex(8)}.xml"
    argv = (
        "python",
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-q",
        "--continue-on-collection-errors",
        f"--junitxml={report.name}",
        "--",
        *files,
    )
    completed = await _run_argv(
        argv,
        root,
        timeout,
        interpreter=interpreter,
        scratch_parent=root.parent,
        writable_root=root,
    )
    if completed.unavailable is not None or completed.launch_error is not None:
        return _Run(None, None, unavailable=True)
    if completed.timed_out:
        return _Run(None, completed.return_code, timed_out=True)
    if completed.output_overflow:
        # Killed for flooding its output: whatever it would have reported is unknown.
        return _Run(None, None, unavailable=True)
    statuses = None
    try:
        if stat.S_ISREG(os.lstat(report).st_mode) and os.lstat(report).st_size <= _REPORT_LIMIT:
            statuses = parse_junit(report.read_bytes())
    except OSError:
        statuses = None
    return _Run(statuses, completed.return_code)


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
    ) -> None:
        self._base = base
        self._base_digest = base_digest
        self._interpreter = interpreter
        self._timeout = timeout_seconds
        self._base_manifest: dict[str, str] | None = None
        self._base_runs: dict[tuple[str, tuple[str, ...]], _Base] = {}
        self._findings: dict[str, tuple[ArtifactFinding, ArtifactFinding]] = {}
        self._lock = asyncio.Lock()

    async def findings(self, candidate: Path) -> tuple[ArtifactFinding, ArtifactFinding]:
        """The base regression and worker-test findings for ``candidate`` as it is now."""
        async with self._lock:
            try:
                return await self._findings_for(candidate.resolve())
            except (OSError, shutil.Error, RecursionError):
                return _unavailable()

    async def _findings_for(self, candidate: Path) -> tuple[ArtifactFinding, ArtifactFinding]:
        base = self._pinned_base()
        if base is None:
            return _unavailable()
        manifest = tree_manifest(candidate)
        digest = manifest_digest(manifest)
        cached = self._findings.get(digest)
        if cached is not None:
            return cached
        assert self._base_manifest is not None
        if UNREADABLE in manifest.values():
            return _unavailable()
        if _PROJECT_RUNNERS & set(self._base_manifest):
            unsupported = ArtifactCheckOutcome.UNSUPPORTED_RUNNER
            found = (
                ArtifactFinding(ArtifactCheck.BASE_REGRESSION, unsupported),
                ArtifactFinding(ArtifactCheck.WORKER_TESTS, unsupported),
            )
        else:
            changed = changed_paths(self._base_manifest, manifest)
            found = (
                await self._regression(base, candidate, changed),
                await self._worker_tests(candidate, added_paths(self._base_manifest, manifest)),
            )
        if tree_manifest(candidate) == manifest:
            # Kept only for the tree it observed: a workspace that changed
            # while the checks ran is checked again on its next call.
            self._findings[digest] = found
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
        self, base: Path, candidate: Path, changed: Sequence[str]
    ) -> ArtifactFinding:
        check = ArtifactCheck.BASE_REGRESSION
        assert self._base_manifest is not None and self._base_digest is not None
        selected = select_tests(self._base_manifest, changed, base_root=base)
        if not selected:
            return ArtifactFinding(check, ArtifactCheckOutcome.NO_SELECTED_FILES)
        key = (self._base_digest, selected)
        if key not in self._base_runs:
            self._base_runs[key] = await self._base_side(base, selected)
        side = self._base_runs[key]
        if side.outcome is not None:
            return ArtifactFinding(check, side.outcome, selected=selected)
        restored = (*selected, *_conftests_above(selected, self._base_manifest))
        with tempfile.TemporaryDirectory(prefix="ouroboros-regression-") as work:
            copy_root = Path(work) / "candidate"
            copy_checkout(candidate, copy_root)
            if not restore_base_bytes(copy_root, base, restored):
                return ArtifactFinding(check, ArtifactCheckOutcome.UNAVAILABLE, selected=selected)
            run = await _pytest(copy_root, selected, self._interpreter, self._timeout)
        if run.unavailable:
            return ArtifactFinding(check, ArtifactCheckOutcome.UNAVAILABLE, selected=selected)
        if run.timed_out:
            return ArtifactFinding(check, ArtifactCheckOutcome.TIMEOUT, selected=selected)
        regressed = regressions_to_keep(regressions(side.stable, run.statuses), ())
        outcome = ArtifactCheckOutcome.REJECTED if regressed else ArtifactCheckOutcome.PASSED
        return ArtifactFinding(check, outcome, regressed, selected)

    async def _base_side(self, base: Path, selected: tuple[str, ...]) -> _Base:
        """Two runs of ``selected`` on fresh base copies; a test must pass on both."""
        runs = []
        for _ in range(2):
            with tempfile.TemporaryDirectory(prefix="ouroboros-regression-") as work:
                copy_root = Path(work) / "base"
                copy_checkout(base, copy_root)
                runs.append(await _pytest(copy_root, selected, self._interpreter, self._timeout))
        if any(run.unavailable for run in runs):
            return _Base(outcome=ArtifactCheckOutcome.UNAVAILABLE)
        if any(run.timed_out for run in runs):
            return _Base(outcome=ArtifactCheckOutcome.TIMEOUT)
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
                copy_checkout(candidate, copy_root)
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
) -> dict[str, CriterionVerdict]:
    """Fail every criterion the package left undecided when a finding rejects.

    A verified pass, a package failure and an indeterminate criterion keep
    the package's verdict. The first rejecting finding names the check.
    """
    rejecting = next((finding for finding in findings if finding.rejects), None)
    if rejecting is None:
        return dict(verdicts)
    out = dict(verdicts)
    for key in criterion_keys:
        prior = verdicts.get(key)
        if not undecided_by_package(prior):
            continue
        out[key] = CriterionVerdict(
            key,
            PackageCriterionStatus.FAIL,
            prior.tier if prior is not None else CheckTier.U,
            rejecting.reason,
            prior.check_ids if prior is not None else (),
            declared_binding_pass=False,
            artifact_check=rejecting.check,
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
    failed_criteria: int,
    criterion_count: int,
    repairs: int,
    surface: str | None,
    runtime_backend: str | None,
) -> None:
    """Send one ``acceptance_artifact_checks`` event for a decided run. Never raises."""
    try:
        outcomes = {finding.check: finding.outcome.value for finding in findings}
        usage_telemetry.capture_acceptance_artifact_checks(
            base_regression=outcomes.get(ArtifactCheck.BASE_REGRESSION),
            worker_tests=outcomes.get(ArtifactCheck.WORKER_TESTS),
            failed_criteria=failed_criteria,
            criterion_count=criterion_count,
            repairs=repairs,
            surface=surface,
            runtime_backend=runtime_backend,
        )
    except Exception:  # noqa: BLE001 - telemetry must never affect the run
        pass


__all__ = [
    "BASE_REGRESSION_DEFAULT",
    "REGRESSION_REASON",
    "WORKER_TESTS_REASON",
    "ArtifactCheckOutcome",
    "ArtifactChecks",
    "ArtifactFinding",
    "apply_findings",
    "base_test_files",
    "imported_names",
    "imports_module",
    "paired_tests",
    "parse_junit",
    "regressions",
    "regressions_to_keep",
    "repair_message",
    "report_artifact_checks",
    "restore_base_bytes",
    "select_tests",
    "undecided_by_package",
    "worker_test_files",
]
