"""TUI screen modules.

This package contains the various screens for the Ouroboros TUI:
- Dashboard: Main monitoring view
- Execution: Detailed execution view
- Logs: Log viewer
- Debug: Debug/inspect view
"""

from ouroboros.tui.screens.dashboard import DashboardScreen
from ouroboros.tui.screens.debug import DebugScreen
from ouroboros.tui.screens.execution import ExecutionScreen
from ouroboros.tui.screens.logs import LogsScreen

__all__ = [
    "DashboardScreen",
    "DebugScreen",
    "ExecutionScreen",
    "LogsScreen",
]
