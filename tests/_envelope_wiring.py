"""Shared AST helpers for ``max_turns=1`` / ``allowed_tools=[]`` wiring tests.

These helpers mirror the production guard at
``scripts/check-max-turns-envelope.py`` so per-file wiring tests cannot
drift from the canonical CI-enforced semantics.

Per Q00/ouroboros#781, every ``max_turns=1`` adapter call site MUST
co-locate ``allowed_tools=[]`` (or the canonical conditional ``[] if cond else None``
form). See PR #786 review-2 for the dedup rationale.
"""

from __future__ import annotations

import ast


def _is_empty_list(node: ast.expr) -> bool:
    return isinstance(node, ast.List) and len(node.elts) == 0


def find_max_turns_one_calls(source_text: str) -> list[ast.Call]:
    """Return every ``Call`` node passing ``max_turns=1`` as a keyword arg."""
    tree = ast.parse(source_text)
    hits: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg == "max_turns" and isinstance(kw.value, ast.Constant) and kw.value.value == 1:
                hits.append(node)
                break
    return hits


def has_empty_allowed_tools(call: ast.Call) -> bool:
    """``allowed_tools`` kwarg value contains an empty-list literal.

    Mirrors the production guard semantics. Accepts:

        * ``allowed_tools=[]``
        * ``allowed_tools=[] if cond else None``  (empty list in body)
        * ``allowed_tools=([] if cond else None)`` (parenthesised IfExp)

    Rejects all other forms (``Name`` reference, function call,
    non-empty literal, comprehension). Resolving Name bindings via AST
    walk is order- and scope-unsafe — see PR #786 review-1.
    """
    for kw in call.keywords:
        if kw.arg != "allowed_tools":
            continue
        value = kw.value
        if _is_empty_list(value):
            return True
        if (
            isinstance(value, ast.IfExp)
            and _is_empty_list(value.body)
            and isinstance(value.orelse, ast.Constant)
            and value.orelse.value is None
        ):
            return True
    return False
