"""AC (Acceptance Criterion) tree structure for hierarchical decomposition.

This module provides data structures for managing AC hierarchy during
recursive decomposition. The tree is reconstructed from events using
event sourcing pattern.

Key concepts:
- ACNode: Individual AC in the tree
- ACTree: Complete tree structure with traversal methods
- Max depth: 5 levels (NFR10)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from uuid import uuid4


class ACStatus(str, Enum):
    """Lifecycle status of an Acceptance Criterion."""

    PENDING = "pending"  # Not yet analyzed
    ATOMIC = "atomic"  # Confirmed atomic, ready for execution
    DECOMPOSED = "decomposed"  # Split into children
    EXECUTING = "executing"  # Currently in Double Diamond cycle
    COMPLETED = "completed"  # Execution finished successfully
    FAILED = "failed"  # Execution failed


@dataclass(frozen=True, slots=True)
class ACNode:
    """Immutable node in the AC decomposition tree.

    Represents a single acceptance criterion in the hierarchy.

    Attributes:
        id: Unique identifier for this AC.
        content: The acceptance criterion text.
        depth: Depth in tree (0 = root, max 5).
        parent_id: ID of parent AC (None for root).
        status: Current lifecycle status.
        is_atomic: Whether this AC is atomic (no further decomposition).
        children_ids: Tuple of child AC IDs (immutable).
        execution_id: Associated execution ID if executing/completed.
        metadata: Additional context (e.g., complexity_score, reasoning).
    """

    id: str
    content: str
    depth: int
    parent_id: str | None = None
    status: ACStatus = ACStatus.PENDING
    is_atomic: bool = False
    children_ids: tuple[str, ...] = field(default_factory=tuple)
    execution_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def create(
        content: str,
        depth: int = 0,
        parent_id: str | None = None,
    ) -> ACNode:
        """Create a new AC node with generated ID.

        Args:
            content: The acceptance criterion text.
            depth: Depth in tree (default: 0 for root).
            parent_id: Parent AC ID (None for root).

        Returns:
            New ACNode instance.
        """
        return ACNode(
            id=f"ac_{uuid4().hex[:12]}",
            content=content,
            depth=depth,
            parent_id=parent_id,
        )

    def with_status(self, status: ACStatus) -> ACNode:
        """Return a new ACNode with updated status.

        Args:
            status: New status to set.

        Returns:
            New ACNode with updated status.
        """
        return ACNode(
            id=self.id,
            content=self.content,
            depth=self.depth,
            parent_id=self.parent_id,
            status=status,
            is_atomic=self.is_atomic,
            children_ids=self.children_ids,
            execution_id=self.execution_id,
            metadata=self.metadata,
        )

    def with_atomic(self, is_atomic: bool) -> ACNode:
        """Return a new ACNode with atomic flag set.

        Args:
            is_atomic: Whether this AC is atomic.

        Returns:
            New ACNode with updated is_atomic flag.
        """
        new_status = ACStatus.ATOMIC if is_atomic else self.status
        return ACNode(
            id=self.id,
            content=self.content,
            depth=self.depth,
            parent_id=self.parent_id,
            status=new_status,
            is_atomic=is_atomic,
            children_ids=self.children_ids,
            execution_id=self.execution_id,
            metadata=self.metadata,
        )

    def with_children(self, children_ids: tuple[str, ...]) -> ACNode:
        """Return a new ACNode with children set.

        Args:
            children_ids: Tuple of child AC IDs.

        Returns:
            New ACNode with children and DECOMPOSED status.
        """
        return ACNode(
            id=self.id,
            content=self.content,
            depth=self.depth,
            parent_id=self.parent_id,
            status=ACStatus.DECOMPOSED,
            is_atomic=False,
            children_ids=children_ids,
            execution_id=self.execution_id,
            metadata=self.metadata,
        )

    def with_execution_id(self, execution_id: str) -> ACNode:
        """Return a new ACNode with execution ID set.

        Args:
            execution_id: The execution ID to associate.

        Returns:
            New ACNode with execution ID set.
        """
        return ACNode(
            id=self.id,
            content=self.content,
            depth=self.depth,
            parent_id=self.parent_id,
            status=ACStatus.EXECUTING,
            is_atomic=self.is_atomic,
            children_ids=self.children_ids,
            execution_id=execution_id,
            metadata=self.metadata,
        )


@dataclass(slots=True)
class ACTree:
    """AC decomposition tree structure.

    Mutable container for managing AC hierarchy.
    Can be reconstructed from events via event replay.

    Attributes:
        root_id: ID of the root AC.
        nodes: Mapping from AC ID to ACNode.
        max_depth: Maximum allowed depth (default: 5).
    """

    root_id: str | None = None
    nodes: dict[str, ACNode] = field(default_factory=dict)
    max_depth: int = 5

    def add_node(self, node: ACNode) -> None:
        """Add a node to the tree.

        Args:
            node: The ACNode to add.

        Raises:
            ValueError: If depth exceeds max_depth.
        """
        if node.depth > self.max_depth:
            msg = f"Node depth {node.depth} exceeds max depth {self.max_depth}"
            raise ValueError(msg)

        self.nodes[node.id] = node

        # Set root if this is the first node or depth 0
        if self.root_id is None or node.depth == 0:
            self.root_id = node.id

    def get_node(self, ac_id: str) -> ACNode | None:
        """Get a node by ID.

        Args:
            ac_id: The AC ID to look up.

        Returns:
            The ACNode if found, None otherwise.
        """
        return self.nodes.get(ac_id)

    def update_node(self, node: ACNode) -> None:
        """Update an existing node.

        Args:
            node: The updated ACNode (must have same ID).

        Raises:
            KeyError: If node ID doesn't exist in tree.
        """
        if node.id not in self.nodes:
            msg = f"Node {node.id} not found in tree"
            raise KeyError(msg)
        self.nodes[node.id] = node

    def get_children(self, ac_id: str) -> list[ACNode]:
        """Get all child nodes of an AC.

        Args:
            ac_id: Parent AC ID.

        Returns:
            List of child ACNodes.
        """
        node = self.nodes.get(ac_id)
        if node is None:
            return []
        return [self.nodes[cid] for cid in node.children_ids if cid in self.nodes]

    def get_ancestors(self, ac_id: str) -> list[ACNode]:
        """Get all ancestor nodes from root to parent.

        Args:
            ac_id: The AC ID to find ancestors for.

        Returns:
            List of ancestor ACNodes from root to immediate parent.
        """
        ancestors: list[ACNode] = []
        node = self.nodes.get(ac_id)

        while node and node.parent_id:
            parent = self.nodes.get(node.parent_id)
            if parent:
                ancestors.insert(0, parent)  # Insert at beginning for root-first order
            node = parent

        return ancestors

    def get_path(self, ac_id: str) -> list[ACNode]:
        """Get the full path from root to the given AC.

        Args:
            ac_id: The target AC ID.

        Returns:
            List of ACNodes from root to target (inclusive).
        """
        node = self.nodes.get(ac_id)
        if node is None:
            return []

        path = self.get_ancestors(ac_id)
        path.append(node)
        return path

    def get_leaves(self) -> list[ACNode]:
        """Get all leaf nodes (no children).

        Returns:
            List of leaf ACNodes.
        """
        return [node for node in self.nodes.values() if not node.children_ids]

    def get_atomic_nodes(self) -> list[ACNode]:
        """Get all nodes marked as atomic.

        Returns:
            List of atomic ACNodes.
        """
        return [node for node in self.nodes.values() if node.is_atomic]

    def get_pending_nodes(self) -> list[ACNode]:
        """Get all nodes with PENDING status.

        Returns:
            List of pending ACNodes.
        """
        return [node for node in self.nodes.values() if node.status == ACStatus.PENDING]

    def can_decompose(self, ac_id: str) -> bool:
        """Check if an AC can be decomposed.

        An AC can be decomposed if:
        - It exists in the tree
        - Its depth is less than max_depth
        - It's not already decomposed
        - It's not marked as atomic

        Args:
            ac_id: The AC ID to check.

        Returns:
            True if decomposition is allowed.
        """
        node = self.nodes.get(ac_id)
        if node is None:
            return False

        return (
            node.depth < self.max_depth
            and node.status not in (ACStatus.DECOMPOSED, ACStatus.COMPLETED)
            and not node.is_atomic
        )

    def is_cyclic(self, parent_content: str, child_content: str) -> bool:
        """Check if decomposition would create a cycle.

        Simple check: child content should not be identical to parent.

        Args:
            parent_content: Parent AC content.
            child_content: Proposed child AC content.

        Returns:
            True if this would create a cycle.
        """
        # Normalize and compare
        parent_normalized = parent_content.strip().lower()
        child_normalized = child_content.strip().lower()
        return parent_normalized == child_normalized

    def to_dict(self) -> dict[str, Any]:
        """Serialize tree to dictionary.

        Returns:
            Dictionary representation for persistence.
        """
        return {
            "root_id": self.root_id,
            "max_depth": self.max_depth,
            "nodes": {
                ac_id: {
                    "id": node.id,
                    "content": node.content,
                    "depth": node.depth,
                    "parent_id": node.parent_id,
                    "status": node.status.value,
                    "is_atomic": node.is_atomic,
                    "children_ids": list(node.children_ids),
                    "execution_id": node.execution_id,
                    "metadata": node.metadata,
                }
                for ac_id, node in self.nodes.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ACTree:
        """Deserialize tree from dictionary.

        Args:
            data: Dictionary from to_dict().

        Returns:
            Reconstructed ACTree.
        """
        tree = cls(
            root_id=data.get("root_id"),
            max_depth=data.get("max_depth", 5),
        )

        for node_data in data.get("nodes", {}).values():
            node = ACNode(
                id=node_data["id"],
                content=node_data["content"],
                depth=node_data["depth"],
                parent_id=node_data.get("parent_id"),
                status=ACStatus(node_data.get("status", "pending")),
                is_atomic=node_data.get("is_atomic", False),
                children_ids=tuple(node_data.get("children_ids", [])),
                execution_id=node_data.get("execution_id"),
                metadata=node_data.get("metadata", {}),
            )
            tree.nodes[node.id] = node

        return tree
