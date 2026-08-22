"""Regression tests for evolve's semantic fallback."""

from typing import Any

import pytest
from structlog.testing import capture_logs

from ouroboros.core.seed import (
    AcceptanceCriterionSpec,
    OntologySchema,
    Seed,
    SeedMetadata,
)
from ouroboros.core.types import Result
from ouroboros.evaluation.models import EvaluationResult, MechanicalResult, SemanticResult
from ouroboros.mcp.server.evolve_evaluation import (
    evaluate_seed_criteria,
    warn_if_seed_has_no_success_contract,
)


class _Pipeline:
    def __init__(self, verdicts: tuple[bool, ...]) -> None:
        self.verdicts = verdicts
        self.contexts: list[Any] = []

    async def evaluate(self, context: Any, *, stage1_result: Any = None) -> Any:
        del stage1_result
        self.contexts.append(context)
        index = int(context.execution_id.rsplit("_", 1)[-1]) - 1
        passed = self.verdicts[index]
        semantic = SemanticResult(
            score=0.9 if passed else 0.4,
            ac_compliance=passed,
            goal_alignment=0.9,
            drift_score=0.1 if passed else 0.6,
            uncertainty=0.1,
            reasoning=f"criterion {index + 1} {'passed' if passed else 'failed'}",
            reward_hacking_risk=0.0,
            evidence=(f"evidence-{index + 1}",),
        )
        return Result.ok(
            EvaluationResult(
                execution_id=context.execution_id,
                stage2_result=semantic,
                final_approved=passed,
            )
        )


class _NoSemanticPipeline:
    def __init__(
        self,
        *,
        final_approved: bool,
        stage1_result: MechanicalResult | None = None,
    ) -> None:
        self.final_approved = final_approved
        self.stage1_result = stage1_result
        self.contexts: list[Any] = []
        self.injected_stage1: list[MechanicalResult | None] = []

    async def evaluate(self, context: Any, *, stage1_result: Any = None) -> Any:
        self.contexts.append(context)
        self.injected_stage1.append(stage1_result)
        effective_stage1 = self.stage1_result if stage1_result is None else stage1_result
        return Result.ok(
            EvaluationResult(
                execution_id=context.execution_id,
                stage1_result=effective_stage1,
                final_approved=self.final_approved,
            )
        )


def _seed(*criteria: AcceptanceCriterionSpec) -> Seed:
    return Seed(
        metadata=SeedMetadata(seed_id="seed_regression"),
        acceptance_criteria=criteria,
        goal="Ship the requested behavior",
        constraints=("Preserve compatibility",),
        ontology_schema=OntologySchema(
            name="Regression",
            description="Regression-test ontology",
        ),
    )


@pytest.mark.asyncio
async def test_fallback_evaluates_each_acceptance_criterion_independently() -> None:
    seed = _seed(
        AcceptanceCriterionSpec(description="first behavior"),
        AcceptanceCriterionSpec(description="second behavior"),
        AcceptanceCriterionSpec(description="third behavior"),
    )
    pipeline = _Pipeline((True, False, True))

    summary = await evaluate_seed_criteria(
        seed=seed,
        artifact="worker report without legacy task markers",
        artifact_bundle=None,
        pipeline=pipeline,
    )

    assert [context.current_ac for context in pipeline.contexts] == [
        "first behavior",
        "second behavior",
        "third behavior",
    ]
    assert [context.current_ac_spec for context in pipeline.contexts] == list(
        seed.acceptance_criteria
    )
    assert [result.passed for result in summary.ac_results] == [True, False, True]
    assert summary.final_approved is False
    assert "1/3 acceptance criteria failed" in (summary.failure_reason or "")
    assert "AC 2 (second behavior)" in (summary.failure_reason or "")
    assert "first behavior" not in (summary.failure_reason or "")


@pytest.mark.asyncio
async def test_fallback_approves_only_when_every_criterion_passes() -> None:
    seed = _seed(
        AcceptanceCriterionSpec(description="first behavior"),
        AcceptanceCriterionSpec(description="second behavior"),
    )

    summary = await evaluate_seed_criteria(
        seed=seed,
        artifact="worker report without legacy task markers",
        artifact_bundle=None,
        pipeline=_Pipeline((True, True)),
    )

    assert summary.final_approved is True
    assert summary.approval_status == "approved"
    assert summary.failure_reason is None
    assert all(result.verdict_is_authoritative for result in summary.ac_results)


