"""TUI HUD components for orchestration visibility.

This package provides high-level HUD (Heads-Up Display) components
for monitoring agent orchestration in the TUI dashboard.

Components:
- agents_panel: Agent pool status display
- token_tracker: Real-time cost tracking
- progress: Visual progress indicators
- event_log: Scrollable event history
"""

from ouroboros.tui.components.agents_panel import AgentsPanel
from ouroboros.tui.components.event_log import EventLog
from ouroboros.tui.components.progress import ProgressTracker
from ouroboros.tui.components.token_tracker import TokenTracker

__all__ = [
    "AgentsPanel",
    "EventLog",
    "ProgressTracker",
    "TokenTracker",
]
