"""AC decomposition tree widget.

Displays the hierarchical acceptance criteria tree
with status indicators for each node.

Supports incremental updates for efficient rendering when
child ACs are dynamically added during decomposition.
"""

from __future__ import annotations

from typing import Any

from rich.cells import cell_len
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Label, Static, Tree
from textual.widgets.tree import TreeNode

from ouroboros.tui.cell_width import truncate_to_cell_width

# Status display configuration
STATUS_ICONS = {
    "pending": "[dim][ ][/dim]",
    "atomic": "[blue][A][/blue]",
    "decomposed": "[cyan][D][/cyan]",
    "executing": "[yellow][*][/yellow]",
    "completed": "[green][OK][/green]",
    "failed": "[red][X][/red]",
}

_FALLBACK_NODE_CONTENT_WIDTH = 50
_FALLBACK_ROOT_CONTENT_WIDTH = 30


class ACTreeWidget(Widget):
    """Widget displaying the AC decomposition tree.

    Shows hierarchical acceptance criteria with their status,
    depth, and parent-child relationships.

    Supports two update modes:
    1. Full rebuild: For initial render or major structural changes
    2. Incremental update: For adding children or status changes (preferred)

    Attributes:
        tree_data: Serialized AC tree data.
        current_ac_id: ID of the currently executing AC.
    """

    DEFAULT_CSS = """
    ACTreeWidget {
        height: auto;
        min-height: 10;
        max-height: 22;
        width: 100%;
        padding: 1 2;
    }

    ACTreeWidget > .header {
        text-align: center;
        text-style: bold;
        color: $text;
        margin-bottom: 1;
    }

    ACTreeWidget > Tree {
        height: auto;
        max-height: 18;
        scrollbar-gutter: stable;
        background: transparent;
    }

    ACTreeWidget > Tree > .tree--guides {
        color: $primary-darken-2;
    }

    ACTreeWidget > Tree > .tree--guides-hover {
        color: $primary;
    }

    ACTreeWidget > Tree > .tree--cursor {
        background: $primary-darken-3;
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
        # Initialize internal state BEFORE calling super().__init__
        # because reactive setters may trigger watch methods
        self._tree_widget: Tree[str] | None = None
        # Map ac_id -> TreeNode for incremental updates
        self._node_map: dict[str, TreeNode[str]] = {}
        # Internal data cache to avoid triggering reactive watch
        self._tree_data_cache: dict[str, Any] = {}
        self._pending_recompose = False

        super().__init__(name=name, id=id, classes=classes)
        self.tree_data = tree_data or {}
        self.current_ac_id = current_ac_id

    @staticmethod
    def _display_depth(node: TreeNode[str]) -> int:
        """Return a node's rendered depth below the visible tree root."""
        depth = 0
        parent = node.parent
        while isinstance(parent, TreeNode):
            depth += 1
            parent = parent.parent
        return depth

    def _tree_label_width(
        self,
        *,
        display_depth: int,
        allow_expand: bool,
        fixed_label_width: int,
        fallback_content_width: int,
    ) -> int:
        """Return the label budget after Tree guides and toggle glyphs."""
        tree = self._tree_widget
        tree_width = getattr(getattr(tree, "scrollable_content_region", None), "width", 0)
        if not isinstance(tree_width, int) or tree_width <= 0:
            tree_width = getattr(getattr(tree, "size", None), "width", 0)
        if tree is not None and isinstance(tree_width, int) and tree_width > 0:
            guide_levels = display_depth if tree.show_root else max(0, display_depth - 1)
            guide_width = guide_levels * tree.guide_depth
            toggle_width = cell_len(tree.ICON_NODE_EXPANDED) if allow_expand else 0
            return max(0, tree_width - guide_width - toggle_width)
        return fixed_label_width + fallback_content_width

    def _format_root_label(
        self,
        content: str,
        *,
        max_width: int | None = None,
    ) -> str:
        """Format the visible root within its toggle-aware row budget."""
        if max_width is None:
            max_width = self._tree_label_width(
                display_depth=0,
                allow_expand=True,
                fixed_label_width=0,
                fallback_content_width=_FALLBACK_ROOT_CONTENT_WIDTH,
            )
        return truncate_to_cell_width(content, max_width)

    def compose(self) -> ComposeResult:
        """Compose the widget layout."""
        yield Label("AC Decomposition Tree", classes="header")

        if not self.tree_data or not self.tree_data.get("nodes"):
            yield Static("No AC tree available", classes="empty-message")
        else:
            # Use root node's content as Tree label to avoid duplication
            nodes = self.tree_data.get("nodes", {})
            root_id = self.tree_data.get("root_id")
            root_label = "AC Tree"
            if root_id and root_id in nodes:
                root_label = self._format_root_label(str(nodes[root_id].get("content", "AC Tree")))

            tree: Tree[str] = Tree(root_label)
            tree.show_root = True
            self._tree_widget = tree
            self._node_map.clear()
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

        root_node_data = nodes[root_id]

        # Register root in node_map
        self._node_map[root_id] = tree.root

        # Add children directly to tree root (skip adding root as child)
        children_ids = root_node_data.get("children_ids", [])
        for child_id in children_ids:
            if child_id in nodes:
                self._add_node(tree.root, nodes[child_id], nodes)

        tree.root.expand()

    def _format_node_label(
        self,
        node_data: dict[str, Any],
        is_current: bool = False,
        *,
        display_depth: int = 1,
        allow_expand: bool | None = None,
        max_width: int | None = None,
    ) -> str:
        """Format display label for a tree node.

        Args:
            node_data: Data for the node.
            is_current: Whether this is the currently executing AC.
            display_depth: Rendered depth below the visible tree root.
            allow_expand: Whether Textual prepends an expand/collapse glyph.
            max_width: Optional label-only width for direct tests.

        Returns:
            Formatted label with status icon and content.
        """
        status = node_data.get("status", "pending")
        content = str(node_data.get("content", "Unknown"))
        is_atomic = node_data.get("is_atomic", False)

        # Build label with status icon
        status_icon = STATUS_ICONS.get(status, "[ ]")
        if is_atomic and status in {"atomic", "pending"}:
            status_icon = STATUS_ICONS["atomic"]

        prefix = Text.from_markup(status_icon)
        prefix.append(" ")
        if allow_expand is None:
            allow_expand = bool(node_data.get("children_ids"))
        if max_width is None:
            max_width = self._tree_label_width(
                display_depth=display_depth,
                allow_expand=allow_expand,
                fixed_label_width=prefix.cell_len,
                fallback_content_width=_FALLBACK_NODE_CONTENT_WIDTH,
            )
        display_content = truncate_to_cell_width(
            content,
            max(0, max_width - prefix.cell_len),
        )

        # Highlight current AC
        if is_current:
            return f"{status_icon} [bold yellow]{display_content}[/bold yellow]"
        return f"{status_icon} {display_content}"

    def _add_node(
        self,
        parent: TreeNode[str],
        node_data: dict[str, Any],
        all_nodes: dict[str, dict[str, Any]],
    ) -> TreeNode[str]:
        """Add a node and its children to the tree.

        Args:
            parent: Parent tree node.
            node_data: Data for this node.
            all_nodes: All nodes in the tree.

        Returns:
            The created TreeNode for this AC.
        """
        node_id = node_data.get("id", "")
        is_current = node_id == self.current_ac_id

        # Add to tree
        child_ids = node_data.get("children_ids", [])
        display_depth = self._display_depth(parent) + 1
        label = self._format_node_label(
            node_data,
            is_current,
            display_depth=display_depth,
            allow_expand=bool(child_ids),
        )
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
            tree_node = parent.add_leaf(label, data=node_id)

        # Register in node map for incremental updates
        self._node_map[node_id] = tree_node
        return tree_node

    def watch_tree_data(self, new_data: dict[str, Any]) -> None:
        """React to tree_data changes.

        Only triggers full recompose if tree widget doesn't exist yet.
        Otherwise, incremental updates are preferred via add_children/update_node_status.

        Args:
            new_data: New tree data.
        """
        # Sync internal cache
        self._tree_data_cache = new_data

        if self._pending_recompose:
            self._pending_recompose = False
            self.refresh(recompose=True)
            return

        # Only recompose if tree doesn't exist (initial build or was cleared)
        if self._tree_widget is None or not self._node_map:
            self.refresh(recompose=True)
            return

        self._sync_existing_nodes(new_data)

    def watch_current_ac_id(self, new_id: str) -> None:
        """React to current_ac_id changes.

        Updates highlighting without full recompose when possible.

        Args:
            new_id: New current AC ID.
        """
        if not self._tree_widget or not self._node_map:
            self.refresh(recompose=True)
            return

        # Update labels for old and new current AC
        nodes = self.tree_data.get("nodes", {})

        # Find and update previous current AC (remove highlight)
        for ac_id, tree_node in self._node_map.items():
            if ac_id in nodes:
                node_data = nodes[ac_id]
                is_current = ac_id == new_id
                tree_node.set_label(
                    self._format_node_label(
                        node_data,
                        is_current,
                        display_depth=self._display_depth(tree_node),
                        allow_expand=tree_node.allow_expand,
                    )
                )

    def update_tree(
        self,
        tree_data: dict[str, Any],
        current_ac_id: str | None = None,
        *,
        force_rebuild: bool = False,
    ) -> None:
        """Update the tree display.

        Args:
            tree_data: New tree data from ACTree.to_dict().
            current_ac_id: Optional new current AC ID.
            force_rebuild: If True, force full recompose instead of incremental update.
        """
        if force_rebuild or self._tree_structure_changed(tree_data):
            self._node_map.clear()
            self._pending_recompose = True

        self.tree_data = tree_data
        if current_ac_id is not None:
            self.current_ac_id = current_ac_id

    def _tree_structure_changed(self, new_data: dict[str, Any]) -> bool:
        """Return True when node membership or parent/child edges changed."""
        if self._tree_widget is None or not self._node_map:
            return False

        old_data = self._tree_data_cache or self.tree_data
        old_nodes = old_data.get("nodes")
        new_nodes = new_data.get("nodes")
        if not isinstance(old_nodes, dict) or not isinstance(new_nodes, dict):
            return True

        if old_data.get("root_id") != new_data.get("root_id"):
            return True

        if set(old_nodes) != set(new_nodes):
            return True

        for node_id, new_node in new_nodes.items():
            old_node = old_nodes.get(node_id)
            if not isinstance(old_node, dict) or not isinstance(new_node, dict):
                return True
            if list(old_node.get("children_ids", [])) != list(new_node.get("children_ids", [])):
                return True

        return False

    def _sync_existing_nodes(self, tree_data: dict[str, Any]) -> None:
        """Patch labels in-place when only node content/status changed."""
        nodes = tree_data.get("nodes")
        if not isinstance(nodes, dict):
            return

        for node_id, tree_node in self._node_map.items():
            node_data = nodes.get(node_id)
            if not isinstance(node_data, dict):
                continue
            if node_id == tree_data.get("root_id"):
                tree_node.set_label(
                    self._format_root_label(str(node_data.get("content", "AC Tree")))
                )
                continue
            is_current = node_id == self.current_ac_id
            tree_node.set_label(
                self._format_node_label(
                    node_data,
                    is_current,
                    display_depth=self._display_depth(tree_node),
                    allow_expand=tree_node.allow_expand,
                )
            )

    def _resync_labels_for_current_width(self) -> None:
        """Reapply root and child labels against the current Tree viewport."""
        if self._tree_widget is None or not self._node_map:
            return

        data = self._tree_data_cache or self.tree_data
        nodes = data.get("nodes")
        if not isinstance(nodes, dict):
            return

        root_id = data.get("root_id")
        for node_id, tree_node in self._node_map.items():
            node_data = nodes.get(node_id)
            if not isinstance(node_data, dict):
                continue
            if node_id == root_id:
                tree_node.set_label(
                    self._format_root_label(str(node_data.get("content", "AC Tree")))
                )
                continue
            tree_node.set_label(
                self._format_node_label(
                    node_data,
                    node_id == self.current_ac_id,
                    display_depth=self._display_depth(tree_node),
                    allow_expand=tree_node.allow_expand,
                )
            )
        # TreeNode.set_label refreshes a row but does not recompute virtual width.
        self._tree_widget._invalidate()

    def on_resize(self, event: events.Resize) -> None:
        """Recalculate labels after Textual measures a new tree width."""
        del event
        self._resync_labels_for_current_width()

    def update_node_status(self, ac_id: str, status: str) -> None:
        """Update status of a single node without recompose.

        This is the preferred method for status updates during execution.

        Args:
            ac_id: AC ID to update.
            status: New status.
        """
        nodes = self._tree_data_cache.get("nodes", {}) or self.tree_data.get("nodes", {})
        if ac_id not in nodes:
            return

        # Update internal data
        new_data = dict(self._tree_data_cache or self.tree_data)
        new_nodes = dict(nodes)
        new_nodes[ac_id] = {**nodes[ac_id], "status": status}
        new_data["nodes"] = new_nodes

        # Update TreeNode label directly if possible (no recompose)
        if ac_id in self._node_map and self._tree_widget:
            node_data = new_nodes[ac_id]
            is_current = ac_id == self.current_ac_id
            tree_node = self._node_map[ac_id]
            tree_node.set_label(
                self._format_node_label(
                    node_data,
                    is_current,
                    display_depth=self._display_depth(tree_node),
                    allow_expand=tree_node.allow_expand,
                )
            )

        # Update reactive data (watch will skip recompose since tree exists)
        self.tree_data = new_data

    def add_children(
        self,
        parent_ac_id: str,
        children_data: list[dict[str, Any]],
    ) -> bool:
        """Add child ACs to a parent node incrementally.

        This is the preferred method for adding decomposed children
        without rebuilding the entire tree.

        Args:
            parent_ac_id: AC ID of the parent node.
            children_data: List of child node data dicts with
                          'id', 'content', 'status', 'depth', 'is_atomic'.

        Returns:
            True if children were added successfully, False otherwise.
        """
        if not self._tree_widget or parent_ac_id not in self._node_map:
            # Tree not initialized or parent not found - need full rebuild
            return False

        parent_tree_node = self._node_map[parent_ac_id]
        base_data = self._tree_data_cache or self.tree_data
        nodes = dict(base_data.get("nodes", {}))

        # Update parent's children_ids
        if parent_ac_id in nodes:
            parent_data = dict(nodes[parent_ac_id])
            existing_children = list(parent_data.get("children_ids", []))
            new_child_ids = [c["id"] for c in children_data]
            parent_data["children_ids"] = existing_children + new_child_ids
            parent_data["status"] = "decomposed"
            nodes[parent_ac_id] = parent_data

            # Update parent node label to show decomposed status
            self._node_map[parent_ac_id].set_label(
                self._format_node_label(
                    parent_data,
                    parent_ac_id == self.current_ac_id,
                    display_depth=self._display_depth(parent_tree_node),
                    allow_expand=parent_tree_node.allow_expand,
                )
            )

        # Add children to tree widget and data
        for child_data in children_data:
            child_id = child_data.get("id", "")
            if not child_id:
                continue

            # Add to internal data
            nodes[child_id] = child_data

            # Add to tree widget
            child_ids = child_data.get("children_ids", [])
            label = self._format_node_label(
                child_data,
                child_id == self.current_ac_id,
                display_depth=self._display_depth(parent_tree_node) + 1,
                allow_expand=bool(child_ids),
            )
            if child_ids:
                child_tree_node = parent_tree_node.add(label, data=child_id)
            else:
                child_tree_node = parent_tree_node.add_leaf(label, data=child_id)
            self._node_map[child_id] = child_tree_node

        # Expand parent to show new children
        parent_tree_node.expand()

        # Update reactive data (watch will skip recompose since tree exists)
        new_data = dict(base_data)
        new_data["nodes"] = nodes
        self.tree_data = new_data

        return True

    def mark_node_atomic(self, ac_id: str) -> None:
        """Mark a node as atomic (no further decomposition needed).

        Args:
            ac_id: AC ID to mark as atomic.
        """
        base_data = self._tree_data_cache or self.tree_data
        nodes = base_data.get("nodes", {})
        if ac_id not in nodes:
            return

        # Update internal data
        new_data = dict(base_data)
        new_nodes = dict(nodes)
        new_nodes[ac_id] = {**nodes[ac_id], "is_atomic": True, "status": "atomic"}
        new_data["nodes"] = new_nodes

        # Update TreeNode label directly if possible
        if ac_id in self._node_map and self._tree_widget:
            node_data = new_nodes[ac_id]
            is_current = ac_id == self.current_ac_id
            tree_node = self._node_map[ac_id]
            tree_node.set_label(
                self._format_node_label(
                    node_data,
                    is_current,
                    display_depth=self._display_depth(tree_node),
                    allow_expand=tree_node.allow_expand,
                )
            )

        # Update reactive data (watch will skip recompose since tree exists)
        self.tree_data = new_data

    def get_node_by_id(self, ac_id: str) -> TreeNode[str] | None:
        """Get the TreeNode for a given AC ID.

        Args:
            ac_id: The AC ID to look up.

        Returns:
            The TreeNode if found, None otherwise.
        """
        return self._node_map.get(ac_id)


__all__ = ["ACTreeWidget"]
