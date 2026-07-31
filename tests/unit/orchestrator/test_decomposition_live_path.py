"""Architecture guards for the sole live decomposition path (issue #1466)."""

from __future__ import annotations

import ast
from pathlib import Path

_FORBIDDEN_MODULES = frozenset(
    {
        "ouroboros.execution.atomicity",
        "ouroboros.execution.decomposition",
    }
)


def _source_root() -> Path:
    return Path(__file__).resolve().parents[3] / "src"


def _imported_modules(
    node: ast.Import | ast.ImportFrom, *, package: tuple[str, ...]
) -> tuple[str, ...]:
    """Resolve every spelling of an import to its absolute module candidates."""
    if isinstance(node, ast.Import):
        return tuple(alias.name for alias in node.names)

    if node.level:
        parent_count = node.level - 1
        if parent_count > len(package):
            return ()
        base = package[: len(package) - parent_count]
    else:
        base = ()
    if node.module:
        base = (*base, *node.module.split("."))
    module = ".".join(base)
    imported: list[str] = []
    if module:
        imported.append(module)
    for alias in node.names:
        if alias.name == "*":
            imported.append(f"{module}.*" if module else "*")
        else:
            imported.append(f"{module}.{alias.name}" if module else alias.name)
    return tuple(imported)


def _references_forbidden_module(module: str) -> bool:
    return any(
        module == forbidden
        or module.startswith(f"{forbidden}.")
        or (module.endswith(".*") and forbidden.startswith(f"{module[:-2]}."))
        for forbidden in _FORBIDDEN_MODULES
    )


def _forbidden_imports(source: str, *, module_name: str) -> tuple[str, ...]:
    tree = ast.parse(source)
    package = tuple(module_name.split(".")[:-1])
    return tuple(
        imported
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for imported in _imported_modules(node, package=package)
        if _references_forbidden_module(imported)
    )


def test_dead_decomposition_modules_and_wiring_cannot_return() -> None:
    """Only orchestrator.parallel_executor may own live decomposition."""
    source_root = _source_root()
    execution_root = source_root / "ouroboros" / "execution"

    assert not (execution_root / "atomicity.py").exists()
    assert not (execution_root / "decomposition.py").exists()

    forbidden_importers: list[str] = []
    forbidden_wiring: list[str] = []
    for source_file in source_root.rglob("*.py"):
        source = source_file.read_text(encoding="utf-8")
        relative_module = source_file.relative_to(source_root).with_suffix("")
        module_name = ".".join(relative_module.parts)
        if _forbidden_imports(source, module_name=module_name):
            forbidden_importers.append(str(source_file.relative_to(source_root)))
        if "run_cycle_with_decomposition" in source:
            forbidden_wiring.append(str(source_file.relative_to(source_root)))

    assert forbidden_importers == []
    assert forbidden_wiring == []


def test_dead_module_guard_covers_every_import_form() -> None:
    """The architecture guard cannot be bypassed by another Python import spelling."""
    source = """
import ouroboros.execution.atomicity
import ouroboros.execution.decomposition.helpers
from ouroboros.execution.atomicity import AtomicityChecker
from ouroboros.execution import decomposition
from . import atomicity
from .decomposition import Decomposer
from ouroboros.execution import *
"""

    forbidden = _forbidden_imports(
        source,
        module_name="ouroboros.execution.double_diamond",
    )

    assert forbidden == (
        "ouroboros.execution.atomicity",
        "ouroboros.execution.decomposition.helpers",
        "ouroboros.execution.atomicity",
        "ouroboros.execution.atomicity.AtomicityChecker",
        "ouroboros.execution.decomposition",
        "ouroboros.execution.atomicity",
        "ouroboros.execution.decomposition",
        "ouroboros.execution.decomposition.Decomposer",
        "ouroboros.execution.*",
    )
