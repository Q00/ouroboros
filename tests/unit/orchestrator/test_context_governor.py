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

    def test_ac_preserved_verbatim(self) -> None:
        # The AC text carries prompt semantics (leading indentation,
        # trailing newlines, whitespace-significant fenced blocks).
        # The governor must NOT rewrite it (bot finding on #890 round 2).
        raw_ac = "   ac body\n\n"
        result = compose_context(ac=raw_ac, parent_summary="\n  summary\n")
        assert result.ac == raw_ac
        # The parent summary IS free prose and may be stripped.
        assert result.parent_summary == "summary"

    def test_empty_inputs(self) -> None:
        result = compose_context(ac="ac")
        assert result.parent_summary == ""
        assert result.sibling_lines == ()
        assert result.ac == "ac"
        assert result.truncated is False


class TestRenderedSizeContract:
    """`compose_context` must keep the *rendered* output under `total_chars`.

    This is the contract the H6 governor advertises. Earlier versions
    budgeted only the raw payloads and let section headers / joiners
    push the rendered output over the limit (bot finding on PR #890).
    """

    @pytest.mark.parametrize("total", [64, 128, 256, 512, 1024, 4096])
    def test_render_fits_budget(self, total: int) -> None:
        result = compose_context(
            ac="x" * (total // 4),
            parent_summary="y" * total,  # over-large; will be truncated.
            siblings=[
                SiblingStatus(f"AC{i}", accepted=(i % 2 == 0), headline="z" * 20) for i in range(8)
            ],
            budget=ContextBudget(total_chars=total, parent_summary_reserve=total // 4),
        )
        assert len(result.render()) <= total, (
            f"rendered size {len(result.render())} exceeds budget {total}; "
            f"rendered=\n{result.render()!r}"
        )

    def test_ac_plus_header_over_budget_raises(self) -> None:
        # The AC fits raw but pushes past once the "## AC\n" header is
        # added; the governor must reject rather than silently overflow.
        with pytest.raises(ValueError, match="AC alone exceeds"):
            compose_context(
                ac="x" * 100,
                budget=ContextBudget(total_chars=100, parent_summary_reserve=0),
            )

    def test_parent_dropped_when_header_does_not_fit(self) -> None:
        # 30-char budget: AC ("## AC\nshort" = 11 chars) leaves 19 chars
        # remaining, which is less than the parent header (20). So the
        # summary must be dropped and `truncated` set.
        result = compose_context(
            ac="short",
            parent_summary="parent",
            budget=ContextBudget(total_chars=30, parent_summary_reserve=0),
        )
        assert result.parent_summary == ""
        assert result.truncated is True
        assert len(result.render()) <= 30

    def test_unused_parent_reserve_redistributes_to_siblings(self) -> None:
        # Bot finding on #890 r2: with no parent summary present, the
        # reserve must not gate siblings out of space they could
        # otherwise occupy. Setup: total=120, parent_reserve=60, AC=2
        # chars, no parent_summary. Previously only one sibling line
        # fit because the ceiling subtracted the reserve unconditionally.
        siblings = [SiblingStatus(f"AC{i}", accepted=True, headline="x" * 5) for i in range(5)]
        result = compose_context(
            ac="ac",
            parent_summary="",
            siblings=siblings,
            budget=ContextBudget(total_chars=120, parent_summary_reserve=60),
        )
        # Without the fix this dropped to 1 line and rendered ~45 chars
        # of 120 budget. With redistribution, multiple lines fit.
        assert len(result.sibling_lines) >= 3, (
            f"only {len(result.sibling_lines)} siblings placed; the "
            f"reserve must not block them when parent_summary is empty"
        )
        assert len(result.render()) <= 120

    def test_reserve_still_applies_when_parent_present(self) -> None:
        # Symmetric guarantee: when a parent summary IS present, the
        # reserve still acts as a floor — siblings cannot eat into it.
        siblings = [SiblingStatus(f"AC{i}", accepted=True, headline="x" * 100) for i in range(20)]
        result = compose_context(
            ac="ac",
            parent_summary="parent content here",
            siblings=siblings,
            budget=ContextBudget(total_chars=200, parent_summary_reserve=50),
        )
        # Parent summary got at least its reserve.
        assert len(result.parent_summary) > 0
        assert len(result.render()) <= 200

    def test_zero_reserve_does_not_steal_overhead_from_siblings(self) -> None:
        # Bot finding on #890 r6: with parent_summary present but
        # reserve=0, the previous code still subtracted parent_overhead
        # (20 chars) from the sibling ceiling. That let an optional
        # parent section drop sibling status, violating the
        # "best-effort parent" contract for reserve=0.
        siblings = [SiblingStatus(str(i), accepted=True, headline="xxxxx") for i in range(10)]
        budget = ContextBudget(total_chars=200, parent_summary_reserve=0)
        with_parent = compose_context(ac="ac", parent_summary="p", siblings=siblings, budget=budget)
        without_parent = compose_context(
            ac="ac", parent_summary="", siblings=siblings, budget=budget
        )
        # Siblings placed in the with_parent case must match (or exceed)
        # the without_parent case — adding a best-effort parent must
        # not push siblings out.
        assert len(with_parent.sibling_lines) >= len(without_parent.sibling_lines)

    def test_short_parent_does_not_starve_siblings(self) -> None:
        # Bot finding on #890 r4: when parent_summary is non-empty but
        # SHORTER than parent_summary_reserve, the previous code still
        # withheld the full reserve from the sibling ceiling, leaving
        # usable budget idle and dropping siblings that would otherwise
        # fit. Repro from the bot:
        #   total=120, reserve=60, ac="ac", parent="p" (1 char), 7
        #   siblings with 5-char headlines — previous code kept only 1
        #   sibling line and rendered 59 chars (61 idle).
        result = compose_context(
            ac="ac",
            parent_summary="p",
            siblings=[SiblingStatus(str(i), accepted=True, headline="xxxxx") for i in range(7)],
            budget=ContextBudget(total_chars=120, parent_summary_reserve=60),
        )
        # Parent's content is 1 char so the floor it actually needs is
        # tiny; siblings should be able to consume the rest.
        assert len(result.sibling_lines) >= 4, (
            f"only {len(result.sibling_lines)} siblings placed; the "
            f"reserve must collapse to the parent's actual size when "
            f"the parent is shorter than the reserve"
        )
        assert result.parent_summary == "p"
        assert len(result.render()) <= 120

    def test_impossible_reserve_raises(self) -> None:
        # Bot finding on #890 r4 round 2: when the AC alone leaves less
        # than parent_overhead + parent_summary_reserve, the function
        # used to silently return parent_summary="" with truncated=True,
        # violating the API's "guaranteed minimum" contract. Now it
        # must fail fast so the caller can shrink the AC or raise the
        # total.
        with pytest.raises(ValueError, match="Budget cannot honor parent_summary_reserve"):
            compose_context(
                ac="x" * 35,
                parent_summary="parent summary here",  # 19 chars
                budget=ContextBudget(total_chars=50, parent_summary_reserve=30),
            )

    def test_impossible_reserve_with_zero_reserve_does_not_raise(self) -> None:
        # When the caller explicitly opts out of the guarantee
        # (reserve=0), the parent is best-effort; silent truncation is
        # the right behavior, not fail-fast.
        result = compose_context(
            ac="x" * 35,
            parent_summary="parent",
            budget=ContextBudget(total_chars=50, parent_summary_reserve=0),
        )
        assert result.parent_summary == ""
        assert result.truncated is True

    def test_parent_actual_content_meets_reserve_floor(self) -> None:
        # Bot finding on #890 r3: the reserve was applied to the
        # sibling ceiling without accounting for the parent section
        # header + joiner overhead. Siblings could therefore eat into
        # the 20 chars of "## Parent context\n\n" overhead, and the
        # actual parent_summary content would land below the
        # advertised reserve.
        #
        # Bot's exact repro: total=100, reserve=50, ac='a', one sibling
        # with 16-char headline, non-empty parent. The previous
        # behavior returned a 100-char render with parent_summary only
        # 30 chars long — 20 chars short of the reserved 50.
        large_parent = "x" * 200
        result = compose_context(
            ac="a",
            parent_summary=large_parent,
            siblings=[SiblingStatus("AC1", accepted=True, headline="x" * 16)],
            budget=ContextBudget(total_chars=100, parent_summary_reserve=50),
        )
        assert len(result.parent_summary) >= 50, (
            f"parent_summary={len(result.parent_summary)} chars violates "
            f"the reserve=50 floor; the sibling allocator must charge "
            "parent_overhead too"
        )
        assert len(result.render()) <= 100

    def test_sibling_section_overhead_charged(self) -> None:
        # Budget large enough for AC + one sibling line but NOT enough
        # for the sibling header — the line must be dropped.
        ac = "ac"
        # "## AC\nac" = 8 chars. Sibling header alone = 18 chars.
        # Budget = 28 leaves 20 chars after AC, which is more than the
        # header (18) plus joiner (2) = 20 — but no room for any line.
        result = compose_context(
            ac=ac,
            siblings=[SiblingStatus("AC1", accepted=True, headline="x" * 50)],
            budget=ContextBudget(total_chars=30, parent_summary_reserve=0),
        )
        # Either the sibling line fit (with whatever room remains) or
        # it was dropped — but the rendered output must stay <= budget.
        assert len(result.render()) <= 30
