"""TUI screen modules.

This package contains the various screens for the Ouroboros TUI:
- Dashboard: Main monitoring view (legacy)
- DashboardV2: Tree-centric command center
- DashboardV3: Split view with node detail + enhanced graph (recommended)
- HUDDashboardScreen: Enhanced dashboard with HUD components (agents, tokens, progress, events)
- Execution: Detailed execution view
- Logs: Log viewer
- Debug: Debug/inspect view
"""

from ouroboros.tui.screens.dashboard import DashboardScreen
from ouroboros.tui.screens.dashboard_v2 import DashboardScreenV2
from ouroboros.tui.screens.dashboard_v3 import DashboardScreenV3
from ouroboros.tui.screens.debug import DebugScreen
from ouroboros.tui.screens.execution import ExecutionScreen
from ouroboros.tui.screens.hud_dashboard import HUDDashboardScreen
from ouroboros.tui.screens.logs import LogsScreen

__all__ = [
    "DashboardScreen",
    "DashboardScreenV2",
    "DashboardScreenV3",
    "HUDDashboardScreen",
    "DebugScreen",
    "ExecutionScreen",
    "LogsScreen",
]
