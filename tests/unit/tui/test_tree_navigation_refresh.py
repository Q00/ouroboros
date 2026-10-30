"""Live status refreshes must not interrupt tree navigation."""

from copy import deepcopy

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Tree

from ouroboros.tui.screens.dashboard_v3 import SelectableACTree

_TREE = {
    "root_id": "root",
    "nodes": {
        "root": {"id": "root", "children_ids": ["branch", "other"]},
        "branch": {
            "id": "branch",
            "content": "Expandable criterion",
            "status": "executing",
            "children_ids": ["leaf"],
        },
        "leaf": {"id": "leaf", "content": "Nested work", "status": "pending"},
        "other": {"id": "other", "content": "Other criterion", "status": "pending"},
    },
}


class _TreeApp(App):
    def compose(self) -> ComposeResult:
        yield SelectableACTree(deepcopy(_TREE))


@pytest.mark.asyncio
@pytest.mark.parametrize("update", ["tree", "status", "activity", "activity-clear"])
async def test_live_refresh_preserves_cursor_and_collapsed_branch(update):
    app = _TreeApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        widget = app.query_one(SelectableACTree)
        tree = app.query_one(Tree)
        branch, other = tree.root.children
        branch.collapse()
        tree.move_cursor(other)
        await pilot.pause()
        assert tree.cursor_node.data["id"] == "other"

        if update == "tree":
            changed = deepcopy(_TREE)
            changed["nodes"]["other"]["content"] = "Updated criterion"
            widget.update_tree(changed)
        elif update == "status":
            widget.update_node_status("other", "completed")
        elif update == "activity":
            widget.update_node_activity("branch", "Read sample.py")
        else:
            widget.clear_node_activity("branch")
        await pilot.pause()

        assert tree.cursor_node.data["id"] == "other"
        assert not tree.root.children[0].is_expanded
        if update == "tree":
            assert tree.cursor_node.data["content"] == "Updated criterion"
        elif update == "status":
            assert tree.cursor_node.data["status"] == "completed"


@pytest.mark.asyncio
async def test_new_branch_expands_without_reopening_existing_collapsed_branch():
    app = _TreeApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        widget = app.query_one(SelectableACTree)
        tree = app.query_one(Tree)
        tree.root.children[0].collapse()
        tree.move_cursor(tree.root.children[1])
        changed = deepcopy(_TREE)
        changed["nodes"]["root"]["children_ids"].append("new-branch")
        changed["nodes"]["new-branch"] = {
            "id": "new-branch",
            "content": "New work",
            "children_ids": ["new-leaf"],
        }
        changed["nodes"]["new-leaf"] = {"id": "new-leaf", "content": "New nested work"}
        widget.update_tree(changed)
        await pilot.pause()
        assert tree.cursor_node.data["id"] == "other"
        assert not tree.root.children[0].is_expanded
        assert tree.root.children[2].is_expanded


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", [False, True])
async def test_removed_cursor_node_falls_back_to_visible_root(empty):
    app = _TreeApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        widget = app.query_one(SelectableACTree)
        tree = app.query_one(Tree)
        tree.move_cursor(tree.root.children[1])
        changed = deepcopy(_TREE)
        changed["nodes"]["root"]["children_ids"].remove("other")
        del changed["nodes"]["other"]
        widget.update_tree({} if empty else changed)
        await pilot.pause()
        assert tree.cursor_node is tree.root


@pytest.mark.asyncio
async def test_collapsed_root_remains_collapsed_after_status_update():
    app = _TreeApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        widget = app.query_one(SelectableACTree)
        tree = app.query_one(Tree)
        tree.root.collapse()
        tree.move_cursor(tree.root)
        widget.update_node_status("other", "completed")
        await pilot.pause()
        assert tree.cursor_node is tree.root
        assert not tree.root.is_expanded
