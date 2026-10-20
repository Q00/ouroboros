"""Safe-default closure reads no keywords.

``finalize_safe_defaultable_gaps`` decides only from ledger structure (a
missing goal, a conflicting or blocked section, a section with no default
policy). A goal full of words such as ``contract``, ``license``, ``deploy`` or
``credentials`` closes like any other, and every default it writes is a
non-binding assumption.
"""

from __future__ import annotations

import pytest

from ouroboros.auto.ledger import LedgerStatus, SeedDraftLedger
from ouroboros.auto.safe_defaults import finalize_safe_defaultable_gaps


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
def test_goal_wording_never_vetoes_safe_default_closure(goal: str) -> None:
    ledger = SeedDraftLedger.from_goal(goal)

    finalization = finalize_safe_defaultable_gaps(ledger, goal=goal, provenance="test")

    assert finalization.completed
    assert not finalization.unsafe_gaps
    assert ledger.is_seed_ready()
    for section in finalization.defaulted_sections:
        for entry in ledger.sections[section].entries:
            if entry.key == f"{section}.safe_default_finalization":
                assert entry.status == LedgerStatus.DEFAULTED
                assert not entry.binds_seed


def test_structural_gaps_still_block_closure() -> None:
    ledger = SeedDraftLedger.from_goal("   ")

    finalization = finalize_safe_defaultable_gaps(ledger, goal="", provenance="test")

    assert not finalization.completed
    assert any(gap.startswith("goal:") for gap in finalization.unsafe_gaps)
