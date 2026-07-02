"""TUI HUD components for orchestration visibility.

This package provides high-level HUD (Heads-Up Display) components
for monitoring agent orchestration in the TUI dashboard.

Components:
- progress: Visual progress indicators
- event_log: Scrollable event history
"""

from ouroboros.tui.components.event_log import EventLog
from ouroboros.tui.components.progress import ProgressTracker

__all__ = [
    "EventLog",
    "ProgressTracker",
]
