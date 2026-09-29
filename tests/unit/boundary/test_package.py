"""Check package format: hashing, Seed linkage, worker view, leak detection."""

from __future__ import annotations

import os
from pathlib import Path
import sys

from pydantic import ValidationError
import pytest

from ouroboros.boundary.package import (
    AssertionLink,
    CheckPackageError,
    CheckRole,
    CheckSpec,
    PackageFile,
    find_text_leaks,
    find_workspace_leaks,
    package_record_bytes,
    publish_exact,
    seal_package,
    seed_criterion_keys,
    seed_digest,
    validate_package_for_seed,
    worker_criteria,
    write_package_record,
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


def test_a_package_with_the_seed_keys_in_another_order_is_refused(seed, package) -> None:
    # Criterion order is the Seed's: decisions are indexed by position, so the
    # same keys in another order name other criteria.
    keys = seed_criterion_keys(seed)
    reordered = package.model_copy(update={"criterion_keys": (keys[1], keys[0], *keys[2:])})
    assert set(reordered.criterion_keys) == set(keys)
    with pytest.raises(CheckPackageError, match="criterion keys"):
        validate_package_for_seed(reordered, seed)


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
    assert summary["package_id"] == package.package_id
    assert package.sha256 not in text  # never the unkeyed package digest
    assert SIGNATURE not in text
    assert "probe/test_add.py" not in text  # a path the constructor chose is not recorded
    assert PY not in text  # nor is argv
    # Files by kind only: no digest or size of bytes the constructor wrote.
    assert summary["files"] == [{"kind": "generated"}] * len(package.files)


def test_a_package_is_persisted_only_as_its_sealed_record(tmp_path: Path, seed) -> None:
    with pytest.raises(CheckPackageError, match="not sealed"):
        write_package_record(build_package(seed), tmp_path)
    sealed = seal_package(build_package(seed))
    stored = write_package_record(sealed, tmp_path)
    assert stored.name == f"{sealed.package_id}.json"
    assert stored.read_bytes() == package_record_bytes(sealed)
    assert write_package_record(sealed, tmp_path) == stored  # the same bytes: confirmed


def test_a_file_planted_at_a_record_target_never_stands_in_for_the_record(
    tmp_path: Path, seed
) -> None:
    sealed = seal_package(build_package(seed))
    target = tmp_path / f"{sealed.package_id}.json"
    target.write_bytes(b"corrupt")
    with pytest.raises(CheckPackageError, match="different file"):
        write_package_record(sealed, tmp_path)
    assert target.read_bytes() == b"corrupt"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_publication_never_goes_through_a_link(tmp_path: Path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"{}")
    (tmp_path / "store").mkdir()
    link = tmp_path / "store" / "target.json"
    link.symlink_to(outside)
    with pytest.raises(CheckPackageError):
        publish_exact(link, b"{}")  # identical bytes, but only through a link
    dangling = tmp_path / "store" / "dangling.json"
    dangling.symlink_to(tmp_path / "missing.json")
    with pytest.raises(CheckPackageError):
        publish_exact(dangling, b"{}")
    assert not (tmp_path / "missing.json").exists()


def test_publication_creates_once_and_confirms_identical_bytes(tmp_path: Path) -> None:
    target = tmp_path / "store" / "record.json"
    assert publish_exact(target, b"abc") == target
    assert publish_exact(target, b"abc") == target
    with pytest.raises(CheckPackageError, match="different file"):
        publish_exact(target, b"abd")
    assert target.read_bytes() == b"abc"


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
    # A manifest summary (as recorded in the journal) carries no path, digest
    # or size to scan for: it is refused, never scanned for nothing.
    with pytest.raises(CheckPackageError, match="live"):
        find_workspace_leaks(workspace, [package.manifest_summary()])  # type: ignore[list-item]


def test_text_leaks(package) -> None:
    assert find_text_leaks("criterion: add(a, b) returns a + b", [package]) == ()
    assert find_text_leaks("bundle\n" + REPRO_SCRIPT, [package]) == ("probe/test_add.py",)


# ----------------------------------------------------------------------
# The workspace scan resolves links and refuses what it cannot clear


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "calc.py").write_text("x = 1\n")
    return workspace


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX links")
def test_a_link_to_package_material_is_a_leak(tmp_path: Path, package) -> None:
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "copy.py").write_text(REPRO_SCRIPT)
    (workspace / "innocent.txt").symlink_to(outside / "copy.py")
    assert find_workspace_leaks(workspace, [package]) == ("innocent.txt",)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX links")
