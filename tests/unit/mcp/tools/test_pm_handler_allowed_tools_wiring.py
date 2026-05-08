"""Wiring lock for ``PMInterviewHandler``'s ``max_turns=1`` adapter.

Per Q00/ouroboros#781, every adapter constructed with ``max_turns=1``
MUST also pin ``allowed_tools=[]`` (when the backend supports a tool
envelope). ``pm_handler.py`` was the prior-art reference cited by
PR #770; this test re-affirms the lock at the module level so a
future sweep cannot silently re-open the envelope.

AST helpers are deduplicated in :mod:`tests._envelope_wiring` so this
file cannot drift from the canonical guard semantics enforced by
``scripts/check-max-turns-envelope.py`` (PR #786 review-2).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import ouroboros.mcp.tools.pm_handler as pm_module
from tests._envelope_wiring import find_max_turns_one_calls, has_empty_allowed_tools

PM_SOURCE = Path(pm_module.__file__)


@pytest.fixture(scope="module")
def pm_source() -> str:
    return PM_SOURCE.read_text(encoding="utf-8")


def test_pm_handler_has_a_max_turns_one_call(pm_source: str) -> None:
    calls = find_max_turns_one_calls(pm_source)
    assert calls, (
        "PMInterviewHandler must still construct an adapter with max_turns=1 — "
        "if this test fails the wiring-lock target moved and must be re-pinned."
    )


def test_pm_handler_max_turns_one_call_pins_allowed_tools_empty(pm_source: str) -> None:
    calls = find_max_turns_one_calls(pm_source)
    unguarded = [c for c in calls if not has_empty_allowed_tools(c)]
    assert not unguarded, (
        f"Found {len(unguarded)} ``max_turns=1`` call site(s) in {PM_SOURCE.name} "
        "without ``allowed_tools=[]``. See issue #781."
    )
