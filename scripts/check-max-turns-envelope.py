#!/usr/bin/env python3
"""Enforce the ``max_turns=1`` ↔ ``allowed_tools=[]`` pairing at PR time.

Per Q00/ouroboros#781, every adapter call site that passes
``max_turns=1`` (or ``max_turns = 1``) MUST also pass an empty
``allowed_tools`` envelope on the *same* call. Otherwise a single
tool-use block from the model burns the only allowed turn and the
SDK raises ``Reached maximum number of turns (1)`` before any final
text response can stream — a latent hang reproduced as
https://github.com/Q00/ouroboros/issues/765 and swept across the
remaining sites in #781.

The guard walks the AST of ``src/ouroboros/`` and exits non-zero if
any keyword call passes ``max_turns=1`` without a co-located empty
``allowed_tools``. Pure comments containing ``max_turns=1`` are
ignored (the AST walker only sees real calls).

Run locally::

    python3 scripts/check-max-turns-envelope.py

CI hookup:
    Add an invocation to ``.github/workflows/`` alongside the
    existing ``check-auto-boundary.py`` job.

Accepted ``allowed_tools`` value forms (Form A — see issue #781):

    * ``allowed_tools=[]``
    * ``allowed_tools=[] if cond else None``
    * ``allowed_tools=([] if cond else None)``
    * ``allowed_tools=<Name>`` where the same scope binds the Name
      to one of the forms above (e.g. ``_shared_allowed_tools``).

This deliberately rejects:

    * a missing ``allowed_tools`` kwarg
    * a non-empty list literal (``allowed_tools=["Read", ...]``)
    * an opaque function call (``allowed_tools=_interview_allowed_tools(...)``)
      — those return non-empty envelopes and re-introduce the regression.
"""

from __future__ import annotations

import ast
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOT = REPO_ROOT / "src" / "ouroboros"


def _is_empty_list(node: ast.expr) -> bool:
    return isinstance(node, ast.List) and len(node.elts) == 0


def _ifexp_resolves_to_empty_list(node: ast.expr) -> bool:
    """``[] if cond else None`` (or symmetric forms) — at least one branch is ``[]``."""
    if not isinstance(node, ast.IfExp):
        return False
    return _is_empty_list(node.body) or _is_empty_list(node.orelse)


def _value_is_empty_envelope(value: ast.expr) -> bool:
    return _is_empty_list(value) or _ifexp_resolves_to_empty_list(value)


def _scope_bindings_to_empty_envelope(scope: ast.AST, name: str) -> bool:
    """Return True if ``name`` is assigned an empty-envelope expression in ``scope``."""
    for node in ast.walk(scope):
        target_id: str | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target_id = node.target.id
            value = node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            t = node.targets[0]
            if isinstance(t, ast.Name):
                target_id = t.id
                value = node.value
        if target_id != name or value is None:
            continue
        if _value_is_empty_envelope(value):
            return True
    return False


def _find_max_turns_one_calls(tree: ast.AST) -> list[ast.Call]:
    hits: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg == "max_turns" and isinstance(kw.value, ast.Constant) and kw.value.value == 1:
                hits.append(node)
                break
    return hits


def _enclosing_scopes(tree: ast.AST, target: ast.Call) -> list[ast.AST]:
    """Return ancestor scopes (Module / FunctionDef / AsyncFunctionDef / ClassDef)
    of ``target`` from innermost to outermost. Used to resolve ``Name`` bindings
    for ``allowed_tools=<Name>`` forms.
    """
    scopes: list[ast.AST] = []

    def _walk(node: ast.AST, ancestors: list[ast.AST]) -> bool:
        if node is target:
            scopes.extend(reversed(ancestors))
            return True
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            ancestors = ancestors + [node]
        return any(_walk(child, ancestors) for child in ast.iter_child_nodes(node))

    _walk(tree, [])
    return scopes


def _call_has_empty_envelope(tree: ast.AST, call: ast.Call) -> bool:
    for kw in call.keywords:
        if kw.arg != "allowed_tools":
            continue
        value = kw.value
        if _value_is_empty_envelope(value):
            return True
        if isinstance(value, ast.Name):
            for scope in _enclosing_scopes(tree, call):
                if _scope_bindings_to_empty_envelope(scope, value.id):
                    return True
    return False


def _scan_file(path: Path) -> list[tuple[int, str]]:
    """Return offending ``(line_no, snippet)`` tuples for ``path``."""
    findings: list[tuple[int, str]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return findings
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        # Don't fail the guard on partial/edited files — main test suite catches that.
        return findings

    for call in _find_max_turns_one_calls(tree):
        if _call_has_empty_envelope(tree, call):
            continue
        # Surface the offending call's location.
        line = (
            text.splitlines()[call.lineno - 1] if call.lineno - 1 < len(text.splitlines()) else ""
        )
        findings.append((call.lineno, line.rstrip()))
    return findings


def main() -> int:
    if not SCAN_ROOT.is_dir():
        sys.stderr.write(
            f"check-max-turns-envelope: FAILED — scan root {SCAN_ROOT} does not exist.\n"
        )
        return 1

    targets = sorted(SCAN_ROOT.rglob("*.py"))
    all_findings: list[tuple[Path, int, str]] = []
    for path in targets:
        for lineno, line in _scan_file(path):
            all_findings.append((path, lineno, line))

    if not all_findings:
        print(f"check-max-turns-envelope: OK ({len(targets)} files scanned, 0 findings)")
        return 0

    sys.stderr.write(
        "check-max-turns-envelope: FAILED — ``max_turns=1`` call sites without "
        "``allowed_tools=[]``.\n"
        "Per Q00/ouroboros#781, every single-shot adapter MUST close its tool "
        "envelope so a tool-use block cannot consume the only allowed turn.\n\n"
    )
    for path, lineno, line in all_findings:
        try:
            rel = path.relative_to(REPO_ROOT)
        except ValueError:
            rel = path
        sys.stderr.write(f"  {rel}:{lineno}\n    {line}\n")
    sys.stderr.write(
        "\nFix: add ``allowed_tools=[]`` (or the conditional form\n"
        "``[] if backend_supports_tool_envelope(resolve_llm_backend(backend)) else None``)\n"
        "to each offending call.\n"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
