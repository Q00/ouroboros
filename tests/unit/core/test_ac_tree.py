"""Unit tests for ouroboros.core.ac_tree module.

Tests cover:
- ACStatus enum values
- ACNode creation and immutability
- ACNode state transitions (with_* methods)
- ACTree operations (add, get, update, query)
- Tree traversal (ancestors, path, leaves)
- Decomposition checks (can_decompose, is_cyclic)
- Serialization (to_dict, from_dict)
"""

import pytest

from ouroboros.core.ac_tree import ACNode, ACStatus, ACTree


class TestACStatusEnum:
    """Tests for ACStatus enumeration."""

    def test_status_values(self):
        """ACStatus enum should have correct values."""
        assert ACStatus.PENDING.value == "pending"
        assert ACStatus.ATOMIC.value == "atomic"
        assert ACStatus.DECOMPOSED.value == "decomposed"
        assert ACStatus.EXECUTING.value == "executing"
        assert ACStatus.COMPLETED.value == "completed"
        assert ACStatus.FAILED.value == "failed"

    def test_status_count(self):
        """ACStatus should have exactly 6 values."""
        assert len(ACStatus) == 6


class TestACNodeCreation:
    """Tests for ACNode creation."""

    def test_acnode_create_generates_id(self):
        """ACNode.create() should generate a unique ID."""
        node = ACNode.create(content="Test AC")

        assert node.id.startswith("ac_")
        assert len(node.id) == 15  # "ac_" + 12 hex chars

    def test_acnode_create_sets_defaults(self):
        """ACNode.create() should set default values."""
        node = ACNode.create(content="Test AC")

        assert node.content == "Test AC"
        assert node.depth == 0
        assert node.parent_id is None
        assert node.status == ACStatus.PENDING
        assert node.is_atomic is False
        assert node.children_ids == ()
        assert node.execution_id is None
        assert node.metadata == {}

    def test_acnode_create_with_depth_and_parent(self):
        """ACNode.create() should accept depth and parent_id."""
        node = ACNode.create(
            content="Child AC",
            depth=2,
            parent_id="ac_parent123",
        )

        assert node.content == "Child AC"
        assert node.depth == 2
        assert node.parent_id == "ac_parent123"

    def test_acnode_direct_construction(self):
        """ACNode can be constructed directly with all fields."""
        node = ACNode(
            id="ac_test123456",
            content="Direct AC",
            depth=1,
            parent_id="ac_root",
            status=ACStatus.ATOMIC,
            is_atomic=True,
            children_ids=(),
            execution_id="exec_123",
            metadata={"complexity": 0.5},
        )

        assert node.id == "ac_test123456"
        assert node.status == ACStatus.ATOMIC
        assert node.is_atomic is True
        assert node.execution_id == "exec_123"
        assert node.metadata == {"complexity": 0.5}


class TestACNodeImmutability:
    """Tests for ACNode immutability."""

    def test_acnode_is_frozen(self):
        """ACNode should be immutable (frozen dataclass)."""
        node = ACNode.create(content="Test AC")

        with pytest.raises((AttributeError, TypeError)):
            node.content = "Modified"

    def test_acnode_has_slots(self):
        """ACNode should use slots for memory efficiency."""
        node = ACNode.create(content="Test AC")

        # Slots prevent __dict__ attribute
        assert not hasattr(node, "__dict__") or node.__dict__ == {}


