"""Tests for ouroboros.orchestrator.context_governor (RFC v2 #830, PR 8)."""

from __future__ import annotations

import pytest

from ouroboros.orchestrator.context_governor import (
    DEFAULT_TOTAL_CHARS,
    ComposedContext,
    ContextBudget,
    SiblingStatus,
    compose_context,
)


class TestBudgetInvariants:
    def test_non_positive_total_rejected(self) -> None:
        with pytest.raises(ValueError, match="total_chars must be positive"):
            ContextBudget(total_chars=0)

    def test_negative_reserve_rejected(self) -> None:
        with pytest.raises(ValueError, match="parent_summary_reserve must be >= 0"):
            ContextBudget(total_chars=1000, parent_summary_reserve=-1)

    def test_reserve_exceeds_total_rejected(self) -> None:
        with pytest.raises(ValueError, match="cannot exceed total_chars"):
            ContextBudget(total_chars=100, parent_summary_reserve=200)

    def test_defaults_are_safe(self) -> None:
        budget = ContextBudget()
        assert budget.total_chars == DEFAULT_TOTAL_CHARS
        assert budget.parent_summary_reserve <= budget.total_chars


class TestSiblingStatus:
    def test_accepted_marker(self) -> None:
        line = SiblingStatus("AC1", accepted=True, headline="added cache").to_line()
        assert "✓" in line
        assert "AC1" in line
        assert "added cache" in line

    def test_failed_marker(self) -> None:
        line = SiblingStatus("AC2", accepted=False).to_line()
        assert "✗" in line
        assert "AC2" in line


class TestComposedContext:
    def test_render_sections(self) -> None:
        ctx = ComposedContext(
            parent_summary="parent",
            sibling_lines=("✓ AC1: done", "✗ AC2"),
            ac="this AC",
            truncated=False,
        )
        rendered = ctx.render()
        assert "## Parent context\nparent" in rendered
        assert "## Sibling status" in rendered
        assert "✓ AC1: done" in rendered
        assert rendered.endswith("## AC\nthis AC")

    def test_render_omits_empty_sections(self) -> None:
        ctx = ComposedContext(parent_summary="", sibling_lines=(), ac="bare", truncated=False)
        rendered = ctx.render()
        assert "## Parent context" not in rendered
        assert "## Sibling status" not in rendered
        assert "## AC\nbare" in rendered


class TestComposeContext:
    def test_under_budget_keeps_everything(self) -> None:
        result = compose_context(
            ac="do the thing",
            parent_summary="we are building X",
            siblings=[SiblingStatus("AC1", accepted=True)],
            budget=ContextBudget(total_chars=10_000, parent_summary_reserve=500),
        )
        assert result.parent_summary == "we are building X"
        assert result.sibling_lines == ("✓ AC1",)
        assert result.ac == "do the thing"
        assert result.truncated is False

    def test_parent_summary_truncated_when_tight(self) -> None:
        big_summary = "x" * 5000
        result = compose_context(
            ac="ac",
            parent_summary=big_summary,
            budget=ContextBudget(total_chars=300, parent_summary_reserve=200),
        )
        assert result.truncated is True
        assert len(result.parent_summary) <= 300
        assert "truncated" in result.parent_summary

    def test_sibling_lines_dropped_under_pressure(self) -> None:
        siblings = [SiblingStatus(f"AC{i}", accepted=True, headline="x" * 80) for i in range(20)]
        result = compose_context(
            ac="ac",
            parent_summary="",
            siblings=siblings,
            budget=ContextBudget(total_chars=500, parent_summary_reserve=200),
        )
        # Some siblings made it; the rest were dropped silently.
        assert 0 < len(result.sibling_lines) < 20
        # Dropping siblings does NOT set the truncation flag — those
        # lines were status-only and not load-bearing.
        assert result.truncated is False

    def test_ac_over_budget_raises(self) -> None:
        with pytest.raises(ValueError, match="AC alone exceeds context budget"):
            compose_context(
                ac="x" * 5000,
                budget=ContextBudget(total_chars=1000, parent_summary_reserve=100),
            )

    def test_render_round_trip(self) -> None:
        result = compose_context(
            ac="ac body",
            parent_summary="summary",
            siblings=[SiblingStatus("AC1", accepted=True)],
        )
        rendered = result.render()
        assert "ac body" in rendered
        assert "summary" in rendered
        assert "AC1" in rendered

    def test_strips_whitespace_from_ac_and_summary(self) -> None:
        result = compose_context(
            ac="   ac body\n\n",
            parent_summary="\n  summary\n",
        )
        assert result.ac == "ac body"
        assert result.parent_summary == "summary"

    def test_empty_inputs(self) -> None:
        result = compose_context(ac="ac")
        assert result.parent_summary == ""
        assert result.sibling_lines == ()
        assert result.ac == "ac"
        assert result.truncated is False
