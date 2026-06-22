"""Pure reducer: EventStore events → a provider-tagged Kanban board.

The board is the projection a human reads: one card per execution node (an AC or
sub-AC worker), placed in a status column, badged with the PROVIDER that ran it.
The provider is not invented here — ``execution.session.started`` already carries
``runtime_backend`` per ``node_id`` (verified: real runs show ``codex_cli`` and
``claude`` side by side), so a multi-provider run renders as mixed-provider cards
with zero new instrumentation.

Kept pure (events in → dict out) so it is trivially testable and reusable by any
transport (SSE now, websockets/SSR later).
"""

from __future__ import annotations

from typing import Any

# Status columns, in board order. Node statuses map onto these directly
# (verified distinct values: pending / executing / completed / failed); synonyms
# from other emitters are normalized via ``_STATUS_ALIASES``.
COLUMNS: tuple[str, ...] = ("pending", "executing", "completed", "failed")

_STATUS_ALIASES = {
    "running": "executing",
    "active": "executing",
    "in_progress": "executing",
    "done": "completed",
    "success": "completed",
    "succeeded": "completed",
    "error": "failed",
    "errored": "failed",
    "cancelled": "failed",
    "canceled": "failed",
}

_NODE_EVENTS = frozenset(
    {
        "execution.node.created",
        "execution.node.updated",
        "execution.subtask.updated",
    }
)
_TOOL_EVENTS = frozenset(
    {
        "execution.tool.started",
        "orchestrator.tool.called",
        "execution.coordinator.tool.started",
    }
)


# Terminal statuses: once an AUTHORITATIVE event sets one, a node is finished
# and a later coarse snapshot must not drag it backwards.
_TERMINAL = frozenset({"completed", "failed"})


def _normalize_status(status: object) -> str | None:
    if not isinstance(status, str) or not status:
        return None
    s = status.strip().lower()
    s = _STATUS_ALIASES.get(s, s)
    return s if s in COLUMNS else None


def _apply_status(
    card: dict[str, Any],
    terminal: set[str],
    node_id: str,
    status: str | None,
    *,
    authoritative: bool,
) -> None:
    """Set a card's status with terminal-state precedence.

    ``execution.ac.completed`` / ``execution.node.*`` are AUTHORITATIVE: they
    reflect the real per-node lifecycle, including genuine re-opens (a retry sends
    ``executing`` after ``completed``), so they always apply and update the
    terminal marker. ``workflow.progress.updated``'s AC snapshot is COARSE and
    lags the per-AC ``ac.completed`` — it must NOT downgrade a node already marked
    terminal (that caused cards to flicker DONE → IN PROGRESS). It may still
    upgrade a node TO a terminal state (the only card source for simple runs).
    """
    if not status:
        return
    if authoritative:
        card["status"] = status
        if status in _TERMINAL:
            terminal.add(node_id)
        else:
            terminal.discard(node_id)  # real re-open / retry
        return
    # Non-authoritative (snapshot): never drag a finished node backwards.
    if node_id in terminal and status not in _TERMINAL:
        return
    card["status"] = status
    if status in _TERMINAL:
        terminal.add(node_id)


def _card(cards: dict[str, dict[str, Any]], node_id: str) -> dict[str, Any]:
    return cards.setdefault(node_id, {"id": node_id, "status": "pending"})


def _upsert_ac_card(
    cards: dict[str, dict[str, Any]],
    terminal: set[str],
    item: Any,
    ordinal: int,
) -> None:
    """Upsert a card from one ``acceptance_criteria`` snapshot entry (COARSE source).

    Entries are normally node dicts (``node_id`` / ``content`` / ``status`` …) but
    may be plain strings in older/simpler payloads — both are supported. The card
    is keyed by ``node_id`` so it merges with per-worker ``execution.node.*`` /
    ``execution.session.started`` events for the same node. Status is applied via
    :func:`_apply_status` as NON-authoritative so a lagging snapshot cannot revert
    a node already finished by ``ac.completed``.
    """
    if isinstance(item, str):
        node_id = f"ac_{ordinal + 1}"
        card = _card(cards, node_id)
        card["title"] = item
        card.setdefault("ac_index", ordinal + 1)
        return
    if not isinstance(item, dict):
        return
    node_id = str(item.get("node_id") or item.get("ac_id") or f"ac_{ordinal + 1}")
    card = _card(cards, node_id)
    title = item.get("content") or item.get("label")
    if title:
        card["title"] = title
    _apply_status(
        card, terminal, node_id, _normalize_status(item.get("status")), authoritative=False
    )
    if item.get("depth") is not None:
        card["depth"] = item.get("depth")
    card["ac_index"] = (
        item.get("root_ac_number") or item.get("index") or card.get("ac_index") or ordinal + 1
    )
    card["parent_id"] = item.get("parent_node_id") or card.get("parent_id")


