"""Timeline View Component for Streamlit Dashboard.

This module provides timeline visualization components that display
Gemini 3's decision-making process across iterations.

Features:
- Interactive timeline with zoom and pan
- Color-coded phases and events
- Insight overlay on timeline
- Drill-down capability for individual iterations
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ouroboros.dashboard.gemini_analyzer import (
    IterationData,
    AnalysisInsight,
    FullHistoryAnalysis,
)


@dataclass
class TimelineEvent:
    """Single event on the timeline.

    Attributes:
        timestamp: When the event occurred
        iteration_id: Related iteration (if applicable)
        event_type: Type of event (iteration/insight/phase_change)
        title: Short title
        description: Detailed description
        color: CSS color for visualization
        metadata: Additional data
    """
    timestamp: datetime
    iteration_id: int | None
    event_type: str
    title: str
    description: str
    color: str
    metadata: dict[str, Any]


class TimelineView:
    """Timeline visualization builder for Streamlit.

    This class converts iteration data and Gemini analysis results
    into timeline events that can be rendered in Streamlit.

    Usage:
        timeline = TimelineView()
        events = timeline.build_timeline(iterations, analysis)

        # In Streamlit:
        timeline.render_streamlit(events)
    """

    # Color scheme for different event types
    COLORS = {
        "iteration": "#4A90D9",
        "phase_discover": "#9B59B6",
        "phase_define": "#3498DB",
        "phase_develop": "#2ECC71",
        "phase_deliver": "#E74C3C",
        "insight_pattern": "#F39C12",
        "insight_anomaly": "#E74C3C",
        "insight_recommendation": "#1ABC9C",
        "insight_critical": "#C0392B",
        "success": "#27AE60",
        "failure": "#E74C3C",
        "gemini_call": "#8E44AD",
    }

    def __init__(self) -> None:
        """Initialize the timeline view."""
        pass

    def build_timeline(
        self,
        iterations: list[IterationData],
        analysis: FullHistoryAnalysis | None = None,
    ) -> list[TimelineEvent]:
        """Build timeline events from iterations and analysis.

        Args:
            iterations: List of iteration data points.
            analysis: Optional Gemini analysis results.

        Returns:
            List of timeline events sorted by timestamp.
        """
        events: list[TimelineEvent] = []

        # Add iteration events
        for it in iterations:
            phase_color = self._get_phase_color(it.phase)
            status_indicator = "success" if "success" in it.result.lower() else "failure"

            events.append(TimelineEvent(
                timestamp=it.timestamp,
                iteration_id=it.iteration_id,
                event_type="iteration",
                title=f"Iteration {it.iteration_id}",
                description=f"{it.action}\nResult: {it.result}",
                color=phase_color,
                metadata={
                    "phase": it.phase,
                    "action": it.action,
                    "result": it.result,
                    "state": it.state,
                    "metrics": it.metrics,
                    "status": status_indicator,
                },
            ))

        # Add insight events from analysis
        if analysis:
            # Create synthetic timestamps for insights based on affected iterations
            for insight in analysis.insights:
                if insight.affected_iterations:
                    # Use the first affected iteration's timestamp
                    affected = [
                        it for it in iterations
                        if it.iteration_id in insight.affected_iterations
                    ]
                    if affected:
                        timestamp = affected[0].timestamp
                    else:
                        timestamp = iterations[-1].timestamp if iterations else datetime.now()
                else:
                    timestamp = iterations[-1].timestamp if iterations else datetime.now()

                events.append(TimelineEvent(
                    timestamp=timestamp,
                    iteration_id=insight.affected_iterations[0] if insight.affected_iterations else None,
                    event_type=f"insight_{insight.insight_type}",
                    title=insight.title,
                    description=insight.description,
                    color=self.COLORS.get(f"insight_{insight.insight_type}", "#95A5A6"),
                    metadata={
                        "insight_type": insight.insight_type,
                        "confidence": insight.confidence,
                        "affected_iterations": insight.affected_iterations,
                        "evidence": insight.evidence,
                    },
                ))

        # Sort by timestamp
        events.sort(key=lambda e: e.timestamp)

        return events

    def _get_phase_color(self, phase: str) -> str:
        """Get color for a phase."""
        phase_lower = phase.lower()
        if "discover" in phase_lower:
            return self.COLORS["phase_discover"]
        if "define" in phase_lower:
            return self.COLORS["phase_define"]
        if "develop" in phase_lower:
            return self.COLORS["phase_develop"]
        if "deliver" in phase_lower:
            return self.COLORS["phase_deliver"]
        return self.COLORS["iteration"]

    def get_phase_transitions(
        self,
        iterations: list[IterationData],
    ) -> list[tuple[int, str, str]]:
        """Identify phase transitions in the iteration history.

        Args:
            iterations: List of iterations.

        Returns:
            List of (iteration_id, from_phase, to_phase) tuples.
        """
        transitions: list[tuple[int, str, str]] = []
        prev_phase = None

        for it in iterations:
            if prev_phase and it.phase != prev_phase:
                transitions.append((it.iteration_id, prev_phase, it.phase))
            prev_phase = it.phase

        return transitions

    def create_streamlit_config(
        self,
        events: list[TimelineEvent],
        analysis: FullHistoryAnalysis | None = None,
    ) -> dict[str, Any]:
        """Create configuration for Streamlit visualization.

        This generates the data structure needed to render the timeline
        in Streamlit using plotly or altair.

        Args:
            events: List of timeline events.
            analysis: Optional analysis for summary metrics.

        Returns:
            Configuration dict for Streamlit rendering.
        """
        # Prepare data for visualization
        timeline_data = {
            "events": [
                {
                    "timestamp": e.timestamp.isoformat(),
                    "iteration": e.iteration_id,
                    "type": e.event_type,
                    "title": e.title,
                    "description": e.description,
                    "color": e.color,
                    **e.metadata,
                }
                for e in events
            ],
            "colors": self.COLORS,
        }

        # Add analysis summary if available
        if analysis:
            timeline_data["summary"] = {
                "total_iterations": analysis.total_iterations,
                "token_count": analysis.token_count,
                "analysis_time_ms": analysis.analysis_time_ms,
                "insight_count": len(analysis.insights),
                "devil_advocate_critique": analysis.devil_advocate_critique,
                "executive_summary": analysis.summary,
            }

            # Add trajectory data
            timeline_data["trajectories"] = [
                {
                    "dimension": t.dimension,
                    "values": t.values,
                    "trend": t.trend,
                    "inflection_points": t.inflection_points,
                }
                for t in analysis.trajectories
            ]

        return timeline_data


__all__ = [
    "TimelineEvent",
    "TimelineView",
]
