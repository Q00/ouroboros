"""Base-state admission and candidate verification on isolated copies."""

from __future__ import annotations

import os
from pathlib import Path
import sys

import pytest

from ouroboros.boundary import admission
from ouroboros.boundary.admission import (
    ADMISSION_TIMEOUT_SECONDS,
    admit_check_package,
    verify_candidate,
)
from ouroboros.boundary.package import BaseFileRef, PackageFile, seal_package, sha256_bytes
from ouroboros.boundary.receipts import CandidateVerdict, CheckStatus, PackageVerdict, write_receipt
from ouroboros.boundary.tree import tree_digest

from .conftest import SIGNATURE, build_package


def _by_id(result):
    return {check.check_id: check for check in result.checks}


def test_default_per_command_timeout_is_120_seconds() -> None:
    assert ADMISSION_TIMEOUT_SECONDS == 120


async def test_happy_path_admits_every_check(tmp_path: Path, base_checkout, package) -> None:
    before = tree_digest(base_checkout)
    result = await admit_check_package(package, base_checkout, work_dir=tmp_path / "work")

    assert result.verdict is PackageVerdict.ADMITTED
    assert result.package_sha256 == package.sha256
    assert result.seed_digest == package.seed_digest
    checks = _by_id(result)
    assert checks["script_1_1"].status is CheckStatus.EXPECTED
    assert checks["script_1_1"].reason == "reached_failing_assertion"
    assert checks["script_1_1"].signature_seen
    assert checks["script_2_1"].reason == "preservation_passed"
    assert not result.protected_bytes_mutated
    assert result.base_tree_digest == result.base_tree_digest_after == before
    # Generated files never touch the base checkout.
    assert not (base_checkout / "probe").exists()
    assert result.timeout_seconds == ADMISSION_TIMEOUT_SECONDS


@pytest.mark.parametrize(
    "script",
    [
        "assert True\n",
        "import subprocess, sys\nsubprocess.run([sys.executable, 'worker_impl.py'])\n",
        "mod = __import__('calc')\n",
        "from calc import add\n",
    ],
    ids=["no_import", "subprocess", "dynamic_import", "static_import"],
)
async def test_a_script_check_is_tier_s_whatever_it_imports(
    tmp_path: Path, seed, base_checkout, script: str
) -> None:
    # #2458 round 4: a script check made a tier claim from a static reading
    # of its imports (a no-import script and a subprocess call were tier A).
    # A script claims no target: its pass is advisory and only its failure
    # counts, so admission records it as tier S, never A.
    package = build_package(seed, preserve_script=script)
    result = await admit_check_package(package, base_checkout, work_dir=tmp_path / "w")
    assert result.check_tiers is not None
    # Every check is S (one per-check admission excluded is C, never A).
    assert set(result.check_tiers.values()) <= {"S", "C"}
    assert "S" in set(result.check_tiers.values())


async def test_reproduction_failing_for_wrong_reason_is_excluded_on_its_own(
    tmp_path: Path, seed, base_checkout
) -> None:
    # v0.55.4 dev run (django__django-14580): one reproduction reached its
    # failing assertion, another ended without its signature, and the whole
    # package was refused. The undecided check shows nothing; it is excluded
    # on its own and the check that met its role is admitted.
    import_error = "import not_a_real_module_xyz\n"
    package = build_package(seed, repro_script=import_error)
    result = await admit_check_package(package, base_checkout, work_dir=tmp_path / "w")

    repro = _by_id(result)["script_1_1"]
    assert repro.status is CheckStatus.INDETERMINATE
    assert repro.reason == "failure_signature_absent"
    assert repro.return_code not in (0, None)
    assert result.verdict is PackageVerdict.ADMITTED
    assert result.excluded_checks == {"script_1_1": "indeterminate_on_base"}
    assert result.check_tiers == {"script_1_1": "C", "script_2_1": "S"}
    assert _by_id(result)["script_2_1"].status is CheckStatus.EXPECTED
    assert "failure_signature_absent:script_1_1" in result.reasons


