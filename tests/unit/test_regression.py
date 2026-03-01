"""Unit tests for RegressionDetector."""

from __future__ import annotations

from ouroboros.core.lineage import (
    ACResult,
    EvaluationSummary,
    GenerationPhase,
    GenerationRecord,
    OntologyLineage,
)
from ouroboros.core.seed import OntologyField, OntologySchema
from ouroboros.evolution.regression import RegressionDetector


def _schema() -> OntologySchema:
    return OntologySchema(
        name="Test",
        description="Test",
        fields=(OntologyField(name="x", field_type="str", description="x", required=True),),
    )


def _ac(idx: int, passed: bool, text: str = "") -> ACResult:
    return ACResult(
        ac_index=idx,
        ac_content=text or f"AC {idx + 1}",
        passed=passed,
        verification_method="test",
    )


def _gen(num: int, acs: tuple[ACResult, ...]) -> GenerationRecord:
    passed = sum(1 for a in acs if a.passed)
    total = len(acs)
    return GenerationRecord(
        generation_number=num,
        seed_id=f"s{num}",
        ontology_snapshot=_schema(),
        evaluation_summary=EvaluationSummary(
            final_approved=passed == total,
            highest_stage_passed=2,
            score=passed / total if total else 0,
            ac_results=acs,
        ),
        phase=GenerationPhase.COMPLETED,
    )


def _lineage(*gens: GenerationRecord) -> OntologyLineage:
    return OntologyLineage(lineage_id="test", goal="test", generations=gens)


class TestRegressionDetector:
    def test_no_regression_all_passing(self) -> None:
        lineage = _lineage(
            _gen(1, (_ac(0, True), _ac(1, True))),
            _gen(2, (_ac(0, True), _ac(1, True))),
        )
        report = RegressionDetector().detect(lineage)
        assert not report.has_regressions

    def test_regression_detected(self) -> None:
        """AC 1 passed in gen 1, fails in gen 2 → regression."""
        lineage = _lineage(
            _gen(1, (_ac(0, True), _ac(1, True))),
            _gen(2, (_ac(0, True), _ac(1, False))),
        )
        report = RegressionDetector().detect(lineage)
        assert report.has_regressions
        assert len(report.regressions) == 1
        reg = report.regressions[0]
        assert reg.ac_index == 1
        assert reg.passed_in_generation == 1
        assert reg.failed_in_generation == 2
        assert reg.consecutive_failures == 1

    def test_persistent_failure_not_regression(self) -> None:
        """AC that always failed is not a regression."""
        lineage = _lineage(
            _gen(1, (_ac(0, False),)),
            _gen(2, (_ac(0, False),)),
        )
        report = RegressionDetector().detect(lineage)
        assert not report.has_regressions

    def test_consecutive_failures_counted(self) -> None:
        """Count consecutive failures from latest backwards."""
        lineage = _lineage(
            _gen(1, (_ac(0, True),)),
            _gen(2, (_ac(0, False),)),
            _gen(3, (_ac(0, False),)),
        )
        report = RegressionDetector().detect(lineage)
        assert report.has_regressions
        assert report.regressions[0].consecutive_failures == 2
        assert report.regressions[0].passed_in_generation == 1

    def test_single_generation_no_regression(self) -> None:
        lineage = _lineage(_gen(1, (_ac(0, False),)))
        report = RegressionDetector().detect(lineage)
        assert not report.has_regressions

    def test_multiple_regressions(self) -> None:
        lineage = _lineage(
            _gen(1, (_ac(0, True), _ac(1, True), _ac(2, True))),
            _gen(2, (_ac(0, False), _ac(1, False), _ac(2, True))),
        )
        report = RegressionDetector().detect(lineage)
        assert len(report.regressions) == 2
        assert report.regressed_ac_indices == (0, 1)

    def test_recovered_ac_no_regression(self) -> None:
        """AC that failed then recovered is not a regression."""
        lineage = _lineage(
            _gen(1, (_ac(0, True),)),
            _gen(2, (_ac(0, False),)),
            _gen(3, (_ac(0, True),)),
        )
        report = RegressionDetector().detect(lineage)
        assert not report.has_regressions

    def test_no_eval_summary(self) -> None:
        gen = GenerationRecord(
            generation_number=1,
            seed_id="s1",
            ontology_snapshot=_schema(),
            phase=GenerationPhase.COMPLETED,
        )
        lineage = _lineage(gen, gen)
        report = RegressionDetector().detect(lineage)
        assert not report.has_regressions
