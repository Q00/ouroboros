"""Backwards-compatible shim for the web Kanban's reducer.

The reducer moved to :mod:`ouroboros.dashboard.board` so the TUI can share it
(D2). This module stays as the web surface's stable import point — ``server.py``,
the package ``__init__`` and the existing ``test_kanban`` suite keep importing
``reduce_board`` / ``COLUMNS`` from here unchanged.
"""

from __future__ import annotations

from ouroboros.dashboard.board import COLUMNS, reduce_board

__all__ = ["COLUMNS", "reduce_board"]
