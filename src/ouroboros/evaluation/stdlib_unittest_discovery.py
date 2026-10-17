"""Bounded deterministic discovery for standard-library unittest projects."""

from __future__ import annotations

import ast
from collections.abc import Callable
import fnmatch
import os
from pathlib import Path
from typing import Any

_MAX_UNITTEST_DISCOVERY_ENTRIES = 512
_MAX_UNITTEST_DISCOVERY_DEPTH = 12
_UNITTEST_BASE_MUTATION = "__unittest_base_mutation__"


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
                    if (
                        path.name.isidentifier()
                        and init_file.is_file()
                        and not init_file.is_symlink()
                    ):
                        next_depth = depth + 1
                        if next_depth > _MAX_UNITTEST_DISCOVERY_DEPTH:
                            return None
                        pending.append((path, next_depth))
                elif (
                    entry.is_file(follow_symlinks=False)
                    and fnmatch.fnmatchcase(entry.name, "test*.py")
                    and path.stem.isidentifier()
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
        if _module_has_unittest_test(tree):
            found_test_case = True
            break
    return {"test": "python3 -m unittest discover -s tests"} if found_test_case else None


def _module_has_unittest_test(tree: ast.Module) -> bool:
    """Find a unittest test class while honoring module-level rebinding order."""
    imported_testcase_names: set[str] = set()
    unittest_aliases: set[str] = set()
    local_testcase_classes: set[str] = set()
    local_testcase_classes_with_tests: set[str] = set()

    for statement in tree.body:
        if isinstance(statement, ast.Import):
            writes = {alias.asname or alias.name.split(".")[0] for alias in statement.names}
            imported_testcase_names.difference_update(writes)
            unittest_aliases.difference_update(writes)
            local_testcase_classes.difference_update(writes)
            local_testcase_classes_with_tests.difference_update(writes)
            unittest_aliases.update(
                alias.asname or "unittest" for alias in statement.names if alias.name == "unittest"
            )
            continue

        if isinstance(statement, ast.ImportFrom):
            writes = {alias.asname or alias.name for alias in statement.names}
            imported_testcase_names.difference_update(writes)
            unittest_aliases.difference_update(writes)
            local_testcase_classes.difference_update(writes)
            local_testcase_classes_with_tests.difference_update(writes)
            if statement.module == "unittest" and statement.level == 0:
                imported_testcase_names.update(
                    alias.asname or alias.name
                    for alias in statement.names
                    if alias.name in {"TestCase", "IsolatedAsyncioTestCase"}
                )
            continue

        if isinstance(statement, ast.ClassDef):
            known_testcase = any(
                (isinstance(base, ast.Name) and base.id in imported_testcase_names)
                or (
                    isinstance(base, ast.Attribute)
                    and base.attr in {"TestCase", "IsolatedAsyncioTestCase"}
                    and isinstance(base.value, ast.Name)
                    and base.value.id in unittest_aliases
                )
                or (isinstance(base, ast.Name) and base.id in local_testcase_classes)
                for base in statement.bases
            )
            has_test_method = _class_has_test_method(statement)
            inherited_test_method = any(
                isinstance(base, ast.Name) and base.id in local_testcase_classes_with_tests
                for base in statement.bases
            )
            imported_testcase_names.discard(statement.name)
            unittest_aliases.discard(statement.name)
            local_testcase_classes.discard(statement.name)
            local_testcase_classes_with_tests.discard(statement.name)
            if known_testcase:
                local_testcase_classes.add(statement.name)
                if has_test_method or inherited_test_method:
                    local_testcase_classes_with_tests.add(statement.name)
            continue

        writes = _module_scope_writes(statement)
        if _UNITTEST_BASE_MUTATION in writes:
            unittest_aliases.clear()
        imported_testcase_names.difference_update(writes)
        unittest_aliases.difference_update(writes)
        local_testcase_classes.difference_update(writes)
        local_testcase_classes_with_tests.difference_update(writes)

    return bool(local_testcase_classes_with_tests)


def _class_has_test_method(node: ast.ClassDef) -> bool:
    """Whether a class namespace ends with a callable test-method definition."""
    if node.decorator_list:
        return False

    method_names: set[str] = set()
    for statement in node.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            method_names.discard(statement.name)
            if statement.name.startswith("test") and not any(
                isinstance(decorator, ast.Name)
                and decorator.id == "property"
                or isinstance(decorator, ast.Attribute)
                and decorator.attr == "property"
                for decorator in statement.decorator_list
            ):
                method_names.add(statement.name)
            continue
        writes = _module_scope_writes(statement)
        method_names.difference_update(name for name in writes if name.startswith("test"))
    return bool(method_names)


def _module_scope_writes(statement: ast.stmt) -> set[str]:
    """Find names written by a module statement without entering class/function bodies."""
    if isinstance(statement, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        return {statement.name}

    class WriteCollector(ast.NodeVisitor):
        def __init__(self) -> None:
            self.names: set[str] = set()

        def visit_Name(self, node: ast.Name) -> None:
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                self.names.add(node.id)

        def visit_Attribute(self, node: ast.Attribute) -> None:
            if isinstance(node.ctx, (ast.Store, ast.Del)) and node.attr in {
                "TestCase",
                "IsolatedAsyncioTestCase",
            }:
                self.names.add(_UNITTEST_BASE_MUTATION)
            self.generic_visit(node)

        def visit_Import(self, node: ast.Import) -> None:
            self.names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            self.names.update(alias.asname or alias.name for alias in node.names)

        def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
            if node.name:
                self.names.add(node.name)
            self.generic_visit(node)

        def visit_MatchAs(self, node: ast.MatchAs) -> None:
            if node.name:
                self.names.add(node.name)
            self.generic_visit(node)

        def visit_MatchStar(self, node: ast.MatchStar) -> None:
            if node.name:
                self.names.add(node.name)
            self.generic_visit(node)

        def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
            if node.rest:
                self.names.add(node.rest)
            self.generic_visit(node)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.names.add(node.name)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self.names.add(node.name)

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.names.add(node.name)

        def visit_Lambda(self, node: ast.Lambda) -> None:
            return

        def visit_comprehension(self, node: ast.comprehension) -> None:
            # Comprehension targets live in their own scope in supported Python.
            for expression in (node.iter, *node.ifs):
                self.visit(expression)

    collector = WriteCollector()
    collector.visit(statement)
    return collector.names


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
