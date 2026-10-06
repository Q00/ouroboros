"""Controller-written code that runs under the task's own interpreter parses for Python 3.6.

The oracle harness and the footprint programs are executed by the task
image's ``python3`` (``python -I -B -c <source>``), which on SWE-bench images
of older repositories is Python 3.6. CI has no 3.6, so these checks are
static: the source must parse with the 3.6 grammar, and no annotation that
Python evaluates (signatures, module and class level) may use a form that
needs 3.7 or later. The runtime proof on a real 3.6 interpreter is in the
pull request that added this file.
"""

import ast

import pytest

from ouroboros.boundary.footprint import PYTEST_BOOTSTRAP, oracle_program
from ouroboros.boundary.oracle import ORACLE_HARNESS_SOURCE

SOURCES = {
    "oracle_harness": ORACLE_HARNESS_SOURCE,
    "pytest_bootstrap": PYTEST_BOOTSTRAP,
    "oracle_program": oracle_program(ORACLE_HARNESS_SOURCE),
}
BUILTIN_GENERICS = frozenset({"list", "dict", "tuple", "set", "frozenset", "type"})


def _evaluated_annotations(tree: ast.Module) -> list[ast.expr]:
    """Every annotation Python evaluates at run time (local variable annotations are not)."""
    found: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            arguments = node.args
            for arg in (
                *arguments.posonlyargs,
                *arguments.args,
                *arguments.kwonlyargs,
                *(a for a in (arguments.vararg, arguments.kwarg) if a is not None),
            ):
                if arg.annotation is not None:
                    found.append(arg.annotation)
            if node.returns is not None:
                found.append(node.returns)
    for scope in [tree, *(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef))]:
        for statement in scope.body:
            if isinstance(statement, ast.AnnAssign):
                found.append(statement.annotation)
    return found


def _newer_forms(annotation: ast.expr) -> list[str]:
    """``X | Y`` and subscripted builtins inside ``annotation`` (both fail on 3.6)."""
    problems = []
    for node in ast.walk(annotation):
        union = isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr)
        generic = (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id in BUILTIN_GENERICS
        )
        if union or generic:
            problems.append(ast.unparse(node))
    return problems


@pytest.mark.parametrize("name", sorted(SOURCES))
def test_source_parses_with_the_python_36_grammar(name: str) -> None:
    ast.parse(SOURCES[name], feature_version=(3, 6))


@pytest.mark.parametrize("name", sorted(SOURCES))
def test_no_future_annotations_import(name: str) -> None:
    for node in ast.walk(ast.parse(SOURCES[name])):
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            assert "annotations" not in {alias.name for alias in node.names}


@pytest.mark.parametrize("name", sorted(SOURCES))
def test_no_evaluated_annotation_needs_python_37(name: str) -> None:
    tree = ast.parse(SOURCES[name])
    problems = [form for item in _evaluated_annotations(tree) for form in _newer_forms(item)]
    assert problems == []


def test_the_annotation_check_sees_signatures_and_class_bodies() -> None:
    tree = ast.parse(
        "def f(a: int | None, *rest: list[int]) -> dict[str, int]:\n"
        "    def g(b: tuple[int, ...]) -> None:\n"
        "        local: list[int] = []\n"
        "class C:\n"
        "    field: set[int]\n"
        "top: type[C]\n"
    )
    problems = [form for item in _evaluated_annotations(tree) for form in _newer_forms(item)]
    assert sorted(problems) == sorted(
        ["int | None", "list[int]", "dict[str, int]", "tuple[int, ...]", "set[int]", "type[C]"]
    )
