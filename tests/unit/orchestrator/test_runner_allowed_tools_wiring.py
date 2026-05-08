"""Wiring lock for ``orchestrator.runner``'s ``max_turns=1`` adapter.

Per Q00/ouroboros#781, every adapter constructed with ``max_turns=1``
MUST also pin ``allowed_tools=[]`` (when the backend supports a tool
envelope). The runner builds a single-shot adapter for the dependency
analyzer fall-through.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import ouroboros.orchestrator.runner as runner_module

RUNNER_SOURCE = Path(runner_module.__file__)


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
def runner_source() -> str:
    return RUNNER_SOURCE.read_text(encoding="utf-8")


def test_runner_has_a_max_turns_one_call(runner_source: str) -> None:
    calls = _find_max_turns_one_calls(runner_source)
    assert calls, (
        "orchestrator.runner must still construct an adapter with max_turns=1 — "
        "if this test fails the wiring-lock target moved and must be re-pinned."
    )


def test_runner_max_turns_one_call_pins_allowed_tools_empty(runner_source: str) -> None:
    calls = _find_max_turns_one_calls(runner_source)
    unguarded = [c for c in calls if not _has_empty_allowed_tools(c)]
    assert not unguarded, (
        f"Found {len(unguarded)} ``max_turns=1`` call site(s) in "
        f"{RUNNER_SOURCE.name} without ``allowed_tools=[]``. See issue #781."
    )