async def test_every_check_indeterminate_is_no_admission(
    tmp_path: Path, seed, base_checkout
) -> None:
    package = build_package(
        seed, repro_script="raise SystemExit(2)\n", preserve_script="import time\ntime.sleep(30)\n"
    )
    result = await admit_check_package(
        package, base_checkout, work_dir=tmp_path / "w", timeout_seconds=1
    )

    assert result.verdict is PackageVerdict.INDETERMINATE
    assert result.reasons[-1] == "all_checks_excluded"
    assert result.excluded_checks is None


async def test_reproduction_passing_on_base_is_excluded_on_its_own(
    tmp_path: Path, seed, base_checkout
) -> None:
    package = build_package(seed, repro_script="print('vacuous')\n")
    result = await admit_check_package(package, base_checkout, work_dir=tmp_path / "w")

    assert _by_id(result)["script_1_1"].reason == "reproduction_passed_on_base"
    assert result.verdict is PackageVerdict.ADMITTED
    assert result.excluded_checks == {"script_1_1": "repro_passes_on_base"}


async def test_preservation_failure_is_excluded_and_retained(
    tmp_path: Path, seed, base_checkout
) -> None:
    package = build_package(seed, preserve_script="raise SystemExit(3)\n")
    result = await admit_check_package(package, base_checkout, work_dir=tmp_path / "w")

    checks = _by_id(result)
    # Per-check admission: the failed check is excluded on its own and stays
    # in the receipt; the rest of the package is admitted.
    assert result.verdict is PackageVerdict.ADMITTED
    assert result.excluded_checks == {"script_2_1": "preservation_fails_on_base"}
    assert checks["script_1_1"].status is CheckStatus.EXPECTED
    assert checks["script_2_1"].status is CheckStatus.VIOLATED
    assert checks["script_2_1"].reason == "preservation_failed"
    assert "preservation_failed:script_2_1" in result.reasons
    assert len(result.checks) == len(package.checks)


async def test_protected_byte_mutation_is_indeterminate_and_flagged(
    tmp_path: Path, seed, base_checkout
) -> None:
    mutate = (
        "open('calc.py', 'w').write('def add(a, b):\\n    return a + b\\n')\n"
        f"print('{SIGNATURE}')\nraise SystemExit(1)\n"
    )
    package = build_package(seed, repro_script=mutate)
    base_before = tree_digest(base_checkout)
    result = await admit_check_package(package, base_checkout, work_dir=tmp_path / "w")

    repro = _by_id(result)["script_1_1"]
    assert repro.status is CheckStatus.INDETERMINATE
    assert repro.reason == "protected_bytes_mutated"
    assert repro.mutated_paths == ("calc.py",)
    assert repro.protected_digest_before != repro.protected_digest_after
    assert result.protected_bytes_mutated
    assert result.verdict is PackageVerdict.INDETERMINATE
    assert "protected_bytes_mutated:script_1_1" in result.reasons
    # The mutation happened on the isolated copy, not the pinned base.
    assert tree_digest(base_checkout) == base_before


async def test_mutating_a_package_file_is_also_protected(
    tmp_path: Path, seed, base_checkout
) -> None:
    mutate_self = "open('probe/test_zero.py', 'a').write('#x\\n')\n"
    package = build_package(seed, preserve_script=mutate_self)
    result = await admit_check_package(package, base_checkout, work_dir=tmp_path / "w")

    assert _by_id(result)["script_2_1"].mutated_paths == ("probe/test_zero.py",)
    assert result.verdict is PackageVerdict.INDETERMINATE


async def test_scratch_and_undeclared_outputs_are_separated(
    tmp_path: Path, seed, base_checkout
) -> None:
    writes = (
        "import os\nos.makedirs('out', exist_ok=True)\n"
        "open('out/log.txt', 'w').write('x')\nopen('stray.txt', 'w').write('y')\n"
    )
    package = build_package(seed, preserve_script=writes, scratch_paths=("out",))
    result = await admit_check_package(package, base_checkout, work_dir=tmp_path / "w")

    zero = _by_id(result)["script_2_1"]
    assert zero.scratch_outputs == ("out/log.txt",)
    assert zero.undeclared_outputs == ("stray.txt",)
    assert zero.mutated_paths == ()
    assert result.verdict is PackageVerdict.ADMITTED


