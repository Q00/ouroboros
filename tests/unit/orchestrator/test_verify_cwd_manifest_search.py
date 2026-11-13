"""Equivalence and cost tests for the sole-Node-manifest search behind verify_cwd."""

from __future__ import annotations

from collections.abc import Callable
import ntpath
import os
from pathlib import Path
import sys
from typing import Any

import pytest

from ouroboros.core.seed import AcceptanceCriterionSpec
from ouroboros.orchestrator import verify_cwd
from ouroboros.orchestrator.verify_cwd import (
    _IGNORED_MANIFEST_DIRECTORIES,
    _sole_node_manifest_directory,
    resolve_verify_command_cwd,
)


def _reference_sole_node_manifest_directory(root: Path) -> Path | None:
    """The previous implementation: ``rglob`` everything, discard ignored matches."""
    if (root / "package.json").is_file():
        return None
    candidates: list[Path] = []
    for manifest in root.rglob("package.json"):
        if (
            any(part in _IGNORED_MANIFEST_DIRECTORIES for part in manifest.relative_to(root).parts)
            or not manifest.is_file()
        ):
            continue
        candidates.append(manifest.parent)
        if len(candidates) > 1:
            return None
    return candidates[0] if candidates else None


def _as_resolved(search: Callable[[Path], Path | None], root: Path) -> Path | None:
    """What ``resolve_verify_command_cwd`` observes: an OSError means no inference."""
    try:
        return search(root)
    except OSError:
        return None


def _workspace(tmp_path: Path) -> Path:
    """A dedicated root: fixtures may leave their own entries in ``tmp_path``."""
    root = (tmp_path / "workspace").resolve()
    root.mkdir()
    return root


