"""Wiring lock for ``mcp/server/adapter.py``'s ``max_turns=1`` adapters.

Covers both call sites:

    * The shared composition-root LLM adapter (``llm_adapter = ...``).
    * The ``fresh_llm_adapter()`` closure used by Wonder/Reflect engines.

Per Q00/ouroboros#781, every adapter constructed with ``max_turns=1``
MUST also pin ``allowed_tools=[]`` (when the backend supports a tool
envelope). Otherwise a single tool-use block from the model burns the
only allowed turn and the SDK raises 'Reached maximum number of turns (1)'.

AST helpers are deduplicated in :mod:`tests._envelope_wiring` so this
file cannot drift from the canonical guard semantics enforced by
``scripts/check-max-turns-envelope.py`` (PR #786 review-2). Both call
sites use inline ``[] if cond else None`` literals — Name-binding
acceptance was removed when the shared-intermediate refactor was
abandoned (PR #786 review-1: AST-walk Name resolution is order- and
scope-unsafe).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import ouroboros.mcp.server.adapter as adapter_module
from tests._envelope_wiring import find_max_turns_one_calls, has_empty_allowed_tools

ADAPTER_SOURCE = Path(adapter_module.__file__)

EXPECTED_MAX_TURNS_ONE_CALLS = 2  # shared adapter + fresh_llm_adapter closure


@pytest.fixture(scope="module")
def adapter_source() -> str:
    return ADAPTER_SOURCE.read_text(encoding="utf-8")


def test_adapter_module_has_expected_max_turns_one_calls(adapter_source: str) -> None:
    calls = find_max_turns_one_calls(adapter_source)
    assert len(calls) == EXPECTED_MAX_TURNS_ONE_CALLS, (
        f"Expected {EXPECTED_MAX_TURNS_ONE_CALLS} ``max_turns=1`` call sites in "
        f"{ADAPTER_SOURCE.name}; found {len(calls)}. Re-pin EXPECTED if a "
        "single-shot adapter was added or removed."
    )


def test_adapter_max_turns_one_calls_pin_allowed_tools_empty(adapter_source: str) -> None:
    calls = find_max_turns_one_calls(adapter_source)
    unguarded = [c for c in calls if not has_empty_allowed_tools(c)]
    assert not unguarded, (
        f"Found {len(unguarded)} ``max_turns=1`` call site(s) in "
        f"{ADAPTER_SOURCE.name} without ``allowed_tools=[]``. See issue #781."
    )
