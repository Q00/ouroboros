"""AC decomposition tree widget.

Displays the hierarchical acceptance criteria tree
with status indicators for each node.
"""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Label, Static, Tree
from textual.widgets.tree import TreeNode


# Status display configuration
STATUS_ICONS = {
    "pending": "[dim][ ][/dim]",
    "atomic": "[blue][A][/blue]",
    "decomposed": "[cyan][D][/cyan]",
    "executing": "[yellow][*][/yellow]",
    "completed": "[green][OK][/green]",
    "failed": "[red][X][/red]",
}


class ACTreeWidget(Widget):
    """Widget displaying the AC decomposition tree.

    Shows hierarchical acceptance criteria with their status,
    depth, and parent-child relationships.

    Attributes:
        tree_data: Serialized AC tree data.
        current_ac_id: ID of the currently executing AC.
    """

    DEFAULT_CSS = """
    ACTreeWidget {
        height: auto;
        min-height: 10;
        max-height: 20;
        width: 100%;
        padding: 1;
        border: solid $surface;
    }

    ACTreeWidget > .header {
        text-align: center;
        text-style: bold;
        margin-bottom: 1;
    }

    ACTreeWidget > Tree {
        height: auto;
        max-height: 15;
        scrollbar-gutter: stable;
    }

    ACTreeWidget > .empty-message {
        text-align: center;
        color: $text-muted;
        padding: 2;
    }
    """

    tree_data: reactive[dict[str, Any]] = reactive({})
    current_ac_id: reactive[str] = reactive("")

    def __init__(
        self,
        tree_data: dict[str, Any] | None = None,
        current_ac_id: str = "",
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        """Initialize AC tree widget.

        Args:
            tree_data: Initial tree data from ACTree.to_dict().
            current_ac_id: ID of currently executing AC.
            name: Widget name.
            id: Widget ID.
            classes: CSS classes.
        """
        super().__init__(name=name, id=id, classes=classes)
        self.tree_data = tree_data or {}
        self.current_ac_id = current_ac_id
        self._tree_widget: Tree[str] | None = None

    def compose(self) -> ComposeResult:
        """Compose the widget layout."""
        yield Label("AC Decomposition Tree", classes="header")

        if not self.tree_data or not self.tree_data.get("nodes"):
            yield Static("No AC tree available", classes="empty-message")
        else:
            tree: Tree[str] = Tree("Acceptance Criteria")
            tree.show_root = True
            self._tree_widget = tree
            self._build_tree(tree)
            yield tree

    def _build_tree(self, tree: Tree[str]) -> None:
        """Build the tree widget from tree data.

        Args:
            tree: The Tree widget to populate.
        """
        nodes = self.tree_data.get("nodes", {})
        root_id = self.tree_data.get("root_id")

        if not root_id or root_id not in nodes:
            return

        # Build from root recursively
        root_node_data = nodes[root_id]
        self._add_node(tree.root, root_node_data, nodes)
        tree.root.expand()

    def _add_node(
        self,
        parent: TreeNode[str],
        node_data: dict[str, Any],
        all_nodes: dict[str, dict[str, Any]],
    ) -> None:
        """Add a node and its children to the tree.

        Args:
            parent: Parent tree node.
            node_data: Data for this node.
            all_nodes: All nodes in the tree.
        """
        # Format node label
        status = node_data.get("status", "pending")
        content = node_data.get("content", "Unknown")
        node_id = node_data.get("id", "")
        depth = node_data.get("depth", 0)
        is_atomic = node_data.get("is_atomic", False)

        # Truncate content for display
        display_content = content[:50] + "..." if len(content) > 50 else content

        # Build label with status icon
        status_icon = STATUS_ICONS.get(status, "[ ]")
        if is_atomic:
            status_icon = STATUS_ICONS["atomic"]

        # Highlight current AC
        if node_id == self.current_ac_id:
            label = f"{status_icon} [bold yellow]{display_content}[/bold yellow]"
        else:
            label = f"{status_icon} {display_content}"

        # Add to tree
        child_ids = node_data.get("children_ids", [])
        if child_ids:
            # Has children - add as expandable node
            tree_node = parent.add(label, data=node_id)
            tree_node.expand()

            # Add children
            for child_id in child_ids:
                if child_id in all_nodes:
                    self._add_node(tree_node, all_nodes[child_id], all_nodes)
        else:
            # Leaf node
            parent.add_leaf(label, data=node_id)

    def watch_tree_data(self, new_data: dict[str, Any]) -> None:
        """React to tree_data changes.

        Args:
            new_data: New tree data.
        """
        # Refresh the tree by recomposing
        self.refresh(recompose=True)

    def watch_current_ac_id(self, new_id: str) -> None:
        """React to current_ac_id changes.

        Args:
            new_id: New current AC ID.
        """
        # Refresh to update highlighting
        self.refresh(recompose=True)

    def update_tree(
        self,
        tree_data: dict[str, Any],
        current_ac_id: str | None = None,
    ) -> None:
        """Update the tree display.

        Args:
            tree_data: New tree data from ACTree.to_dict().
            current_ac_id: Optional new current AC ID.
        """
        self.tree_data = tree_data
        if current_ac_id is not None:
            self.current_ac_id = current_ac_id

    def update_node_status(self, ac_id: str, status: str) -> None:
        """Update status of a single node.

        Args:
            ac_id: AC ID to update.
            status: New status.
        """
        nodes = self.tree_data.get("nodes", {})
        if ac_id in nodes:
            # Create new dict to trigger reactive update
            new_data = dict(self.tree_data)
            new_nodes = dict(nodes)
            new_nodes[ac_id] = {**nodes[ac_id], "status": status}
            new_data["nodes"] = new_nodes
            self.tree_data = new_data


__all__ = ["ACTreeWidget"]
