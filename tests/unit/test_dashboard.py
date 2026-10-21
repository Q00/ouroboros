"""Unit tests for AC Compliance Dashboard."""

from __future__ import annotations

import pytest

from ouroboros.core.acceptance import AcceptanceState
from ouroboros.core.lineage import (
    ACAuthorityState,
    ACResult,
    EvaluationSummary,
    GenerationPhase,
    GenerationRecord,
    OntologyLineage,
)
from ouroboros.core.seed import OntologyField, OntologySchema
from ouroboros.mcp.tools.dashboard import (
    _classify_ac,
    _extract_ac_history,
    _trend_dots,
    format_full,
    format_single_ac,
    format_summary,
)

# -- Helpers --

PASS = ACAuthorityState.PASS
FAIL = ACAuthorityState.FAIL
UNRESOLVED = ACAuthorityState.UNRESOLVED


def _schema() -> OntologySchema:
    return OntologySchema(
        name="Test",
        description="Test schema",
        fields=(OntologyField(name="x", field_type="string", description="x", required=True),),
    )


def _ac_result(idx: int, passed: bool, content: str = "") -> ACResult:
    return ACResult(
        ac_index=idx,
        ac_content=content or f"AC {idx + 1} description",
        passed=passed,
        score=1.0 if passed else 0.0,
        evidence="test evidence",
        verification_method="mechanical",
    )


def _eval_summary(ac_results: tuple[ACResult, ...]) -> EvaluationSummary:
    passed = sum(1 for ac in ac_results if ac.passed)
    total = len(ac_results)
    score = passed / total if total else 0.0
    return EvaluationSummary(
        final_approved=passed == total,
        highest_stage_passed=2,
        score=score,
        ac_results=ac_results,
    )


def _generation(
    gen_num: int,
    ac_results: tuple[ACResult, ...],
) -> GenerationRecord:
    return GenerationRecord(
        generation_number=gen_num,
        seed_id=f"seed_{gen_num}",
        ontology_snapshot=_schema(),
        evaluation_summary=_eval_summary(ac_results),
        phase=GenerationPhase.COMPLETED,
    )


def _lineage_with_gens(*gen_ac_lists: tuple[ACResult, ...]) -> OntologyLineage:
    gens = tuple(_generation(i + 1, acs) for i, acs in enumerate(gen_ac_lists))
    return OntologyLineage(
        lineage_id="test_lin",
        goal="test goal",
        generations=gens,
    )


# -- Tests --


class TestExtractACHistory:
    """Tests for _extract_ac_history."""

    def test_extracts_from_multiple_gens(self) -> None:
        lineage = _lineage_with_gens(
            (_ac_result(0, True), _ac_result(1, False)),
            (_ac_result(0, True), _ac_result(1, True)),
        )
        history = _extract_ac_history(lineage)
        assert len(history) == 2
        assert history[0] == [(1, PASS), (2, PASS)]
        assert history[1] == [(1, FAIL), (2, PASS)]

    def test_empty_lineage(self) -> None:
        lineage = OntologyLineage(lineage_id="empty", goal="test")
        history = _extract_ac_history(lineage)
        assert history == {}

    def test_no_ac_results_skipped(self) -> None:
        gen = GenerationRecord(
            generation_number=1,
            seed_id="s1",
            ontology_snapshot=_schema(),
            evaluation_summary=EvaluationSummary(final_approved=True, highest_stage_passed=2),
            phase=GenerationPhase.COMPLETED,
        )
        lineage = OntologyLineage(lineage_id="no_ac", goal="test", generations=(gen,))
        history = _extract_ac_history(lineage)
        assert history == {}


class TestTrendDots:
    """Tests for _trend_dots."""

    def test_all_pass(self) -> None:
        results = [(1, PASS), (2, PASS), (3, PASS)]
        trend = _trend_dots(results)
        assert "PPP" in trend
        assert "3/3" in trend

    def test_mixed(self) -> None:
        results = [(1, FAIL), (2, PASS), (3, FAIL)]
        trend = _trend_dots(results)
        assert "FPF" in trend
        assert "1/3" in trend

    def test_unresolved_is_marked_apart_from_fail(self) -> None:
        results = [(1, FAIL), (2, UNRESOLVED), (3, PASS)]
        assert _trend_dots(results) == "FUP (1/3)"

    def test_truncates_to_max_dots(self) -> None:
        results = [(i, PASS) for i in range(10)]
        trend = _trend_dots(results, max_dots=5)
        assert trend.count("P") == 5


