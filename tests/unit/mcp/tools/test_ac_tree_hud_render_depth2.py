"""Depth-2 rendering tests for the AC tree HUD markdown output."""

from __future__ import annotations

from ouroboros.mcp.tools.ac_tree_hud_handler import render_ac_tree_hud_markdown


def test_renders_depth_2_tree_with_nested_prefixes() -> None:
    """Depth-2 trees should render child branches under the active AC."""
    markdown = render_ac_tree_hud_markdown(
        session_id="sess_depth2",
        execution_id="exec_depth2",
        session_status="running",
        progress_data={
            "current_phase": "deliver",
            "completed_count": 1,
            "total_count": 3,
            "activity": "executing",
            "activity_detail": "Implement renderer",
            "acceptance_criteria": [
                {"index": 1, "content": "Parent criterion", "status": "executing"},
                {
                    "id": "sub_ac_1_1",
                    "parent_id": "ac_1",
                    "depth": 2,
                    "content": "First child",
                    "status": "completed",
                },
                {
                    "id": "sub_ac_1_2",
                    "parent_id": "ac_1",
                    "depth": 2,
                    "content": "Second child",
                    "status": "executing",
                },
                {"index": 2, "content": "Sibling criterion", "status": "pending"},
            ],
            "last_update": {
                "tool_name": "Edit",
                "tool_input": {"file_path": "src/x.py"},
            },
        },
    )

    assert "◇ Acceptance Criteria" in markdown
    assert "├─ ◐ AC 1: Parent criterion" in markdown
    assert "│  ├─ ● First child" in markdown
    assert "│  └─ ◐ Second child" in markdown
    assert "└─ ○ AC 2: Sibling criterion" in markdown
