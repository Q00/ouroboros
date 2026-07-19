"""Kanban board folding of the two new durable signals (Task 3):

* ``execution.ac.decomposition_attested`` (Task 1) — a trust badge per card.
* ``execution.ac.parked_for_operator`` (Task 2) — an escalation/parked
  indicator per card.

Neither event drives the status column; both are pure badge data folded onto
the existing per-node card.
"""

from __future__ import annotations

from typing import Any

from ouroboros.dashboard.board import reduce_board


def _events(*pairs: tuple[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"event_type": t, "payload": d} for t, d in pairs]


def _card_by_id(board: dict[str, Any], node_id: str) -> dict[str, Any]:
    for column in board["columns"].values():
        for card in column:
            if card["id"] == node_id:
                return card
    raise AssertionError(f"no card with id {node_id!r} in board {board!r}")


class TestDecompositionAttestedFolding:
    def test_trustworthy_verdict_is_folded_onto_the_card(self) -> None:
        board = reduce_board(
            _events(
                (
                    "execution.node.created",
                    {"node_id": "ac_1", "label": "Parent AC", "status": "executing"},
                ),
                (
                    "execution.ac.decomposition_attested",
                    {
                        "node_id": "ac_1",
                        "verdict": "trustworthy",
                        "trustworthy": True,
                        "failed_axis": None,
                        "failed_sibling_id": None,
                        "reason": "all siblings passed and parent gate re-confirmed",
                    },
                ),
            ),
            execution_id="exec_1",
        )

        card = _card_by_id(board, "ac_1")
        assert card["trust_verdict"] == "trustworthy"
        assert card["trustworthy"] is True
        # A badge, not a lifecycle signal — status is untouched.
        assert card["status"] == "executing"

    def test_untrustworthy_verdict_is_folded_onto_the_card(self) -> None:
        board = reduce_board(
            _events(
                (
                    "execution.ac.decomposition_attested",
                    {
                        "node_id": "ac_2",
                        "verdict": "untrustworthy",
                        "trustworthy": False,
                        "failed_axis": "parent_gate",
                        "failed_sibling_id": None,
                        "reason": "parent verify gate failed after decomposition",
                    },
                ),
            ),
            execution_id="exec_1",
        )

        card = _card_by_id(board, "ac_2")
        assert card["trust_verdict"] == "untrustworthy"
        assert card["trustworthy"] is False

    def test_missing_node_id_is_ignored(self) -> None:
        board = reduce_board(
            _events(("execution.ac.decomposition_attested", {"verdict": "trustworthy"})),
            execution_id="exec_1",
        )

        assert all(len(column) == 0 for column in board["columns"].values())

    def test_delayed_older_retry_cannot_hide_newer_untrustworthy_verdict(self) -> None:
        board = reduce_board(
            _events(
                (
                    "execution.ac.decomposition_attested",
                    {
                        "node_id": "ac_1",
                        "retry_attempt": 2,
                        "verdict": "untrustworthy",
                        "trustworthy": False,
                    },
                ),
                (
                    "execution.ac.decomposition_attested",
                    {
                        "node_id": "ac_1",
                        "retry_attempt": 1,
                        "verdict": "trustworthy",
                        "trustworthy": True,
                    },
                ),
            ),
            execution_id="exec_1",
        )

        card = _card_by_id(board, "ac_1")
        assert card["trust_verdict"] == "untrustworthy"
        assert card["trustworthy"] is False

    def test_same_retry_attempt_uses_chronology_as_tiebreaker(self) -> None:
        board = reduce_board(
            _events(
                (
                    "execution.ac.decomposition_attested",
                    {
                        "node_id": "ac_1",
                        "retry_attempt": 2,
                        "verdict": "untrustworthy",
                        "trustworthy": False,
                    },
                ),
                (
                    "execution.ac.decomposition_attested",
                    {
                        "node_id": "ac_1",
                        "retry_attempt": 2,
                        "verdict": "trustworthy",
                        "trustworthy": True,
                    },
                ),
            ),
            execution_id="exec_1",
        )

        card = _card_by_id(board, "ac_1")
        assert card["trust_verdict"] == "trustworthy"
        assert card["trustworthy"] is True

    def test_malformed_retry_attempt_cannot_override_valid_attempt(self) -> None:
        board = reduce_board(
            _events(
                (
                    "execution.ac.decomposition_attested",
                    {
                        "node_id": "ac_1",
                        "retry_attempt": 2,
                        "verdict": "untrustworthy",
                        "trustworthy": False,
                    },
                ),
                (
                    "execution.ac.decomposition_attested",
                    {
                        "node_id": "ac_1",
                        "retry_attempt": "3",
                        "verdict": "trustworthy",
                        "trustworthy": True,
                    },
                ),
            ),
            execution_id="exec_1",
        )

        card = _card_by_id(board, "ac_1")
        assert card["trust_verdict"] == "untrustworthy"
        assert card["trustworthy"] is False