class TestClassifyAC:
    """Tests for _classify_ac."""

    def test_stable(self) -> None:
        results = [(1, PASS), (2, PASS), (3, PASS)]
        assert _classify_ac(results) == "stable"

    def test_failing(self) -> None:
        results = [(1, FAIL), (2, FAIL), (3, FAIL)]
        assert _classify_ac(results) == "failing"

    def test_flaky(self) -> None:
        results = [(1, PASS), (2, FAIL), (3, PASS)]
        assert _classify_ac(results) == "flaky"

    def test_new(self) -> None:
        assert _classify_ac([]) == "new"

    def test_all_unresolved_is_not_failing(self) -> None:
        results = [(1, UNRESOLVED), (2, UNRESOLVED), (3, UNRESOLVED)]
        assert _classify_ac(results) == "unresolved"

    def test_unresolved_among_fails_is_not_failing(self) -> None:
        results = [(1, FAIL), (2, UNRESOLVED), (3, FAIL)]
        assert _classify_ac(results) == "flaky"

    def test_single_pass_not_stable(self) -> None:
        """Need >= 2 results for stable."""
        results = [(1, PASS)]
        classification = _classify_ac(results)
        assert classification != "stable"


class TestFormatSummary:
    """Tests for format_summary."""

    def test_basic_summary(self) -> None:
        lineage = _lineage_with_gens(
            (_ac_result(0, True, "Create tasks"), _ac_result(1, False, "Delete tasks")),
        )
        output = format_summary(lineage)
        assert "AC Dashboard" in output
        assert "Gen 1" in output
        assert "PASS" in output
        assert "FAIL" in output
        assert "Create tasks" in output
        assert "Delete tasks" in output

    def test_non_authoritative_pass_displays_as_unresolved(self) -> None:
        unknown = _ac_result(0, True, "Unverified task creation").model_copy(
            update={"ac_verdict_state": "not_evaluated"}
        )

        output = format_summary(_lineage_with_gens((unknown,)))

        assert "UNRESOLVED" in output
        assert "U (0/1)" in output
        assert "F (0/1)" not in output

    def test_no_generations(self) -> None:
        lineage = OntologyLineage(lineage_id="empty", goal="test")
        output = format_summary(lineage)
        assert "No generations" in output

    def test_no_ac_results(self) -> None:
        gen = GenerationRecord(
            generation_number=1,
            seed_id="s1",
            ontology_snapshot=_schema(),
            evaluation_summary=EvaluationSummary(final_approved=True, highest_stage_passed=2),
            phase=GenerationPhase.COMPLETED,
        )
        lineage = OntologyLineage(lineage_id="no_ac", goal="test", generations=(gen,))
        output = format_summary(lineage)
        assert "No per-AC data" in output

    def test_stable_acs_collapsed_when_many(self) -> None:
        """When >10 ACs, stable ones should be collapsed."""
        # 12 ACs: 2 failing + 10 stable (across 3 gens)
        acs_gen1 = tuple(
            _ac_result(i, i >= 2)  # 0,1 fail; 2-11 pass
            for i in range(12)
        )
        acs_gen2 = tuple(_ac_result(i, i >= 2) for i in range(12))
        acs_gen3 = tuple(_ac_result(i, i >= 2) for i in range(12))
        lineage = _lineage_with_gens(acs_gen1, acs_gen2, acs_gen3)
        output = format_summary(lineage)
        assert "stable ACs" in output


class TestFormatFull:
    """Tests for format_full."""

    def test_full_matrix(self) -> None:
        lineage = _lineage_with_gens(
            (_ac_result(0, True), _ac_result(1, False)),
            (_ac_result(0, True), _ac_result(1, True)),
        )
        output = format_full(lineage)
        assert "Gen1" in output
        assert "Gen2" in output
        assert "[P]" in output
        assert "[F]" in output

    def test_empty(self) -> None:
        lineage = OntologyLineage(lineage_id="empty", goal="test")
        output = format_full(lineage)
        assert "No generations" in output


class TestFormatSingleAC:
    """Tests for format_single_ac."""

    def test_single_ac_history(self) -> None:
        lineage = _lineage_with_gens(
            (_ac_result(0, False, "Create tasks"),),
            (_ac_result(0, True, "Create tasks"),),
            (_ac_result(0, True, "Create tasks"),),
        )
        output = format_single_ac(lineage, 0)
        assert "AC 1 History" in output
        assert "Create tasks" in output
        assert "FAIL" in output
        assert "PASS" in output
        assert "Gen 1" in output
        assert "Gen 3" in output

    def test_unknown_ac(self) -> None:
        lineage = _lineage_with_gens((_ac_result(0, True),))
        output = format_single_ac(lineage, 5)
        assert "No data" in output


