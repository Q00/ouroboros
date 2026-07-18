"""AC decomposition tree widget.

Displays the hierarchical acceptance criteria tree
with status indicators for each node.

Supports incremental updates for efficient rendering when
child ACs are dynamically added during decomposition.
"""

from __future__ import annotations

from typing import Any

from rich.cells import cell_len, set_cell_size
from textual import events
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

# Width-aware label truncation (was a fixed 50/30-char slice regardless of
# terminal width). ``_DEFAULT_LABEL_WIDTH``/``_DEFAULT_ROOT_LABEL_WIDTH`` are
# only the FALLBACK used when the widget isn't mounted yet (no real size to
# read) — once mounted, the actual available width always wins.
_DEFAULT_LABEL_WIDTH = 50
_DEFAULT_ROOT_LABEL_WIDTH = 30
_MIN_LABEL_WIDTH = 20
# Reserved for the tree's guide lines/indentation and the status icon prefix
# (e.g. "[green][OK][/green] "), which don't count toward the content budget.
_LABEL_WIDTH_RESERVED_CHARS = 20


def _truncate_to_cell_width(content: str, width: int) -> str:
    """Truncate ``content`` to at most ``width`` terminal display cells.

    ``width`` (from ``Widget.size``) is already a CELL budget, but plain
    ``len()``/slicing counts Python code points -- CJK characters (2 cells
    wide), emoji, and combining characters can then overflow or underuse the
    available width. Uses Rich's cell-width measurement
    (``rich.cells.cell_len``/``set_cell_size``, the same primitives Rich/
    Textual use internally to lay out text) instead.
    """
    if cell_len(content) <= width:
        return content
    ellipsis = "..."
    budget = max(0, width - cell_len(ellipsis))
    # ``set_cell_size`` pads with spaces when the cropped remainder is
    # narrower than ``budget`` (e.g. the cut landed right before a
    # double-width character) -- strip that padding before appending the
    # ellipsis so it never renders as a stray gap.
    return set_cell_size(content, budget).rstrip() + ellipsis


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

    def _label_max_width(self, *, default: int = _DEFAULT_LABEL_WIDTH) -> int:
        """Return the available content width for a label, from the widget's
        ACTUAL rendered size when it is known (mounted with a real layout).

        Falls back to ``default`` when the widget has no size yet (not
        mounted, or a zero-size layout pass) — this is a fallback for that
        case only, not a hand-picked constant every label is forced through.
        """
        try:
            width = self.size.width
        except Exception:
            width = 0
        if width and width > 0:
            return max(_MIN_LABEL_WIDTH, width - _LABEL_WIDTH_RESERVED_CHARS)
        return default

    def _format_root_label(self, root_content: str) -> str:
        """Truncate the root's own label to the widget's current width.

        Kept separate from :meth:`_format_node_label` because the root label
        (passed straight to ``Tree(...)``) never carries a status icon
        prefix, unlike every other node's label. Truncates by terminal
        DISPLAY CELLS via the same ``_truncate_to_cell_width`` helper every
        ordinary node label already uses -- code-point slicing miscounts
        CJK/emoji widths (round-4 follow-up: the root label was missed when
        that fix was applied to the ordinary labels).
        """
        root_width = self._label_max_width(default=_DEFAULT_ROOT_LABEL_WIDTH)
        return _truncate_to_cell_width(root_content, root_width)

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
                root_content = nodes[root_id].get("content", "AC Tree")
                root_label = self._format_root_label(root_content)

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
        max_width: int | None = None,
    ) -> str:
        """Format display label for a tree node.

        Args:
            node_data: Data for the node.
            is_current: Whether this is the currently executing AC.
            max_width: Explicit content-width override, mainly for tests
                (proving truncation is width-aware, not a fixed constant).
                Live callers omit this and get the widget's actual rendered
                width via :meth:`_label_max_width`.

        Returns:
            Formatted label with status icon and content.
        """
        status = node_data.get("status", "pending")
        content = node_data.get("content", "Unknown")
        is_atomic = node_data.get("is_atomic", False)

        # Truncate content for display, sized to the widget's actual
        # available width instead of a fixed character count -- and by
        # terminal DISPLAY CELLS, not Python code points (see
        # ``_truncate_to_cell_width``).
        width = max_width if max_width is not None else self._label_max_width()
        display_content = _truncate_to_cell_width(content, width)

        # Build label with status icon
        status_icon = STATUS_ICONS.get(status, "[ ]")
        if is_atomic and status in {"atomic", "pending"}:
            status_icon = STATUS_ICONS["atomic"]

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
        label = self._format_node_label(node_data, is_current)

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
                tree_node.set_label(self._format_node_label(node_data, is_current))

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
            is_current = node_id == self.current_ac_id
            tree_node.set_label(self._format_node_label(node_data, is_current))

    def _resync_labels_for_current_width(self) -> None:
        """Re-render every node's label at the widget's CURRENT available width.

        Reuses the same per-node formatting :meth:`_sync_existing_nodes`
        already applies for ordinary content/status updates, with one
        exception: the root's own label is re-truncated via
        :meth:`_format_root_label` (no status-icon prefix), matching how
        :meth:`compose` originally built it.
        """
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
                tree_node.set_label(self._format_root_label(node_data.get("content", "AC Tree")))
                continue
            is_current = node_id == self.current_ac_id
            tree_node.set_label(self._format_node_label(node_data, is_current))

    def on_resize(self, event: events.Resize) -> None:
        """Recompute label truncation for the new width (Fix 9, P2).

        Labels are computed once — at compose time — using the widget's
        width at that moment. Without this handler, resizing the terminal
        afterward never updates the truncation budget: a label truncated (or
        left whole) for the OLD width stays exactly as it was until some
        unrelated tree/status update happens to force a resync or full
        recompose.
        """
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
            self._node_map[ac_id].set_label(self._format_node_label(node_data, is_current))

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
                self._format_node_label(parent_data, parent_ac_id == self.current_ac_id)
            )

        # Add children to tree widget and data
        for child_data in children_data:
            child_id = child_data.get("id", "")
            if not child_id:
                continue

            # Add to internal data
            nodes[child_id] = child_data

            # Add to tree widget
            label = self._format_node_label(child_data, child_id == self.current_ac_id)
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
            self._node_map[ac_id].set_label(self._format_node_label(node_data, is_current))

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
