"""Unit tests for TUI widgets."""

from unittest.mock import MagicMock

from rich.cells import cell_len
from textual import events
from textual.geometry import Size

from ouroboros.tui.widgets.ac_progress import ACProgressItem, ACProgressWidget
from ouroboros.tui.widgets.ac_tree import STATUS_ICONS as TREE_STATUS_ICONS
from ouroboros.tui.widgets.ac_tree import ACTreeWidget
from ouroboros.tui.widgets.phase_progress import PhaseIndicator, PhaseProgressWidget


def _resize_event(width: int = 30, height: int = 24) -> events.Resize:
    """Build a minimal ``events.Resize`` for directly invoking ``on_resize``."""
    size = Size(width, height)
    return events.Resize(size=size, virtual_size=size)


class TestPhaseIndicator:
    """Tests for PhaseIndicator widget."""

    def test_create_phase_indicator(self) -> None:
        """Test creating a phase indicator."""
        indicator = PhaseIndicator(
            phase_name="discover",
            phase_label="Discover",
            phase_type="diverge",
            is_active=False,
            is_completed=False,
        )

        assert indicator.phase_name == "discover"
        assert indicator.phase_type == "diverge"
        assert indicator.has_class("diverge")

    def test_active_indicator(self) -> None:
        """Test active phase indicator."""
        indicator = PhaseIndicator(
            phase_name="define",
            phase_label="Define",
            phase_type="converge",
            is_active=True,
        )

        assert indicator.has_class("active")

    def test_completed_indicator(self) -> None:
        """Test completed phase indicator."""
        indicator = PhaseIndicator(
            phase_name="discover",
            phase_label="Discover",
            phase_type="diverge",
            is_completed=True,
        )

        assert indicator.has_class("completed")

    def test_set_active(self) -> None:
        """Test setting active state."""
        indicator = PhaseIndicator(
            phase_name="discover",
            phase_label="Discover",
            phase_type="diverge",
        )

        indicator.set_active(True)
        assert indicator.has_class("active")

        indicator.set_active(False)
        assert not indicator.has_class("active")

    def test_set_completed(self) -> None:
        """Test setting completed state."""
        indicator = PhaseIndicator(
            phase_name="discover",
            phase_label="Discover",
            phase_type="diverge",
        )

        indicator.set_completed(True)
        assert indicator.has_class("completed")

        indicator.set_completed(False)
        assert not indicator.has_class("completed")


class TestPhaseProgressWidget:
    """Tests for PhaseProgressWidget."""

    def test_create_widget(self) -> None:
        """Test creating phase progress widget."""
        widget = PhaseProgressWidget(current_phase="discover", iteration=1)

        assert widget.current_phase == "discover"
        assert widget.iteration == 1

    def test_update_phase(self) -> None:
        """Test updating current phase."""
        widget = PhaseProgressWidget()

        widget.update_phase("define", iteration=2)

        assert widget.current_phase == "define"
        assert widget.iteration == 2

    def test_is_phase_completed(self) -> None:
        """Test phase completion check."""
        widget = PhaseProgressWidget(current_phase="design")

        # Discover and Define should be completed
        assert widget._is_phase_completed("discover") is True
        assert widget._is_phase_completed("define") is True
        # Design and Deliver should not be completed
        assert widget._is_phase_completed("design") is False
        assert widget._is_phase_completed("deliver") is False

    def test_is_phase_completed_no_current(self) -> None:
        """Test phase completion when no current phase."""
        widget = PhaseProgressWidget(current_phase="")

        assert widget._is_phase_completed("discover") is False