async def test_timeout_is_indeterminate(tmp_path: Path, seed, base_checkout) -> None:
    package = build_package(seed, preserve_script="import time\ntime.sleep(30)\n")
    result = await admit_check_package(
        package, base_checkout, work_dir=tmp_path / "w", timeout_seconds=1
    )

    zero = _by_id(result)["script_2_1"]
    assert zero.timed_out
    assert zero.reason == "timeout"
    assert zero.status is CheckStatus.INDETERMINATE
    # A timeout decides nothing about the other check: excluded on its own.
    assert result.verdict is PackageVerdict.ADMITTED
    assert result.excluded_checks == {"script_2_1": "indeterminate_on_base"}


async def test_launch_failure_is_indeterminate(tmp_path: Path, seed, base_checkout) -> None:
    package = build_package(seed, repro_argv=("definitely-not-a-binary-7f3e", "x"))
    result = await admit_check_package(package, base_checkout, work_dir=tmp_path / "w")

    assert _by_id(result)["script_1_1"].reason == "launch_failed"
    assert _by_id(result)["script_1_1"].status is CheckStatus.INDETERMINATE
    assert result.verdict is PackageVerdict.ADMITTED
    assert result.excluded_checks == {"script_1_1": "indeterminate_on_base"}


async def test_package_path_collision_runs_nothing(tmp_path: Path, seed, base_checkout) -> None:
    package = build_package(seed, extra_files=(PackageFile.from_content("calc.py", "x = 1\n"),))
    result = await admit_check_package(package, base_checkout, work_dir=tmp_path / "w")

    assert result.checks == ()
    assert "package_path_collision:calc.py" in result.reasons
    assert result.verdict is PackageVerdict.INDETERMINATE


async def test_pinned_base_file_mismatch_is_indeterminate(
    tmp_path: Path, seed, base_checkout
) -> None:
    package = build_package(seed, base_files=(BaseFileRef(path="README.md", sha256="0" * 64),))
    result = await admit_check_package(package, base_checkout, work_dir=tmp_path / "w")

    assert "base_file_mismatch:README.md" in result.reasons
    assert result.verdict is PackageVerdict.INDETERMINATE


async def test_admission_journal_payload_has_no_argv_or_output(
    tmp_path: Path, base_checkout, package
) -> None:
    result = await admit_check_package(package, base_checkout, work_dir=tmp_path / "w")
    payload = result.event_summary()
    assert SIGNATURE not in repr(payload)
    assert all("argv" not in c and "output_tail" not in c for c in payload["checks"])
    stored = write_receipt(result, tmp_path / "receipts")
    assert SIGNATURE in stored.read_text()


async def test_candidate_verification_pass_and_fail(
    tmp_path: Path, base_checkout, fixed_checkout, package
) -> None:
    passed = await verify_candidate(package, fixed_checkout, work_dir=tmp_path / "a")
    failed = await verify_candidate(package, base_checkout, work_dir=tmp_path / "b")

    assert passed.verdict is CandidateVerdict.PASS
    assert passed.artifact_tree_digest == tree_digest(fixed_checkout)
    assert passed.package_sha256 == package.sha256
    assert failed.verdict is CandidateVerdict.FAIL
    assert _by_id(failed)["script_1_1"].reason == "reproduction_still_failing"
    assert _by_id(failed)["script_1_1"].signature_seen


async def test_reused_work_dir_is_refused(tmp_path: Path, base_checkout, package) -> None:
    work = tmp_path / "w"
    work.mkdir()
    (work / "leftover").write_text("x")
    with pytest.raises(ValueError, match="new or empty"):
        await admit_check_package(package, base_checkout, work_dir=work)


async def test_candidate_reproduction_without_signature_is_indeterminate(
    tmp_path: Path, seed, fixed_checkout
) -> None:
    """A candidate crash before the intended assertion is not a detected failure."""
    (fixed_checkout / "calc.py").write_text("import not_a_real_module_xyz\n")
    package = build_package(seed)
    result = await verify_candidate(package, fixed_checkout, work_dir=tmp_path / "w")

    checks = _by_id(result)
    assert checks["script_1_1"].status is CheckStatus.INDETERMINATE
    assert checks["script_1_1"].reason == "failure_signature_absent"
    # Preservation has no signature: its non-zero exit is a failure.
    assert checks["script_2_1"].reason == "preservation_failed"
    assert result.verdict is CandidateVerdict.FAIL


# --------------------------------------------------------------------------
# Candidate layout: package files and check directories never go through a link


