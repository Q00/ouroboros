"""The events->board reducer is ONE projection shared by the web Kanban and TUI.

These tests lock the D2 contract: the reducer lives in ``ouroboros.dashboard.board``
(re-exported by ``ouroboros.dashboard_web.kanban`` for the web surface), and the
TUI folds the SAME reducer output to tag provider identity — so the two surfaces
can never drift on who ran what.
"""

from __future__ import annotations

from typing import Any

from ouroboros.dashboard.board import BOARD_EVENT_TYPES, reduce_board
from ouroboros.events.base import BaseEvent
from ouroboros.tui.app import OuroborosTUI

# A fixed, mixed-provider run: a run-level backend (claude) plus one worker that
# ran on codex_cli. ac_1 must resolve to its per-worker provider; ac_2 has no
# per-worker session, so it falls back to the run-level backend.
_RUN: list[tuple[str, dict[str, Any]]] = [
    (
        "orchestrator.session.started",
        {"execution_id": "exec_1", "runtime_backend": "claude", "seed_goal": "Ship it"},
    ),
    ("execution.node.created", {"node_id": "ac_1", "label": "First AC", "status": "executing"}),
    (
        "execution.session.started",
        {"node_id": "ac_1", "runtime_backend": "codex_cli", "session_id": "worker_1"},
    ),
    ("execution.node.created", {"node_id": "ac_2", "label": "Second AC", "status": "pending"}),
]


def _raw_events() -> list[dict[str, Any]]:
    """The web reader's shape: ``{"event_type", "payload"}`` rows."""
    return [{"event_type": t, "payload": d} for t, d in _RUN]


def _base_events() -> list[BaseEvent]:
    """The TUI's shape: ``BaseEvent`` objects off the same run."""
    return [
        BaseEvent(type=t, aggregate_type="execution", aggregate_id="exec_1", data=d)
        for t, d in _RUN
    ]


def _providers_from_board(board: dict[str, Any]) -> dict[str, str]:
    providers: dict[str, str] = {}
    for column in board["columns"].values():
        for card in column:
            if isinstance(card.get("provider"), str) and card["provider"]:
                providers[card["id"]] = card["provider"]
    return providers


class TestSharedReducerLocation:
    def test_reducer_importable_from_shared_module(self) -> None:
        """The reducer resolves from the shared home and still folds a board."""
        board = reduce_board(_raw_events(), execution_id="exec_1")
        assert set(board) == {"meta", "columns", "providers"}
        assert board["providers"] == ["claude", "codex_cli"]

    def test_web_shim_reexports_same_object(self) -> None:
        """The web surface's import path is the very same reducer function."""
        from ouroboros.dashboard_web.kanban import reduce_board as web_reduce_board

        assert web_reduce_board is reduce_board


class TestNoDualReducerDrift:
    def test_web_and_tui_agree_on_provider_per_node(self) -> None:
        """Web board and TUI fold produce identical provider-per-node maps."""
        web_board = reduce_board(_raw_events(), execution_id="exec_1")
        web_providers = _providers_from_board(web_board)

        app = OuroborosTUI(execution_id="exec_1")
        for event in _base_events():
            app._ingest_board_event(event)

        # Identical output from the ONE reducer — this is the anti-drift assertion.
        assert app.state.provider_by_node == web_providers
        assert app.state.provider_by_node == {"ac_1": "codex_cli", "ac_2": "claude"}
        assert app.state.board_providers == web_board["providers"]

    def test_board_event_types_gate_ingestion(self) -> None:
        """Irrelevant events (e.g. tool spam) never enter the folded tail."""
        assert "execution.tool.started" not in BOARD_EVENT_TYPES

        app = OuroborosTUI(execution_id="exec_1")
        app._ingest_board_event(
            BaseEvent(
                type="execution.tool.started",
                aggregate_type="execution",
                aggregate_id="exec_1",
                data={"node_id": "ac_1", "tool_name": "Read"},
            )
        )
        assert app.state.board_events == []
        assert app.state.provider_by_node == {}


class TestProviderIdentityReachesTui:
    def test_provider_stamped_onto_tree_nodes(self) -> None:
        """Folding provider identity annotates the TUI's ac_tree nodes in place."""
        app = OuroborosTUI(execution_id="exec_1")
        # A tree the TUI would have built from workflow progress / subtask events.
        app._state.ac_tree = {
            "root_id": "root",
            "nodes": {
                "root": {"id": "root", "content": "ACs", "children_ids": ["ac_1", "ac_2"]},
                "ac_1": {"id": "ac_1", "content": "First AC", "status": "executing"},
                "ac_2": {"id": "ac_2", "content": "Second AC", "status": "pending"},
            },
        }

        for event in _base_events():
            app._ingest_board_event(event)

        nodes = app.state.ac_tree["nodes"]
        assert nodes["ac_1"]["provider"] == "codex_cli"
        assert nodes["ac_2"]["provider"] == "claude"
        # The structural root has no card, so it is never mis-tagged.
        assert "provider" not in nodes["root"]

    def test_provider_stamped_via_node_id_alias(self) -> None:
        """A tree node keyed differently is matched through its ``node_id``."""
        app = OuroborosTUI(execution_id="exec_1")
        app._state.ac_tree = {
            "root_id": "root",
            "nodes": {
                "root": {"id": "root", "children_ids": ["ac_1"]},
                # Tree keyed by a legacy id but carrying the canonical node_id.
                "ac_1": {"id": "legacy_1", "node_id": "ac_1", "status": "executing"},
            },
        }
        for event in _base_events():
            app._ingest_board_event(event)

        assert app.state.ac_tree["nodes"]["ac_1"]["provider"] == "codex_cli"

    def test_reset_clears_provider_state(self) -> None:
        """set_execution wipes the folded provider state for the next run."""
        app = OuroborosTUI(execution_id="exec_1")
        for event in _base_events():
            app._ingest_board_event(event)
        assert app.state.provider_by_node

        app.set_execution("exec_2")
        assert app.state.provider_by_node == {}
        assert app.state.board_events == []
        assert app.state.board_providers == []
