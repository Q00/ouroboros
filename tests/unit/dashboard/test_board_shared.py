"""The events->board provider derivation is ONE fold shared by web Kanban and TUI.

These tests lock the D2 contract: the reducer lives in ``ouroboros.dashboard.board``
(re-exported by ``ouroboros.dashboard_web.kanban`` for the web surface), and BOTH
``reduce_board`` (batch, web) and the TUI's live ingestion go through the same
``fold_provider_event``/``ProviderLedger`` — so the two surfaces can never drift
on who ran what.
"""

from __future__ import annotations

from typing import Any

from ouroboros.dashboard.board import ProviderLedger, fold_provider_event, reduce_board
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
    def test_reduce_board_and_incremental_fold_agree(self) -> None:
        """Batch reduce and repeated fold_provider_event derive identical providers.

        This is meaningful because reduce_board itself calls fold_provider_event
        internally — one derivation, two consumption modes.
        """
        ledger = ProviderLedger()
        for event_type, payload in _RUN:
            fold_provider_event(event_type, payload, ledger=ledger)

        web_board = reduce_board(_raw_events(), execution_id="exec_1")
        web_providers = _providers_from_board(web_board)

        assert web_providers == {"ac_1": "codex_cli", "ac_2": "claude"}
        for node_id, provider in web_providers.items():
            assert ledger.resolve(node_id) == provider
        assert ledger.providers() == web_board["providers"]

    def test_web_and_tui_agree_on_provider_per_node(self) -> None:
        """The TUI's live fold ends with the same provider map the web board shows."""
        web_board = reduce_board(_raw_events(), execution_id="exec_1")
        web_providers = _providers_from_board(web_board)

        app = OuroborosTUI(execution_id="exec_1")
        for event in _base_events():
            app._ingest_board_event(event)

        assert {
            node_id: app._provider_ledger.resolve(node_id) for node_id in web_providers
        } == web_providers
        # Per-worker map merged in place; run-level fallback lives on the ledger.
        assert app.state.provider_by_node == {"ac_1": "codex_cli"}
        assert app.state.provider_by_node is app._provider_ledger.provider_by_node
        assert app.state.board_providers == web_board["providers"]

    def test_fold_reports_change_only_on_provider_movement(self) -> None:
        """Non-provider events and repeats fold to False — no re-render churn."""
        ledger = ProviderLedger()
        assert (
            fold_provider_event(
                "execution.node.created",
                {"node_id": "ac_1", "status": "executing"},
                ledger=ledger,
            )
            is False
        )
        assert (
            fold_provider_event(
                "execution.tool.started",
                {"node_id": "ac_1", "tool_name": "Read"},
                ledger=ledger,
            )
            is False
        )

        payload = {"node_id": "ac_1", "runtime_backend": "codex_cli"}
        assert fold_provider_event("execution.session.started", payload, ledger=ledger) is True
        # Same provider again: no change, so a consumer must not re-render.
        assert fold_provider_event("execution.session.started", payload, ledger=ledger) is False


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
        # No per-worker session for ac_2 -> run-level fallback, like a web card.
        assert nodes["ac_2"]["provider"] == "claude"
        # The structural root is not a board card, so it is never tagged.
        assert "provider" not in nodes["root"]

    def test_provider_stamped_via_node_id_alias(self) -> None:
        """A tree node keyed differently is matched through its ``node_id``."""
        app = OuroborosTUI(execution_id="exec_1")
        app._state.ac_tree = {
            "root_id": "root",
            "nodes": {
                "root": {"id": "root", "children_ids": ["legacy_1"]},
                # Tree keyed by a legacy id but carrying the canonical node_id.
                "legacy_1": {"id": "legacy_1", "node_id": "ac_1", "status": "executing"},
            },
        }
        for event in _base_events():
            app._ingest_board_event(event)

        assert app.state.ac_tree["nodes"]["legacy_1"]["provider"] == "codex_cli"

    def test_reset_clears_provider_state(self) -> None:
        """set_execution wipes the folded provider state for the next run."""
        app = OuroborosTUI(execution_id="exec_1")
        for event in _base_events():
            app._ingest_board_event(event)
        assert app.state.provider_by_node
        assert app._provider_ledger.run_provider == "claude"

        app.set_execution("exec_2")
        assert app.state.provider_by_node == {}
        assert app.state.board_providers == []
        assert app._provider_ledger.run_provider is None
        # The ledger still wraps the SAME state dict after reset.
        assert app.state.provider_by_node is app._provider_ledger.provider_by_node
