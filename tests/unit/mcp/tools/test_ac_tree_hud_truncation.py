"""Large-tree truncation tests for the AC tree HUD markdown output."""

from __future__ import annotations

from ouroboros.mcp.tools.ac_tree_hud_handler import render_ac_tree_hud_markdown


def test_renders_executing_window_with_first_last_and_ellipses_when_tree_is_large() -> None:
    """Large trees should focus the executing AC, its neighbors, and the boundaries."""
    acceptance_criteria = [
        {
            "index": index,
            "content": f"Criterion {index}",
            "status": "in_progress" if index == 10 else "pending",
        }
        for index in range(1, 16)
    ]

    markdown = render_ac_tree_hud_markdown(
        session_id="sess_large_tree",
        execution_id="exec_large_tree",
        session_status="running",
        progress_data={
            "current_phase": "deliver",
            "completed_count": 0,
            "total_count": 15,
            "current_ac_index": 10,
            "acceptance_criteria": acceptance_criteria,
        },
    )

    assert markdown == "\n".join(
        [
            "Session: sess_large_tree",
            "Execution: exec_large_tree",
            "Status: running",
            "Phase: deliver",
            "Progress: 0/15 AC complete",
            "",
            "◇ Acceptance Criteria",
            "├─ ○ AC 1: Criterion 1",
            "├─ ... (+6 tasks)",
            "├─ ○ AC 8: Criterion 8",
            "├─ ○ AC 9: Criterion 9",
            "├─ ◐ AC 10: Criterion 10  [working]",
            "├─ ○ AC 11: Criterion 11",
            "├─ ○ AC 12: Criterion 12",
            "├─ ... (+2 tasks)",
            "└─ ○ AC 15: Criterion 15",
        ]
    )