class TestACNodeWithMethods:
    """Tests for ACNode with_* transition methods."""

    def test_with_status_returns_new_node(self):
        """with_status() should return a new node with updated status."""
        original = ACNode.create(content="Test AC")
        updated = original.with_status(ACStatus.EXECUTING)

        assert original.status == ACStatus.PENDING
        assert updated.status == ACStatus.EXECUTING
        assert original.id == updated.id
        assert original.content == updated.content

    def test_with_atomic_true(self):
        """with_atomic(True) should set is_atomic and status to ATOMIC."""
        original = ACNode.create(content="Test AC")
        updated = original.with_atomic(True)

        assert updated.is_atomic is True
        assert updated.status == ACStatus.ATOMIC

    def test_with_atomic_false(self):
        """with_atomic(False) should set is_atomic=False and preserve status."""
        original = ACNode.create(content="Test AC")
        original = original.with_status(ACStatus.PENDING)
        updated = original.with_atomic(False)

        assert updated.is_atomic is False
        assert updated.status == ACStatus.PENDING

    def test_with_children(self):
        """with_children() should set children and status to DECOMPOSED."""
        original = ACNode.create(content="Test AC")
        children_ids = ("ac_child1", "ac_child2")
        updated = original.with_children(children_ids)

        assert updated.children_ids == children_ids
        assert updated.status == ACStatus.DECOMPOSED
        assert updated.is_atomic is False

    def test_with_execution_id(self):
        """with_execution_id() should set execution_id and status to EXECUTING."""
        original = ACNode.create(content="Test AC")
        updated = original.with_execution_id("exec_123")

        assert updated.execution_id == "exec_123"
        assert updated.status == ACStatus.EXECUTING


class TestACTreeBasicOperations:
    """Tests for ACTree basic operations."""

    def test_tree_initialization(self):
        """ACTree should initialize with empty state."""
        tree = ACTree()

        assert tree.root_id is None
        assert tree.nodes == {}
        assert tree.max_depth == 5

    def test_tree_custom_max_depth(self):
        """ACTree should accept custom max_depth."""
        tree = ACTree(max_depth=3)

        assert tree.max_depth == 3

    def test_add_node(self):
        """add_node() should add a node to the tree."""
        tree = ACTree()
        node = ACNode.create(content="Root AC")

        tree.add_node(node)

        assert node.id in tree.nodes
        assert tree.root_id == node.id

    def test_add_node_sets_root_for_depth_0(self):
        """add_node() should set root_id for depth 0 nodes."""
        tree = ACTree()
        root = ACNode.create(content="Root AC", depth=0)
        child = ACNode.create(content="Child AC", depth=1, parent_id=root.id)

        tree.add_node(child)  # Add child first
        tree.add_node(root)  # Then add root

        assert tree.root_id == root.id

    def test_add_node_rejects_exceeding_depth(self):
        """add_node() should reject nodes exceeding max_depth."""
        tree = ACTree(max_depth=3)
        node = ACNode(
            id="ac_deep",
            content="Too deep",
            depth=4,
        )

        with pytest.raises(ValueError, match="exceeds max depth"):
            tree.add_node(node)

    def test_get_node(self):
        """get_node() should return the node by ID."""
        tree = ACTree()
        node = ACNode.create(content="Test AC")
        tree.add_node(node)

        retrieved = tree.get_node(node.id)

        assert retrieved == node

    def test_get_node_not_found(self):
        """get_node() should return None for non-existent ID."""
        tree = ACTree()

        result = tree.get_node("nonexistent")

        assert result is None

    def test_update_node(self):
        """update_node() should replace an existing node."""
        tree = ACTree()
        original = ACNode.create(content="Original")
        tree.add_node(original)

        updated = original.with_status(ACStatus.COMPLETED)
        tree.update_node(updated)

        assert tree.get_node(original.id).status == ACStatus.COMPLETED

    def test_update_node_not_found_raises(self):
        """update_node() should raise KeyError for non-existent node."""
        tree = ACTree()
        node = ACNode.create(content="Test")

        with pytest.raises(KeyError, match="not found in tree"):
            tree.update_node(node)


