"""Regression tests for AC tree HUD status icon rendering."""

from __future__ import annotations

from ouroboros.mcp.tools.ac_tree_hud_handler import render_ac_tree_hud_markdown
from ouroboros.mcp.tools.ac_tree_hud_render import render_ac_tree_markdown


def test_render_ac_tree_markdown_maps_done_executing_pending_and_failed_icons() -> None:
    """The pure tree renderer should use the compact glyph for each AC status."""
    snapshot = {
        "tree": {
            "root_id": "root",
            "nodes": {
                "root": {
                    "id": "root",
                    "content": "Acceptance Criteria",
                    "status": "executing",
                    "children_ids": ["ac_1", "ac_2", "ac_3", "ac_4"],
                },
                "ac_1": {
                    "id": "ac_1",
                    "content": "Done AC",
                    "status": "done",
                    "children_ids": [],
                },
                "ac_2": {
                    "id": "ac_2",
                    "content": "Executing AC",
                    "status": "executing",
                    "children_ids": [],
                },
                "ac_3": {
                    "id": "ac_3",
                    "content": "Pending AC",
                    "status": "pending",
                    "children_ids": [],
                },
                "ac_4": {
                    "id": "ac_4",
                    "content": "Failed AC",
                    "status": "failed",
                    "children_ids": [],
                },
            },
        }
    }

    rendered = render_ac_tree_markdown(snapshot)

    assert "├─ ● Done AC" in rendered
    assert "├─ ◐ Executing AC" in rendered
    assert "├─ ○ Pending AC" in rendered
    assert "└─ ✖ Failed AC" in rendered


def test_render_ac_tree_hud_markdown_maps_done_executing_pending_and_failed_icons() -> None:
    """The HUD markdown wrapper should preserve the same status glyphs."""
    markdown = render_ac_tree_hud_markdown(
        session_id="sess_status_icons",
        execution_id="exec_status_icons",
        session_status="running",
        progress_data={
            "completed_count": 1,
            "total_count": 4,
            "acceptance_criteria": [
                {"index": 1, "content": "Done AC", "status": "done"},
                {"index": 2, "content": "Executing AC", "status": "executing"},
                {"index": 3, "content": "Pending AC", "status": "pending"},
                {"index": 4, "content": "Failed AC", "status": "failed"},
            ],
        },
    )

    assert "├─ ● AC 1: Done AC" in markdown
    assert "├─ ◐ AC 2: Executing AC" in markdown
    assert "├─ ○ AC 3: Pending AC" in markdown
    assert "└─ ✖ AC 4: Failed AC" in markdown


def test_render_ac_tree_markdown_shows_inline_tool_activity_only_for_executing_rows() -> None:
    """Executing rows should render current tool activity inline without changing other rows."""
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
                    "content": "Completed AC",
                    "status": "completed",
                    "current_tool_activity": {
                        "tool_name": "Read",
                        "tool_input": {"file_path": "src/completed.py"},
                    },
                    "children_ids": [],
                },
                "ac_2": {
                    "id": "ac_2",
                    "content": "Executing AC",
                    "status": "executing",
                    "current_tool_activity": {
                        "tool_name": "Edit",
                        "tool_input": {"file_path": "src/executing.py"},
                    },
                    "children_ids": [],
                },
            },
        }
    }

    rendered = render_ac_tree_markdown(snapshot)

    assert "├─ ● Completed AC" in rendered
    assert "[Read src/completed.py]" not in rendered
    assert "└─ ◐ Executing AC [Edit src/executing.py]" in rendered


def test_render_ac_tree_markdown_uses_compact_working_fallback_for_missing_activity() -> None:
    """Executing rows with fallback activity states should still show compact inline text."""
    snapshot = {
        "tree": {
            "root_id": "root",
            "nodes": {
                "root": {
                    "id": "root",
                    "content": "Acceptance Criteria",
                    "status": "executing",
                    "children_ids": ["ac_1", "ac_2", "ac_3"],
                },
                "ac_1": {
                    "id": "ac_1",
                    "content": "Missing tool payload",
                    "status": "executing",
                    "tool_activity_state": "missing",
                    "children_ids": [],
                },
                "ac_2": {
                    "id": "ac_2",
                    "content": "Unavailable tool payload",
                    "status": "executing",
                    "tool_activity": {"state": "unavailable"},
                    "children_ids": [],
                },
                "ac_3": {
                    "id": "ac_3",
                    "content": "Stale tool payload",
                    "status": "executing",
                    "tool_activity": {"state": "stale"},
                    "children_ids": [],
                },
            },
        }
    }

    rendered = render_ac_tree_markdown(snapshot)

    assert "├─ ◐ Missing tool payload [working]" in rendered
    assert "├─ ◐ Unavailable tool payload [working]" in rendered
    assert "└─ ◐ Stale tool payload" in rendered
    assert "└─ ◐ Stale tool payload [working]" not in rendered
