"""Tests for the typed self-contained subagent assignment contract."""

from __future__ import annotations

import pytest

from ouroboros.mcp.tools.assignment import AssignmentMessage


class TestAssignmentRender:
    def test_renders_authority_delimited_block(self) -> None:
        rendered = AssignmentMessage(
            task="Implement the parser.",
            deliverable="A parser that passes the suite.",
        ).render()
        assert rendered.startswith("<assignment>")
        assert rendered.rstrip().endswith("</assignment>")
        assert "## Task" in rendered
        assert "Implement the parser." in rendered
        assert "## Deliverable" in rendered
        assert "A parser that passes the suite." in rendered

    def test_scope_and_verify_render_as_bullets(self) -> None:
        rendered = AssignmentMessage(
            task="t",
            deliverable="d",
            scope=("Session ID: s-1", "Max Iterations: 5"),
            verify=("All ACs pass", "QA green"),
        ).render()
        assert "## Scope" in rendered
        assert "- Session ID: s-1" in rendered
        assert "- Max Iterations: 5" in rendered
        assert "## Verify" in rendered
        assert "- All ACs pass" in rendered
        assert "- QA green" in rendered

    def test_empty_scope_and_verify_sections_are_omitted(self) -> None:
        rendered = AssignmentMessage(task="t", deliverable="d").render()
        assert "## Scope" not in rendered
        assert "## Verify" not in rendered

    def test_blank_bullet_entries_are_dropped(self) -> None:
        rendered = AssignmentMessage(
            task="t",
            deliverable="d",
            scope=("keep", "   ", ""),
        ).render()
        assert "- keep" in rendered
        assert "- \n" not in rendered

    def test_body_appended_verbatim_after_the_contract(self) -> None:
        rendered = AssignmentMessage(
            task="t",
            deliverable="d",
            body="## Seed Specification\n```yaml\ngoal: x\n```",
        ).render()
        assert "</assignment>" in rendered
        body_index = rendered.index("## Seed Specification")
        assert body_index > rendered.index("</assignment>")
        assert "goal: x" in rendered


class TestAssignmentValidation:
    def test_empty_task_rejected(self) -> None:
        with pytest.raises(ValueError, match="task"):
            AssignmentMessage(task="  ", deliverable="d")

    def test_empty_deliverable_rejected(self) -> None:
        with pytest.raises(ValueError, match="deliverable"):
            AssignmentMessage(task="t", deliverable="")
