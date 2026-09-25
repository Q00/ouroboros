"""Base-state admission and candidate verification on isolated copies."""

from __future__ import annotations

from pathlib import Path

import pytest

from ouroboros.boundary import (
    ADMISSION_TIMEOUT_SECONDS,
    BaseFileRef,
    CandidateVerdict,
    CheckStatus,
    PackageFile,
    PackageVerdict,
    admit_check_package,
    tree_digest,
    verify_candidate,
    write_receipt,
)

from .conftest import SIGNATURE, build_package


def _by_id(result):
    return {check.check_id: check for check in result.checks}


def test_default_per_command_timeout_is_120_seconds() -> None:
    assert ADMISSION_TIMEOUT_SECONDS == 120


async def test_happy_path_admits_whole_package(tmp_path: Path, base_checkout, package) -> None:
    before = tree_digest(base_checkout)
    result = await admit_check_package(package, base_checkout, work_dir=tmp_path / "work")

    assert result.verdict is PackageVerdict.ADMITTED
    assert result.package_sha256 == package.sha256
    assert result.seed_digest == package.seed_digest
    checks = _by_id(result)
    assert checks["repro-add"].status is CheckStatus.EXPECTED
    assert checks["repro-add"].reason == "reached_failing_assertion"
    assert checks["repro-add"].signature_seen
    assert checks["preserve-zero"].reason == "preservation_passed"
    assert not result.protected_bytes_mutated
    assert result.base_tree_digest == result.base_tree_digest_after == before
    # Generated files never touch the base checkout.
    assert not (base_checkout / "probe").exists()
    assert result.timeout_seconds == ADMISSION_TIMEOUT_SECONDS


async def test_reproduction_failing_for_wrong_reason_is_indeterminate(
    tmp_path: Path, seed, base_checkout
) -> None:
    import_error = "import not_a_real_module_xyz\n"
    package = build_package(seed, repro_script=import_error)
    result = await admit_check_package(package, base_checkout, work_dir=tmp_path / "w")

    repro = _by_id(result)["repro-add"]
    assert repro.status is CheckStatus.INDETERMINATE
    assert repro.reason == "failure_signature_absent"
    assert repro.return_code not in (0, None)
    assert result.verdict is PackageVerdict.INDETERMINATE


async def test_reproduction_passing_on_base_is_admission_failure(
    tmp_path: Path, seed, base_checkout
) -> None:
    package = build_package(seed, repro_script="print('vacuous')\n")
    result = await admit_check_package(package, base_checkout, work_dir=tmp_path / "w")

    assert _by_id(result)["repro-add"].reason == "reproduction_passed_on_base"
    assert result.verdict is PackageVerdict.REJECTED


async def test_preservation_failure_rejects_and_is_retained(
    tmp_path: Path, seed, base_checkout
) -> None:
    package = build_package(seed, preserve_script="raise SystemExit(3)\n")
    result = await admit_check_package(package, base_checkout, work_dir=tmp_path / "w")

    checks = _by_id(result)
    assert result.verdict is PackageVerdict.REJECTED
    # Whole-package evaluation: the passing check does not rescue the package,
    # and the failed one stays in the receipt.
    assert checks["repro-add"].status is CheckStatus.EXPECTED
    assert checks["preserve-zero"].status is CheckStatus.VIOLATED
    assert checks["preserve-zero"].reason == "preservation_failed"
    assert "preservation_failed:preserve-zero" in result.reasons
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

    repro = _by_id(result)["repro-add"]
    assert repro.status is CheckStatus.INDETERMINATE
    assert repro.reason == "protected_bytes_mutated"
    assert repro.mutated_paths == ("calc.py",)
    assert repro.protected_digest_before != repro.protected_digest_after
    assert result.protected_bytes_mutated
    assert result.verdict is PackageVerdict.INDETERMINATE
    assert "protected_bytes_mutated:repro-add" in result.reasons
    # The mutation happened on the isolated copy, not the pinned base.
    assert tree_digest(base_checkout) == base_before


async def test_mutating_a_package_file_is_also_protected(
    tmp_path: Path, seed, base_checkout
) -> None:
    mutate_self = "open('probe/test_zero.py', 'a').write('#x\\n')\n"
    package = build_package(seed, preserve_script=mutate_self)
    result = await admit_check_package(package, base_checkout, work_dir=tmp_path / "w")

    assert _by_id(result)["preserve-zero"].mutated_paths == ("probe/test_zero.py",)
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

    zero = _by_id(result)["preserve-zero"]
    assert zero.scratch_outputs == ("out/log.txt",)
    assert zero.undeclared_outputs == ("stray.txt",)
    assert zero.mutated_paths == ()
    assert result.verdict is PackageVerdict.ADMITTED


async def test_timeout_is_indeterminate(tmp_path: Path, seed, base_checkout) -> None:
    package = build_package(seed, preserve_script="import time\ntime.sleep(30)\n")
    result = await admit_check_package(
        package, base_checkout, work_dir=tmp_path / "w", timeout_seconds=1
    )

    zero = _by_id(result)["preserve-zero"]
    assert zero.timed_out
    assert zero.reason == "timeout"
    assert result.verdict is PackageVerdict.INDETERMINATE


async def test_launch_failure_is_indeterminate(tmp_path: Path, seed, base_checkout) -> None:
    package = build_package(seed, repro_argv=("definitely-not-a-binary-7f3e", "x"))
    result = await admit_check_package(package, base_checkout, work_dir=tmp_path / "w")

    assert _by_id(result)["repro-add"].reason == "launch_failed"
    assert result.verdict is PackageVerdict.INDETERMINATE


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
    assert _by_id(failed)["repro-add"].reason == "reproduction_still_failing"
    assert _by_id(failed)["repro-add"].signature_seen


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
    assert checks["repro-add"].status is CheckStatus.INDETERMINATE
    assert checks["repro-add"].reason == "failure_signature_absent"
    # Preservation has no signature: its non-zero exit is a failure.
    assert checks["preserve-zero"].reason == "preservation_failed"
    assert result.verdict is CandidateVerdict.FAIL
