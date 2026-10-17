"""Bounded deterministic discovery for standard-library unittest projects."""

from __future__ import annotations

import ast
from collections.abc import Callable
import os
from pathlib import Path
from typing import Any

_MAX_UNITTEST_DISCOVERY_ENTRIES = 512
_MAX_UNITTEST_DISCOVERY_DEPTH = 12


def manifest_free_unittest_proposal(working_dir: Path) -> dict[str, str] | None:
    """Return the safe Stage 1 command for a discoverable stdlib test suite.

    This follows the same importable-package boundary as ``unittest discover``:
    it checks ``tests/test*.py`` and recurses into subdirectories containing
    ``__init__.py``. Symlinks are never followed, and bounded entry/depth
    limits make an unusually large package tree fail closed. A filename alone
    is not enough: unittest exits successfully when discovery finds zero
    tests, so the source is parsed to confirm a real TestCase test method.
    """
    tests_dir = working_dir / "tests"
    if tests_dir.is_symlink() or not tests_dir.is_dir():
        return None

    test_files: list[Path] = []
    pending: list[tuple[Path, int]] = [(tests_dir, 0)]
    examined_entries = 0
    while pending:
        directory, depth = pending.pop()
        try:
            with os.scandir(directory) as entries:
                children = []
                for entry in entries:
                    examined_entries += 1
                    if examined_entries > _MAX_UNITTEST_DISCOVERY_ENTRIES:
                        return None
                    children.append(entry)
        except OSError:
            return None

        for entry in sorted(children, key=lambda child: child.name):
            path = Path(entry.path)
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    init_file = path / "__init__.py"
                    if init_file.is_file() and not init_file.is_symlink():
                        next_depth = depth + 1
                        if next_depth > _MAX_UNITTEST_DISCOVERY_DEPTH:
                            return None
                        pending.append((path, next_depth))
                elif (
                    entry.is_file(follow_symlinks=False)
                    and entry.name.startswith("test")
                    and entry.name.endswith(".py")
                ):
                    test_files.append(path)
            except OSError:
                return None

    found_test_case = False
    for path in test_files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, SyntaxError):
            continue
        imported_testcase_names: set[str] = set()
        unittest_aliases = {"unittest"}
        local_testcase_classes: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "unittest":
                        unittest_aliases.add(alias.asname or "unittest")
            elif isinstance(node, ast.ImportFrom) and node.module == "unittest":
                for alias in node.names:
                    if alias.name in {"TestCase", "IsolatedAsyncioTestCase"}:
                        imported_testcase_names.add(alias.asname or alias.name)
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            known_testcase = any(
                (isinstance(base, ast.Name) and base.id in imported_testcase_names)
                or (
                    isinstance(base, ast.Attribute)
                    and base.attr in {"TestCase", "IsolatedAsyncioTestCase"}
                    and isinstance(base.value, ast.Name)
                    and base.value.id in unittest_aliases
                )
                or (isinstance(base, ast.Name) and base.id in local_testcase_classes)
                for base in node.bases
            )
            if known_testcase:
                if any(
                    isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and member.name.startswith("test")
                    for member in node.body
                ):
                    found_test_case = True
                local_testcase_classes.add(node.name)
    return {"test": "python3 -m unittest discover -s tests"} if found_test_case else None


def persist_manifest_free_unittest(
    working_dir: Path,
    target: Path,
    validate_proposal: Callable[[Path, dict[str, str]], Any],
    persist_toml: Callable[[Path, Any], bool],
    log: Any,
) -> bool:
    """Discover, validate, and persist a manifest-free unittest command."""
    proposal = manifest_free_unittest_proposal(working_dir)
    if proposal is None:
        log.info(
            "detector.skipped", reason="no_manifests_or_unittest", working_dir=str(working_dir)
        )
        return False
    validated = validate_proposal(working_dir, proposal)
    if validated.test is None:
        log.info(
            "detector.skipped", reason="unittest_command_invalid", working_dir=str(working_dir)
        )
        return False
    return persist_toml(target, validated)