def _manifest(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")


def _outside(root: Path) -> Path:
    outside = root.parent / f"{root.name}-outside"
    outside.mkdir(exist_ok=True)
    return outside


def _sole_nested(root: Path) -> None:
    _manifest(root / "web" / "package.json")
    (root / "web" / "src").mkdir()
    (root / "web" / "src" / "index.js").write_text("", encoding="utf-8")


def _nested_node_modules(root: Path) -> None:
    _manifest(root / "web" / "package.json")
    _manifest(root / "web" / "node_modules" / "left-pad" / "package.json")
    _manifest(root / "web" / "node_modules" / "a" / "node_modules" / "b" / "package.json")
    _manifest(root / "node_modules" / "top" / "package.json")


def _only_in_ignored_directories(root: Path) -> None:
    for ignored in sorted(_IGNORED_MANIFEST_DIRECTORIES):
        _manifest(root / ignored / "package.json")
        _manifest(root / "src" / ignored / "deep" / "package.json")


def _ignored_name_in_other_case(root: Path) -> None:
    _manifest(root / "Node_Modules" / "package.json")


def _ignored_directory_deep_and_sibling(root: Path) -> None:
    _manifest(root / "app" / ".venv" / "lib" / "package.json")
    _manifest(root / "app" / "__pycache__" / "package.json")
    _manifest(root / "site" / "package.json")


def _symlinked_directory_to_manifest(root: Path) -> None:
    _manifest(root / "real" / "package.json")
    (root / "linked").symlink_to(root / "real", target_is_directory=True)


def _manifest_only_through_symlinked_directory(root: Path) -> None:
    outside = _outside(root)
    _manifest(outside / "web" / "package.json")
    (root / "linked").symlink_to(outside, target_is_directory=True)
    (root / "linked-web").symlink_to(outside / "web", target_is_directory=True)


def _symlinked_ignored_directory(root: Path) -> None:
    outside = _outside(root)
    _manifest(outside / "pkg" / "package.json")
    (root / "node_modules").symlink_to(outside, target_is_directory=True)
    _manifest(root / "web" / "package.json")


def _two_sibling_candidates(root: Path) -> None:
    _manifest(root / "app" / "package.json")
    _manifest(root / "site" / "package.json")


def _two_nested_candidates(root: Path) -> None:
    _manifest(root / "app" / "package.json")
    _manifest(root / "app" / "packages" / "ui" / "package.json")


def _root_manifest_present(root: Path) -> None:
    _manifest(root / "package.json")
    _manifest(root / "web" / "package.json")


def _root_manifest_is_a_directory(root: Path) -> None:
    (root / "package.json").mkdir()
    _manifest(root / "web" / "package.json")


def _directory_named_package_json(root: Path) -> None:
    (root / "web" / "package.json").mkdir(parents=True)
    _manifest(root / "app" / "package.json")


def _manifest_inside_directory_named_package_json(root: Path) -> None:
    _manifest(root / "web" / "package.json" / "package.json")


def _symlinked_manifest_file(root: Path) -> None:
    (root / "manifest.json").write_text("{}", encoding="utf-8")
    (root / "web").mkdir()
    (root / "web" / "package.json").symlink_to(root / "manifest.json")


def _broken_symlink_manifest(root: Path) -> None:
    (root / "web").mkdir()
    (root / "web" / "package.json").symlink_to(root / "missing.json")
    _manifest(root / "app" / "package.json")


def _deep_sole_manifest(root: Path) -> None:
    _manifest(root / "a" / "b" / "c" / "d" / "e" / "package.json")
    (root / "a" / "b" / "notes.txt").write_text("", encoding="utf-8")


def _no_manifest(root: Path) -> None:
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("", encoding="utf-8")


_TREES = [
    pytest.param(_sole_nested, "web", id="sole-nested"),
    pytest.param(_nested_node_modules, "web", id="nested-node-modules"),
    pytest.param(_only_in_ignored_directories, None, id="only-in-ignored-directories"),
    pytest.param(_ignored_name_in_other_case, "Node_Modules", id="ignored-name-other-case"),
    pytest.param(_ignored_directory_deep_and_sibling, "site", id="ignored-below-project"),
    pytest.param(_symlinked_directory_to_manifest, "real", id="symlinked-dir-in-workspace"),
    pytest.param(_manifest_only_through_symlinked_directory, None, id="symlinked-dir-outside"),
    pytest.param(_symlinked_ignored_directory, "web", id="symlinked-node-modules"),
    pytest.param(_two_sibling_candidates, None, id="two-sibling-candidates"),
    pytest.param(_two_nested_candidates, None, id="two-nested-candidates"),
    pytest.param(_root_manifest_present, None, id="root-manifest-present"),
    pytest.param(_root_manifest_is_a_directory, "web", id="root-manifest-is-directory"),
    pytest.param(_directory_named_package_json, "app", id="directory-named-package-json"),
    pytest.param(
        _manifest_inside_directory_named_package_json,
        "web/package.json",
        id="manifest-inside-directory-named-package-json",
    ),
    pytest.param(_symlinked_manifest_file, "web", id="symlinked-manifest-file"),
    pytest.param(_broken_symlink_manifest, "app", id="broken-symlink-manifest"),
    pytest.param(_deep_sole_manifest, "a/b/c/d/e", id="deep-sole-manifest"),
    pytest.param(_no_manifest, None, id="no-manifest"),
]


@pytest.mark.parametrize(("build", "expected"), _TREES)
def test_manifest_search_matches_previous_rglob(
    tmp_path: Path, build: Callable[[Path], None], expected: str | None
) -> None:
    root = _workspace(tmp_path)
    build(root)

    reference = _as_resolved(_reference_sole_node_manifest_directory, root)
    found = _sole_node_manifest_directory(root)

    assert reference == (None if expected is None else root / expected)
    assert found == reference
    spec = AcceptanceCriterionSpec(description="ac", verify_command="npm test")
    resolved = resolve_verify_command_cwd(str(root), spec)
    assert resolved == (str(root if found is None else found), None)


def test_manifest_name_is_matched_exactly_on_posix(tmp_path: Path) -> None:
    """A case variant is not the manifest, as with Python 3.12's ``rglob``.

    Python 3.13+ ``rglob`` probes the literal name with ``lstat`` instead of
    matching listed names, so on a case-insensitive filesystem (the macOS
    default) it also accepted ``Package.json``. The search pins 3.12's rule.
    """
    root = _workspace(tmp_path)
    _manifest(root / "web" / "Package.json")

    assert _sole_node_manifest_directory(root) is None
    if sys.version_info < (3, 13):
        assert _reference_sole_node_manifest_directory(root) is None


@pytest.mark.skipif(os.name == "nt", reason="simulates the Windows rule on a POSIX host")
def test_manifest_name_folds_case_where_the_platform_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows paths fold case; 3.12's ``rglob`` matched ``Package.JSON`` there."""
    root = _workspace(tmp_path)
    _manifest(root / "web" / "Package.JSON")
    # Populate Python 3.12's cached path-flavour case rule before patching normcase.
    assert Path("A") != Path("a")

    with monkeypatch.context() as patched:
        patched.setattr(verify_cwd.os.path, "normcase", ntpath.normcase)
        found = _sole_node_manifest_directory(root)

    assert found == root / "web"


@pytest.mark.skipif(
    os.name == "nt" or os.geteuid() == 0, reason="needs POSIX permission enforcement"
)
def test_manifest_in_unlistable_directory_is_not_a_candidate(tmp_path: Path) -> None:
    """A directory that cannot be listed contributes no candidate, as in 3.12.

    Python 3.13+ ``rglob`` found it by probing the literal name with ``lstat``.
    """
    root = _workspace(tmp_path)
    _manifest(root / "web" / "package.json")
    (root / "web").chmod(0o311)
    try:
        assert _sole_node_manifest_directory(root) is None
        if sys.version_info < (3, 13):
            assert _reference_sole_node_manifest_directory(root) is None
    finally:
        (root / "web").chmod(0o755)


class _ScandirCounter:
    def __init__(self) -> None:
        self.listed: list[str] = []
        self._scandir = os.scandir

    def __call__(self, path: str | os.PathLike[str] = ".") -> Any:
        self.listed.append(os.fspath(path))
        return self._scandir(path)


def _large_ignored_tree(root: Path) -> None:
    _manifest(root / "web" / "package.json")
    (root / "web" / "src").mkdir()
    for index in range(150):
        package = root / "web" / "node_modules" / f"pkg{index}"
        _manifest(package / "package.json")
        (package / "lib").mkdir()
        (package / "lib" / "index.js").write_text("", encoding="utf-8")
    for index in range(40):
        (root / ".venv" / "lib" / f"mod{index}").mkdir(parents=True)
    for index in range(20):
        (root / ".git" / "objects" / f"{index:02x}").mkdir(parents=True)


def test_manifest_search_lists_only_unignored_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)
    _large_ignored_tree(root)
    counter = _ScandirCounter()
    monkeypatch.setattr(os, "scandir", counter)

    found = _sole_node_manifest_directory(root)
    listed = sorted(counter.listed)

    assert found == _reference_sole_node_manifest_directory(root) == root / "web"
    assert listed == sorted(str(path) for path in (root, root / "web", root / "web" / "src"))


def test_manifest_search_stops_listing_at_the_second_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)
    _manifest(root / "app" / "package.json")
    _manifest(root / "app" / "sub" / "package.json")
    (root / "app" / "sub" / "deeper" / "deepest").mkdir(parents=True)
    counter = _ScandirCounter()
    monkeypatch.setattr(os, "scandir", counter)

    assert _sole_node_manifest_directory(root) is None
    assert counter.listed == [str(root), str(root / "app"), str(root / "app" / "sub")]


def test_manifest_search_does_not_enter_symlinked_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)
    outside = _outside(root)
    (outside / "nested").mkdir()
    _manifest(outside / "nested" / "package.json")
    (root / "linked").symlink_to(outside, target_is_directory=True)
    counter = _ScandirCounter()
    monkeypatch.setattr(os, "scandir", counter)

    assert _sole_node_manifest_directory(root) is None
    assert counter.listed == [str(root)]
