"""Mounted regressions for terminal-cell-aware TUI labels."""

from __future__ import annotations

import pytest
from rich.cells import cell_len
from rich.text import Text
from textual.app import App, ComposeResult
from textual.widgets import Static, Tree

from ouroboros.tui.cell_width import fit_text, truncate_to_cell_width
from ouroboros.tui.screens.dashboard_v3 import SelectableACTree
from ouroboros.tui.widgets.ac_progress import ACProgressItem, ACProgressWidget
from ouroboros.tui.widgets.ac_tree import ACTreeWidget


def _tree_line_widths(tree: Tree[object]) -> list[int]:
    """Return each unscrolled tree row's full display width."""
    return [
        tree.get_label_width(line.node) + line._get_guide_width(tree.guide_depth, tree.show_root)
        for line in tree._tree_lines
    ]


def _tree_viewport_width(tree: Tree[object]) -> int:
    """Return the row width left after Tree scrollbars and padding."""
    return tree.scrollable_content_region.width


class _SelectableTreeApp(App[None]):
    def __init__(self, tree_data: dict[str, object]) -> None:
        super().__init__()
        self._tree_data = tree_data

    def compose(self) -> ComposeResult:
        yield SelectableACTree(self._tree_data)


class _LegacyTreeApp(App[None]):
    def __init__(self, tree_data: dict[str, object]) -> None:
        super().__init__()
        self._tree_data = tree_data

    def compose(self) -> ComposeResult:
        yield ACTreeWidget(self._tree_data)


class _ProgressApp(App[None]):
    def compose(self) -> ComposeResult:
        yield ACProgressWidget(
            [
                ACProgressItem(
                    index=123,
                    content="한글🙂" * 40,
                    status="in_progress",
                    elapsed_display="1m 05s",
                )
            ],
            total_count=1,
        )


@pytest.mark.parametrize("content", ["한글" * 20, "🙂" * 20, "e\u0301" * 40])
def test_truncate_to_cell_width_handles_unicode_and_tiny_widths(content: str) -> None:
    for width in range(31):
        rendered = truncate_to_cell_width(content, width)
        assert cell_len(rendered) <= width


def test_fit_text_reserves_styled_prefix_and_suffix() -> None:
    prefix = Text("⚡ 123. ", style="yellow")
    suffix = Text(" [1m 05s]", style="dim")

    rendered = fit_text("검증🙂" * 20, 24, prefix=prefix, suffix=suffix)

    assert rendered.cell_len <= 24
    assert rendered.plain.startswith(prefix.plain)
    assert rendered.plain.endswith(suffix.plain)
    assert "..." in rendered.plain


@pytest.mark.asyncio
async def test_selectable_tree_fits_guides_toggle_content_and_badges() -> None:
    tree_data = {
        "root_id": "root",
        "nodes": {
            "root": {"id": "root", "content": "Root", "children_ids": ["parent"]},
            "parent": {
                "id": "parent",
                "content": "한글🙂" * 40,
                "status": "executing",
                "children_ids": ["child"],
                "provider": "codex",
                "model": "claude-haiku-4-5",
                "tokens": 1200,
            },
            "child": {
                "id": "child",
                "content": "검증🙂" * 40,
                "status": "pending",
                "children_ids": [],
                "provider": "claude",
                "model_tier": "frugal",
                "tokens": 999,
            },
        },
    }

    app = _SelectableTreeApp(tree_data)
    async with app.run_test(size=(72, 20)) as pilot:
        await pilot.pause()
        tree = app.query_one("#ac-tree", Tree)

        assert tree.virtual_size.width <= tree.size.width
        assert all(width <= tree.size.width for width in _tree_line_widths(tree))

        parent = tree.root.children[0]
        child = parent.children[0]
        assert "..." in parent.label.plain
        assert parent.label.plain.endswith("[codex] ⚡haiku 1.2k")
        assert child.label.plain.endswith("[claude] ⚡frugal 999")


@pytest.mark.asyncio
async def test_selectable_tree_reflows_content_when_terminal_resizes() -> None:
    content = "W" * 200
    tree_data = {
        "root_id": "root",
        "nodes": {
            "root": {"id": "root", "content": "Root", "children_ids": ["child"]},
            "child": {
                "id": "child",
                "content": content,
                "status": "pending",
                "children_ids": [],
            },
        },
    }

    app = _SelectableTreeApp(tree_data)
    async with app.run_test(size=(120, 20)) as pilot:
        await pilot.pause()
        tree = app.query_one("#ac-tree", Tree)
        wide_label = tree.root.children[0].label.plain

        assert wide_label.count("W") > 40

        await pilot.resize_terminal(56, 20)
        tree = app.query_one("#ac-tree", Tree)
        narrow_label = tree.root.children[0].label.plain

        assert narrow_label.count("W") < wide_label.count("W")
        assert tree.virtual_size.width <= tree.size.width
        assert all(width <= tree.size.width for width in _tree_line_widths(tree))