class TestParkedForOperatorFolding:
    def test_parked_event_sets_escalation_state(self) -> None:
        board = reduce_board(
            _events(
                (
                    "execution.node.created",
                    {"node_id": "ac_3", "label": "Stubborn AC", "status": "executing"},
                ),
                (
                    "execution.ac.parked_for_operator",
                    {
                        "node_id": "ac_3",
                        "root_ac_index": 2,
                        "personas_tried": ["hacker", "researcher", "simplifier"],
                        "consecutive_terminal_failures": 7,
                        "backoff_seconds": 300.0,
                        "reason": "all lateral-thinking personas exhausted",
                    },
                ),
            ),
            execution_id="exec_1",
        )

        card = _card_by_id(board, "ac_3")
        assert card["escalation_state"] == "parked"
        assert card["escalation_personas_tried"] == 3
        # A badge, not a lifecycle signal — status is untouched (never FAILED).
        assert card["status"] == "executing"

    def test_missing_node_id_is_ignored(self) -> None:
        board = reduce_board(
            _events(("execution.ac.parked_for_operator", {"root_ac_index": 0})),
            execution_id="exec_1",
        )

        assert all(len(column) == 0 for column in board["columns"].values())


class TestParkedResolvedFolding:
    """Fix 8 (BLOCKING, PR #1648 review): a parked AC that later succeeds must
    have its parked badge cleared, not left showing ``completed`` AND still-
    ``parked`` forever."""

    def test_parked_resolved_clears_the_badge(self) -> None:
        board = reduce_board(
            _events(
                (
                    "execution.node.created",
                    {"node_id": "ac_3", "label": "Stubborn AC", "status": "executing"},
                ),
                (
                    "execution.ac.parked_for_operator",
                    {
                        "node_id": "ac_3",
                        "root_ac_index": 2,
                        "personas_tried": ["hacker", "researcher"],
                        "consecutive_terminal_failures": 5,
                        "backoff_seconds": 300.0,
                        "reason": "all lateral-thinking personas exhausted",
                    },
                ),
                (
                    "execution.ac.parked_resolved",
                    {"node_id": "ac_3", "root_ac_index": 2},
                ),
                (
                    "execution.ac.completed",
                    {"node_id": "ac_3", "success": True},
                ),
            ),
            execution_id="exec_1",
        )

        card = _card_by_id(board, "ac_3")
        assert "escalation_state" not in card
        assert "escalation_personas_tried" not in card
        assert card["status"] == "completed"

    def test_missing_node_id_is_ignored(self) -> None:
        board = reduce_board(
            _events(
                (
                    "execution.node.created",
                    {"node_id": "ac_4", "label": "AC", "status": "executing"},
                ),
                (
                    "execution.ac.parked_for_operator",
                    {"node_id": "ac_4", "root_ac_index": 3},
                ),
                ("execution.ac.parked_resolved", {"root_ac_index": 3}),
            ),
            execution_id="exec_1",
        )

        # No node_id on the resolution event: the badge stays (fail-safe —
        # never silently clear a badge without a resolvable target).
        card = _card_by_id(board, "ac_4")
        assert card["escalation_state"] == "parked"


class TestLateralEscalationProgressedFolding:
    """Round-4 follow-up: ``execution.ac.lateral_escalation_progressed`` is
    emitted on EVERY ladder iteration, so the board shows active
    persona-escalation progress (which persona is currently being tried)
    while it happens — not only once the AC is fully parked."""

    def test_progressed_event_sets_escalating_state_and_current_persona(self) -> None:
        board = reduce_board(
            _events(
                (
                    "execution.node.created",
                    {"node_id": "ac_5", "label": "Stubborn AC", "status": "executing"},
                ),
                (
                    "execution.ac.lateral_escalation_progressed",
                    {
                        "node_id": "ac_5",
                        "root_ac_index": 4,
                        "personas_tried": ["hacker"],
                        "consecutive_terminal_failures": 3,
                        "parked": False,
                        "persona": "hacker",
                    },
                ),
            ),
            execution_id="exec_1",
        )

        card = _card_by_id(board, "ac_5")
        assert card["escalation_state"] == "escalating"
        assert card["escalation_persona"] == "hacker"
        assert card["escalation_personas_tried"] == 1
        # A badge, not a lifecycle signal — status is untouched.
        assert card["status"] == "executing"

    def test_progressed_event_with_parked_true_sets_parked_state(self) -> None:
        board = reduce_board(
            _events(
                (
                    "execution.ac.lateral_escalation_progressed",
                    {
                        "node_id": "ac_6",
                        "root_ac_index": 5,
                        "personas_tried": ["hacker", "contrarian"],
                        "consecutive_terminal_failures": 6,
                        "parked": True,
                        "persona": None,
                    },
                ),
            ),
            execution_id="exec_1",
        )

        card = _card_by_id(board, "ac_6")
        assert card["escalation_state"] == "parked"
        assert card["escalation_personas_tried"] == 2

    def test_parked_resolved_clears_progressed_badges_too(self) -> None:
        board = reduce_board(
            _events(
                (
                    "execution.ac.lateral_escalation_progressed",
                    {
                        "node_id": "ac_7",
                        "root_ac_index": 6,
                        "personas_tried": ["hacker"],
                        "consecutive_terminal_failures": 3,
                        "parked": False,
                        "persona": "hacker",
                    },
                ),
                ("execution.ac.parked_resolved", {"node_id": "ac_7", "root_ac_index": 6}),
            ),
            execution_id="exec_1",
        )

        card = _card_by_id(board, "ac_7")
        assert "escalation_state" not in card
        assert "escalation_persona" not in card
        assert "escalation_personas_tried" not in card

    def test_missing_node_id_is_ignored(self) -> None:
        board = reduce_board(
            _events(
                (
                    "execution.ac.lateral_escalation_progressed",
                    {"root_ac_index": 0, "parked": False, "persona": "hacker"},
                ),
            ),
            execution_id="exec_1",
        )

        assert all(len(column) == 0 for column in board["columns"].values())