class TestACDashboardHandler:
    """Tests for ACDashboardHandler MCP tool."""

    @pytest.mark.asyncio
    async def test_summary_mode(self) -> None:
        from ouroboros.events.lineage import lineage_created, lineage_generation_completed
        from ouroboros.mcp.tools.definitions import ACDashboardHandler
        from ouroboros.persistence.event_store import EventStore

        store = EventStore("sqlite+aiosqlite:///:memory:")
        await store.initialize()

        await store.append(lineage_created("lin_dash", "test"))
        eval_summary = EvaluationSummary(
            final_approved=True,
            highest_stage_passed=2,
            score=0.9,
            ac_results=(
                _ac_result(0, True, "Create tasks"),
                _ac_result(1, True, "List tasks"),
            ),
        )
        await store.append(
            lineage_generation_completed(
                "lin_dash",
                1,
                "seed_1",
                _schema().model_dump(mode="json"),
                eval_summary.model_dump(mode="json"),
                ["Q1"],
            )
        )

        handler = ACDashboardHandler(event_store=store)
        handler._event_store = store
        handler._initialized = True

        result = await handler.handle({"lineage_id": "lin_dash", "mode": "summary"})
        assert result.is_ok
        text = result.value.text_content
        assert "AC Dashboard" in text
        assert "Create tasks" in text

    @pytest.mark.asyncio
    async def test_missing_lineage(self) -> None:
        from ouroboros.mcp.tools.definitions import ACDashboardHandler
        from ouroboros.persistence.event_store import EventStore

        store = EventStore("sqlite+aiosqlite:///:memory:")
        await store.initialize()

        handler = ACDashboardHandler(event_store=store)
        handler._event_store = store
        handler._initialized = True

        result = await handler.handle({"lineage_id": "nonexistent"})
        assert result.is_err

    @pytest.mark.asyncio
    async def test_ac_mode_requires_index(self) -> None:
        from ouroboros.events.lineage import lineage_created
        from ouroboros.mcp.tools.definitions import ACDashboardHandler
        from ouroboros.persistence.event_store import EventStore

        store = EventStore("sqlite+aiosqlite:///:memory:")
        await store.initialize()
        await store.append(lineage_created("lin_ac_mode", "test"))

        handler = ACDashboardHandler(event_store=store)
        handler._event_store = store
        handler._initialized = True

        result = await handler.handle({"lineage_id": "lin_ac_mode", "mode": "ac"})
        assert result.is_err
        assert "ac_index" in str(result.error)


# -- Acceptance tri-state (approved, rejected, unverified) --


def _unverified_ac(idx: int, content: str = "") -> ACResult:
    """An AC row as the Ralph chain projects it when no executed check ran."""
    return ACResult(
        ac_index=idx,
        ac_content=content or f"AC {idx + 1} description",
        passed=False,
        score=0.0,
        evidence="No executed verification evidence; formal AC verdict not evaluated.",
        verification_method="formal_evaluation",
        ac_verdict_state="not_evaluated",
        final_verdict="fail",
        rendered_verdict="NOT_EVALUATED",
    )


def _summary(state: AcceptanceState, ac_results: tuple[ACResult, ...]) -> EvaluationSummary:
    approval_status = {
        AcceptanceState.APPROVED: "approved",
        AcceptanceState.REJECTED: "rejected",
        AcceptanceState.UNVERIFIED: "not_evaluated",
    }[state]
    return EvaluationSummary(
        final_approved=state is AcceptanceState.APPROVED,
        highest_stage_passed=2,
        score=0.5,
        ac_results=ac_results,
        approval_status=approval_status,
    )


def _lineage_from_summaries(*summaries: EvaluationSummary) -> OntologyLineage:
    gens = tuple(
        GenerationRecord(
            generation_number=i + 1,
            seed_id=f"seed_{i + 1}",
            ontology_snapshot=_schema(),
            evaluation_summary=summary,
            phase=GenerationPhase.COMPLETED,
        )
        for i, summary in enumerate(summaries)
    )
    return OntologyLineage(lineage_id="tri_lin", goal="test goal", generations=gens)