def _sandbox_mode(request: pytest.FixtureRequest, mode: str) -> None:
    if mode == "sandbox_on":
        from ouroboros.runtime.exec_sandbox import sandbox_unavailable_reason

        request.getfixturevalue("real_check_isolation")
        reason = sandbox_unavailable_reason(deny_network=True)
        if reason is not None:
            pytest.skip(f"execution sandbox unavailable on this host: {reason}")


def _candidate_with(tmp_path: Path, fixed_checkout: Path, kind: str) -> tuple[Path, Path]:
    """``fixed_checkout`` with ``probe`` as a link to an outside directory, or a file."""
    outside = tmp_path / "outside"
    outside.mkdir()
    if kind == "symlink":
        (fixed_checkout / "probe").symlink_to(outside, target_is_directory=True)
    else:
        (fixed_checkout / "probe").write_text("not a directory\n")
    return fixed_checkout, outside


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
@pytest.mark.parametrize("mode", ["sandbox_off", "sandbox_on"])
@pytest.mark.parametrize("kind", ["symlink", "file"])
async def test_a_candidate_ancestor_of_a_package_file_writes_nothing_outside(
    tmp_path: Path, fixed_checkout, package, kind: str, mode: str, request
) -> None:
    _sandbox_mode(request, mode)
    candidate, outside = _candidate_with(tmp_path, fixed_checkout, kind)

    result = await verify_candidate(package, candidate, work_dir=tmp_path / "w")

    assert result.verdict is CandidateVerdict.INDETERMINATE
    assert "package_path_collision:probe/test_add.py" in result.reasons
    assert result.checks == ()
    assert list(outside.iterdir()) == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
@pytest.mark.parametrize("mode", ["sandbox_off", "sandbox_on"])
@pytest.mark.parametrize("kind", ["symlink", "file"])
async def test_materialization_never_follows_a_candidate_link_even_after_the_manifest(
    tmp_path: Path, fixed_checkout, package, kind: str, mode: str, request, monkeypatch
) -> None:
    # The candidate's layout changes after its manifest was taken (a leftover
    # process of the worker): materialization itself refuses the link.
    _sandbox_mode(request, mode)
    candidate, outside = _candidate_with(tmp_path, fixed_checkout, kind)
    monkeypatch.setattr(admission, "_package_preconditions", lambda *_args, **_kwargs: [])

    result = await verify_candidate(package, candidate, work_dir=tmp_path / "w")

    assert result.verdict is CandidateVerdict.INDETERMINATE
    assert {check.status for check in result.checks} == {CheckStatus.INDETERMINATE}
    assert {check.reason for check in result.checks} == {admission.CANDIDATE_LAYOUT}
    assert list(outside.iterdir()) == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
