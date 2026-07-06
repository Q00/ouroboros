"""Shared, transport-neutral dashboard projection.

``board.py`` is the single ``events -> board`` derivation consumed by BOTH the
web Kanban (``ouroboros.dashboard_web``) and the TUI (``ouroboros.tui``):
``reduce_board`` folds a full event list into the Kanban board, and the SAME
provider rules are exposed incrementally via ``ProviderLedger`` /
``fold_provider_event`` for live consumers. One derivation, pure, Textual-free
and web-free — that is what kills dual-reducer drift: every surface tags the
same provider per node from the same fold.
"""

from __future__ import annotations

from ouroboros.dashboard.board import (
    COLUMNS,
    ProviderLedger,
    fold_provider_event,
    reduce_board,
)

__all__ = ["COLUMNS", "ProviderLedger", "fold_provider_event", "reduce_board"]
