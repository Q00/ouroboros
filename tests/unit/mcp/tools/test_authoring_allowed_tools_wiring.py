"""Wiring lock for ``authoring_handlers``' ``max_turns=1`` adapters.

Covers both call sites in ``src/ouroboros/mcp/tools/authoring_handlers.py``:

    * ``GenerateSeedHandler.handle()`` — in-process seed generation.
    * ``InterviewHandler.handle()``   — nested-MCP question generator
      (already pinned by ``test_interview_allowed_tools_wiring.py``;
      this test re-affirms the lock at the module level so a future
      sweep cannot silently re-open the envelope).

Per Q00/ouroboros#781, every adapter constructed with ``max_turns=1``
MUST also pin ``allowed_tools=[]`` (when the backend supports a tool
envelope). Otherwise a single tool-use block from the model burns the
only allowed turn and the SDK raises 'Reached maximum number of turns (1)'.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import ouroboros.mcp.tools.authoring_handlers as authoring_module

AUTHORING_SOURCE = Path(authoring_module.__file__)

EXPECTED_MAX_TURNS_ONE_CALLS = 2  # GenerateSeedHandler + InterviewHandler


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
def authoring_source() -> str:
    return AUTHORING_SOURCE.read_text(encoding="utf-8")


def test_authoring_module_has_expected_max_turns_one_calls(authoring_source: str) -> None:
    """Sanity-pin: the count of ``max_turns=1`` call sites is fixed.

    A drift here is a signal — either a new single-shot adapter was added
    (extend this lock) or an existing one was removed (re-pin EXPECTED).
    """
    calls = _find_max_turns_one_calls(authoring_source)
    assert len(calls) == EXPECTED_MAX_TURNS_ONE_CALLS, (
        f"Expected {EXPECTED_MAX_TURNS_ONE_CALLS} ``max_turns=1`` call sites in "
        f"{AUTHORING_SOURCE.name}; found {len(calls)}. If a new single-shot "
        "adapter was added, extend this wiring lock alongside the new site."
    )


def test_authoring_max_turns_one_calls_pin_allowed_tools_empty(authoring_source: str) -> None:
    """Both authoring ``max_turns=1`` adapter calls MUST set ``allowed_tools=[]``."""
    calls = _find_max_turns_one_calls(authoring_source)
    unguarded = [c for c in calls if not _has_empty_allowed_tools(c)]
    assert not unguarded, (
        f"Found {len(unguarded)} ``max_turns=1`` call site(s) in "
        f"{AUTHORING_SOURCE.name} without ``allowed_tools=[]``. Each such site "
        "is a latent turn-starvation hang — see issue #781."
    )
