"""Wiring lock for ``orchestrator.runner``'s ``max_turns=1`` adapter.

Per Q00/ouroboros#781, every adapter constructed with ``max_turns=1``
MUST also pin ``allowed_tools=[]`` (when the backend supports a tool
envelope). The runner builds a single-shot adapter for the dependency
analyzer fall-through.

AST helpers are deduplicated in :mod:`tests._envelope_wiring` so this
file cannot drift from the canonical guard semantics enforced by
``scripts/check-max-turns-envelope.py`` (PR #786 review-2).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import ouroboros.orchestrator.runner as runner_module
from tests._envelope_wiring import find_max_turns_one_calls, has_empty_allowed_tools

RUNNER_SOURCE = Path(runner_module.__file__)


@pytest.fixture(scope="module")
def runner_source() -> str:
    return RUNNER_SOURCE.read_text(encoding="utf-8")


def test_runner_has_a_max_turns_one_call(runner_source: str) -> None:
    calls = find_max_turns_one_calls(runner_source)
    assert calls, (
        "orchestrator.runner must still construct an adapter with max_turns=1 — "
        "if this test fails the wiring-lock target moved and must be re-pinned."
    )


def test_runner_max_turns_one_call_pins_allowed_tools_empty(runner_source: str) -> None:
    calls = find_max_turns_one_calls(runner_source)
    unguarded = [c for c in calls if not has_empty_allowed_tools(c)]
    assert not unguarded, (
        f"Found {len(unguarded)} ``max_turns=1`` call site(s) in "
        f"{RUNNER_SOURCE.name} without ``allowed_tools=[]``. See issue #781."
    )
