"""TUI screen modules.

This package contains the various screens for the Ouroboros TUI:
- DashboardV3: Split view with node detail + enhanced graph (recommended)
- Execution: Detailed execution view
- Logs: Log viewer
- Debug: Debug/inspect view
"""

from ouroboros.tui.screens.confirm_rewind import ConfirmRewindScreen
from ouroboros.tui.screens.dashboard_v3 import DashboardScreenV3
from ouroboros.tui.screens.debug import DebugScreen
from ouroboros.tui.screens.execution import ExecutionScreen
from ouroboros.tui.screens.lineage_detail import LineageDetailScreen
from ouroboros.tui.screens.lineage_selector import LineageSelectorScreen
from ouroboros.tui.screens.logs import LogsScreen

__all__ = [
    "ConfirmRewindScreen",
    "DashboardScreenV3",
    "DebugScreen",
    "ExecutionScreen",
    "LineageDetailScreen",
    "LineageSelectorScreen",
    "LogsScreen",
]
