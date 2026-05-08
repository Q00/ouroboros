"""Wiring lock for ``PMInterviewHandler``'s ``max_turns=1`` adapter.

Per Q00/ouroboros#781, every adapter constructed with ``max_turns=1``
MUST also pin ``allowed_tools=[]`` (when the backend supports a tool
envelope). ``pm_handler.py`` was the prior-art reference cited by
PR #770; this test re-affirms the lock at the module level so a
future sweep cannot silently re-open the envelope.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import ouroboros.mcp.tools.pm_handler as pm_module

PM_SOURCE = Path(pm_module.__file__)


def _find_max_turns_one_calls(source_text: str) -> list[ast.Call]:
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
def pm_source() -> str:
    return PM_SOURCE.read_text(encoding="utf-8")


def test_pm_handler_has_a_max_turns_one_call(pm_source: str) -> None:
    calls = _find_max_turns_one_calls(pm_source)
    assert calls, (
        "PMInterviewHandler must still construct an adapter with max_turns=1 — "
        "if this test fails the wiring-lock target moved and must be re-pinned."
    )


def test_pm_handler_max_turns_one_call_pins_allowed_tools_empty(pm_source: str) -> None:
    calls = _find_max_turns_one_calls(pm_source)
    unguarded = [c for c in calls if not _has_empty_allowed_tools(c)]
    assert not unguarded, (
        f"Found {len(unguarded)} ``max_turns=1`` call site(s) in {PM_SOURCE.name} "
        "without ``allowed_tools=[]``. See issue #781."
    )