class TestACTreeWidget:
    """Tests for ACTreeWidget."""

    def test_create_widget_empty(self) -> None:
        """Test creating empty AC tree widget."""
        widget = ACTreeWidget()

        assert widget.tree_data == {}
        assert widget.current_ac_id == ""
        assert widget._node_map == {}

    def test_create_widget_with_data(self) -> None:
        """Test creating widget with tree data."""
        tree_data = {
            "root_id": "ac_123",
            "nodes": {
                "ac_123": {
                    "id": "ac_123",
                    "content": "Root AC",
                    "depth": 0,
                    "status": "pending",
                    "is_atomic": False,
                    "children_ids": [],
                },
            },
        }

        widget = ACTreeWidget(tree_data=tree_data, current_ac_id="ac_123")

        assert widget.tree_data == tree_data
        assert widget.current_ac_id == "ac_123"

    def test_compose_root_label_truncates_with_ellipsis(self) -> None:
        """The root Tree label truncation must append '...' when it actually
        truncates (previously a silent hard cut with no visual indicator).

        Round-4 follow-up: the root label now truncates by display CELLS via
        the same ``_truncate_to_cell_width`` helper ordinary labels use, so
        the "..." is reserved WITHIN the 30-cell budget (27 kept + 3
        ellipsis), not appended past it.
        """
        long_content = "R" * 100
        tree_data = {
            "root_id": "ac_root",
            "nodes": {
                "ac_root": {
                    "id": "ac_root",
                    "content": long_content,
                    "depth": 0,
                    "status": "pending",
                    "is_atomic": False,
                    "children_ids": [],
                },
            },
        }
        widget = ACTreeWidget(tree_data=tree_data)

        widgets = list(widget.compose())
        tree = next(w for w in widgets if hasattr(w, "root"))

        assert str(tree.root.label).endswith("...")
        assert long_content[:27] in str(tree.root.label)
        assert long_content[:30] not in str(tree.root.label)

    def test_root_label_truncation_is_cell_width_aware_for_cjk(self) -> None:
        """Round-4 follow-up: the root label was still truncated by Python
        code points while ordinary labels had already been fixed to truncate
        by terminal display cells. A CJK root label (2 cells per character)
        sliced by code points overflows its cell budget -- the root must use
        the SAME ``_truncate_to_cell_width`` helper as every other label."""
        from rich.cells import cell_len

        widget = ACTreeWidget()
        cjk_content = "가" * 100  # Hangul syllables: 2 display cells each

        label = widget._format_root_label(cjk_content)

        assert label.endswith("...")
        # The whole rendered label must fit the 30-cell default root budget.
        # Code-point slicing would have kept 30 chars = 60 cells + "...".
        assert cell_len(label) <= 30

    def test_update_tree(self) -> None:
        """Test updating tree data."""
        widget = ACTreeWidget()
        tree_data = {"root_id": "ac_456", "nodes": {}}

        widget.update_tree(tree_data, current_ac_id="ac_456")

        assert widget.tree_data == tree_data
        assert widget.current_ac_id == "ac_456"

    def test_update_tree_force_rebuild(self) -> None:
        """Test update_tree with force_rebuild clears node map."""
        widget = ACTreeWidget()
        widget._node_map = {"ac_old": "dummy"}

        widget.update_tree({}, force_rebuild=True)

        assert widget._node_map == {}

    def test_update_tree_recomposes_when_subtask_changes_tree_shape(self) -> None:
        """New Sub-AC nodes should force a rebuild so the rendered tree stays in sync."""
        initial_tree = {
            "root_id": "root",
            "nodes": {
                "root": {
                    "id": "root",
                    "content": "Acceptance Criteria",
                    "children_ids": ["ac_1"],
                },
                "ac_1": {
                    "id": "ac_1",
                    "content": "Composite AC",
                    "status": "executing",
                    "children_ids": [],
                },
            },
        }
        updated_tree = {
            "root_id": "root",
            "nodes": {
                **initial_tree["nodes"],
                "ac_1": {
                    "id": "ac_1",
                    "content": "Composite AC",
                    "status": "executing",
                    "children_ids": ["ac_1_sub_1"],
                },
                "ac_1_sub_1": {
                    "id": "ac_1_sub_1",
                    "content": "Draft migration plan",
                    "status": "executing",
                    "is_atomic": True,
                    "children_ids": [],
                },
            },
        }

        widget = ACTreeWidget(tree_data=initial_tree)
        widget._tree_widget = MagicMock()
        widget._tree_data_cache = initial_tree
        widget._node_map = {"root": MagicMock(), "ac_1": MagicMock()}
        widget.refresh = MagicMock()

        widget.update_tree(updated_tree)

        assert any(call.kwargs.get("recompose") is True for call in widget.refresh.call_args_list)
        assert widget._node_map == {}

    def test_update_tree_syncs_existing_labels_for_rapid_subtask_status_changes(self) -> None:
        """Status-only Sub-AC updates should patch the rendered labels without a full rebuild."""
        initial_tree = {
            "root_id": "root",
            "nodes": {
                "root": {
                    "id": "root",
                    "content": "Acceptance Criteria",
                    "children_ids": ["ac_1"],
                },
                "ac_1": {
                    "id": "ac_1",
                    "content": "Composite AC",
                    "status": "executing",
                    "children_ids": ["ac_1_sub_1"],
                },
                "ac_1_sub_1": {
                    "id": "ac_1_sub_1",
                    "content": "Draft migration plan",
                    "status": "executing",
                    "is_atomic": True,
                    "children_ids": [],
                },
            },
        }
        updated_tree = {
            "root_id": "root",
            "nodes": {
                **initial_tree["nodes"],
                "ac_1_sub_1": {
                    "id": "ac_1_sub_1",
                    "content": "Draft migration plan",
                    "status": "completed",
                    "is_atomic": True,
                    "children_ids": [],
                },
            },
        }

        root_node = MagicMock()
        ac_node = MagicMock()
        subtask_node = MagicMock()

        widget = ACTreeWidget(tree_data=initial_tree)
        widget._tree_widget = MagicMock()
        widget._tree_data_cache = initial_tree
        widget._node_map = {
            "root": root_node,
            "ac_1": ac_node,
            "ac_1_sub_1": subtask_node,
        }
        widget.refresh = MagicMock()

        widget.update_tree(updated_tree)

        assert not any(
            call.kwargs.get("recompose") is True for call in widget.refresh.call_args_list
        )
        subtask_node.set_label.assert_called_once()
        rendered_label = subtask_node.set_label.call_args[0][0]
        assert "[green][OK][/green]" in rendered_label
        assert "Draft migration plan" in rendered_label

    def test_update_node_status(self) -> None:
        """Test updating a node's status."""
        tree_data = {
            "root_id": "ac_123",
            "nodes": {
                "ac_123": {
                    "id": "ac_123",
                    "content": "Test AC",
                    "depth": 0,
                    "status": "pending",
                    "is_atomic": False,
                    "children_ids": [],
                },
            },
        }
        widget = ACTreeWidget(tree_data=tree_data)

        widget.update_node_status("ac_123", "completed")

        assert widget.tree_data["nodes"]["ac_123"]["status"] == "completed"

    def test_update_node_status_nonexistent(self) -> None:
        """Test updating status of nonexistent node does nothing."""
        tree_data = {
            "root_id": "ac_123",
            "nodes": {
                "ac_123": {"id": "ac_123", "content": "Test", "status": "pending"},
            },
        }
        widget = ACTreeWidget(tree_data=tree_data)

        # Should not raise
        widget.update_node_status("nonexistent", "completed")

        assert widget.tree_data["nodes"]["ac_123"]["status"] == "pending"

    def test_format_node_label_pending(self) -> None:
        """Test formatting label for pending node."""
        widget = ACTreeWidget()
        node_data = {
            "status": "pending",
            "content": "Test content",
            "is_atomic": False,
        }

        label = widget._format_node_label(node_data)

        assert "[dim][ ][/dim]" in label
        assert "Test content" in label

    def test_format_node_label_atomic(self) -> None:
        """Test formatting label for atomic node."""
        widget = ACTreeWidget()
        node_data = {
            "status": "atomic",
            "content": "Atomic task",
            "is_atomic": True,
        }

        label = widget._format_node_label(node_data)

        assert "[blue][A][/blue]" in label

    def test_format_node_label_atomic_subtask_keeps_runtime_status_icon(self) -> None:
        """Atomic Sub-ACs should still surface live execution status changes."""
        widget = ACTreeWidget()
        node_data = {
            "status": "completed",
            "content": "Atomic subtask",
            "is_atomic": True,
        }

        label = widget._format_node_label(node_data)

        assert "[green][OK][/green]" in label
        assert "[blue][A][/blue]" not in label

    def test_format_node_label_current(self) -> None:
        """Test formatting label for current AC."""
        widget = ACTreeWidget()
        node_data = {
            "status": "executing",
            "content": "Current task",
            "is_atomic": False,
        }

        label = widget._format_node_label(node_data, is_current=True)

        assert "[bold yellow]" in label

    def test_format_node_label_truncation(self) -> None:
        """Test content truncation in label."""
        widget = ACTreeWidget()
        long_content = "A" * 100
        node_data = {
            "status": "pending",
            "content": long_content,
            "is_atomic": False,
        }

        label = widget._format_node_label(node_data)

        assert "..." in label
        # Cell-width-aware truncation reserves 3 cells for the "..." within
        # the 50-cell budget, so the kept content is 47 chars, not 50.
        assert long_content[:47] in label
        assert long_content[:50] not in label

    def test_format_node_label_is_width_aware_not_a_fixed_constant(self) -> None:
        """Truncation must scale with the available width, not a hard-coded
        50-char slice: a wider budget keeps more content, a narrower one
        keeps less — proving the label-building function is no longer a
        fixed constant."""
        widget = ACTreeWidget()
        long_content = "B" * 100
        node_data = {
            "status": "pending",
            "content": long_content,
            "is_atomic": False,
        }

        narrow_label = widget._format_node_label(node_data, max_width=15)
        wide_label = widget._format_node_label(node_data, max_width=80)

        # Cell-width-aware truncation reserves 3 cells for "..." within each
        # budget, so the kept content is width - 3 chars.
        assert long_content[:12] in narrow_label
        assert long_content[:77] in wide_label
        assert narrow_label != wide_label
        assert len(wide_label) > len(narrow_label)

    def test_format_node_label_truncation_is_cell_width_aware_for_cjk(self) -> None:
        """CJK characters occupy 2 terminal display cells each. Truncating by
        Python code-point count (the pre-fix behavior) could keep TWICE the
        intended cell budget's worth of content and overflow the available
        terminal width. The truncated label's rendered cell width must never
        exceed the requested budget."""
        widget = ACTreeWidget()
        long_cjk_content = "한글테스트문자열" * 10  # 80 code points, 160 cells
        node_data = {
            "status": "pending",
            "content": long_cjk_content,
            "is_atomic": False,
        }

        label = widget._format_node_label(node_data, max_width=20)

        assert "..." in label
        # Strip the leading status icon + space (not part of the content
        # truncation budget) before measuring the content's cell width.
        prefix = f"{TREE_STATUS_ICONS['pending']} "
        assert label.startswith(prefix)
        content_part = label[len(prefix) :]
        assert cell_len(content_part) <= 20

    def test_label_max_width_falls_back_when_unmounted(self) -> None:
        """A widget with no real rendered size yet falls back to the default
        budget rather than crashing or truncating to zero."""
        widget = ACTreeWidget()

        assert widget._label_max_width() == 50

    def test_on_resize_retruncates_root_and_child_labels(self) -> None:
        """Fix 9 (P2, PR #1648 review): labels are computed once at compose
        time using the widget's width at that moment; without a resize
        handler, resizing the terminal afterward never updates the
        truncation budget. A simulated resize (here, a narrower budget
        exactly as ``_label_max_width`` would report after a real terminal
        shrink) must re-truncate every rendered label — root AND children."""
        long_content = "N" * 100
        tree_data = {
            "root_id": "ac_root",
            "nodes": {
                "ac_root": {
                    "id": "ac_root",
                    "content": long_content,
                    "depth": 0,
                    "status": "pending",
                    "is_atomic": False,
                    "children_ids": ["ac_child"],
                },
                "ac_child": {
                    "id": "ac_child",
                    "content": long_content,
                    "depth": 1,
                    "status": "pending",
                    "is_atomic": True,
                    "children_ids": [],
                },
            },
        }
        widget = ACTreeWidget(tree_data=tree_data)
        tree = next(w for w in widget.compose() if hasattr(w, "root"))

        initial_root_label = str(tree.root.label)
        child_node = widget._node_map["ac_child"]
        initial_child_label = str(child_node.label)
        # Unmounted defaults: root truncates at 30, other nodes at 50 cells.
        # Cell-width-aware truncation reserves 3 cells for "..." within each
        # budget, so the kept content is width - 3 chars.
        assert long_content[:27] in initial_root_label
        assert long_content[:47] in initial_child_label

        # Simulate resizing to a much narrower terminal.
        widget._label_max_width = lambda **_kwargs: 10  # type: ignore[method-assign]
        widget.on_resize(_resize_event(width=25))

        resized_root_label = str(tree.root.label)
        resized_child_label = str(child_node.label)
        assert resized_root_label != initial_root_label
        assert resized_child_label != initial_child_label
        assert long_content[:7] in resized_root_label
        assert long_content[:7] in resized_child_label

    def test_on_resize_before_compose_is_a_no_op(self) -> None:
        """A resize delivered before the tree has ever been built (no
        ``_tree_widget``/``_node_map`` yet) must not raise."""
        widget = ACTreeWidget()

        widget.on_resize(_resize_event())  # must not raise

    def test_mark_node_atomic(self) -> None:
        """Test marking a node as atomic."""
        tree_data = {
            "root_id": "ac_123",
            "nodes": {
                "ac_123": {
                    "id": "ac_123",
                    "content": "Test AC",
                    "depth": 0,
                    "status": "pending",
                    "is_atomic": False,
                },
            },
        }
        widget = ACTreeWidget(tree_data=tree_data)

        widget.mark_node_atomic("ac_123")

        assert widget.tree_data["nodes"]["ac_123"]["is_atomic"] is True
        assert widget.tree_data["nodes"]["ac_123"]["status"] == "atomic"

    def test_mark_node_atomic_nonexistent(self) -> None:
        """Test marking nonexistent node does nothing."""
        tree_data = {
            "root_id": "ac_123",
            "nodes": {"ac_123": {"id": "ac_123", "is_atomic": False}},
        }
        widget = ACTreeWidget(tree_data=tree_data)

        # Should not raise
        widget.mark_node_atomic("nonexistent")

        assert widget.tree_data["nodes"]["ac_123"]["is_atomic"] is False

    def test_add_children_no_tree_widget(self) -> None:
        """Test add_children returns False when tree widget not initialized."""
        widget = ACTreeWidget()
        children = [{"id": "child_1", "content": "Child 1"}]

        result = widget.add_children("parent_id", children)

        assert result is False

    def test_add_children_parent_not_found(self) -> None:
        """Test add_children returns False when parent not in node_map."""
        widget = ACTreeWidget()
        widget._tree_widget = "dummy"  # Simulate initialized tree
        widget._node_map = {"other_id": "node"}
        children = [{"id": "child_1", "content": "Child 1"}]

        result = widget.add_children("parent_id", children)

        assert result is False

    def test_get_node_by_id_found(self) -> None:
        """Test getting node by ID when it exists."""
        widget = ACTreeWidget()
        mock_node = "mock_tree_node"
        widget._node_map = {"ac_123": mock_node}

        result = widget.get_node_by_id("ac_123")

        assert result == mock_node

    def test_get_node_by_id_not_found(self) -> None:
        """Test getting node by ID when it doesn't exist."""
        widget = ACTreeWidget()
        widget._node_map = {}

        result = widget.get_node_by_id("nonexistent")

        assert result is None


