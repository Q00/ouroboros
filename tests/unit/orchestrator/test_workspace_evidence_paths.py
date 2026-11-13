"""Equivalence and cost tests for the Git-ignored workspace path lookup."""

from __future__ import annotations

from collections.abc import Iterator
import itertools
from pathlib import Path, PurePosixPath
import subprocess

import pytest

from ouroboros.orchestrator import workspace_evidence_paths
from ouroboros.orchestrator.parallel_executor import (
    _WORKSPACE_FINGERPRINT_IGNORED_DIRECTORIES,
    ParallelACExecutor,
)
from ouroboros.orchestrator.workspace_evidence_paths import (
    collect_bounded_workspace_paths,
    is_git_ignored_path,
    load_ignored_workspace_paths,
    load_tracked_workspace_paths,
)


def _reference_is_git_ignored_path(
    relative: Path, *, ignored_paths: frozenset[Path] | None
) -> bool:
    """The previous implementation: compare against every ignored path."""
    return bool(
        ignored_paths is not None
        and relative.parts
        and any(relative == ignored or ignored in relative.parents for ignored in ignored_paths)
    )


class _UniterableFrozenset(frozenset):
    """A frozenset whose iteration fails, so any full scan of it is observable."""

    def __iter__(self) -> Iterator[Path]:
        raise AssertionError("ignored_paths was scanned entry by entry")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


_CASES = [
    pytest.param(Path("target"), frozenset({Path("target")}), True, id="equal-path"),
    pytest.param(
        Path("target/debug/app.bin"), frozenset({Path("target")}), True, id="nested-child"
    ),
    pytest.param(
        Path("pkg/build/o"), frozenset({Path("pkg/build")}), True, id="nested-ignored-dir"
    ),
    pytest.param(Path("pkg"), frozenset({Path("pkg/build")}), False, id="ancestor-of-ignored"),
    pytest.param(Path("ab"), frozenset({Path("a")}), False, id="sibling-name-prefix"),
    pytest.param(Path("ab/c"), frozenset({Path("a")}), False, id="sibling-prefix-child"),
    pytest.param(Path("a"), frozenset({Path("ab")}), False, id="shorter-sibling"),
    pytest.param(Path("."), frozenset({Path(".")}), False, id="dot-relative-dot-ignored"),
    pytest.param(Path("."), frozenset({Path("a")}), False, id="dot-relative"),
    pytest.param(Path("a"), frozenset({Path(".")}), True, id="dot-ignored-is-every-parent"),
    pytest.param(Path("a"), frozenset(), False, id="empty-set"),
    pytest.param(Path("a"), None, False, id="none"),
    pytest.param(
        Path("src/gen/x.py"),
        frozenset({Path("dist"), Path("src/gen"), Path("x.log")}),
        True,
        id="one-of-many",
    ),
    pytest.param(Path("target/x"), frozenset({Path("./target/")}), True, id="normalized-entry"),
    pytest.param(Path("Target/x"), frozenset({Path("target")}), False, id="case-differs"),
    pytest.param(Path("a/b"), frozenset({PurePosixPath("a")}), True, id="pure-posix-entry"),
    pytest.param(Path("a/b"), frozenset({"a"}), False, id="str-entry-never-equal"),
]


@pytest.mark.parametrize(("relative", "ignored_paths", "expected"), _CASES)
def test_ignored_lookup_matches_previous_scan(
    relative: Path, ignored_paths: frozenset[Path] | None, expected: bool
) -> None:
    reference = _reference_is_git_ignored_path(relative, ignored_paths=ignored_paths)

    assert reference is expected
    assert is_git_ignored_path(relative, ignored_paths=ignored_paths) is reference


