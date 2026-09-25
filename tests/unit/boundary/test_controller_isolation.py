"""Controller isolation: admission and selection read only declared inputs.

The runtime canary plants evaluator-only files (a reference patch, private
tests, a grader report) next to the base checkout, then runs admission,
candidate verification, and selection under a CPython audit hook. The test
fails if the controller opens, lists, or executes anything outside its
declared inputs, or touches a canary. The static half scans the controller
modules for evaluator-only identifiers.
"""

from __future__ import annotations

from collections.abc import Iterator
import contextlib
import os
from pathlib import Path
import re
import sys
from typing import Any

import ouroboros.boundary as boundary_pkg
from ouroboros.boundary import (
    ArtifactRef,
    PackageVerdict,
    admit_check_package,
    select_incumbent,
    tree_digest,
    verify_candidate,
)

FORBIDDEN = re.compile(
    r"test_patch|FAIL_TO_PASS|PASS_TO_PASS|gold_patch|reference_patch|"
    r"full_summaries_verified|fully-specified\.csv|underspecified\.csv|interaction\.csv|"
    r"swebench|run_evaluation\.py|report\.json|resolved_instances"
)

_records: list[tuple[str, Any]] | None = None
_hook_installed = False


def _audit(event: str, args: tuple[Any, ...]) -> None:
    records = _records
    if records is None:
        return
    if event == "open":
        target = args[0]
        if isinstance(target, (str, bytes, os.PathLike)):
            records.append(("open", os.fsdecode(target)))
    elif event in {"os.listdir", "os.scandir"}:
        target = args[0]
        if isinstance(target, (str, bytes, os.PathLike)):
            records.append(("list", os.fsdecode(target)))
    elif event == "subprocess.Popen":
        records.append(("exec", tuple(os.fsdecode(a) for a in args[1])))
    elif event == "import":
        records.append(("import", args[0]))


@contextlib.contextmanager
def _recording() -> Iterator[list[tuple[str, Any]]]:
    global _records, _hook_installed
    if not _hook_installed:
        sys.addaudithook(_audit)
        _hook_installed = True
    records: list[tuple[str, Any]] = []
    _records = records
    try:
        yield records
    finally:
        _records = None


def _under(path: Path, roots: list[Path]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def _plant_canary(root: Path) -> Path:
    canary = root / "evaluator_only"
    (canary / "private_tests").mkdir(parents=True)
    (canary / "grader").mkdir()
    (canary / "reference.patch").write_text("CANARY reference\n")
    (canary / "private_tests" / "test_hidden.py").write_text("CANARY private\n")
    (canary / "grader" / "report.json").write_text('{"CANARY": true}\n')
    return canary


async def _run_controller(package, base_checkout: Path, candidate: Path, work: Path):
    admission = await admit_check_package(package, base_checkout, work_dir=work / "a")
    verification = await verify_candidate(package, candidate, work_dir=work / "v")
    decision = select_incumbent(
        incumbent=ArtifactRef(
            artifact_id="inc",
            tree_digest=tree_digest(base_checkout),
            seed_digest=package.seed_digest,
        ),
        candidate=ArtifactRef(
            artifact_id="cand",
            tree_digest=tree_digest(candidate),
            seed_digest=package.seed_digest,
        ),
        package=package,
        admission=admission,
        verification=verification,
        candidate_checkout=candidate,
    )
    return admission, decision


def _violations(
    records: list[tuple[str, Any]], package, declared: list[Path], canary: Path
) -> list[str]:
    runtime = [
        Path(p).resolve()
        for p in {sys.prefix, sys.base_prefix, sys.exec_prefix, *sys.path}
        if p and Path(p).exists()
    ]
    runtime.append(Path(boundary_pkg.__file__).resolve().parents[1])
    runtime.append(Path("/dev"))
    allowed = [p.resolve() for p in declared] + runtime
    problems: list[str] = []
    touched = [(k, Path(v).resolve()) for k, v in records if k in {"open", "list"}]
    if not touched:
        problems.append("no file access recorded; the canary would be vacuous")
    problems += [f"canary {k}: {p}" for k, p in touched if _under(p, [canary.resolve()])]
    problems += [f"undeclared {k}: {p}" for k, p in touched if not _under(p, allowed)]
    executed = [v for k, v in records if k == "exec"]
    allowed_argv = {check.argv for check in package.checks}
    if not executed:
        problems.append("no check command was observed")
    problems += [f"undeclared exec: {argv}" for argv in executed if argv not in allowed_argv]
    problems += [f"import: {m}" for k, m in records if k == "import" and FORBIDDEN.search(m)]
    return problems


async def test_runtime_canary_admission_and_selection(
    tmp_path: Path, base_checkout, fixed_checkout, package
) -> None:
    canary = _plant_canary(tmp_path)
    work = tmp_path / "work"
    with _recording() as records:
        admission, decision = await _run_controller(package, base_checkout, fixed_checkout, work)

    assert admission.verdict is PackageVerdict.ADMITTED
    assert decision.replaced
    declared = [base_checkout, fixed_checkout, work]
    assert _violations(records, package, declared, canary) == []


async def test_runtime_canary_detects_a_planted_read(
    tmp_path: Path, base_checkout, fixed_checkout, package, monkeypatch
) -> None:
    """Negative control: a controller that peeks at a canary is caught."""
    import ouroboros.boundary.admission as admission_module

    canary = _plant_canary(tmp_path)
    original = admission_module.copy_checkout

    def peeking_copy(source: Path, destination: Path) -> None:
        (canary / "reference.patch").read_text()
        original(source, destination)

    monkeypatch.setattr(admission_module, "copy_checkout", peeking_copy)
    work = tmp_path / "work"
    with _recording() as records:
        await _run_controller(package, base_checkout, fixed_checkout, work)

    problems = _violations(records, package, [base_checkout, fixed_checkout, work], canary)
    assert any(p.startswith("canary open:") for p in problems), problems


def test_static_controller_modules_have_no_evaluator_only_identifiers() -> None:
    package_dir = Path(boundary_pkg.__file__).parent
    modules = sorted(package_dir.glob("*.py"))
    assert modules
    for module in modules:
        hits = sorted(set(FORBIDDEN.findall(module.read_text())))
        assert not hits, f"{module.name} references evaluator-only material: {hits}"