class TestACProgressItem:
    """Tests for ACProgressItem dataclass."""

    def test_create_item(self) -> None:
        """Test creating an AC progress item."""
        item = ACProgressItem(
            index=1,
            content="Create a hello.py file",
            status="pending",
        )

        assert item.index == 1
        assert item.content == "Create a hello.py file"
        assert item.status == "pending"
        assert item.elapsed_display == ""
        assert item.is_current is False

    def test_create_item_with_elapsed(self) -> None:
        """Test creating item with elapsed time."""
        item = ACProgressItem(
            index=2,
            content="Run tests",
            status="in_progress",
            elapsed_display="45s",
            is_current=True,
        )

        assert item.index == 2
        assert item.status == "in_progress"
        assert item.elapsed_display == "45s"
        assert item.is_current is True


class TestACProgressWidget:
    """Tests for ACProgressWidget."""

    def test_create_widget_empty(self) -> None:
        """Test creating an empty progress widget."""
        widget = ACProgressWidget()

        assert widget.acceptance_criteria == []
        assert widget.completed_count == 0
        assert widget.total_count == 0

    def test_create_widget_with_criteria(self) -> None:
        """Test creating widget with acceptance criteria."""
        items = [
            ACProgressItem(index=1, content="AC 1", status="completed"),
            ACProgressItem(index=2, content="AC 2", status="in_progress"),
            ACProgressItem(index=3, content="AC 3", status="pending"),
        ]

        widget = ACProgressWidget(
            acceptance_criteria=items,
            completed_count=1,
            total_count=3,
        )

        assert len(widget.acceptance_criteria) == 3
        assert widget.completed_count == 1
        assert widget.total_count == 3

    def test_update_progress(self) -> None:
        """Test updating progress."""
        widget = ACProgressWidget()

        items = [
            ACProgressItem(index=1, content="AC 1", status="completed"),
        ]

        widget.update_progress(
            acceptance_criteria=items,
            completed_count=1,
            total_count=2,
            estimated_remaining="~5m remaining",
        )

        assert len(widget.acceptance_criteria) == 1
        assert widget.completed_count == 1
        assert widget.total_count == 2
        assert widget.estimated_remaining == "~5m remaining"

    def test_update_progress_partial(self) -> None:
        """Test partial progress update."""
        widget = ACProgressWidget(
            completed_count=0,
            total_count=3,
        )

        widget.update_progress(completed_count=1)

        assert widget.completed_count == 1
        assert widget.total_count == 3  # Unchanged

    def test_render_ac_item_truncation_is_width_aware_not_a_fixed_constant(self) -> None:
        """Truncation must scale with the available width, not a hard-coded
        45-char slice: a wider budget keeps more content, a narrower one
        keeps less."""
        widget = ACProgressWidget()
        long_content = "C" * 100
        item = ACProgressItem(index=1, content=long_content, status="pending")

        narrow = widget._render_ac_item(item, max_width=15)
        wide = widget._render_ac_item(item, max_width=80)

        # Cell-width-aware truncation reserves 3 cells for "..." within each
        # budget, so the kept content is width - 3 chars.
        assert long_content[:12] in str(narrow.render())
        assert long_content[:77] in str(wide.render())
        assert str(narrow.render()) != str(wide.render())

    def test_content_max_width_is_bounded_by_panes_narrower_than_the_minimum(self) -> None:
        """Round-6 (non-blocking): the 20-cell minimum truncation width must
        not exceed the widget's ACTUAL width in a very narrow pane — the
        truncation budget is capped by the space that exists."""
        from unittest.mock import PropertyMock, patch

        widget = ACProgressWidget()

        def _width_for(size_width: int) -> int:
            fake_size = type("Size", (), {"width": size_width, "height": 5})()
            with patch.object(
                ACProgressWidget, "size", new_callable=PropertyMock, return_value=fake_size
            ):
                return widget._content_max_width()

        # Narrower than the minimum: bounded by the actual pane width.
        assert _width_for(10) == 10
        # Tight-but-usable pane: the readability minimum still applies.
        assert _width_for(25) == 20
        # Ample pane: reserved chars subtracted as before.
        assert _width_for(100) == 80

    def test_render_ac_item_truncation_is_cell_width_aware_for_cjk(self) -> None:
        """CJK characters occupy 2 terminal display cells each. Truncating by
        Python code-point count (the pre-fix behavior) could keep TWICE the
        intended cell budget's worth of content and overflow the available
        terminal width. The truncated content's rendered cell width must
        never exceed the requested budget."""
        widget = ACProgressWidget()
        long_cjk_content = "한글테스트문자열" * 10  # 80 code points, 160 cells
        item = ACProgressItem(index=1, content=long_cjk_content, status="pending")

        rendered = str(widget._render_ac_item(item, max_width=20).render())

        assert "..." in rendered
        # The content is everything after the "N. " index prefix.
        content_part = rendered.split(". ", 1)[1]
        assert cell_len(content_part) <= 20

    def test_content_max_width_falls_back_when_unmounted(self) -> None:
        """A widget with no real rendered size yet falls back to the default
        budget rather than crashing or truncating to zero."""
        widget = ACProgressWidget()

        assert widget._content_max_width() == 45

    def test_on_resize_forces_a_recompose(self) -> None:
        """Fix 9 (P2, PR #1648 review): items are truncated at compose time
        using the widget's width at that moment; without a resize handler,
        resizing the terminal afterward never updates the truncation budget
        (the same class of bug as ``ACTreeWidget``, but this widget always
        rebuilds from scratch rather than patching items in place, so a full
        recompose — the same mechanism ``watch_acceptance_criteria`` already
        uses for content changes — is the correct, minimal fix)."""
        widget = ACProgressWidget()
        widget.refresh = MagicMock()  # type: ignore[method-assign]

        widget.on_resize(_resize_event())

        widget.refresh.assert_called_once_with(recompose=True)
