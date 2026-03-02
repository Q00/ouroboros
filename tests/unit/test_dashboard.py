"""Unit tests for AC Compliance Dashboard."""

from __future__ import annotations

import pytest

from ouroboros.core.lineage import (
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
        assert history[0] == [(1, True), (2, True)]
        assert history[1] == [(1, False), (2, True)]

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
        results = [(1, True), (2, True), (3, True)]
        trend = _trend_dots(results)
        assert "PPP" in trend
        assert "3/3" in trend

    def test_mixed(self) -> None:
        results = [(1, False), (2, True), (3, False)]
        trend = _trend_dots(results)
        assert "FPF" in trend
        assert "1/3" in trend

    def test_truncates_to_max_dots(self) -> None:
        results = [(i, True) for i in range(10)]
        trend = _trend_dots(results, max_dots=5)
        assert trend.count("P") == 5


class TestClassifyAC:
    """Tests for _classify_ac."""

    def test_stable(self) -> None:
        results = [(1, True), (2, True), (3, True)]
        assert _classify_ac(results) == "stable"

    def test_failing(self) -> None:
        results = [(1, False), (2, False), (3, False)]
        assert _classify_ac(results) == "failing"

    def test_flaky(self) -> None:
        results = [(1, True), (2, False), (3, True)]
        assert _classify_ac(results) == "flaky"

    def test_new(self) -> None:
        assert _classify_ac([]) == "new"

    def test_single_pass_not_stable(self) -> None:
        """Need >= 2 results for stable."""
        results = [(1, True)]
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
