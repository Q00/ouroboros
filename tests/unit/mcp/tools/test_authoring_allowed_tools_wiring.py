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

AST helpers are deduplicated in :mod:`tests._envelope_wiring` so this
file cannot drift from the canonical guard semantics enforced by
``scripts/check-max-turns-envelope.py`` (PR #786 review-2).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import ouroboros.mcp.tools.authoring_handlers as authoring_module
from tests._envelope_wiring import find_max_turns_one_calls, has_empty_allowed_tools

AUTHORING_SOURCE = Path(authoring_module.__file__)

EXPECTED_MAX_TURNS_ONE_CALLS = 2  # GenerateSeedHandler + InterviewHandler


@pytest.fixture(scope="module")
def authoring_source() -> str:
    return AUTHORING_SOURCE.read_text(encoding="utf-8")


def test_authoring_module_has_expected_max_turns_one_calls(authoring_source: str) -> None:
    """Sanity-pin: the count of ``max_turns=1`` call sites is fixed.

    A drift here is a signal — either a new single-shot adapter was added
    (extend this lock) or an existing one was removed (re-pin EXPECTED).
    """
    calls = find_max_turns_one_calls(authoring_source)
    assert len(calls) == EXPECTED_MAX_TURNS_ONE_CALLS, (
        f"Expected {EXPECTED_MAX_TURNS_ONE_CALLS} ``max_turns=1`` call sites in "
        f"{AUTHORING_SOURCE.name}; found {len(calls)}. If a new single-shot "
        "adapter was added, extend this wiring lock alongside the new site."
    )


def test_authoring_max_turns_one_calls_pin_allowed_tools_empty(authoring_source: str) -> None:
    """Both authoring ``max_turns=1`` adapter calls MUST set ``allowed_tools=[]``."""
    calls = find_max_turns_one_calls(authoring_source)
    unguarded = [c for c in calls if not has_empty_allowed_tools(c)]
    assert not unguarded, (
        f"Found {len(unguarded)} ``max_turns=1`` call site(s) in "
        f"{AUTHORING_SOURCE.name} without ``allowed_tools=[]``. Each such site "
        "is a latent turn-starvation hang — see issue #781."
    )