def test_ignored_lookup_matches_previous_scan_on_generated_inputs() -> None:
    names = ("a", "ab", "b")
    paths = [
        Path(*parts) for depth in range(1, 4) for parts in itertools.product(names, repeat=depth)
    ]
    pool = [Path("."), *paths]
    ignored_sets = [
        frozenset(combination)
        for size in range(3)
        for combination in itertools.combinations(pool, size)
    ]
    compared = 0
    for ignored_paths in ignored_sets:
        for relative in [Path("."), *paths]:
            assert is_git_ignored_path(
                relative, ignored_paths=ignored_paths
            ) is _reference_is_git_ignored_path(relative, ignored_paths=ignored_paths)
            compared += 1
    assert compared == len(ignored_sets) * (len(paths) + 1)


def test_ignored_lookup_does_not_scan_the_ignored_set() -> None:
    ignored_paths = _UniterableFrozenset({Path("target"), Path("pkg/build")})

    with pytest.raises(AssertionError, match="scanned"):
        _reference_is_git_ignored_path(Path("src/app.py"), ignored_paths=ignored_paths)

    assert is_git_ignored_path(Path("target"), ignored_paths=ignored_paths) is True
    assert is_git_ignored_path(Path("pkg/build/o/x"), ignored_paths=ignored_paths) is True
    assert is_git_ignored_path(Path("src/app.py"), ignored_paths=ignored_paths) is False
    assert is_git_ignored_path(Path("."), ignored_paths=ignored_paths) is False


def _ignored_workspace(root: Path) -> None:
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test User")
    (root / ".gitignore").write_text("target/\n*.log\nbuild/\n/a\n", encoding="utf-8")
    for package in ("pkg1", "pkg2"):
        source = root / "src" / package
        source.mkdir(parents=True)
        (source / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
        (source / "debug.log").write_text("log\n", encoding="utf-8")
        (source / "build").mkdir()
        (source / "build" / "out.o").write_bytes(b"\x00")
    (root / "target" / "debug").mkdir(parents=True)
    (root / "target" / "debug" / "app.bin").write_bytes(b"\x01")
    (root / "targetx").mkdir()
    (root / "targetx" / "kept.py").write_text("KEPT = 1\n", encoding="utf-8")
    (root / "a").mkdir()
    (root / "a" / "ignored.txt").write_text("ignored\n", encoding="utf-8")
    (root / "ab").mkdir()
    (root / "ab" / "kept.txt").write_text("kept\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "initial")


def test_collection_does_not_scan_the_ignored_set_per_entry(tmp_path: Path) -> None:
    _ignored_workspace(tmp_path)
    ignored_paths = load_ignored_workspace_paths(tmp_path)
    assert ignored_paths is not None and len(ignored_paths) >= 6

    def collect(paths: frozenset[Path]) -> list[Path] | None:
        return collect_bounded_workspace_paths(
            tmp_path,
            declared_paths=set(),
            tracked_paths=load_tracked_workspace_paths(tmp_path),
            ignored_paths=paths,
            ignored_directory_names=_WORKSPACE_FINGERPRINT_IGNORED_DIRECTORIES,
            max_entries=10_000,
            max_depth=16,
        )

    expected = collect(ignored_paths)
    collected = collect(_UniterableFrozenset(ignored_paths))

    assert collected == expected
    relative = {path.relative_to(tmp_path).as_posix() for path in collected or ()}
    assert {"ab/kept.txt", "targetx/kept.py", "src/pkg1/module.py"} <= relative
    assert not relative & {"a", "target", "src/pkg1/build", "src/pkg2/debug.log"}


def test_workspace_digest_is_unchanged_from_previous_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ignored_workspace(tmp_path)
    digest = ParallelACExecutor._workspace_content_digest(str(tmp_path))
    declared = ParallelACExecutor._workspace_content_digest(
        str(tmp_path), expected_artifacts=("target/debug/app.bin",)
    )

    monkeypatch.setattr(
        workspace_evidence_paths, "is_git_ignored_path", _reference_is_git_ignored_path
    )
    reference_digest = ParallelACExecutor._workspace_content_digest(str(tmp_path))
    reference_declared = ParallelACExecutor._workspace_content_digest(
        str(tmp_path), expected_artifacts=("target/debug/app.bin",)
    )

    assert digest is not None
    assert digest == reference_digest
    assert declared == reference_declared
    assert declared != digest