class TestACTreeTraversal:
    """Tests for ACTree traversal methods."""

    @pytest.fixture
    def sample_tree(self):
        """Create a sample tree with 3 levels."""
        tree = ACTree()

        root = ACNode(id="ac_root", content="Root", depth=0)
        child1 = ACNode(id="ac_child1", content="Child 1", depth=1, parent_id="ac_root")
        child2 = ACNode(id="ac_child2", content="Child 2", depth=1, parent_id="ac_root")
        grandchild = ACNode(id="ac_gc", content="Grandchild", depth=2, parent_id="ac_child1")

        # Update root with children
        root_with_children = root.with_children(("ac_child1", "ac_child2"))
        child1_with_children = child1.with_children(("ac_gc",))

        tree.add_node(root_with_children)
        tree.add_node(child1_with_children)
        tree.add_node(child2)
        tree.add_node(grandchild)

        return tree

    def test_get_children(self, sample_tree):
        """get_children() should return child nodes."""
        children = sample_tree.get_children("ac_root")

        assert len(children) == 2
        child_ids = {c.id for c in children}
        assert child_ids == {"ac_child1", "ac_child2"}

    def test_get_children_empty(self, sample_tree):
        """get_children() should return empty list for leaves."""
        children = sample_tree.get_children("ac_child2")

        assert children == []

    def test_get_ancestors(self, sample_tree):
        """get_ancestors() should return ancestors from root to parent."""
        ancestors = sample_tree.get_ancestors("ac_gc")

        assert len(ancestors) == 2
        assert ancestors[0].id == "ac_root"
        assert ancestors[1].id == "ac_child1"

    def test_get_ancestors_for_root(self, sample_tree):
        """get_ancestors() should return empty list for root."""
        ancestors = sample_tree.get_ancestors("ac_root")

        assert ancestors == []

    def test_get_path(self, sample_tree):
        """get_path() should return full path from root to target."""
        path = sample_tree.get_path("ac_gc")

        assert len(path) == 3
        assert path[0].id == "ac_root"
        assert path[1].id == "ac_child1"
        assert path[2].id == "ac_gc"

    def test_get_leaves(self, sample_tree):
        """get_leaves() should return nodes without children."""
        leaves = sample_tree.get_leaves()

        leaf_ids = {n.id for n in leaves}
        assert leaf_ids == {"ac_child2", "ac_gc"}


class TestACTreeQueries:
    """Tests for ACTree query methods."""

    def test_get_atomic_nodes(self):
        """get_atomic_nodes() should return nodes marked as atomic."""
        tree = ACTree()
        atomic1 = ACNode(id="ac_a1", content="Atomic 1", depth=0, is_atomic=True, status=ACStatus.ATOMIC)
        atomic2 = ACNode(id="ac_a2", content="Atomic 2", depth=0, is_atomic=True, status=ACStatus.ATOMIC)
        non_atomic = ACNode(id="ac_na", content="Non-atomic", depth=0, is_atomic=False)

        tree.add_node(atomic1)
        tree.add_node(atomic2)
        tree.add_node(non_atomic)

        atomic_nodes = tree.get_atomic_nodes()

        assert len(atomic_nodes) == 2
        atomic_ids = {n.id for n in atomic_nodes}
        assert atomic_ids == {"ac_a1", "ac_a2"}

    def test_get_pending_nodes(self):
        """get_pending_nodes() should return nodes with PENDING status."""
        tree = ACTree()
        pending1 = ACNode(id="ac_p1", content="Pending 1", depth=0, status=ACStatus.PENDING)
        pending2 = ACNode(id="ac_p2", content="Pending 2", depth=0, status=ACStatus.PENDING)
        completed = ACNode(id="ac_c", content="Completed", depth=0, status=ACStatus.COMPLETED)

        tree.add_node(pending1)
        tree.add_node(pending2)
        tree.add_node(completed)

        pending_nodes = tree.get_pending_nodes()

        assert len(pending_nodes) == 2
        pending_ids = {n.id for n in pending_nodes}
        assert pending_ids == {"ac_p1", "ac_p2"}


