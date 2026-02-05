"""AC progress list widget.

Displays acceptance criteria as a flat list with status,
progress bar, and timing information. Designed to mirror
the CLI workflow display.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Label, ProgressBar, Static

if TYPE_CHECKING:
    pass


# Status icons matching CLI display
STATUS_ICONS = {
    "pending": "○",
    "in_progress": "⚡",
    "completed": "✓",
    "failed": "✗",
}

STATUS_STYLES = {
    "pending": "dim",
    "in_progress": "yellow",
    "completed": "green",
    "failed": "red",
}


@dataclass
class ACProgressItem:
    """Single AC progress item for display.

    Attributes:
        index: 1-based AC index.
        content: AC content text.
        status: AC status (pending, in_progress, completed, failed).
        elapsed_display: Formatted elapsed time string.
        is_current: Whether this is the current AC being worked on.
    """

    index: int
    content: str
    status: str = "pending"
    elapsed_display: str = ""
    is_current: bool = False


class ACProgressWidget(Widget):
    """Widget displaying AC progress list.

    Shows acceptance criteria with status icons, progress bar,
    elapsed times, and estimated remaining time.

    Attributes:
        acceptance_criteria: List of AC progress items.
        completed_count: Number of completed ACs.
        total_count: Total number of ACs.
        estimated_remaining: Estimated remaining time string.
    """

    DEFAULT_CSS = """
    ACProgressWidget {
        height: auto;
        width: 100%;
        padding: 0 1;
    }

    ACProgressWidget > .header {
        text-style: bold;
        color: $text;
        margin-bottom: 0;
    }

    ACProgressWidget > .progress-header {
        height: 1;
        margin-bottom: 0;
        color: $text-muted;
    }

    ACProgressWidget > .ac-list {
        height: auto;
    }

    ACProgressWidget > .ac-item {
        height: 1;
        padding: 0 0;
    }

    ACProgressWidget > .ac-item.current {
        text-style: bold;
        background: $primary-darken-3;
    }

    ACProgressWidget > .empty-message {
        text-align: center;
        color: $text-muted;
        padding: 2;
    }

    ACProgressWidget > ProgressBar {
        margin-top: 1;
        width: 100%;
    }

    ACProgressWidget > ProgressBar > .bar--bar {
        color: $primary-darken-2;
    }

    ACProgressWidget > ProgressBar > .bar--complete {
        color: $success;
    }

    ACProgressWidget > .progress-footer {
        height: 1;
        text-align: right;
        color: $text-muted;
        margin-top: 0;
    }
    """

    acceptance_criteria: reactive[list[ACProgressItem]] = reactive(
        list, always_update=True
    )
    completed_count: reactive[int] = reactive(0)
    total_count: reactive[int] = reactive(0)
    estimated_remaining: reactive[str] = reactive("")

    def __init__(
        self,
        acceptance_criteria: list[ACProgressItem] | None = None,
        completed_count: int = 0,
        total_count: int = 0,
        estimated_remaining: str = "",
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        """Initialize AC progress widget.

        Args:
            acceptance_criteria: List of AC progress items.
            completed_count: Number of completed ACs.
            total_count: Total number of ACs.
            estimated_remaining: Estimated remaining time display.
            name: Widget name.
            id: Widget ID.
            classes: CSS classes.
        """
        # Initialize _progress_bar before super().__init__ to avoid watcher issues
        self._progress_bar: ProgressBar | None = None
        super().__init__(name=name, id=id, classes=classes)
        self.acceptance_criteria = acceptance_criteria or []
        self.completed_count = completed_count
        self.total_count = total_count
        self.estimated_remaining = estimated_remaining

    def compose(self) -> ComposeResult:
        """Compose the widget layout."""
        yield Label("Acceptance Criteria", classes="header")

        # Show empty state or progress
        if self.total_count == 0 and not self.acceptance_criteria:
            yield Static(
                "[dim]No workflow running[/dim]",
                classes="empty-message",
            )
        else:
            # Progress summary
            percent = (
                int(self.completed_count / self.total_count * 100)
                if self.total_count > 0
                else 0
            )
            yield Static(
                f"{self.completed_count}/{self.total_count} complete ({percent}%)",
                classes="progress-header",
            )

            # AC list
            for ac in self.acceptance_criteria:
                yield self._render_ac_item(ac)

            # Progress bar
            self._progress_bar = ProgressBar(
                total=max(self.total_count, 1), show_eta=False, show_percentage=False
            )
            self._progress_bar.advance(self.completed_count)
            yield self._progress_bar

            # Remaining time
            remaining_text = self.estimated_remaining or (
                "Calculating..." if self.completed_count == 0 and self.total_count > 0 else ""
            )
            yield Static(remaining_text, classes="progress-footer")

    def _render_ac_item(self, ac: ACProgressItem) -> Static:
        """Render a single AC item.

        Args:
            ac: AC progress item to render.

        Returns:
            Static widget with formatted AC line.
        """
        icon = STATUS_ICONS.get(ac.status, "○")
        style = STATUS_STYLES.get(ac.status, "dim")

        # Truncate content
        content = ac.content[:45] + "..." if len(ac.content) > 45 else ac.content

        # Build label
        label = f" [{style}]{icon}[/{style}]  {ac.index}. {content}"

        # Add elapsed time
        if ac.elapsed_display:
            if ac.status == "in_progress":
                label += f"  [yellow dim][{ac.elapsed_display}...][/yellow dim]"
            else:
                label += f"  [dim][{ac.elapsed_display}][/dim]"

        classes = "ac-item current" if ac.is_current else "ac-item"
        return Static(label, classes=classes)

    def watch_acceptance_criteria(
        self, _new_criteria: list[ACProgressItem]
    ) -> None:
        """React to acceptance_criteria changes."""
        self.refresh(recompose=True)

    def watch_completed_count(self, _new_count: int) -> None:
        """React to completed_count changes."""
        if self._progress_bar is not None:
            self._progress_bar.update(progress=self.completed_count)

    def update_progress(
        self,
        acceptance_criteria: list[ACProgressItem] | None = None,
        completed_count: int | None = None,
        total_count: int | None = None,
        estimated_remaining: str | None = None,
    ) -> None:
        """Update progress display.

        Args:
            acceptance_criteria: New AC list.
            completed_count: New completed count.
            total_count: New total count.
            estimated_remaining: New remaining time display.
        """
        if acceptance_criteria is not None:
            self.acceptance_criteria = acceptance_criteria
        if completed_count is not None:
            self.completed_count = completed_count
        if total_count is not None:
            self.total_count = total_count
        if estimated_remaining is not None:
            self.estimated_remaining = estimated_remaining


__all__ = ["ACProgressWidget", "ACProgressItem"]
