"""Ouroboros Terminal User Interface module.

This module provides an interactive TUI for real-time workflow monitoring
using the Textual framework.

Key components:
- OuroborosTUI: Main Textual app with keybindings
- Dashboard: Main monitoring view with status, phase progress, drift/cost
- Screens: Execution detail, log viewer, debug views
- Widgets: Phase progress, AC tree, drift meter, cost tracker

Usage:
    from ouroboros.tui import OuroborosTUI

    app = OuroborosTUI()
    await app.run_async()
"""

from ouroboros.tui.app import OuroborosTUI

__all__ = ["OuroborosTUI"]
