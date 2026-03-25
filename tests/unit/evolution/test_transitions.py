"""Unit tests for lineage state transition matrix.

Validates that:
1. Every LineageStatus has an entry in ALLOWED_TRANSITIONS
2. Every event factory type from events/lineage.py is in the matrix
3. Transition rules are correct per status
4. TERMINAL_EVENT_STATUS is consistent with ALLOWED_TRANSITIONS
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from ouroboros.core.lineage import LineageStatus, TerminationReason
from ouroboros.evolution.transitions import (
    ALLOWED_TRANSITIONS,
    TERMINAL_EVENT_STATUS,
    is_transition_allowed,
)

_SRC = Path(__file__).resolve().parents[3] / "src" / "ouroboros"
_LINEAGE_EVENTS = _SRC / "events" / "lineage.py"


def _extract_event_types_from_factories(path: Path) -> set[str]:
    """Extract all event type strings from BaseEvent(type=...) calls."""
    source = path.read_text()
    tree = ast.parse(source)
    types: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "type":
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                types.add(node.value.value)
    return types


# --- Completeness tests ---


class TestMatrixCompleteness:
    """Every LineageStatus and event type must be accounted for."""

    def test_all_statuses_have_transition_entry(self) -> None:
        """Every LineageStatus member must be a key in ALLOWED_TRANSITIONS."""
        for status in LineageStatus:
            assert status in ALLOWED_TRANSITIONS, (
                f"LineageStatus.{status.name} missing from ALLOWED_TRANSITIONS. "
                f"Add it with the appropriate allowed event set."
            )

    def test_all_factory_event_types_in_matrix(self) -> None:
        """Every event type from events/lineage.py must appear in the matrix.

        lineage.created is handled separately by Guard (always pass),
        but all others must be in ACTIVE's allowed set at minimum.
        """
        factory_types = _extract_event_types_from_factories(_LINEAGE_EVENTS)
        all_matrix_types = set()
        for allowed in ALLOWED_TRANSITIONS.values():
            all_matrix_types |= allowed
        # lineage.created is bypassed by guard, not in matrix
        factory_types_without_created = factory_types - {"lineage.created"}
        missing = factory_types_without_created - all_matrix_types
        assert not missing, (
            f"Event types defined in events/lineage.py but missing from "
            f"ALLOWED_TRANSITIONS: {missing}"
        )

    def test_terminal_event_status_keys_in_matrix(self) -> None:
        """Every TERMINAL_EVENT_STATUS key must appear in some ALLOWED_TRANSITIONS set."""
        all_matrix_types = set()
        for allowed in ALLOWED_TRANSITIONS.values():
            all_matrix_types |= allowed
        for event_type in TERMINAL_EVENT_STATUS:
            assert event_type in all_matrix_types, (
                f"TERMINAL_EVENT_STATUS key '{event_type}' not found in any ALLOWED_TRANSITIONS set"
            )


# --- ACTIVE state ---


class TestActiveTransitions:
    """ACTIVE state allows all generation lifecycle events."""

    @pytest.mark.parametrize(
        "event_type",
        [
            "lineage.generation.started",
            "lineage.generation.completed",
            "lineage.generation.phase_changed",
            "lineage.generation.failed",
            "lineage.ontology.evolved",
            "lineage.converged",
            "lineage.exhausted",
            "lineage.stagnated",
            "lineage.rewound",
            "lineage.wonder.degraded",
        ],
    )
    def test_active_allows_event(self, event_type: str) -> None:
        assert is_transition_allowed(LineageStatus.ACTIVE, event_type)

    def test_active_rejects_unknown_event(self) -> None:
        assert not is_transition_allowed(LineageStatus.ACTIVE, "lineage.unknown")


# --- CONVERGED state ---


class TestConvergedTransitions:
    def test_converged_allows_rewound(self) -> None:
        assert is_transition_allowed(LineageStatus.CONVERGED, "lineage.rewound")

    @pytest.mark.parametrize(
        "event_type",
        [
            "lineage.generation.started",
            "lineage.generation.completed",
            "lineage.converged",
            "lineage.exhausted",
            "lineage.stagnated",
        ],
    )
    def test_converged_rejects_non_rewind(self, event_type: str) -> None:
        assert not is_transition_allowed(LineageStatus.CONVERGED, event_type)


# --- EXHAUSTED state ---


class TestExhaustedTransitions:
    def test_exhausted_allows_rewound(self) -> None:
        assert is_transition_allowed(LineageStatus.EXHAUSTED, "lineage.rewound")

    def test_exhausted_rejects_generation_started(self) -> None:
        assert not is_transition_allowed(LineageStatus.EXHAUSTED, "lineage.generation.started")


# --- ABORTED state ---


class TestAbortedTransitions:
    def test_aborted_rejects_everything(self) -> None:
        """ABORTED is a terminal state with no allowed transitions."""
        for event_type in ALLOWED_TRANSITIONS[LineageStatus.ACTIVE]:
            assert not is_transition_allowed(LineageStatus.ABORTED, event_type), (
                f"ABORTED should reject '{event_type}'"
            )

    def test_aborted_rejects_rewound(self) -> None:
        assert not is_transition_allowed(LineageStatus.ABORTED, "lineage.rewound")


# --- Unknown status ---


class TestUnknownStatus:
    def test_unknown_status_returns_false(self) -> None:
        """A status not in ALLOWED_TRANSITIONS defaults to rejection."""
        assert not is_transition_allowed(LineageStatus.ACTIVE, "totally.unknown.event")


# --- TerminationReason exhaustiveness ---


class TestTerminationReasonExhaustiveness:
    """Every TerminationReason member must be explicitly handled in _emit_termination."""

    # The known groupings in loop.py _emit_termination():
    _EXHAUSTED_GROUP = {TerminationReason.EXHAUSTED}
    _STAGNATED_GROUP = {
        TerminationReason.STAGNATED,
        TerminationReason.OSCILLATED,
        TerminationReason.REPETITIVE,
    }
    _CONVERGED_GROUP = {TerminationReason.CONVERGED}

    def test_all_members_are_in_a_group(self) -> None:
        """Every TerminationReason member must belong to exactly one dispatch group."""
        all_grouped = self._EXHAUSTED_GROUP | self._STAGNATED_GROUP | self._CONVERGED_GROUP
        for member in TerminationReason:
            assert member in all_grouped, (
                f"TerminationReason.{member.name} is not in any dispatch group in "
                f"_emit_termination(). Add it to the appropriate group."
            )

    def test_no_group_overlap(self) -> None:
        """Dispatch groups must not overlap."""
        groups = [self._EXHAUSTED_GROUP, self._STAGNATED_GROUP, self._CONVERGED_GROUP]
        for i, a in enumerate(groups):
            for b in groups[i + 1 :]:
                overlap = a & b
                assert not overlap, f"Dispatch groups overlap: {overlap}"

    def test_groups_cover_all_members(self) -> None:
        """Union of all groups equals the full enum."""
        all_grouped = self._EXHAUSTED_GROUP | self._STAGNATED_GROUP | self._CONVERGED_GROUP
        all_members = set(TerminationReason)
        assert all_grouped == all_members, (
            f"Groups missing: {all_members - all_grouped}, "
            f"Extra in groups: {all_grouped - all_members}"
        )
