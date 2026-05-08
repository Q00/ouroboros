"""Wiring lock for ``QAHandler``'s ``max_turns=1`` adapter.

Per Q00/ouroboros#781, every adapter constructed with ``max_turns=1``
MUST also pin ``allowed_tools=[]`` (when the backend supports a tool
envelope). Otherwise a single tool-use block from the model burns the
only allowed turn and ``_is_usable_max_turns_partial`` rejects the
``stop_reason="tool_use"`` partial — yielding a latent hang.

This test asserts the wiring at the AST level: a non-empty allowlist
or a missing ``allowed_tools`` kwarg on the QA adapter call would
re-introduce the regression and is statically forbidden here.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import ouroboros.mcp.tools.qa as qa_module

QA_SOURCE = Path(qa_module.__file__)


def _find_max_turns_one_calls(source_text: str) -> list[ast.Call]:
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


def _has_empty_allowed_tools(call: ast.Call) -> bool:
    """``allowed_tools`` kwarg whose value contains an empty list literal.

    Accepts:
        * ``allowed_tools=[]``
        * ``allowed_tools=[] if cond else None``  (IfExp with empty list body)
        * ``allowed_tools=([] if cond else None)``
    """
    for kw in call.keywords:
        if kw.arg != "allowed_tools":
            continue
        value = kw.value
        if isinstance(value, ast.List) and len(value.elts) == 0:
            return True
        if isinstance(value, ast.IfExp) and (
            isinstance(value.body, ast.List) and len(value.body.elts) == 0
        ):
            return True
    return False


@pytest.fixture(scope="module")
def qa_source() -> str:
    return QA_SOURCE.read_text(encoding="utf-8")


def test_qa_module_has_a_max_turns_one_call(qa_source: str) -> None:
    """Sanity: the QA handler still constructs at least one max_turns=1 adapter."""
    calls = _find_max_turns_one_calls(qa_source)
    assert calls, (
        "QAHandler must still construct an adapter with max_turns=1 — if this "
        "test fails the wiring-lock target moved and the lock must be re-pinned."
    )


def test_qa_max_turns_one_call_pins_allowed_tools_empty(qa_source: str) -> None:
    """Every ``max_turns=1`` adapter call in qa.py MUST set ``allowed_tools=[]``.

    Regression guard for issue #781 — a non-empty allowlist paired with
    ``max_turns=1`` lets the model burn the single allowed turn on a
    tool-use block and the SDK raises 'Reached maximum number of turns (1)'.
    """
    calls = _find_max_turns_one_calls(qa_source)
    unguarded = [c for c in calls if not _has_empty_allowed_tools(c)]
    assert not unguarded, (
        f"Found {len(unguarded)} ``max_turns=1`` call site(s) in {QA_SOURCE.name} "
        "without ``allowed_tools=[]``. Each such site is a latent turn-starvation "
        "hang. See https://github.com/Q00/ouroboros/issues/781."
    )