async def test_a_check_directory_the_candidate_linked_out_of_the_copy_runs_nothing(
    tmp_path: Path, fixed_checkout, package, seed
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (fixed_checkout / "sub").symlink_to(outside, target_is_directory=True)
    # A differently shaped package is sealed on its own; a sealed one is never edited.
    moved = seal_package(
        build_package(seed).model_copy(
            update={
                "checks": tuple(check.model_copy(update={"cwd": "sub"}) for check in package.checks)
            }
        )
    )

    result = await verify_candidate(moved, fixed_checkout, work_dir=tmp_path / "w")

    assert result.verdict is CandidateVerdict.INDETERMINATE
    assert {check.reason for check in result.checks} == {admission.CANDIDATE_LAYOUT}
    assert sorted(path.name for path in outside.iterdir()) == ["calc.py"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
async def test_a_base_ancestor_link_is_a_collision_and_writes_nothing_outside(
    tmp_path: Path, base_checkout, package
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (base_checkout / "probe").symlink_to(outside, target_is_directory=True)

    result = await admit_check_package(package, base_checkout, work_dir=tmp_path / "w")

    assert result.verdict is PackageVerdict.INDETERMINATE
    assert "package_path_collision:probe/test_add.py" in result.reasons
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("selection", [(), ("not-a-package-check",)], ids=["empty", "unknown"])
async def test_a_selection_outside_the_package_runs_nothing_and_never_passes(
    tmp_path: Path, fixed_checkout, package, selection
) -> None:
    result = await verify_candidate(
        package, fixed_checkout, work_dir=tmp_path / "w", only_checks=selection
    )

    assert result.verdict is CandidateVerdict.INDETERMINATE
    assert result.checks == ()


# --------------------------------------------------------------------------
# Layout preflight: every entry type at a package path is classified first


def _occupy_with_directory(root: Path, kind: str) -> None:
    """A directory at the exact package-file path that the file-only manifest does not list."""
    occupied = root / "probe" / "test_add.py"
    occupied.mkdir(parents=True)
    if kind == "unprotected_only":
        (occupied / "__pycache__").mkdir()
        (occupied / "__pycache__" / "x.pyc").write_bytes(b"\0")


@pytest.mark.parametrize("kind", ["empty", "unprotected_only"])
async def test_a_base_directory_at_a_package_file_path_is_a_collision(
    tmp_path: Path, base_checkout, package, kind: str
) -> None:
    _occupy_with_directory(base_checkout, kind)

    result = await admit_check_package(package, base_checkout, work_dir=tmp_path / "w")

    assert result.verdict is PackageVerdict.INDETERMINATE
    assert "package_path_collision:probe/test_add.py" in result.reasons
    assert result.checks == ()


@pytest.mark.parametrize("kind", ["empty", "unprotected_only"])
async def test_a_candidate_directory_at_a_package_file_path_is_a_layout_fault_per_check(
    tmp_path: Path, fixed_checkout, package, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The worker controls the candidate tree: the path it occupied decides
    # nothing, check by check, and is classified before anything is copied.
    _occupy_with_directory(fixed_checkout, kind)
    copied: list[Path] = []
    real_copy = admission.copy_checkout
    monkeypatch.setattr(
        admission, "copy_checkout", lambda src, dst: (copied.append(dst), real_copy(src, dst))
    )

    result = await verify_candidate(package, fixed_checkout, work_dir=tmp_path / "w")

    assert result.verdict is CandidateVerdict.INDETERMINATE
    assert not any(reason.startswith("package_path_collision") for reason in result.reasons)
    assert {check.check_id for check in result.checks} == {"script_1_1", "script_2_1"}
    assert {check.status for check in result.checks} == {CheckStatus.INDETERMINATE}
    assert {check.reason for check in result.checks} == {admission.CANDIDATE_LAYOUT}
    assert copied == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX named pipes")
@pytest.mark.parametrize("where", ["probe/test_add.py", "probe"], ids=["exact", "ancestor"])
async def test_a_candidate_special_file_at_a_package_path_is_unreadable_per_check(
    tmp_path: Path, fixed_checkout, package, where: str
) -> None:
    (fixed_checkout / where).parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(fixed_checkout / where)

    result = await verify_candidate(package, fixed_checkout, work_dir=tmp_path / "w")

    assert result.verdict is CandidateVerdict.INDETERMINATE
    assert not any(reason.startswith("package_path_collision") for reason in result.reasons)
    assert {check.check_id for check in result.checks} == {"script_1_1", "script_2_1"}
    assert {check.status for check in result.checks} == {CheckStatus.INDETERMINATE}
    assert {check.reason for check in result.checks} == {admission.CANDIDATE_UNREADABLE}


async def test_a_candidate_that_edits_a_pinned_base_file_is_still_verified(
    tmp_path: Path, seed, base_checkout, fixed_checkout
) -> None:
    # A pinned base file is a precondition of admission on the base only: a
    # finished workspace may change it, and its checks still run on its own
    # copied contents.
    readme = BaseFileRef(
        path="README.md", sha256=sha256_bytes((base_checkout / "README.md").read_bytes())
    )
    package = build_package(seed, base_files=(readme,))
    admitted = await admit_check_package(package, base_checkout, work_dir=tmp_path / "a")
    assert admitted.verdict is PackageVerdict.ADMITTED

    (fixed_checkout / "README.md").write_text("calc, now adding correctly\n")
    result = await verify_candidate(package, fixed_checkout, work_dir=tmp_path / "v")

    assert not any(reason.startswith("base_file_mismatch") for reason in result.reasons)
    assert {check.check_id for check in result.checks} == {"script_1_1", "script_2_1"}
    assert {check.status for check in result.checks} == {CheckStatus.EXPECTED}
    assert result.verdict is CandidateVerdict.PASS
