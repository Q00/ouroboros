"""Freedom-policy guard for the unsafe-context veto.

The unsafe-context bank that vetoed safe-default closure is intentionally
empty: ``ooo auto`` must NOT block autonomous gap-defaulting just because the
interview context mentions an everyday software word like ``contract``,
``license``, ``security``, ``deploy``, or ``credentials``. These keywords
previously over-blocked benign runs (a CSV→JSON tool was classified as
"legal/medical judgment" because an answer said "contract").

This module deliberately does NOT opt into the ``_legacy_unsafe_bank``
fixture (see conftest.py) — it asserts the production default directly.
"""

from __future__ import annotations

import pytest

from ouroboros.auto.ledger import SeedDraftLedger
from ouroboros.auto.safe_defaults import _UNSAFE_CONTEXT_PATTERNS, _unsafe_context_reason


def test_production_unsafe_context_bank_is_empty() -> None:
    """The shipped default vetoes nothing — freedom policy."""
    assert _UNSAFE_CONTEXT_PATTERNS == ()


@pytest.mark.parametrize(
    "goal",
    [
        "Build a CSV to JSON converter that honours the data contract",
        "Add a permissive open-source license picker to the CLI",
        "Implement password hashing and authentication for the local app",
        "Deploy the static site preview and publish release notes",
        "Store the API key and access token the user pastes in",
        "Charge the customer's credit card for the subscription",
    ],
)
def test_everyday_keywords_do_not_veto_safe_default_closure(goal: str) -> None:
    """Goals full of formerly-blocked keywords no longer trip the matcher."""
    ledger = SeedDraftLedger.from_goal(goal)
    assert _unsafe_context_reason(ledger, goal=goal, pending_question=None) is None
