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
                        and _package_uses_default_discovery(init_file)
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
            # Let Python honor coding cookies and UTF-8 BOMs just as its
            # module loader does, rather than pre-decoding every file as UTF-8.
            tree = ast.parse(path.read_bytes())
        except (OSError, UnicodeError, SyntaxError):
            continue
        if _module_has_unittest_test(tree):
            found_test_case = True
            break
    return {"test": "python3 -m unittest discover -s tests"} if found_test_case else None


def _package_uses_default_discovery(init_file: Path) -> bool:
    """A package load_tests hook replaces discovery of its descendants."""
    try:
        tree = ast.parse(init_file.read_bytes())
    except (OSError, UnicodeError, SyntaxError):
        return False
    return not _module_binds_load_tests(tree)


def _module_binds_load_tests(tree: ast.Module) -> bool:
    """Find loader hooks without importing the project modules."""
    return any("load_tests" in _module_scope_writes(statement) for statement in tree.body)


def _module_has_unittest_test(tree: ast.Module) -> bool:
    """Find a unittest test class while honoring statically visible module writes."""
    if _module_binds_load_tests(tree):
        # unittest hands the entire discovered suite to this arbitrary hook.
        # Visible TestCase methods do not prove the returned suite contains them.
        return False

    imported_testcase_names: set[str] = set()
    unittest_aliases: set[str] = set()
    local_testcase_classes: set[str] = set()
    local_test_methods: dict[str, set[str]] = {}

    for statement in tree.body:
        if isinstance(statement, ast.Import):
            writes = {alias.asname or alias.name.split(".")[0] for alias in statement.names}
            imported_testcase_names.difference_update(writes)
            unittest_aliases.difference_update(writes)
            local_testcase_classes.difference_update(writes)
            for name in writes:
                local_test_methods.pop(name, None)
            unittest_aliases.update(
                alias.asname or "unittest" for alias in statement.names if alias.name == "unittest"
            )
            continue

        if isinstance(statement, ast.ImportFrom):
            writes = {alias.asname or alias.name for alias in statement.names}
            imported_testcase_names.difference_update(writes)
            unittest_aliases.difference_update(writes)
            local_testcase_classes.difference_update(writes)
            for name in writes:
                local_test_methods.pop(name, None)
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
            # Single local inheritance has a known namespace. For multiple or
            # opaque bases, count only methods defined by this class rather than
            # guessing Python's MRO or names supplied by external code.
            inherited_methods: set[str] = set()
            if len(statement.bases) == 1 and isinstance(statement.bases[0], ast.Name):
                inherited_methods = local_test_methods.get(statement.bases[0].id, set())
            method_names = _class_test_methods(statement, inherited_methods)
            imported_testcase_names.discard(statement.name)
            unittest_aliases.discard(statement.name)
            local_testcase_classes.discard(statement.name)
            local_test_methods.pop(statement.name, None)
            if known_testcase:
                local_testcase_classes.add(statement.name)
                local_test_methods[statement.name] = method_names
            continue

        if _module_scope_test_method_mutation(statement, local_testcase_classes):
            # The class still exists, but a module-level attribute write/delete
            # can remove its only discoverable method. Do not infer runtime
            # unittest discovery from a class namespace we have seen mutated.
            return False

        writes = _module_scope_writes(statement)
        if _UNITTEST_BASE_MUTATION in writes:
            unittest_aliases.clear()
        imported_testcase_names.difference_update(writes)
        unittest_aliases.difference_update(writes)
        local_testcase_classes.difference_update(writes)
        for name in writes:
            local_test_methods.pop(name, None)

    return any(local_test_methods.values())


def _module_scope_test_method_mutation(statement: ast.stmt, testcase_classes: set[str]) -> bool:
    """Detect direct module-scope writes/deletes to known test-class methods."""
    if not testcase_classes:
        return False

    class MutationCollector(ast.NodeVisitor):
        found = False

        def visit_Attribute(self, node: ast.Attribute) -> None:
            if (
                isinstance(node.ctx, (ast.Store, ast.Del))
                and node.attr.startswith("test")
                and isinstance(node.value, ast.Name)
                and node.value.id in testcase_classes
            ):
                self.found = True
            self.generic_visit(node)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            return

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            return

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            return

        def visit_Lambda(self, node: ast.Lambda) -> None:
            return

    collector = MutationCollector()
    collector.visit(statement)
    return collector.found


def _class_test_methods(node: ast.ClassDef, inherited_methods: set[str]) -> set[str]:
    """Known callable test names after inherited names and local shadowing."""
    if node.decorator_list:
        return set()

    method_names = inherited_methods.copy()
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
    return method_names


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
