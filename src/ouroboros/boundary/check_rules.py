"""Static admission rules over a package's check scripts.

``prose_only_checks`` finds checks that decide a criterion by reading prose
files (a README, a changelog, other documentation) and matching their text,
without executing any project code. Such a check is brittle: a correct change
worded differently fails it. Because the admitted package is authoritative for
the criteria it covers, a brittle text check would turn a correct run into a
failure. The constructor is told to list criteria that can only be checked
this way as uncovered, so the legacy verifier decides them; admission enforces
it by rejecting the package (``prose_only_check:<check_id>``) before any check
runs, and the product policy regenerates with that reason as feedback.

The rule is deliberately narrow. A script is prose-only when both hold:

- it names at least one prose file (a string literal whose file name has a
  prose extension, such as ``.md``/``.rst``/``.txt``, or a conventional
  documentation stem such as ``README`` or ``CHANGELOG``); and
- it executes nothing: no import of a non-standard-library module (the
  project's code), no ``subprocess``/``runpy``/``importlib`` use, no
  ``os.system``/``os.popen``/``os.exec*``/``os.spawn*``, and no
  ``exec``/``eval``/``compile``/``__import__``.

A script that cannot be parsed is left to admission, which runs it.
"""

from __future__ import annotations

import ast
from pathlib import PurePosixPath
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ouroboros.boundary.package import CheckPackage

PROSE_ONLY_CHECK_REASON = "prose_only_check"
PROSE_SUFFIXES = frozenset(
    {".md", ".markdown", ".mdx", ".rst", ".txt", ".adoc", ".asciidoc", ".org", ".rtf"}
)
PROSE_STEMS = frozenset(
    {"readme", "changelog", "changes", "history", "contributing", "license", "notice", "authors"}
)
_EXECUTING_MODULES = frozenset({"subprocess", "runpy", "importlib", "multiprocessing", "pty"})
_EXECUTING_BUILTINS = frozenset({"exec", "eval", "compile", "__import__"})
_EXECUTING_OS_PREFIXES = ("os.system", "os.popen", "os.exec", "os.spawn", "os.posix_spawn")
_MAX_PATH_LITERAL = 256


def _is_prose_path(value: str) -> bool:
    if not value or len(value) > _MAX_PATH_LITERAL or "\n" in value:
        return False
    name = PurePosixPath(value.replace("\\", "/")).name.lower()
    if not name:
        return False
    if PurePosixPath(name).suffix in PROSE_SUFFIXES:
        return True
    return name.split(".", 1)[0] in PROSE_STEMS


def _dotted(node: ast.expr) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _top_level(module: str) -> str:
    return module.split(".", 1)[0]


def _imports_executable_code(node: ast.AST) -> bool:
    stdlib = sys.stdlib_module_names
    if isinstance(node, ast.Import):
        return any(
            _top_level(alias.name) not in stdlib or _top_level(alias.name) in _EXECUTING_MODULES
            for alias in node.names
        )
    if isinstance(node, ast.ImportFrom):
        if node.level > 0 or not node.module:
            return True  # a relative import loads project code
        top = _top_level(node.module)
        if top not in stdlib or top in _EXECUTING_MODULES:
            return True
        if top == "os":
            return any(
                f"os.{alias.name}".startswith(_EXECUTING_OS_PREFIXES) for alias in node.names
            )
    return False


def _calls_executable_code(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    name = _dotted(node.func)
    return name in _EXECUTING_BUILTINS or name.startswith(_EXECUTING_OS_PREFIXES)


def is_prose_only_script(source: str) -> bool:
    """Whether ``source`` names a prose file and executes no code (module docstring)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    names_prose = False
    for node in ast.walk(tree):
        if _imports_executable_code(node) or _calls_executable_code(node):
            return False
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            names_prose = names_prose or _is_prose_path(node.value)
    return names_prose


def prose_only_checks(package: CheckPackage) -> tuple[str, ...]:
    """Ids of the package's checks whose script is prose-only."""
    scripts = {item.path: item.content for item in package.files}
    found: list[str] = []
    for check in package.checks:
        script = next((scripts[arg] for arg in check.argv[1:] if arg in scripts), None)
        if script is not None and is_prose_only_script(script):
            found.append(check.check_id)
    return tuple(found)


__all__ = [
    "PROSE_ONLY_CHECK_REASON",
    "PROSE_STEMS",
    "PROSE_SUFFIXES",
    "is_prose_only_script",
    "prose_only_checks",
]
