"""Progress Indicators - Visual progress tracking.

Displays:
- Overall execution progress bar
- Phase-by-phase progress
- Milestone tracking
- ETA estimates
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import ProgressBar, Static

from ouroboros.tui.events import TUIState


class Phase(Enum):
    """Double Diamond phases."""

    DISCOVER = "discover"
    DEFINE = "define"
    DESIGN = "design"
    DELIVER = "deliver"


@dataclass
class Milestone:
    """A milestone in the execution."""

    id: str
    name: str
    completed: bool = False
    timestamp: datetime | None = None
    phase: Phase = Phase.DISCOVER


@dataclass
class PhaseProgress:
    """Progress for a single phase."""

    phase: Phase
    percent_complete: float = 0.0
    total_steps: int = 0
    completed_steps: int = 0
    current_step: str = ""
    eta_seconds: float | None = None


@dataclass
class OverallProgress:
    """Overall execution progress."""

    total_percent: float = 0.0
    total_acs: int = 0
    completed_acs: int = 0
    current_ac_index: int | None = None
    elapsed_seconds: float = 0.0
    eta_seconds: float | None = None
    elapsed_display: str = ""
    eta_display: str = ""


class ProgressTracker(Widget):
    """Progress tracking widget.

    Displays:
    - Overall progress bar with percentage
    - Phase-by-phase progress bars
    - Current activity and milestone tracking
    - ETA estimates

    Attributes:
        overall: Overall progress data.
        phases: Progress for each phase.
        milestones: List of milestones.
    """

    DEFAULT_CSS = """
    ProgressTracker {
        height: auto;
        min-height: 12;
        width: 100%;
        padding: 1;
        border: round $warning;
        background: $surface;
    }

    ProgressTracker > .header {
        text-style: bold;
        color: $warning;
        text-align: center;
        margin-bottom: 1;
    }

    ProgressTracker > .overall-section {
        width: 100%;
        margin-bottom: 1;
    }

    ProgressTracker > .overall-section > .overall-label {
        text-align: center;
        color: $text-muted;
        margin-bottom: 0;
    }

    ProgressTracker > .overall-section > .overall-bar {
        width: 100%;
    }

    ProgressTracker > .overall-section > .overall-stats {
        height: 1;
        width: 100%;
        text-align: center;
        color: $text-muted;
        margin-top: 0;
    }

    ProgressTracker > .phases-section {
        width: 100%;
        margin-top: 1;
    }

    ProgressTracker > .phases-section > .phases-header {
        text-style: bold;
        color: $text;
        margin-bottom: 1;
    }

    ProgressTracker > .phases-section > .phase-row {
        height: 2;
        width: 100%;
    }

    ProgressTracker > .phases-section > .phase-row > .phase-name {
        width: 12;
        color: $text;
    }

    ProgressTracker > .phases-section > .phase-row > .phase-bar-container {
        width: 1fr;
    }

    ProgressTracker > .phases-section > .phase-row > .phase-bar-container > ProgressBar {
        width: 100%;
    }

    ProgressTracker > .phases-section > .phase-row > .phase-percent {
        width: 6;
        text-align: right;
        color: $text-muted;
    }

    ProgressTracker > .current-activity {
        height: auto;
        min-height: 2;
        width: 100%;
        margin-top: 1;
        padding-top: 1;
        border-top: dashed $surface;
    }

    ProgressTracker > .current-activity > .activity-label {
        color: $text-muted;
    }

    ProgressTracker > .current-activity > .activity-text {
        color: $text;
        text-style: bold;
    }

    ProgressTracker > .milestones {
        height: auto;
        max-height: 6;
        width: 100%;
        margin-top: 1;
        overflow-y: auto;
    }

    ProgressTracker > .milestones > .milestone-item {
        height: 1;
        width: 100%;
    }

    ProgressTracker > .milestones > .milestone-item > .milestone-icon {
        width: 4;
    }

    ProgressTracker > .milestones > .milestone-item > .milestone-name {
        width: 1fr;
        color: $text;
    }

    ProgressTracker > .milestones > .milestone-item.completed > .milestone-icon {
        color: $success;
    }

    ProgressTracker > .milestones > .milestone-item.completed > .milestone-name {
        color: $text-muted;
        text-style: dim;
    }

    ProgressTracker > .milestones > .milestone-item.current > .milestone-icon {
        color: $warning;
    }

    ProgressTracker > .milestones > .milestone-item.current > .milestone-name {
        color: $warning;
        text-style: bold;
    }
    """

    overall: reactive[OverallProgress] = reactive(OverallProgress())
    phases: reactive[dict[Phase, PhaseProgress]] = reactive(
        {
            Phase.DISCOVER: PhaseProgress(Phase.DISCOVER),
            Phase.DEFINE: PhaseProgress(Phase.DEFINE),
            Phase.DESIGN: PhaseProgress(Phase.DESIGN),
            Phase.DELIVER: PhaseProgress(Phase.DELIVER),
        }
    )
    milestones: reactive[list[Milestone]] = reactive([])
    current_activity: reactive[str] = reactive("")

    def __init__(
        self,
        state: TUIState | None = None,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        """Initialize progress tracker.

        Args:
            state: TUIState for tracking progress.
            name: Widget name.
            id: Widget ID.
            classes: CSS classes.
        """
        super().__init__(name=name, id=id, classes=classes)
        self._state = state

    def compose(self) -> ComposeResult:
        """Compose the widget layout."""
        yield Static("╔══ PROGRESS TRACKER ══╗", classes="header")

        # Overall progress section
        with Static(classes="overall-section"):
            yield Static("Overall Progress", classes="overall-label")
            yield ProgressBar(
                id="overall-bar",
                show_eta=False,
                total=100,
            )
            yield Static(
                "0% complete",
                classes="overall-stats",
                id="overall-stats",
            )

        # Phases section
        with Static(classes="phases-section"):
            yield Static("Phase Progress", classes="phases-header")

            for phase in Phase:
                with Static(classes="phase-row"):
                    yield Static(phase.value.title(), classes="phase-name")
                    with Static(classes="phase-bar-container"):
                        yield ProgressBar(
                            id=f"phase-bar-{phase.value}",
                            show_eta=False,
                            total=100,
                        )
                    yield Static("0%", classes="phase-percent", id=f"phase-percent-{phase.value}")

        # Current activity
        with Static(classes="current-activity"):
            yield Static("Current:", classes="activity-label", id="activity-label")
            yield Static("Idle", classes="activity-text", id="activity-text")

        # Milestones (scrollable)
        with Static(classes="milestones", id="milestones-container"):
            pass  # Milestones added dynamically

    def on_mount(self) -> None:
        """Initialize display."""
        self._update_display()

    def _update_display(self) -> None:
        """Update all display elements."""
        overall = self.overall

        # Update overall bar
        try:
            bar = self.query_one("#overall-bar", ProgressBar)
            bar.progress = overall.total_percent
        except Exception:
            pass

        # Update overall stats
        try:
            stats = self.query_one("#overall-stats", Static)
            parts = [f"{overall.total_percent:.0f}% complete"]
            if overall.completed_acs > 0:
                parts.append(f"({overall.completed_acs}/{overall.total_acs} ACs)")
            if overall.elapsed_display:
                parts.append(f"[dim]{overall.elapsed_display} elapsed[/]")
            if overall.eta_display:
                parts.append(f"[dim]{overall.eta_display} remaining[/]")
            stats.update(" | ".join(parts))
        except Exception:
            pass

        # Update phase bars
        for phase in Phase:
            try:
                phase_progress = self.phases.get(phase)
                if not phase_progress:
                    continue

                bar = self.query_one(f"#phase-bar-{phase.value}", ProgressBar)
                bar.progress = phase_progress.percent_complete

                percent = self.query_one(f"#phase-percent-{phase.value}", Static)
                percent.update(f"{phase_progress.percent_complete:.0f}%")
            except Exception:
                pass

        # Update current activity
        try:
            activity_text = self.query_one("#activity-text", Static)
            activity_label = self.query_one("#activity-label", Static)

            if self.current_activity:
                activity_text.update(self.current_activity)
                activity_label.update("Current:")
            else:
                activity_text.update("Idle")
                activity_label.update("Status:")
        except Exception:
            pass

        # Update milestones
        self._update_milestones()

    def _update_milestones(self) -> None:
        """Update milestones display."""
        try:
            container = self.query_one("#milestones-container", Static)
            # Clear existing milestones (by removing children)
            container.remove_children()

            # Build milestone content as a single string
            milestone_lines = []
            for i, milestone in enumerate(self.milestones):
                # Determine styling based on state
                if milestone.completed:
                    icon = "[bold green]●[/]"
                    style = "dim"
                elif i == 0 or (self.milestones[i - 1].completed if i > 0 else False):
                    icon = "[bold yellow]◐[/]"
                    style = "bold yellow"
                else:
                    icon = "[dim]○[/]"
                    style = "dim"

                # Format milestone line
                name_style = f"[{style}]" if style != "dim" else "[dim]"
                milestone_lines.append(f"  {icon} {name_style}{milestone.name}[/]")

            # Update container with all milestones
            if milestone_lines:
                container.update("\n".join(milestone_lines))
            else:
                container.update("[dim]No milestones yet[/]")

        except Exception:
            pass

    def watch_overall(self, _: OverallProgress) -> None:
        """React to overall progress changes."""
        self._update_display()

    def watch_phases(self, _: dict[Phase, PhaseProgress]) -> None:
        """React to phase progress changes."""
        self._update_display()

    def watch_current_activity(self, _: str) -> None:
        """React to activity changes."""
        self._update_display()

    def watch_milestones(self, _: list[Milestone]) -> None:
        """React to milestone changes."""
        self._update_display()

    def update_from_state(self, state: TUIState) -> None:
        """Update tracker from TUIState.

        Args:
            state: Current TUI state.
        """
        # Extract AC progress from ac_tree
        nodes = state.ac_tree.get("nodes", {})
        total_acs = len([n for n in nodes.values() if n.get("depth") == 1])
        completed_acs = len(
            [n for n in nodes.values() if n.get("depth") == 1 and n.get("status") == "completed"]
        )

        percent = (completed_acs / total_acs * 100) if total_acs > 0 else 0.0

        self.overall = OverallProgress(
            total_percent=percent,
            total_acs=total_acs,
            completed_acs=completed_acs,
            elapsed_display="",  # Would be calculated from timestamps
        )

        # Update phase progress based on current_phase
        current_phase = state.current_phase.lower()
        for phase in Phase:
            if phase.value == current_phase:
                phase_progress = self.phases[phase]
                phase_progress.percent_complete = percent
                phase_progress.completed_steps = completed_acs
                phase_progress.total_steps = total_acs
                break

    def set_overall_progress(
        self,
        percent: float,
        completed: int,
        total: int,
        elapsed: str = "",
        eta: str = "",
    ) -> None:
        """Set overall progress.

        Args:
            percent: Percentage complete (0-100).
            completed: Number of completed items.
            total: Total number of items.
            elapsed: Elapsed time display string.
            eta: Estimated time remaining display string.
        """
        self.overall = OverallProgress(
            total_percent=percent,
            completed_acs=completed,
            total_acs=total,
            elapsed_display=elapsed,
            eta_display=eta,
        )

    def set_phase_progress(
        self,
        phase: Phase,
        percent: float,
        current_step: str = "",
    ) -> None:
        """Set progress for a specific phase.

        Args:
            phase: Phase to update.
            percent: Percentage complete (0-100).
            current_step: Current step description.
        """
        phases = dict(self.phases)
        phase_progress = phases.get(phase)
        if phase_progress:
            phase_progress.percent_complete = percent
            phase_progress.current_step = current_step
        self.phases = phases

    def set_current_activity(self, activity: str) -> None:
        """Set current activity display.

        Args:
            activity: Activity description.
        """
        self.current_activity = activity

    def add_milestone(
        self,
        milestone_id: str,
        name: str,
        phase: Phase = Phase.DISCOVER,
    ) -> None:
        """Add a milestone.

        Args:
            milestone_id: Unique milestone ID.
            name: Milestone display name.
            phase: Phase this milestone belongs to.
        """
        # Check if milestone already exists
        for m in self.milestones:
            if m.id == milestone_id:
                return

        milestones = list(self.milestones)
        milestones.append(
            Milestone(
                id=milestone_id,
                name=name,
                phase=phase,
            )
        )
        self.milestones = milestones

    def complete_milestone(self, milestone_id: str) -> None:
        """Mark a milestone as completed.

        Args:
            milestone_id: Milestone ID to complete.
        """
        milestones = []
        for m in self.milestones:
            if m.id == milestone_id:
                m.completed = True
                m.timestamp = datetime.now()
            milestones.append(m)
        self.milestones = milestones


__all__ = ["Milestone", "OverallProgress", "Phase", "PhaseProgress", "ProgressTracker"]
