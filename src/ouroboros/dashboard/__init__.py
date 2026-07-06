"""Shared, transport-neutral dashboard projection.

``board.py`` is the single ``events -> board`` reducer consumed by BOTH the web
Kanban (``ouroboros.dashboard_web``) and the TUI (``ouroboros.tui``). Keeping ONE
reducer here — pure, Textual-free and web-free — is what kills dual-reducer drift:
every surface tags the same provider per node from the same event fold.
"""

from __future__ import annotations

from ouroboros.dashboard.board import BOARD_EVENT_TYPES, COLUMNS, reduce_board

__all__ = ["BOARD_EVENT_TYPES", "COLUMNS", "reduce_board"]
