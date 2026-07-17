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
