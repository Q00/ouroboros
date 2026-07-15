"""Pure reducer: EventStore events → a provider-tagged board projection.

The board is the projection a human reads: one card per execution node (an AC or
sub-AC worker), placed in a status column, badged with the PROVIDER that ran it.
The provider is not invented here — ``execution.session.started`` already carries
``runtime_backend`` per ``node_id`` (verified: real runs show ``codex_cli`` and
``claude`` side by side), so a multi-provider run renders as mixed-provider cards
with zero new instrumentation.

Kept pure (events in → dict out), Textual-free and web-free, so it is trivially
testable and reusable by ANY transport: the web Kanban (SSE) renders the status
columns, and the TUI folds the SAME output to tag each AC/worker row with its
provider. One reducer, many surfaces — no dual-reducer drift.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ouroboros.observability.frugality_retrospective import (
    project_frugality_retrospective,
)

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
_TELEMETRY_EVENTS = frozenset(
    {
        "execution.ac.model_routed",
        "execution.ac.token_attribution.reported",
    }
)


# Terminal statuses: once an AUTHORITATIVE event sets one, a node is finished
# and a later coarse snapshot must not drag it backwards.
_TERMINAL = frozenset({"completed", "failed"})


@dataclass
class ProviderLedger:
    """Incrementally folded per-node board identity — ONE derivation, two consumers.

    Encapsulates exactly the provider rules :func:`reduce_board` applies:
    per-worker ``runtime_backend`` from ``execution.session.started`` (keyed by
    ``node_id``) wins; the run-level backend from ``orchestrator.session.started``
    is the fallback for nodes without a per-worker session (simple runs).

    It also folds the frugality telemetry the Kanban badges: per-node model tier /
    model id (``execution.ac.model_routed``, latest routing wins so an escalated
    retry overwrites) and cumulative runtime token spend
    (``execution.ac.token_attribution.reported``, summed across attempts plus a
    run total). These share the ledger so the batch web reduce and any live fold
    derive identical values — the same anti-drift guarantee as the provider axis.

    ``reduce_board`` folds its events through a ledger internally, and the TUI
    folds live events through its own ledger via :func:`fold_provider_event` —
    O(1) per event, no accumulated event list — so the two surfaces cannot drift.
    """

    provider_by_node: dict[str, str] = field(default_factory=dict)
    run_provider: str | None = None
    tier_by_node: dict[str, str] = field(default_factory=dict)
    model_by_node: dict[str, str] = field(default_factory=dict)
    tokens_by_node: dict[str, float] = field(default_factory=dict)
    total_tokens: float = 0.0

    def resolve(self, node_id: object) -> str | None:
        """Provider for a node: per-worker if known, else the run-level backend."""
        if isinstance(node_id, str) and node_id in self.provider_by_node:
            return self.provider_by_node[node_id]
        return self.run_provider

    def resolve_tier(self, node_id: object) -> str | None:
        """Model tier a node was routed to (latest routing), or None if unknown."""
        if isinstance(node_id, str):
            return self.tier_by_node.get(node_id)
        return None

    def resolve_model(self, node_id: object) -> str | None:
        """Concrete model id a node was routed to (latest), or None if unknown."""
        if isinstance(node_id, str):
            return self.model_by_node.get(node_id)
        return None

    def resolve_tokens(self, node_id: object) -> float | None:
        """Cumulative runtime token spend for a node, or None if never reported."""
        if isinstance(node_id, str):
            return self.tokens_by_node.get(node_id)
        return None

    def providers(self) -> list[str]:
        """Sorted provider legend for the run (per-worker + run-level)."""
        found = {p for p in self.provider_by_node.values() if p}
        if self.run_provider:
            found.add(self.run_provider)
        return sorted(found)

    def reset(self) -> None:
        self.provider_by_node.clear()
        self.run_provider = None
        self.tier_by_node.clear()
        self.model_by_node.clear()
        self.tokens_by_node.clear()
        self.total_tokens = 0.0


def fold_provider_event(
    event_type: str,
    payload: Mapping[str, Any],
    *,
    ledger: ProviderLedger,
) -> bool:
    """Fold ONE event's provider information into ``ledger`` (merge, in place).

    Returns True when the ledger changed — consumers use this to re-render only
    when provider identity actually moved (a handful of times per run), never on
    node/status/tool chatter.
    """
    if event_type == "execution.session.started":
        node_id = payload.get("node_id")
        backend = payload.get("runtime_backend")
        if isinstance(node_id, str) and node_id and backend:
            backend_str = str(backend)
            if ledger.provider_by_node.get(node_id) != backend_str:
                ledger.provider_by_node[node_id] = backend_str
                return True
    elif event_type == "orchestrator.session.started":
        backend = payload.get("runtime_backend")
        if backend:
            backend_str = str(backend)
            if ledger.run_provider != backend_str:
                ledger.run_provider = backend_str
                return True
    return False


def _coerce_spend(value: object, *, reject_negative: bool = False) -> float | None:
    """A finite, non-bool numeric value, else None (omit — never render NaN).

    ``reject_negative`` additionally drops negative values — used for
    ``token_spend`` (a cumulative counter; negative is malformed per the
    frugality proof contract, mirroring the TUI's parse-time guard in
    ``src/ouroboros/tui/events.py``) but NOT for ``token_reduction_pct``
    (a signed delta where negative legitimately means spend increased).
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        spend = float(value)
    except (OverflowError, ValueError):  # e.g. int too large to convert to float
        return None
    if spend != spend or spend in (float("inf"), float("-inf")):  # NaN / inf guard
        return None
    if reject_negative and spend < 0:
        return None
    return spend