@pytest.mark.asyncio
async def test_selectable_tree_reserves_vertical_scrollbar_gutter() -> None:
    child_ids = [f"child_{index}" for index in range(30)]
    nodes: dict[str, object] = {
        "root": {"id": "root", "content": "Root", "children_ids": child_ids}
    }
    nodes.update(
        {
            child_id: {
                "id": child_id,
                "content": "X" * 200,
                "status": "pending",
                "children_ids": [],
                "provider": "codex",
                "model": "claude-haiku-4-5",
                "tokens": 1200,
            }
            for child_id in child_ids
        }
    )

    app = _SelectableTreeApp({"root_id": "root", "nodes": nodes})
    async with app.run_test(size=(72, 20)) as pilot:
        await pilot.pause()
        tree = app.query_one("#ac-tree", Tree)
        viewport_width = _tree_viewport_width(tree)

        assert tree.show_vertical_scrollbar
        assert viewport_width < tree.size.width
        assert tree.virtual_size.width <= viewport_width
        assert tree.max_scroll_x == 0
        assert all(width <= viewport_width for width in _tree_line_widths(tree))


@pytest.mark.asyncio
async def test_legacy_tree_reflows_root_and_child_to_viewport() -> None:
    tree_data = {
        "root_id": "root",
        "nodes": {
            "root": {
                "id": "root",
                "content": "R" * 200,
                "status": "pending",
                "children_ids": ["child"],
            },
            "child": {
                "id": "child",
                "content": "C" * 200,
                "status": "pending",
                "children_ids": [],
            },
        },
    }

    app = _LegacyTreeApp(tree_data)
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        tree = app.query_one(Tree)
        wide_root = tree.root.label.plain
        wide_child = tree.root.children[0].label.plain

        assert wide_root.count("R") > 30
        assert wide_child.count("C") > 50

        await pilot.resize_terminal(52, 24)
        tree = app.query_one(Tree)

        assert tree.root.label.plain.count("R") < wide_root.count("R")
        assert tree.root.children[0].label.plain.count("C") < wide_child.count("C")
        assert tree.virtual_size.width <= tree.size.width
        assert all(width <= tree.size.width for width in _tree_line_widths(tree))


@pytest.mark.asyncio
async def test_legacy_tree_incremental_root_update_keeps_root_within_viewport() -> None:
    tree_data = {
        "root_id": "root",
        "nodes": {
            "root": {
                "id": "root",
                "content": "Short root",
                "status": "pending",
                "children_ids": ["child"],
            },
            "child": {
                "id": "child",
                "content": "Short child",
                "status": "pending",
                "children_ids": [],
            },
        },
    }

    app = _LegacyTreeApp(tree_data)
    async with app.run_test(size=(52, 18)) as pilot:
        await pilot.pause()
        widget = app.query_one(ACTreeWidget)
        tree = app.query_one(Tree)

        updated_data = {
            "root_id": "root",
            "nodes": {
                "root": {
                    "id": "root",
                    "content": "한글🙂" * 60,
                    "status": "pending",
                    "children_ids": ["child"],
                },
                "child": {
                    "id": "child",
                    "content": "Short child",
                    "status": "pending",
                    "children_ids": [],
                },
            },
        }
        widget.update_tree(updated_data)
        await pilot.pause()

        assert "..." in tree.root.label.plain
        assert cell_len(tree.root.label.plain) <= tree.size.width
        assert tree.virtual_size.width <= tree.size.width
        assert all(width <= tree.size.width for width in _tree_line_widths(tree))


@pytest.mark.asyncio
async def test_legacy_tree_deep_narrow_labels_fit_with_guides_and_toggles() -> None:
    node_ids = [f"node_{index}" for index in range(8)]
    nodes: dict[str, object] = {}
    for index, node_id in enumerate(node_ids):
        child_ids = [node_ids[index + 1]] if index + 1 < len(node_ids) else []
        nodes[node_id] = {
            "id": node_id,
            "content": "深い階層🙂" * 30,
            "status": "pending",
            "children_ids": child_ids,
        }

    app = _LegacyTreeApp({"root_id": node_ids[0], "nodes": nodes})
    async with app.run_test(size=(44, 24)) as pilot:
        await pilot.pause()
        tree = app.query_one(Tree)

        assert tree.virtual_size.width <= tree.size.width
        assert tree.max_scroll_x == 0
        assert all(width <= tree.size.width for width in _tree_line_widths(tree))
        assert any("..." in line.node.label.plain for line in tree._tree_lines)


@pytest.mark.asyncio
async def test_progress_row_reserves_status_index_and_elapsed_suffix() -> None:
    app = _ProgressApp()
    async with app.run_test(size=(60, 12)) as pilot:
        await pilot.pause()
        item = app.query_one(".ac-item", Static)
        rendered = item.render()

        assert rendered.cell_length <= item.size.width
        assert rendered.plain.endswith("[1m 05s...]")
        assert "..." in rendered.plain