class TestAcceptanceTriState:
    """Each surface keeps approved, rejected, and unverified distinct."""

    def test_summary_header_approved(self) -> None:
        summary = _summary(AcceptanceState.APPROVED, (_ac_result(0, True),))

        output = format_summary(_lineage_from_summaries(summary))

        assert "### Gen 1 | Score: 0.50 | APPROVED" in output
        assert "NOT APPROVED" not in output

    def test_summary_header_rejected(self) -> None:
        summary = _summary(AcceptanceState.REJECTED, (_ac_result(0, False),))

        output = format_summary(_lineage_from_summaries(summary))

        assert "### Gen 1 | Score: 0.50 | REJECTED" in output
        assert "| 1 | FAIL |" in output
        assert "unverified" not in output

    def test_summary_unverified_is_not_rejected_or_failing(self) -> None:
        summary = _summary(
            AcceptanceState.UNVERIFIED,
            (_unverified_ac(0, "Create tasks"), _unverified_ac(1, "Delete tasks")),
        )

        output = format_summary(_lineage_from_summaries(summary))

        assert "### Gen 1 | Score: 0.50 | NOT APPROVED (unverified)" in output
        assert "REJECTED" not in output
        assert "| 1 | UNRESOLVED | Create tasks | U (0/1) |" in output
        assert "| 2 | UNRESOLVED | Delete tasks | U (0/1) |" in output
        assert "FAIL" not in output

    def test_summary_sorts_unresolved_after_failing_and_flaky(self) -> None:
        gen1 = _summary(
            AcceptanceState.REJECTED,
            (_ac_result(0, True), _ac_result(1, False), _unverified_ac(2), _ac_result(3, False)),
        )
        gen2 = _summary(
            AcceptanceState.REJECTED,
            (_ac_result(0, True), _ac_result(1, False), _unverified_ac(2), _ac_result(3, True)),
        )

        output = format_summary(_lineage_from_summaries(gen1, gen2))
        cells = [line.split("|")[1].strip() for line in output.splitlines() if line.startswith("|")]

        # AC 2 failing, AC 4 flaky, AC 3 unresolved, AC 1 stable.
        assert [cell for cell in cells if cell.isdigit()] == ["2", "4", "3", "1"]

    def test_full_matrix_marks_each_state(self) -> None:
        lineage = _lineage_from_summaries(
            _summary(AcceptanceState.REJECTED, (_ac_result(0, False),)),
            _summary(AcceptanceState.UNVERIFIED, (_unverified_ac(0),)),
            _summary(AcceptanceState.APPROVED, (_ac_result(0, True),)),
        )

        output = format_full(lineage)
        row = next(line for line in output.splitlines() if line.startswith("AC 1"))

        assert row.split()[2:5] == ["[F]", "[U]", "[P]"]
        assert "U = unresolved" in output

    def test_full_matrix_all_unresolved_is_not_failing(self) -> None:
        lineage = _lineage_from_summaries(
            _summary(AcceptanceState.UNVERIFIED, (_unverified_ac(0),)),
            _summary(AcceptanceState.UNVERIFIED, (_unverified_ac(0),)),
        )

        output = format_full(lineage)
        row = next(line for line in output.splitlines() if line.startswith("AC 1"))

        assert row.split()[2:] == ["[U]", "[U]", "unresolved"]
        assert "[F]" not in output
        assert "failing" not in output

    def test_single_ac_timeline_keeps_each_state(self) -> None:
        lineage = _lineage_from_summaries(
            _summary(AcceptanceState.REJECTED, (_ac_result(0, False, "Create tasks"),)),
            _summary(AcceptanceState.UNVERIFIED, (_unverified_ac(0, "Create tasks"),)),
            _summary(AcceptanceState.APPROVED, (_ac_result(0, True, "Create tasks"),)),
        )

        output = format_single_ac(lineage, 0)

        assert "| Gen 1 | FAIL |" in output
        assert "| Gen 2 | UNRESOLVED |" in output
        assert "| Gen 3 | PASS |" in output
        assert "**Pass rate**: 1/3" in output

    def test_single_ac_all_unresolved_timeline(self) -> None:
        lineage = _lineage_from_summaries(
            _summary(AcceptanceState.UNVERIFIED, (_unverified_ac(0),)),
            _summary(AcceptanceState.UNVERIFIED, (_unverified_ac(0),)),
        )

        output = format_single_ac(lineage, 0)

        assert "**Classification**: unresolved" in output
        assert "FAIL" not in output

    @pytest.mark.asyncio
    async def test_handler_renders_unverified_generation_from_events(self) -> None:
        """The persisted not_evaluated status survives projection into the dashboard."""
        from ouroboros.events.lineage import lineage_created, lineage_generation_completed
        from ouroboros.mcp.tools.definitions import ACDashboardHandler
        from ouroboros.persistence.event_store import EventStore

        store = EventStore("sqlite+aiosqlite:///:memory:")
        await store.initialize()
        await store.append(lineage_created("lin_unverified", "test"))
        summary = _summary(AcceptanceState.UNVERIFIED, (_unverified_ac(0, "Create tasks"),))
        await store.append(
            lineage_generation_completed(
                "lin_unverified",
                1,
                "seed_1",
                _schema().model_dump(mode="json"),
                summary.model_dump(mode="json"),
                [],
            )
        )

        handler = ACDashboardHandler(event_store=store)
        handler._event_store = store
        handler._initialized = True

        result = await handler.handle({"lineage_id": "lin_unverified", "mode": "summary"})

        assert result.is_ok
        text = result.value.text_content
        assert "NOT APPROVED (unverified)" in text
        assert "REJECTED" not in text
        assert "| 1 | UNRESOLVED | Create tasks | U (0/1) |" in text