def fold_telemetry_event(
    event_type: str,
    payload: Mapping[str, Any],
    *,
    ledger: ProviderLedger,
) -> bool:
    """Fold ONE frugality-telemetry event into ``ledger`` (merge, in place).

    Mirrors :func:`fold_provider_event`: ``execution.ac.model_routed`` sets a
    node's model tier / model (latest routing wins, so an escalated retry
    overwrites) and ``execution.ac.token_attribution.reported`` adds its
    ``token_spend`` to the node's running total and the run total. Malformed
    values are dropped so a card never shows ``None``/``NaN``. Returns True when
    the ledger changed. The frugality *proof* is run-level, not per-node, so it is
    handled directly in :func:`reduce_board`, not here.
    """
    if event_type == "execution.ac.model_routed":
        node_id = payload.get("node_id")
        if not isinstance(node_id, str) or not node_id:
            return False
        changed = False
        tier = payload.get("model_tier")
        if tier and ledger.tier_by_node.get(node_id) != str(tier):
            ledger.tier_by_node[node_id] = str(tier)
            changed = True
        model = payload.get("model")
        if model and ledger.model_by_node.get(node_id) != str(model):
            ledger.model_by_node[node_id] = str(model)
            changed = True
        return changed
    if event_type == "execution.ac.token_attribution.reported":
        node_id = payload.get("node_id")
        spend = _coerce_spend(payload.get("token_spend"), reject_negative=True)
        if not isinstance(node_id, str) or not node_id or spend is None:
            return False
        ledger.tokens_by_node[node_id] = ledger.tokens_by_node.get(node_id, 0.0) + spend
        ledger.total_tokens += spend
        return True
    return False


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
    # Provider derivation is delegated to the SAME ledger the TUI folds live —
    # the single source of truth for per-node provider + run-level fallback.
    ledger = ProviderLedger()
    session_by_node: dict[str, str] = {}
    tool_by_node: dict[str, str] = {}
    terminal: set[str] = set()  # node_ids finished by an authoritative event
    meta: dict[str, Any] = {
        "execution_id": execution_id,
        "session_id": None,
        "goal": None,
        "phase": None,
        "activity": None,
        "completed": 0,
        "total": 0,
        "provider": None,
        "total_tokens": 0.0,
        "frugality": None,
        "frugality_retrospective": None,
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
            fold_provider_event(event_type, payload, ledger=ledger)
            node_id = payload.get("node_id")
            if isinstance(node_id, str) and node_id:
                card = _card(cards, node_id)
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

        elif event_type in _TELEMETRY_EVENTS:
            # Per-AC model tier/model + runtime token spend — folded through the
            # SAME ledger as provider so batch reduce and any live fold agree.
            fold_telemetry_event(event_type, payload, ledger=ledger)

        elif event_type == "execution.frugality_proof.evaluated":
            # Run-end frugality proof (run-level, not per-node): a compact summary
            # for the header. Omit malformed fields rather than render None/NaN.
            frugality: dict[str, Any] = {}
            status = payload.get("status")
            if status:
                frugality["status"] = str(status)
            reduction = _coerce_spend(payload.get("token_reduction_pct"))
            if reduction is not None:
                frugality["token_reduction_pct"] = reduction
            reason = payload.get("reason")
            if reason:
                frugality["reason"] = str(reason)
            if frugality:
                meta["frugality"] = frugality

        elif event_type == "execution.frugality_retrospective.reported":
            retrospective = project_frugality_retrospective(payload)
            if retrospective is not None:
                meta["frugality_retrospective"] = retrospective

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
            fold_provider_event(event_type, payload, ledger=ledger)
            if ledger.run_provider:
                meta["provider"] = ledger.run_provider
            if payload.get("seed_goal") and not meta["goal"]:
                meta["goal"] = payload["seed_goal"]

        elif event_type == "execution.session.completed":
            if payload.get("goal") and not meta["goal"]:
                meta["goal"] = payload["goal"]

    for node_id, card in cards.items():
        # Per-worker provider (execution.session.started) wins; otherwise fall back
        # to the run-level backend (orchestrator.session.started) so simple runs are
        # still provider-tagged.
        card["provider"] = ledger.resolve(node_id)
        card["session_id"] = session_by_node.get(node_id)
        card["tool"] = tool_by_node.get(node_id)
        # Frugality telemetry, stamped exactly like the provider above (per-node,
        # None when the run never reported it — the surface omits absent fields).
        card["model_tier"] = ledger.resolve_tier(node_id)
        card["model"] = ledger.resolve_model(node_id)
        card["tokens"] = ledger.resolve_tokens(node_id)
        card.setdefault("title", node_id)
        card.setdefault("status", "pending")

    # Run-total token spend for the header (0.0 when no attribution was reported).
    meta["total_tokens"] = ledger.total_tokens

    columns: dict[str, list[dict[str, Any]]] = {col: [] for col in COLUMNS}
    for card in sorted(
        cards.values(),
        key=lambda c: (c.get("ac_index") or 0, c.get("depth") or 0, c["id"]),
    ):
        column = card["status"] if card["status"] in columns else "executing"
        columns[column].append(card)

    # Providers present in this run — lets the UI build a stable legend. Includes
    # the run-level backend so a simple run's single provider still shows.
    return {"meta": meta, "columns": columns, "providers": ledger.providers()}


__all__ = [
    "COLUMNS",
    "ProviderLedger",
    "fold_provider_event",
    "fold_telemetry_event",
    "reduce_board",
]
