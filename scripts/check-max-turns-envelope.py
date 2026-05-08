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

This deliberately rejects:

    * a missing ``allowed_tools`` kwarg
    * a non-empty list literal (``allowed_tools=["Read", ...]``)
    * an opaque function call (``allowed_tools=_interview_allowed_tools(...)``)
      — those return non-empty envelopes and re-introduce the regression.
    * a ``Name`` reference (``allowed_tools=_shared_allowed_tools``).
      Resolving Names via AST walk is order- and scope-unsafe (PR #786
      review): an assignment after the call, in a sibling/inner function,
      or in a nested class would be erroneously accepted. Inline the
      ``[] if cond else None`` literal at every call site instead.
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
    """``[] if cond else None`` only — the supported-backend branch must close."""
    if not isinstance(node, ast.IfExp):
        return False
    return (
        _is_empty_list(node.body)
        and isinstance(node.orelse, ast.Constant)
        and node.orelse.value is None
    )


def _value_is_empty_envelope(value: ast.expr) -> bool:
    """Approach A — strict literal-only acceptance (PR #786 review).

    Only a direct ``[]`` literal, or an ``IfExp`` of the canonical
    ``[] if cond else None`` shape, counts as an empty envelope. Any other form
    (``Name`` reference, function call, attribute access, comprehension,
    starred expansion) is rejected. Resolving ``Name`` bindings via AST
    walk is order- and scope-unsafe — see the rejection notes in this
    file's module docstring.
    """
    return _is_empty_list(value) or _ifexp_resolves_to_empty_list(value)


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


def _call_has_empty_envelope(call: ast.Call) -> bool:
    for kw in call.keywords:
        if kw.arg != "allowed_tools":
            continue
        if _value_is_empty_envelope(kw.value):
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
        if _call_has_empty_envelope(call):
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
