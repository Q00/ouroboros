"""Wiring lock for ``verification_artifacts``'s ``max_turns=1`` adapter.

Per Q00/ouroboros#781, every adapter constructed with ``max_turns=1``
MUST also pin ``allowed_tools=[]`` (when the backend supports a tool
envelope). ``verification_artifacts`` builds a default single-shot
adapter for Stage 1 mechanical.toml fall-through.

AST helpers are deduplicated in :mod:`tests._envelope_wiring` so this
file cannot drift from the canonical guard semantics enforced by
``scripts/check-max-turns-envelope.py`` (PR #786 review-2).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import ouroboros.evaluation.verification_artifacts as va_module
from tests._envelope_wiring import find_max_turns_one_calls, has_empty_allowed_tools

VA_SOURCE = Path(va_module.__file__)


@pytest.fixture(scope="module")
def va_source() -> str:
    return VA_SOURCE.read_text(encoding="utf-8")


def test_verification_artifacts_has_a_max_turns_one_call(va_source: str) -> None:
    calls = find_max_turns_one_calls(va_source)
    assert calls, (
        "verification_artifacts must still construct an adapter with max_turns=1 — "
        "if this test fails the wiring-lock target moved and must be re-pinned."
    )


def test_verification_artifacts_max_turns_one_call_pins_allowed_tools_empty(
    va_source: str,
) -> None:
    calls = find_max_turns_one_calls(va_source)
    unguarded = [c for c in calls if not has_empty_allowed_tools(c)]
    assert not unguarded, (
        f"Found {len(unguarded)} ``max_turns=1`` call site(s) in "
        f"{VA_SOURCE.name} without ``allowed_tools=[]``. See issue #781."
    )
