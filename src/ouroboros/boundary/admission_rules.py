"""Static admission rules for model-written check scripts (tier ``C``).

A check that matches a rule here is rejected at admission before any command
runs (reason ``unsafe_check:<rule>:<check_id>``), and its tier is ``C``. The
rules target checks that compute their verdict from the artifact instead of
calling it through a binding, or that reach outside the checkout:

- ``dynamic_workspace_import``: loading code by file path or by a computed
  name, for example ``glob('*.py')`` plus ``spec_from_file_location`` and
  ``exec_module``, ``SourceFileLoader``, ``runpy.run_path``, ``imp.load_source``,
  or ``importlib.import_module`` / ``__import__`` / ``runpy.run_module`` with a
  non-literal argument. Such a check scans the workspace for whatever matches
  instead of calling a declared entry point.
- ``exec_of_workspace_content``: ``exec``, ``eval``, or ``compile`` of anything
  other than a literal string.
- ``network``: importing a network client or socket module (``socket``,
  ``urllib.request``, ``http.client``, ``requests``, ``httpx``, ``aiohttp``,
  ...), or calling ``urlopen``.

A literal ``importlib.import_module("pkg.mod")`` or ``find_spec("pkg")`` is an
ordinary import and is allowed; a guarded ``hasattr`` or ``find_spec`` lookup
of a new symbol is the prescribed feature-check pattern. The product oracle
harness (``boundary/oracle.py``) is not subject to these rules: it is product
code and runs from the controller directory.

A script that does not parse is left to admission, which runs it.
"""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

from ouroboros.boundary.oracle import is_oracle_file

if TYPE_CHECKING:
    from ouroboros.boundary.package import CheckPackage

UNSAFE_CHECK_REASON = "unsafe_check"
RULE_DYNAMIC_IMPORT = "dynamic_workspace_import"
RULE_EXEC = "exec_of_workspace_content"
RULE_NETWORK = "network"

_PATH_LOADERS = frozenset(
    {
        "spec_from_file_location",
        "exec_module",
        "SourceFileLoader",
        "SourcelessFileLoader",
        "load_source",
        "load_module",
        "run_path",
        "module_from_spec",
    }
)
_NAME_IMPORTERS = frozenset({"import_module", "__import__", "run_module"})
_EXEC_BUILTINS = frozenset({"exec", "eval", "compile"})
_NETWORK_MODULES = frozenset(
    {
        "socket",
        "ssl",
        "urllib.request",
        "urllib3",
        "http.client",
        "http.server",
        "requests",
        "httpx",
        "aiohttp",
        "ftplib",
        "smtplib",
        "poplib",
        "imaplib",
        "telnetlib",
        "websocket",
        "websockets",
        "xmlrpc.client",
        "paramiko",
    }
)
_NETWORK_CALLS = frozenset({"urlopen", "create_connection", "urlretrieve"})


def _dotted(node: ast.expr) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _is_literal(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str | bytes)


def _network_module(name: str) -> bool:
    return any(name == module or name.startswith(module + ".") for module in _NETWORK_MODULES)


def script_rule_violations(source: str) -> tuple[str, ...]:
    """Return the sorted rule names ``source`` violates (empty when it is admissible)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _network_module(alias.name):
                    found.add(RULE_NETWORK)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level == 0 and _network_module(module):
                found.add(RULE_NETWORK)
            for alias in node.names:
                full = f"{module}.{alias.name}" if module else alias.name
                if node.level == 0 and _network_module(full):
                    found.add(RULE_NETWORK)
                if alias.name in _PATH_LOADERS:
                    found.add(RULE_DYNAMIC_IMPORT)
                if alias.name in _NETWORK_CALLS:
                    found.add(RULE_NETWORK)
        elif isinstance(node, ast.Call):
            name = _dotted(node.func)
            last = name.rsplit(".", 1)[-1] if name else ""
            if isinstance(node.func, ast.Attribute):
                last = node.func.attr
            if last in _PATH_LOADERS or last in _NAME_IMPORTERS and (not node.args or not _is_literal(node.args[0])):
                found.add(RULE_DYNAMIC_IMPORT)
            elif name in _EXEC_BUILTINS and (not node.args or not _is_literal(node.args[0])):
                found.add(RULE_EXEC)
            if last in _NETWORK_CALLS:
                found.add(RULE_NETWORK)
    return tuple(sorted(found))


def unsafe_checks(package: CheckPackage) -> tuple[tuple[str, str], ...]:
    """``(check_id, rule)`` for every model-written check script that breaks a rule.

    A check's script is the package file its argv names; helper files the
    script may import (other model-written package files) are attributed to
    every script check. Product oracle files are exempt.
    """
    model_files = {
        item.path: item.content for item in package.files if not is_oracle_file(item.path)
    }
    helper_rules: set[str] = set()
    script_paths = {arg for check in package.checks for arg in check.argv[1:]}
    for path, content in model_files.items():
        if path not in script_paths and path.endswith(".py"):
            helper_rules.update(script_rule_violations(content))
    found: list[tuple[str, str]] = []
    for check in package.checks:
        script = next((arg for arg in check.argv[1:] if arg in model_files), None)
        if script is None:
            continue
        rules = set(script_rule_violations(model_files[script])) | helper_rules
        found.extend((check.check_id, rule) for rule in sorted(rules))
    return tuple(found)


__all__ = [
    "RULE_DYNAMIC_IMPORT",
    "RULE_EXEC",
    "RULE_NETWORK",
    "UNSAFE_CHECK_REASON",
    "script_rule_violations",
    "unsafe_checks",
]
