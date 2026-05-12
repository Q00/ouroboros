"""Tests for the material-progress taxonomy module."""

from __future__ import annotations

from ouroboros.evolution.material_progress import (
    EXECUTION_MATERIAL_EVENTS,
    LINEAGE_MATERIAL_EVENTS,
    MATERIAL_EVENTS_BY_SURFACE,
    SESSION_MATERIAL_EVENTS,
    TERMINAL_AC_STATUSES,
)

# ---------------------------------------------------------------------------
# Pairwise disjointness
# ---------------------------------------------------------------------------


def test_lineage_and_execution_are_disjoint() -> None:
    overlap = LINEAGE_MATERIAL_EVENTS & EXECUTION_MATERIAL_EVENTS
    assert not overlap, f"Shared events: {overlap}"


def test_lineage_and_session_are_disjoint() -> None:
    overlap = LINEAGE_MATERIAL_EVENTS & SESSION_MATERIAL_EVENTS
    assert not overlap, f"Shared events: {overlap}"


def test_execution_and_session_are_disjoint() -> None:
    overlap = EXECUTION_MATERIAL_EVENTS & SESSION_MATERIAL_EVENTS
    assert not overlap, f"Shared events: {overlap}"


# ---------------------------------------------------------------------------
# Surface prefix sanity checks
# ---------------------------------------------------------------------------


def test_lineage_events_have_lineage_prefix() -> None:
    non_lineage = [e for e in LINEAGE_MATERIAL_EVENTS if not e.startswith("lineage.")]
    assert not non_lineage, f"Unexpected prefixes: {non_lineage}"


def test_execution_events_have_execution_prefix() -> None:
    non_execution = [e for e in EXECUTION_MATERIAL_EVENTS if not e.startswith("execution.")]
    assert not non_execution, f"Unexpected prefixes: {non_execution}"


def test_session_events_have_orchestrator_prefix() -> None:
    non_orchestrator = [e for e in SESSION_MATERIAL_EVENTS if not e.startswith("orchestrator.")]
    assert not non_orchestrator, f"Unexpected prefixes: {non_orchestrator}"


# ---------------------------------------------------------------------------
# MATERIAL_EVENTS_BY_SURFACE coverage
# ---------------------------------------------------------------------------

_EXPECTED_SURFACES = {"lineage", "execution", "session", "auto", "agent_process"}


def test_all_five_surfaces_present() -> None:
    assert set(MATERIAL_EVENTS_BY_SURFACE.keys()) == _EXPECTED_SURFACES


def test_populated_surfaces_match_standalone_sets() -> None:
    assert MATERIAL_EVENTS_BY_SURFACE["lineage"] == LINEAGE_MATERIAL_EVENTS
    assert MATERIAL_EVENTS_BY_SURFACE["execution"] == EXECUTION_MATERIAL_EVENTS
    assert MATERIAL_EVENTS_BY_SURFACE["session"] == SESSION_MATERIAL_EVENTS


def test_empty_surfaces_are_explicitly_empty() -> None:
    assert MATERIAL_EVENTS_BY_SURFACE["auto"] == frozenset()
    assert MATERIAL_EVENTS_BY_SURFACE["agent_process"] == frozenset()


def test_populated_surfaces_are_non_empty() -> None:
    for surface in ("lineage", "execution", "session"):
        assert MATERIAL_EVENTS_BY_SURFACE[surface], f"Surface '{surface}' should be non-empty"


# ---------------------------------------------------------------------------
# TERMINAL_AC_STATUSES
# ---------------------------------------------------------------------------


def test_terminal_ac_statuses_non_empty() -> None:
    assert TERMINAL_AC_STATUSES


def test_terminal_ac_statuses_are_lowercase() -> None:
    non_lower = [s for s in TERMINAL_AC_STATUSES if s != s.lower()]
    assert not non_lower, f"Non-lowercase statuses: {non_lower}"


def test_terminal_ac_statuses_no_whitespace() -> None:
    with_ws = [s for s in TERMINAL_AC_STATUSES if s != s.strip()]
    assert not with_ws, f"Statuses with surrounding whitespace: {with_ws}"
