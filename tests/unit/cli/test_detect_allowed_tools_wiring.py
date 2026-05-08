"""Wiring lock for ``ooo detect``'s ``max_turns=1`` adapter.

Per Q00/ouroboros#781, every adapter constructed with ``max_turns=1``
MUST also pin ``allowed_tools=[]`` (when the backend supports a tool
envelope). The detect CLI builds a single-shot adapter for the
mechanical.toml proposal flow.

AST helpers are deduplicated in :mod:`tests._envelope_wiring` so this
file cannot drift from the canonical guard semantics enforced by
``scripts/check-max-turns-envelope.py`` (PR #786 review-2).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import ouroboros.cli.commands.detect as detect_module
from tests._envelope_wiring import find_max_turns_one_calls, has_empty_allowed_tools

DETECT_SOURCE = Path(detect_module.__file__)


@pytest.fixture(scope="module")
def detect_source() -> str:
    return DETECT_SOURCE.read_text(encoding="utf-8")


def test_detect_module_has_a_max_turns_one_call(detect_source: str) -> None:
    calls = find_max_turns_one_calls(detect_source)
    assert calls, (
        "ooo detect must still construct an adapter with max_turns=1 — if this "
        "test fails the wiring-lock target moved and must be re-pinned."
    )


def test_detect_max_turns_one_call_pins_allowed_tools_empty(detect_source: str) -> None:
    calls = find_max_turns_one_calls(detect_source)
    unguarded = [c for c in calls if not has_empty_allowed_tools(c)]
    assert not unguarded, (
        f"Found {len(unguarded)} ``max_turns=1`` call site(s) in "
        f"{DETECT_SOURCE.name} without ``allowed_tools=[]``. See issue #781."
    )
