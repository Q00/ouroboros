"""StrEnum exhaustiveness safety tests.

Ensures that when new members are added to StrEnum types, all dispatch
sites that handle them are updated. These tests fail-fast when a new
enum member is added but not handled.
"""

from __future__ import annotations

from pathlib import Path
import re

from ouroboros.core.lineage import MutationAction

_SRC = Path(__file__).resolve().parents[2] / "src" / "ouroboros"


class TestMutationActionExhaustiveness:
    """Every MutationAction member must be handled in seed_generator._apply_mutations."""

    _SEED_GENERATOR = _SRC / "bigbang" / "seed_generator.py"

    def test_all_actions_in_apply_mutations(self) -> None:
        """Every MutationAction value appears in _apply_mutations if/elif chain."""
        source = self._SEED_GENERATOR.read_text()
        # Extract string comparisons: action == "add", action == "modify", etc.
        handled = set(re.findall(r'action\s*==\s*"(\w+)"', source))
        for member in MutationAction:
            assert member.value in handled, (
                f"MutationAction.{member.name} ('{member.value}') is not handled "
                f"in seed_generator.py _apply_mutations(). "
                f"Handled actions: {sorted(handled)}"
            )


class TestTerminationReasonDispatchCoverage:
    """_TERMINATION_DISPATCH must cover all TerminationReason members."""

    def test_dispatch_covers_all_termination_reasons(self) -> None:
        """Every TerminationReason member has a dispatch entry in loop.py."""
        from ouroboros.core.lineage import TerminationReason
        from ouroboros.evolution.loop import EvolutionaryLoop

        dispatch = EvolutionaryLoop._TERMINATION_DISPATCH
        for member in TerminationReason:
            assert member in dispatch, (
                f"TerminationReason.{member.name} is not in "
                f"EvolutionaryLoop._TERMINATION_DISPATCH. "
                f"Dispatched: {sorted(m.name for m in dispatch)}"
            )


class TestTerminationReasonProjectorCoverage:
    """Projector legacy/default mappings must cover all terminal event types."""

    def test_legacy_map_covers_known_defaults(self) -> None:
        """_LEGACY_REASON_MAP has entries for all historical default reason strings."""
        from ouroboros.evolution.projector import _LEGACY_REASON_MAP

        # These are the known legacy default strings from before enum introduction
        expected_keys = {"ontology_converged", "max_generations", "stagnation"}
        assert set(_LEGACY_REASON_MAP.keys()) == expected_keys

    def test_default_termination_covers_terminal_events(self) -> None:
        """_DEFAULT_TERMINATION has entries for all terminal event types."""
        from ouroboros.evolution.projector import _DEFAULT_TERMINATION
        from ouroboros.evolution.transitions import TERMINAL_EVENT_STATUS

        # Every terminal event type (except rewound, which doesn't set termination_reason)
        terminal_events_with_reason = {
            et for et in TERMINAL_EVENT_STATUS if et != "lineage.rewound"
        }
        assert set(_DEFAULT_TERMINATION.keys()) == terminal_events_with_reason