def test_a_linked_directory_outside_the_workspace_is_scanned_through(
    tmp_path: Path, package
) -> None:
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside" / "deep"
    outside.mkdir(parents=True)
    (outside / "copy.py").write_text(REPRO_SCRIPT)
    (workspace / "vendor").symlink_to(tmp_path / "outside", target_is_directory=True)
    # A link loop and a link back into the workspace are not followed twice.
    (outside / "loop").symlink_to(tmp_path / "outside", target_is_directory=True)
    (workspace / "self").symlink_to(workspace, target_is_directory=True)
    assert find_workspace_leaks(workspace, [package]) == ("vendor/deep/copy.py",)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX links")
def test_dangling_and_special_links_carry_nothing(tmp_path: Path, package) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "dangling").symlink_to(tmp_path / "missing")
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    (workspace / "pipe").symlink_to(fifo)
    os.mkfifo(workspace / "local_pipe")
    assert find_workspace_leaks(workspace, [package]) == ()


def test_generated_check_files_hidden_in_git_metadata_are_found(tmp_path: Path, package) -> None:
    # Version-control metadata is workspace content too: a renamed copy of a
    # generated check under .git is a leak like anywhere else.
    workspace = _workspace(tmp_path)
    (workspace / ".git" / "hooks").mkdir(parents=True)
    (workspace / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (workspace / ".git" / "hooks" / "pre-commit").write_text(REPRO_SCRIPT)
    assert find_workspace_leaks(workspace, [package]) == (".git/hooks/pre-commit",)


def test_a_hard_link_to_package_material_is_a_leak(tmp_path: Path, package) -> None:
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text(REPRO_SCRIPT)
    os.link(outside, workspace / "linked.py")
    assert find_workspace_leaks(workspace, [package]) == ("linked.py",)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX links")
def test_what_cannot_be_read_refuses_the_start(
    tmp_path: Path, package, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A file of a package file's size that cannot be read, reached directly or
    # through a link, cannot be cleared (injected so it holds as root too).
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("y" * len(REPRO_SCRIPT.encode()))
    (workspace / "sized.py").write_text("z" * len(REPRO_SCRIPT.encode()))
    (workspace / "through").symlink_to(outside)
    real_open = open

    def refusing(file: object, *args: object, **kwargs: object) -> object:
        if str(file).endswith(("sized.py", "through")):
            raise PermissionError(str(file))
        return real_open(file, *args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr("builtins.open", refusing)
    assert find_workspace_leaks(workspace, [package]) == ("sized.py", "through")


def test_a_scan_over_its_budget_refuses_the_start(
    tmp_path: Path, package, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ouroboros.boundary.package as package_module

    workspace = _workspace(tmp_path)
    for index in range(5):
        (workspace / f"f{index}.txt").write_text("x\n")
    monkeypatch.setattr(package_module, "_MAX_SCAN_ENTRIES", 3)
    assert find_workspace_leaks(workspace, [package]) != ()


def test_a_copy_of_the_product_harness_is_not_a_leak(tmp_path: Path, seed) -> None:
    from ouroboros.boundary.oracle import ORACLE_HARNESS_SOURCE
    from ouroboros.boundary.oracle_build import package_from_reply

    from .clamp_fixtures import GOOD_REPRO_1
    from .clamp_fixtures import _seed as clamp_seed

    oracle_package = package_from_reply(
        {"oracles": [GOOD_REPRO_1]}, clamp_seed(), input_digest="1" * 64, generator="t"
    )
    workspace = _workspace(tmp_path)
    # Product code (for example this repository's own source tree): not generated.
    (workspace / "harness.py").write_text(ORACLE_HARNESS_SOURCE)
    assert find_workspace_leaks(workspace, [oracle_package]) == ()