class TestACTreeDecomposition:
    """Tests for ACTree decomposition checks."""

    def test_can_decompose_true(self):
        """can_decompose() should return True for eligible nodes."""
        tree = ACTree(max_depth=5)
        node = ACNode(id="ac_test", content="Test", depth=2, status=ACStatus.PENDING)
        tree.add_node(node)

        assert tree.can_decompose("ac_test") is True

    def test_can_decompose_false_max_depth(self):
        """can_decompose() should return False at max depth."""
        tree = ACTree(max_depth=5)
        node = ACNode(id="ac_test", content="Test", depth=5, status=ACStatus.PENDING)
        tree.add_node(node)

        assert tree.can_decompose("ac_test") is False

    def test_can_decompose_false_already_decomposed(self):
        """can_decompose() should return False for decomposed nodes."""
        tree = ACTree()
        node = ACNode(id="ac_test", content="Test", depth=0, status=ACStatus.DECOMPOSED)
        tree.add_node(node)

        assert tree.can_decompose("ac_test") is False

    def test_can_decompose_false_already_atomic(self):
        """can_decompose() should return False for atomic nodes."""
        tree = ACTree()
        node = ACNode(id="ac_test", content="Test", depth=0, is_atomic=True, status=ACStatus.ATOMIC)
        tree.add_node(node)

        assert tree.can_decompose("ac_test") is False

    def test_can_decompose_false_completed(self):
        """can_decompose() should return False for completed nodes."""
        tree = ACTree()
        node = ACNode(id="ac_test", content="Test", depth=0, status=ACStatus.COMPLETED)
        tree.add_node(node)

        assert tree.can_decompose("ac_test") is False

    def test_can_decompose_nonexistent(self):
        """can_decompose() should return False for non-existent nodes."""
        tree = ACTree()

        assert tree.can_decompose("nonexistent") is False

    def test_is_cyclic_identical(self):
        """is_cyclic() should detect identical content."""
        tree = ACTree()

        assert tree.is_cyclic("Test AC", "Test AC") is True
        assert tree.is_cyclic("Test AC", "  test ac  ") is True

    def test_is_cyclic_different(self):
        """is_cyclic() should return False for different content."""
        tree = ACTree()

        assert tree.is_cyclic("Parent AC", "Child AC") is False


class TestACTreeSerialization:
    """Tests for ACTree serialization."""

    def test_to_dict(self):
        """to_dict() should serialize tree correctly."""
        tree = ACTree(max_depth=5)
        node = ACNode(
            id="ac_test",
            content="Test AC",
            depth=0,
            status=ACStatus.PENDING,
            metadata={"key": "value"},
        )
        tree.add_node(node)

        data = tree.to_dict()

        assert data["root_id"] == "ac_test"
        assert data["max_depth"] == 5
        assert "ac_test" in data["nodes"]
        assert data["nodes"]["ac_test"]["content"] == "Test AC"
        assert data["nodes"]["ac_test"]["metadata"] == {"key": "value"}

    def test_from_dict(self):
        """from_dict() should deserialize tree correctly."""
        data = {
            "root_id": "ac_test",
            "max_depth": 3,
            "nodes": {
                "ac_test": {
                    "id": "ac_test",
                    "content": "Test AC",
                    "depth": 0,
                    "parent_id": None,
                    "status": "pending",
                    "is_atomic": False,
                    "children_ids": [],
                    "execution_id": None,
                    "metadata": {},
                },
            },
        }

        tree = ACTree.from_dict(data)

        assert tree.root_id == "ac_test"
        assert tree.max_depth == 3
        assert tree.get_node("ac_test").content == "Test AC"

    def test_roundtrip_serialization(self):
        """Tree should survive serialization roundtrip."""
        original = ACTree(max_depth=4)
        node = ACNode(
            id="ac_root",
            content="Root AC",
            depth=0,
            status=ACStatus.COMPLETED,
            children_ids=("ac_child1", "ac_child2"),
            metadata={"complexity": 0.8},
        )
        original.add_node(node)

        data = original.to_dict()
        restored = ACTree.from_dict(data)

        assert restored.root_id == original.root_id
        assert restored.max_depth == original.max_depth
        restored_node = restored.get_node("ac_root")
        assert restored_node.content == node.content
        assert restored_node.status == node.status
        assert restored_node.children_ids == node.children_ids
        assert restored_node.metadata == node.metadata
