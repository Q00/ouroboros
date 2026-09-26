"""Check package format: hashing, Seed linkage, worker view, leak detection."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError
import pytest

from ouroboros.boundary import (
    AssertionLink,
    CheckPackageError,
    CheckRole,
    CheckSpec,
    PackageFile,
    find_text_leaks,
    find_workspace_leaks,
    load_check_package,
    seed_criterion_keys,
    seed_digest,
    validate_package_for_seed,
    worker_criteria,
    write_check_package,
)

from .conftest import PY, REPRO_SCRIPT, SIGNATURE, build_package, make_seed


def test_package_digest_is_deterministic_and_content_sensitive(seed, package) -> None:
    assert package.sha256 == build_package(seed).sha256
    changed = build_package(seed, repro_script=REPRO_SCRIPT + "# changed\n")
    assert changed.sha256 != package.sha256


def test_seed_digest_is_stable_and_package_is_linked(seed, package) -> None:
    assert seed_digest(seed) == seed_digest(make_seed())
    assert seed_digest(seed) == seed_digest(type(seed).from_dict(seed.to_dict()))
    assert package.seed_digest == seed_digest(seed)
    validate_package_for_seed(package, seed)


def test_package_for_another_seed_is_refused(package) -> None:
    other = make_seed().model_copy(update={"goal": "a different goal"})
    with pytest.raises(CheckPackageError, match="seed_digest"):
        validate_package_for_seed(package, other)


def test_file_content_must_match_digest() -> None:
    with pytest.raises(ValidationError, match="digest mismatch"):
        PackageFile(path="probe/x.py", sha256="0" * 64, content="print(1)\n")


@pytest.mark.parametrize("path", ["/abs/x.py", "../x.py", "a/../x.py", "a\\x.py", "C:/x.py"])
def test_file_paths_must_stay_inside_checkout(path: str) -> None:
    with pytest.raises(ValidationError):
        PackageFile.from_content(path, "x")


def test_reproduction_requires_failure_signature(seed) -> None:
    key = seed_criterion_keys(seed)[0]
    with pytest.raises(ValidationError, match="failure_signature"):
        CheckSpec(
            check_id="r",
            role=CheckRole.REPRODUCTION,
            argv=(PY, "x.py"),
            assertions=(AssertionLink(assertion_id="a", criterion_key=key),),
        )


def test_every_criterion_is_linked_or_uncovered(seed) -> None:
    with pytest.raises(ValidationError, match="linked to an assertion or listed as uncovered"):
        build_package(seed, uncovered=())


def test_assertion_cannot_link_unknown_criterion(seed) -> None:
    bad = CheckSpec(
        check_id="x",
        role=CheckRole.PRESERVATION,
        argv=(PY, "x.py"),
        assertions=(AssertionLink(assertion_id="a", criterion_key="ac_ffffffffffffffff"),),
    )
    with pytest.raises(ValidationError, match="unknown criterion"):
        build_package(seed, checks=(bad,))


def test_manifest_summary_excludes_check_code(package) -> None:
    summary = package.manifest_summary()
    text = repr(summary)
    assert summary["package_sha256"] == package.sha256
    assert SIGNATURE not in text
    assert "probe/test_add.py" in text  # path and digest are recorded
    assert PY not in text  # argv is not
    assert {f["sha256"] for f in summary["files"]} == {f.sha256 for f in package.files}


def test_write_and_load_round_trip_is_create_only(tmp_path: Path, package) -> None:
    stored = write_check_package(package, tmp_path)
    assert stored.name == f"{package.sha256}.json"
    assert write_check_package(package, tmp_path) == stored
    loaded = load_check_package(stored, expected_sha256=package.sha256)
    assert loaded == package
    with pytest.raises(CheckPackageError):
        load_check_package(stored, expected_sha256="0" * 64)


def test_worker_criteria_carry_descriptions_only(seed) -> None:
    view = worker_criteria(seed)
    assert [c.description for c in view] == [
        "add(a, b) returns a + b",
        "add(0, 0) keeps returning 0",
        "the change keeps the public signature",
    ]
    assert tuple(c.criterion_key for c in view) == seed_criterion_keys(seed)
    assert "verify_command" not in repr(view)
    assert "probe/test_add.py" not in repr(view)


def test_workspace_leaks_by_path_and_by_renamed_content(tmp_path: Path, package) -> None:
    workspace = tmp_path / "ws"
    (workspace / "probe").mkdir(parents=True)
    (workspace / "calc.py").write_text("x = 1\n")
    assert find_workspace_leaks(workspace, [package]) == ()
    (workspace / "probe" / "test_add.py").write_text("unrelated\n")
    (workspace / "renamed.py").write_text(REPRO_SCRIPT)
    assert find_workspace_leaks(workspace, [package]) == ("probe/test_add.py", "renamed.py")
    # A manifest summary (as recorded in the journal) is enough to detect leaks.
    summary = package.manifest_summary()
    assert find_workspace_leaks(workspace, [summary]) == ("probe/test_add.py", "renamed.py")


def test_text_leaks(package) -> None:
    assert find_text_leaks("criterion: add(a, b) returns a + b", [package]) == ()
    assert find_text_leaks("bundle\n" + REPRO_SCRIPT, [package]) == ("probe/test_add.py",)