def test_prose_only_acceptance_contract_emits_structural_warning() -> None:
    seed = _seed(
        AcceptanceCriterionSpec(description="first behavior"),
        AcceptanceCriterionSpec(description="second behavior"),
    )

    with capture_logs() as logs:
        warn_if_seed_has_no_success_contract(seed)

    assert logs == [
        {
            "event": "evolution.acceptance_contract.unstructured",
            "log_level": "warning",
            "seed_id": "seed_regression",
            "acceptance_criteria": 2,
            "consequence": (
                "no AC declares verify_command, expected_artifacts, or output_assertion; "
                "evolve has no fixed structured verification anchor"
            ),
        }
    ]


def test_any_structured_contract_suppresses_structural_warning() -> None:
    seed = _seed(
        AcceptanceCriterionSpec(description="first behavior"),
        AcceptanceCriterionSpec(description="second behavior", verify_command="pytest -q"),
    )

    with capture_logs() as logs:
        warn_if_seed_has_no_success_contract(seed)

    assert logs == []


@pytest.mark.asyncio
async def test_stage1_failure_never_becomes_authoritative_semantic_verdict() -> None:
    seed = _seed(
        AcceptanceCriterionSpec(description="first behavior"),
        AcceptanceCriterionSpec(description="second behavior"),
    )
    stage1_failure = MechanicalResult(passed=False, checks=())
    pipeline = _NoSemanticPipeline(final_approved=False, stage1_result=stage1_failure)

    summary = await evaluate_seed_criteria(
        seed=seed,
        artifact="worker report without legacy task markers",
        artifact_bundle=None,
        pipeline=pipeline,
    )

    assert pipeline.injected_stage1 == [None, stage1_failure]
    assert summary.final_approved is False
    assert [result.ac_verdict_state for result in summary.ac_results] == [
        "not_evaluated",
        "not_evaluated",
    ]
    assert [result.rendered_verdict for result in summary.ac_results] == [
        "NOT_EVALUATED",
        "NOT_EVALUATED",
    ]
    assert not any(result.verdict_is_authoritative for result in summary.ac_results)
    assert "AC 1 (first behavior): semantic evaluation did not run" in (
        summary.failure_reason or ""
    )


@pytest.mark.asyncio
async def test_no_stage2_result_cannot_mint_authoritative_pass() -> None:
    seed = _seed(AcceptanceCriterionSpec(description="first behavior"))

    summary = await evaluate_seed_criteria(
        seed=seed,
        artifact="worker report without legacy task markers",
        artifact_bundle=None,
        pipeline=_NoSemanticPipeline(final_approved=True),
    )

    assert summary.final_approved is False
    assert summary.approval_status == "rejected"
    assert summary.ac_results[0].passed is False
    assert summary.ac_results[0].ac_verdict_state == "not_evaluated"
    assert summary.ac_results[0].rendered_verdict == "NOT_EVALUATED"
    assert summary.ac_results[0].verdict_is_authoritative is False


@pytest.mark.asyncio
async def test_zero_ac_seed_fails_closed_without_synthetic_coverage() -> None:
    seed = _seed()
    pipeline = _Pipeline(())

    with capture_logs() as logs:
        warn_if_seed_has_no_success_contract(seed)
    summary = await evaluate_seed_criteria(
        seed=seed,
        artifact="worker report without legacy task markers",
        artifact_bundle=None,
        pipeline=pipeline,
    )

    assert pipeline.contexts == []
    assert summary.final_approved is False
    assert summary.ac_results == ()
    assert summary.approval_status == "rejected"
    assert summary.failure_reason == (
        "Seed has no acceptance criteria; semantic evaluation cannot establish coverage"
    )
    assert logs[0]["event"] == "evolution.acceptance_contract.unstructured"
    assert logs[0]["acceptance_criteria"] == 0
    assert logs[0]["consequence"] == (
        "the Seed declares no acceptance criteria; evolve cannot establish coverage"
    )
