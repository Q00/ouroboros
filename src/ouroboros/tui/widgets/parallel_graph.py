"""Parallel AC execution graph widget.

Displays AC execution as a left-to-right dependency graph,
showing parallel execution levels and current progress.

Graph structure:
    Level 0       Level 1       Level 2
    ┌─────┐      ┌─────┐      ┌─────┐
    │ AC1 │─────▶│ AC2 │─────▶│ AC4 │
    └─────┘      └─────┘      └─────┘
                 ┌─────┐
                 │ AC3 │──────┘
                 └─────┘

Status colors:
- Pending: dim
- Executing: yellow (animated)
- Completed: green
- Failed: red
"""

from __future__ import annotations

from dataclasses import dataclass, field

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Label, Static

# Box drawing characters for graph
BOX_CHARS = {
    "h_line": "─",
    "v_line": "│",
    "arrow_right": "▶",
    "corner_dr": "┌",  # down-right
    "corner_ur": "└",  # up-right
    "corner_dl": "┐",  # down-left
    "corner_ul": "┘",  # up-left
    "t_right": "├",
    "t_left": "┤",
    "t_down": "┬",
    "t_up": "┴",
    "cross": "┼",
}


@dataclass
class GraphNode:
    """A node in the execution graph.

    Attributes:
        ac_id: Unique AC identifier.
        content: AC content (truncated for display).
        level: Execution level (0 = no dependencies).
        status: Current status (pending, executing, completed, failed).
        dependencies: List of AC IDs this node depends on.
    """

    ac_id: str
    content: str
    level: int
    status: str = "pending"
    dependencies: list[str] = field(default_factory=list)


