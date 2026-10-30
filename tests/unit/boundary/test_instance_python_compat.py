"""Controller-written code that runs under the task's own interpreter parses for Python 3.5.

The oracle harness, the footprint programs and the probe wrapper are run by the task
image's ``python3`` (``python -I -B -c <source>``), which on SWE-bench images
of older repositories is Python 3.6, and for two of them 3.5. CI has neither,
so these checks are static: the source must parse with the 3.5 and 3.6
grammars, hold no f-string or async generator (the 3.5 grammar check of
``ast`` does not reject those), name no builtin newer than 3.5, and no
annotation that Python evaluates (signatures, module and class level) may use
a form that needs 3.7 or later. The runtime proofs on real 3.5 and 3.6
interpreters are in the pull requests that added these checks.
"""

import ast
from pathlib import Path
import sys

import pytest

from ouroboros.boundary import harness
from ouroboros.boundary.footprint import PYTEST_BOOTSTRAP, oracle_program
from ouroboros.boundary.oracle import ORACLE_HARNESS_SOURCE
from ouroboros.examples.probe_runner import PROBE_WRAPPER_SOURCE

SOURCES = {
    "oracle_harness": ORACLE_HARNESS_SOURCE,
    "pytest_bootstrap": PYTEST_BOOTSTRAP,
    "oracle_program": oracle_program(ORACLE_HARNESS_SOURCE),
    "probe_wrapper": PROBE_WRAPPER_SOURCE,
}
BUILTIN_GENERICS = frozenset({"list", "dict", "tuple", "set", "frozenset", "type"})
NEWER_BUILTINS = frozenset(
    {
        "ModuleNotFoundError",
        "breakpoint",
        "aiter",
        "anext",
        "EncodingWarning",
        "ExceptionGroup",
        "BaseExceptionGroup",
    }
)


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
def test_source_parses_with_the_python_35_grammar(name: str) -> None:
    ast.parse(SOURCES[name], feature_version=(3, 5))


def _python_36_forms(tree: ast.Module) -> list[str]:
    """F-strings and async generators: 3.6 syntax that ``feature_version=(3, 5)`` accepts."""
    problems = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            problems.append("f-string at line " + str(node.lineno))
        if isinstance(node, ast.AsyncFunctionDef) and any(
            isinstance(inner, ast.Yield | ast.YieldFrom) for inner in ast.walk(node)
        ):
            problems.append("async generator at line " + str(node.lineno))
        if isinstance(node, ast.Name) and node.id in NEWER_BUILTINS:
            problems.append(node.id + " at line " + str(node.lineno))
    return problems


@pytest.mark.parametrize("name", sorted(SOURCES))
def test_no_form_the_python_35_grammar_check_misses(name: str) -> None:
    assert _python_36_forms(ast.parse(SOURCES[name])) == []


def test_the_python_35_checks_see_each_form() -> None:
    for source in ("held: list[int] = []\n", "x = 1_000\n"):
        with pytest.raises(SyntaxError):
            ast.parse(source, feature_version=(3, 5))
    tree = ast.parse(
        "name = f'{1}'\n"
        "async def g():\n"
        "    yield 1\n"
        "try:\n"
        "    pass\n"
        "except ModuleNotFoundError:\n"
        "    pass\n"
    )
    assert sorted(_python_36_forms(tree)) == sorted(
        ["f-string at line 1", "async generator at line 2", "ModuleNotFoundError at line 6"]
    )


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


# What Python 3.5 raises (type ``ImportError`` throughout), observed on
# python:3.5-slim, and whether 3.6 raises the same failure as
# ``ModuleNotFoundError``.
PYTHON_35_IMPORT_FAILURES = [
    ("absent", "No module named 'absent'", True),
    ("pkg.absent", "No module named 'pkg.absent'", True),
    ("pkg.mod.x", "No module named 'pkg.mod.x'; 'pkg.mod' is not a package", True),
    ("pkg.blocked", "import of pkg.blocked halted; None in sys.modules", True),
    ("pkg.blocked", "import of 'pkg.blocked' halted; None in sys.modules", True),
    (None, "cannot import name 'missing_dependency'", False),
    (None, "boom", False),
    ("pkg.m_raise_named", "boom", False),
    # Python 3.7 and later name the package of a failed ``from pkg import x``.
    ("pkg", "cannot import name 'missing_dependency' from 'pkg' (pkg/__init__.py)", False),
]


@pytest.mark.parametrize(("name", "message", "not_found"), PYTHON_35_IMPORT_FAILURES)
def test_python_35_import_failures_are_classified_as_on_python_36(
    monkeypatch: pytest.MonkeyPatch, name: str | None, message: str, not_found: bool
) -> None:
    monkeypatch.setattr(harness, "_MODULE_NOT_FOUND", None)
    assert harness._module_not_found(ImportError(message, name=name)) is not_found


def test_python_35_lookup_reports_an_import_failure_inside_the_target_module(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package = tmp_path / "py35_lookup_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "mod.py").write_text(
        "from py35_lookup_pkg import missing_dependency\n\ndef f():\n    return 1\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(harness, "_MODULE_NOT_FOUND", None)
    try:
        # An import error of the module itself: import_error, never missing.
        with pytest.raises(ImportError, match="missing_dependency"):
            harness._lookup("py35_lookup_pkg.mod.f")
    finally:
        for loaded in [key for key in sys.modules if key.startswith("py35_lookup_pkg")]:
            del sys.modules[loaded]
