"""Wiring lock for ``mcp/server/adapter.py``'s ``max_turns=1`` adapters.

Covers both call sites:

    * The shared composition-root LLM adapter (``llm_adapter = ...``).
    * The ``fresh_llm_adapter()`` closure used by Wonder/Reflect engines.

Per Q00/ouroboros#781, every adapter constructed with ``max_turns=1``
MUST also pin ``allowed_tools=[]`` (when the backend supports a tool
envelope). Otherwise a single tool-use block from the model burns the
only allowed turn and the SDK raises 'Reached maximum number of turns (1)'.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import ouroboros.mcp.server.adapter as adapter_module

ADAPTER_SOURCE = Path(adapter_module.__file__)

EXPECTED_MAX_TURNS_ONE_CALLS = 2  # shared adapter + fresh_llm_adapter closure


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
    """An ``allowed_tools`` kwarg whose value either *is* the empty list
    literal or *can be* the empty list literal (IfExp), or is a Name that
    in the same scope is bound to such an expression."""
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
        # Name reference — accept if it's our known intermediate variable.
        # The composition root binds the envelope to ``_shared_allowed_tools``
        # to share it across the shared adapter and downstream engines.
        if isinstance(value, ast.Name) and value.id == "_shared_allowed_tools":
            return True
    return False


@pytest.fixture(scope="module")
def adapter_source() -> str:
    return ADAPTER_SOURCE.read_text(encoding="utf-8")


def test_adapter_module_has_expected_max_turns_one_calls(adapter_source: str) -> None:
    calls = _find_max_turns_one_calls(adapter_source)
    assert len(calls) == EXPECTED_MAX_TURNS_ONE_CALLS, (
        f"Expected {EXPECTED_MAX_TURNS_ONE_CALLS} ``max_turns=1`` call sites in "
        f"{ADAPTER_SOURCE.name}; found {len(calls)}. Re-pin EXPECTED if a "
        "single-shot adapter was added or removed."
    )


def test_adapter_max_turns_one_calls_pin_allowed_tools_empty(adapter_source: str) -> None:
    calls = _find_max_turns_one_calls(adapter_source)
    unguarded = [c for c in calls if not _has_empty_allowed_tools(c)]
    assert not unguarded, (
        f"Found {len(unguarded)} ``max_turns=1`` call site(s) in "
        f"{ADAPTER_SOURCE.name} without ``allowed_tools=[]`` (or a name "
        "binding equivalent). See issue #781."
    )


def test_shared_allowed_tools_is_bound_to_empty_list_envelope(adapter_source: str) -> None:
    """``_shared_allowed_tools`` (if used) must resolve to ``[]`` for envelope-aware backends.

    The shared composition-root adapter passes ``allowed_tools=_shared_allowed_tools``
    to thread the envelope through; this test pins that the variable's binding
    contains an empty list literal (matching the per-site Form A pattern).
    """
    if "_shared_allowed_tools" not in adapter_source:
        pytest.skip("_shared_allowed_tools intermediate not in use")

    tree = ast.parse(adapter_source)
    for node in ast.walk(tree):
        # AnnAssign or Assign of _shared_allowed_tools = ([] if ... else None)
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
        if target_id != "_shared_allowed_tools" or value is None:
            continue
        if isinstance(value, ast.IfExp) and (
            isinstance(value.body, ast.List) and len(value.body.elts) == 0
        ):
            return
        if isinstance(value, ast.List) and len(value.elts) == 0:
            return
    pytest.fail(
        "_shared_allowed_tools is referenced but its binding does not resolve "
        "to an empty list literal — pair with ``allowed_tools=[]`` per #781."
    )