class ParallelGraphWidget(Widget):
    """Widget displaying parallel AC execution as a Graph LR.

    Shows ACs organized by execution level with dependency arrows.
    Multiple ACs in the same level can execute in parallel.

    Attributes:
        nodes: List of graph nodes to display.
        executing_ids: Set of currently executing AC IDs.
    """

    DEFAULT_CSS = """
    ParallelGraphWidget {
        height: auto;
        min-height: 8;
        max-height: 20;
        width: 100%;
        padding: 1 2;
    }

    ParallelGraphWidget > .header {
        text-align: center;
        text-style: bold;
        color: $text;
        margin-bottom: 1;
    }

    ParallelGraphWidget > .graph-container {
        height: auto;
        width: 100%;
        overflow-x: auto;
    }

    ParallelGraphWidget > .empty-message {
        text-align: center;
        color: $text-muted;
        padding: 2;
    }

    ParallelGraphWidget > .level-header {
        color: $primary;
        text-style: bold;
        margin-bottom: 0;
    }
    """

    nodes: reactive[list[GraphNode]] = reactive([], always_update=True)
    executing_ids: reactive[frozenset[str]] = reactive(frozenset())

    def __init__(
        self,
        nodes: list[GraphNode] | None = None,
        executing_ids: frozenset[str] | None = None,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        """Initialize parallel graph widget.

        Args:
            nodes: Initial graph nodes.
            executing_ids: Set of currently executing AC IDs.
            name: Widget name.
            id: Widget ID.
            classes: CSS classes.
        """
        super().__init__(name=name, id=id, classes=classes)
        self.nodes = nodes or []
        self.executing_ids = executing_ids or frozenset()

    def compose(self) -> ComposeResult:
        """Compose the widget layout."""
        yield Label("Parallel Execution Graph", classes="header")

        if not self.nodes:
            yield Static("No parallel execution data", classes="empty-message")
        else:
            yield Static(self._render_graph(), classes="graph-container")

    def _render_graph(self) -> str:
        """Render the graph as a string.

        Returns:
            ASCII art representation of the execution graph.
        """
        if not self.nodes:
            return ""

        # Group nodes by level
        levels: dict[int, list[GraphNode]] = {}
        for node in self.nodes:
            if node.level not in levels:
                levels[node.level] = []
            levels[node.level].append(node)

        if not levels:
            return ""

        max_level = max(levels.keys())
        lines: list[str] = []

        # Render level headers
        header_parts = []
        for level in range(max_level + 1):
            level_nodes = levels.get(level, [])
            count = len(level_nodes)
            parallel_marker = f"[cyan]×{count}[/]" if count > 1 else "   "
            header_parts.append(f"  Level {level} {parallel_marker}  ")

        lines.append("     ".join(header_parts))
        lines.append("")

        # Calculate max nodes per level for alignment
        max_nodes = max(len(nodes) for nodes in levels.values())

        # Render nodes level by level
        for row in range(max_nodes):
            row_parts = []
            connector_parts = []

            for level in range(max_level + 1):
                level_nodes = levels.get(level, [])

                if row < len(level_nodes):
                    node = level_nodes[row]
                    node_str = self._render_node(node)
                    row_parts.append(node_str)

                    # Add arrow to next level if not last level
                    if level < max_level:
                        connector_parts.append(f"──{BOX_CHARS['arrow_right']}  ")
                    else:
                        connector_parts.append("")
                else:
                    # Empty cell
                    row_parts.append("              ")
                    if level < max_level:
                        connector_parts.append("     ")
                    else:
                        connector_parts.append("")

            # Combine row parts with connectors
            combined = ""
            for i, (node_part, conn_part) in enumerate(zip(row_parts, connector_parts + [""])):
                combined += node_part
                if i < len(row_parts) - 1:
                    combined += conn_part

            lines.append(combined)

        # Add legend
        lines.append("")
        lines.append(
            "[dim][ ][/] Pending  [yellow][*][/] Executing  [green][OK][/] Done  [red][X][/] Failed"
        )

        return "\n".join(lines)

    def _render_node(self, node: GraphNode) -> str:
        """Render a single node.

        Args:
            node: The node to render.

        Returns:
            Formatted node string.
        """
        # Truncate content
        content = node.content[:10] + ".." if len(node.content) > 12 else node.content
        content = content.ljust(12)

        # Status styling
        is_executing = node.ac_id in self.executing_ids
        status = node.status

        if is_executing or status == "executing":
            # Yellow background for executing
            return f"[on yellow][black]{self._get_status_icon(status)} {content}[/black][/]"
        elif status == "completed":
            return f"[green]{self._get_status_icon(status)} {content}[/]"
        elif status == "failed":
            return f"[red]{self._get_status_icon(status)} {content}[/]"
        else:
            return f"[dim]{self._get_status_icon(status)} {content}[/]"

    def _get_status_icon(self, status: str) -> str:
        """Get status icon.

        Args:
            status: Node status.

        Returns:
            Status icon string.
        """
        icons = {
            "pending": "[ ]",
            "executing": "[*]",
            "completed": "[OK]",
            "failed": "[X]",
            "atomic": "[A]",
        }
        return icons.get(status, "[ ]")

    def watch_nodes(self, _new_nodes: list[GraphNode]) -> None:
        """React to nodes changes."""
        self.refresh(recompose=True)

    def watch_executing_ids(self, _new_ids: frozenset[str]) -> None:
        """React to executing_ids changes."""
        self.refresh(recompose=True)

    def update_from_decomposition(
        self,
        child_acs: tuple[str, ...],
        child_ac_ids: tuple[str, ...],
        dependencies: tuple[tuple[int, ...], ...],
        execution_levels: list[list[int]],
    ) -> None:
        """Update graph from decomposition result.

        Args:
            child_acs: Tuple of child AC contents.
            child_ac_ids: Tuple of child AC IDs.
            dependencies: Dependency structure.
            execution_levels: Computed execution levels.
        """
        new_nodes: list[GraphNode] = []

        # Create index to level mapping
        idx_to_level: dict[int, int] = {}
        for level, indices in enumerate(execution_levels):
            for idx in indices:
                idx_to_level[idx] = level

        # Create nodes
        for idx, (content, ac_id) in enumerate(zip(child_acs, child_ac_ids)):
            deps = dependencies[idx] if idx < len(dependencies) else ()
            dep_ids = [child_ac_ids[d] for d in deps if d < len(child_ac_ids)]

            node = GraphNode(
                ac_id=ac_id,
                content=content,
                level=idx_to_level.get(idx, 0),
                status="pending",
                dependencies=dep_ids,
            )
            new_nodes.append(node)

        self.nodes = new_nodes

    def update_node_status(self, ac_id: str, status: str) -> None:
        """Update status of a single node.

        Args:
            ac_id: AC ID to update.
            status: New status.
        """
        updated_nodes = []
        for node in self.nodes:
            if node.ac_id == ac_id:
                updated_nodes.append(
                    GraphNode(
                        ac_id=node.ac_id,
                        content=node.content,
                        level=node.level,
                        status=status,
                        dependencies=node.dependencies,
                    )
                )
            else:
                updated_nodes.append(node)
        self.nodes = updated_nodes

    def set_executing(self, ac_ids: list[str]) -> None:
        """Set currently executing AC IDs.

        Args:
            ac_ids: List of executing AC IDs.
        """
        self.executing_ids = frozenset(ac_ids)

    def clear(self) -> None:
        """Clear the graph."""
        self.nodes = []
        self.executing_ids = frozenset()


__all__ = ["GraphNode", "ParallelGraphWidget"]
