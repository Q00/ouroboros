"""Agent activity widget.

Displays current subAgent activity including:
- Current tool being used
- Current file being accessed
- Thinking/reasoning state
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static


class AgentActivityWidget(Widget):
    """Widget showing current agent activity.

    Displays tool usage, file access, and thinking state
    in a compact format.

    Attributes:
        current_tool: Name of tool being used.
        current_file: Path of file being accessed.
        thinking: Current thinking/reasoning text.
    """

    DEFAULT_CSS = """
    AgentActivityWidget {
        height: auto;
        width: 100%;
        padding: 0 1;
    }

    AgentActivityWidget > .header {
        text-style: bold;
        color: $text;
    }

    AgentActivityWidget > .activity-line {
        height: 1;
    }

    AgentActivityWidget > .activity-line > .label {
        color: $text-muted;
    }

    AgentActivityWidget > .activity-line > .value {
        color: $text;
    }

    AgentActivityWidget > .thinking-line {
        height: auto;
        max-height: 3;
        color: $text-muted;
    }

    AgentActivityWidget .tool-value {
        color: $warning;
    }

    AgentActivityWidget .file-value {
        color: $secondary;
    }

    AgentActivityWidget .thinking-value {
        color: $text-muted;
        text-style: italic;
    }
    """

    current_tool: reactive[str] = reactive("")
    current_file: reactive[str] = reactive("")
    thinking: reactive[str] = reactive("")

    def __init__(
        self,
        current_tool: str = "",
        current_file: str = "",
        thinking: str = "",
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        """Initialize agent activity widget.

        Args:
            current_tool: Initial tool name.
            current_file: Initial file path.
            thinking: Initial thinking text.
            name: Widget name.
            id: Widget ID.
            classes: CSS classes.
        """
        super().__init__(name=name, id=id, classes=classes)
        self.current_tool = current_tool
        self.current_file = current_file
        self.thinking = thinking

    def compose(self) -> ComposeResult:
        """Compose the widget layout."""
        yield Static("Agent Activity", classes="header")
        yield Static(self._format_tool_line(), classes="activity-line", id="tool-line")
        yield Static(self._format_file_line(), classes="activity-line", id="file-line")
        yield Static(
            self._format_thinking_line(), classes="thinking-line", id="thinking-line"
        )

    def _format_tool_line(self) -> str:
        """Format tool display line."""
        tool = self.current_tool or "[dim]--[/dim]"
        return f"Tool: [warning]{tool}[/warning]"

    def _format_file_line(self) -> str:
        """Format file display line."""
        if not self.current_file:
            return "File: [dim]--[/dim]"
        # Truncate long paths, show last part
        file_display = self.current_file
        if len(file_display) > 40:
            file_display = "..." + file_display[-37:]
        return f"File: [secondary]{file_display}[/secondary]"

    def _format_thinking_line(self) -> str:
        """Format thinking display line."""
        if not self.thinking:
            return "[dim]Idle...[/dim]"
        # Truncate long thinking
        text = self.thinking[:80] + "..." if len(self.thinking) > 80 else self.thinking
        return f"[italic dim]{text}[/italic dim]"

    def _update_display(self) -> None:
        """Update all display elements."""
        try:
            self.query_one("#tool-line", Static).update(self._format_tool_line())
            self.query_one("#file-line", Static).update(self._format_file_line())
            self.query_one("#thinking-line", Static).update(self._format_thinking_line())
        except Exception:
            pass

    def watch_current_tool(self, _new_value: str) -> None:
        """React to current_tool changes."""
        self._update_display()

    def watch_current_file(self, _new_value: str) -> None:
        """React to current_file changes."""
        self._update_display()

    def watch_thinking(self, _new_value: str) -> None:
        """React to thinking changes."""
        self._update_display()

    def update_activity(
        self,
        current_tool: str | None = None,
        current_file: str | None = None,
        thinking: str | None = None,
    ) -> None:
        """Update activity display.

        Args:
            current_tool: New tool name.
            current_file: New file path.
            thinking: New thinking text.
        """
        if current_tool is not None:
            self.current_tool = current_tool
        if current_file is not None:
            self.current_file = current_file
        if thinking is not None:
            self.thinking = thinking


__all__ = ["AgentActivityWidget"]
