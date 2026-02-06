"""TUI widget modules.

This package contains reusable widgets for the Ouroboros TUI:
- PhaseProgress: Phase progress indicator showing Double Diamond phases
- ACTree: AC decomposition tree visualization
- ACProgress: AC progress list with status and timing
- DriftMeter: Drift score visualization with thresholds
- CostTracker: Cost and token usage display
- AgentActivity: Current agent tool/file/thinking display
- ParallelGraph: Parallel AC execution graph (Graph LR visualization)
"""

from ouroboros.tui.widgets.ac_progress import ACProgressItem, ACProgressWidget
from ouroboros.tui.widgets.ac_tree import ACTreeWidget
from ouroboros.tui.widgets.agent_activity import AgentActivityWidget
from ouroboros.tui.widgets.cost_tracker import CostTrackerWidget
from ouroboros.tui.widgets.drift_meter import DriftMeterWidget
from ouroboros.tui.widgets.parallel_graph import GraphNode, ParallelGraphWidget
from ouroboros.tui.widgets.phase_progress import PhaseProgressWidget

__all__ = [
    "ACProgressItem",
    "ACProgressWidget",
    "ACTreeWidget",
    "AgentActivityWidget",
    "CostTrackerWidget",
    "DriftMeterWidget",
    "GraphNode",
    "ParallelGraphWidget",
    "PhaseProgressWidget",
]
