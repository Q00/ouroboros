"""Unit tests for depth-limited AC tree HUD markdown rendering."""

from ouroboros.mcp.tools.ac_tree_hud_render import render_ac_tree_markdown


def test_render_ac_tree_markdown_shows_depth_3_nodes() -> None:
    snapshot = {
        "tree": {
            "root_id": "root",
            "nodes": {
                "root": {
                    "id": "root",
                    "content": "Acceptance Criteria",
                    "status": "executing",
                    "children_ids": ["ac_1"],
                },
                "ac_1": {
                    "id": "ac_1",
                    "content": "Top-level AC",
                    "status": "completed",
                    "children_ids": ["sub_ac_1"],
                },
                "sub_ac_1": {
                    "id": "sub_ac_1",
                    "content": "Depth 2 task",
                    "status": "executing",
                    "tool_detail": "Read src/app.py",
                    "children_ids": ["sub_sub_ac_1"],
                },
                "sub_sub_ac_1": {
                    "id": "sub_sub_ac_1",
                    "content": "Depth 3 task",
                    "status": "pending",
                    "children_ids": [],
                },
            },
        }
    }

    rendered = render_ac_tree_markdown(snapshot)

    assert rendered == "\n".join(
        [
            "◇ Acceptance Criteria",
            "└─ ● Top-level AC",
            "   └─ ◐ Depth 2 task [Read src/app.py]",
            "      └─ ○ Depth 3 task",
        ]
    )


def test_render_ac_tree_markdown_collapses_nodes_deeper_than_depth_3() -> None:
    snapshot = {
        "tree": {
            "root_id": "root",
            "nodes": {
                "root": {
                    "id": "root",
                    "content": "Acceptance Criteria",
                    "status": "executing",
                    "children_ids": ["ac_1", "ac_2"],
                },
                "ac_1": {
                    "id": "ac_1",
                    "content": "Visible branch",
                    "status": "completed",
                    "children_ids": [],
                },
                "ac_2": {
                    "id": "ac_2",
                    "content": "Collapsed branch",
                    "status": "pending",
                    "children_ids": ["sub_ac_2"],
                },
                "sub_ac_2": {
                    "id": "sub_ac_2",
                    "content": "Depth 2 parent",
                    "status": "pending",
                    "children_ids": ["sub_sub_ac_2"],
                },
                "sub_sub_ac_2": {
                    "id": "sub_sub_ac_2",
                    "content": "Depth 3 boundary",
                    "status": "failed",
                    "children_ids": ["sub_sub_sub_ac_2_a", "sub_sub_sub_ac_2_b"],
                },
                "sub_sub_sub_ac_2_a": {
                    "id": "sub_sub_sub_ac_2_a",
                    "content": "Depth 4 hidden",
                    "status": "pending",
                    "children_ids": ["sub_sub_sub_sub_ac_2"],
                },
                "sub_sub_sub_ac_2_b": {
                    "id": "sub_sub_sub_ac_2_b",
                    "content": "Depth 4 hidden sibling",
                    "status": "pending",
                    "children_ids": [],
                },
                "sub_sub_sub_sub_ac_2": {
                    "id": "sub_sub_sub_sub_ac_2",
                    "content": "Depth 5 hidden",
                    "status": "pending",
                    "children_ids": [],
                },
            },
        }
    }

    rendered = render_ac_tree_markdown(snapshot)

    assert rendered == "\n".join(
        [
            "◇ Acceptance Criteria",
            "├─ ● Visible branch",
            "└─ ○ Collapsed branch",
            "   └─ ○ Depth 2 parent",
            "      └─ ✖ Depth 3 boundary",
            "         └─ ... (+3 sub-tasks)",
        ]
    )