def reduce_board(
    events: list[dict[str, Any]],
    *,
    execution_id: str | None = None,
) -> dict[str, Any]:
    """Fold a list of EventStore rows into a Kanban board.

    Each event is ``{"event_type": str, "payload": dict}`` (the ``rowid`` may be
    present but is unused here). Order matters: later events overwrite earlier
    status/provider for the same node, so pass events in rowid/timestamp order.
    """
    cards: dict[str, dict[str, Any]] = {}
    provider_by_node: dict[str, str] = {}
    session_by_node: dict[str, str] = {}
    tool_by_node: dict[str, str] = {}
    terminal: set[str] = set()  # node_ids finished by an authoritative event
    run_backend: str | None = None
    meta: dict[str, Any] = {
        "execution_id": execution_id,
        "session_id": None,
        "goal": None,
        "phase": None,
        "activity": None,
        "completed": 0,
        "total": 0,
        "provider": None,
    }

    for ev in events:
        event_type = ev.get("event_type")
        payload = ev.get("payload")
        if not isinstance(payload, dict):
            continue

        if event_type in _NODE_EVENTS:
            node_id = payload.get("node_id")
            if not isinstance(node_id, str) or not node_id:
                continue
            card = _card(cards, node_id)
            title = payload.get("label") or payload.get("content")
            if title:
                card["title"] = title
            _apply_status(
                card,
                terminal,
                node_id,
                _normalize_status(payload.get("status")),
                authoritative=True,
            )
            if payload.get("depth") is not None:
                card["depth"] = payload.get("depth")
            card["parent_id"] = payload.get("parent_node_id") or card.get("parent_id")
            card["ac_index"] = (
                payload.get("root_ac_number") or payload.get("ac_index") or card.get("ac_index")
            )

        elif event_type == "execution.session.started":
            node_id = payload.get("node_id")
            if isinstance(node_id, str) and node_id:
                card = _card(cards, node_id)
                if payload.get("runtime_backend"):
                    provider_by_node[node_id] = str(payload["runtime_backend"])
                if payload.get("session_id"):
                    session_by_node[node_id] = str(payload["session_id"])
                card.setdefault("title", payload.get("acceptance_criterion") or node_id)
                # A started session is in flight unless a later status overrides it.
                if card.get("status") == "pending":
                    card["status"] = "executing"

        elif event_type == "execution.ac.completed":
            node_id = payload.get("node_id")
            if isinstance(node_id, str) and node_id:
                card = _card(cards, node_id)
                _apply_status(
                    card,
                    terminal,
                    node_id,
                    "completed" if payload.get("success") else "failed",
                    authoritative=True,
                )

        elif event_type in _TOOL_EVENTS:
            node_id = payload.get("node_id")
            tool = payload.get("tool_name") or payload.get("tool")
            if isinstance(node_id, str) and node_id and tool:
                tool_by_node[node_id] = str(tool)

        elif event_type == "workflow.progress.updated":
            if payload.get("completed_count") is not None:
                meta["completed"] = payload["completed_count"]
            if payload.get("total_count") is not None:
                meta["total"] = payload["total_count"]
            meta["phase"] = payload.get("current_phase") or meta["phase"]
            meta["activity"] = payload.get("activity") or meta["activity"]
            if payload.get("session_id"):
                meta["session_id"] = payload["session_id"]
            # ``acceptance_criteria`` is a FULL snapshot of every AC node (id /
            # content / status). It is present in EVERY run — including simple,
            # non-decomposed ones that never emit per-node ``execution.node.*``
            # events — so it is the universal card source. Keyed by node_id, it
            # merges cleanly with the richer per-worker node/session events.
            criteria = payload.get("acceptance_criteria")
            if isinstance(criteria, list):
                for ordinal, item in enumerate(criteria):
                    _upsert_ac_card(cards, terminal, item, ordinal)

        elif event_type == "orchestrator.session.started":
            # Run-level provider — the fallback tag for cards that have no
            # per-worker execution.session.started (i.e. simple runs).
            if payload.get("runtime_backend"):
                run_backend = str(payload["runtime_backend"])
                meta["provider"] = run_backend
            if payload.get("seed_goal") and not meta["goal"]:
                meta["goal"] = payload["seed_goal"]

        elif event_type == "execution.session.completed":
            if payload.get("goal") and not meta["goal"]:
                meta["goal"] = payload["goal"]

    for node_id, card in cards.items():
        # Per-worker provider (execution.session.started) wins; otherwise fall back
        # to the run-level backend (orchestrator.session.started) so simple runs are
        # still provider-tagged.
        card["provider"] = provider_by_node.get(node_id) or run_backend
        card["session_id"] = session_by_node.get(node_id)
        card["tool"] = tool_by_node.get(node_id)
        card.setdefault("title", node_id)
        card.setdefault("status", "pending")

    columns: dict[str, list[dict[str, Any]]] = {col: [] for col in COLUMNS}
    for card in sorted(
        cards.values(),
        key=lambda c: (c.get("ac_index") or 0, c.get("depth") or 0, c["id"]),
    ):
        column = card["status"] if card["status"] in columns else "executing"
        columns[column].append(card)

    # Providers present in this run — lets the UI build a stable legend. Includes
    # the run-level backend so a simple run's single provider still shows.
    providers = sorted(
        {p for p in provider_by_node.values() if p} | ({run_backend} if run_backend else set())
    )

    return {"meta": meta, "columns": columns, "providers": providers}


__all__ = ["COLUMNS", "reduce_board"]
