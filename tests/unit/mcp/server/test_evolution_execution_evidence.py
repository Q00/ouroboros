"""Execution observations must reach executed evaluation, not source regex checks."""

from __future__ import annotations

from functools import partial
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from ouroboros.core.lineage import EvaluationSummary
from ouroboros.core.types import Result
from ouroboros.evaluation.mechanical import MechanicalConfig
from ouroboros.evaluation.models import SemanticResult
from ouroboros.mcp.server import evolution_pipeline_evaluation as pipeline_module
from ouroboros.mcp.server.evolution_check_package import GenerationCheckPackages
from ouroboros.mcp.server.evolution_pipeline_evaluation import evaluate_criteria_with_pipeline
from ouroboros.mcp.server.spec_verification_adapter import evaluation_summary_from_spec_verification
from ouroboros.verification.extractor import AssertionExtractor
from ouroboros.verification.models import SpecAssertion, VerificationOutcome
from ouroboros.verification.verifier import SpecVerifier

CRITERION = "The completed run's terminal record reports iterations=2."


def _extract(scope: object = "execution") -> tuple[SpecAssertion, ...] | None:
    extractor = AssertionExtractor(llm_adapter=AsyncMock(), model="mock")
    return extractor._parse_response(
        json.dumps(
            [
                {
                    "ac_index": 0,
                    "tier": "t1_constant",
                    "pattern": r"iterations\s*=\s*",
                    "expected_value": "2",
                    "file_hint": "*.py",
                    "evidence_scope": scope,
                }
            ]
        ),
        (CRITERION,),
    )


@pytest.mark.parametrize("scope", ["execution", "source"])
@pytest.mark.parametrize("check_passes", [True, False])
async def test_execution_observation_reaches_real_checks_but_source_mismatch_blocks(
    tmp_path: Path, scope: str, check_passes: bool
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    # The execution's record is deliberately outside the source checkout.
    record = tmp_path / "terminal.json"
    record.write_text('{"iterations": 2}')
    (project / "app.py").write_text("iterations = 99\n")
    seed = SimpleNamespace(
        metadata=SimpleNamespace(seed_id="seed-runtime-record"),
        goal="Verify the recorded execution",
        constraints=(),
        acceptance_criteria=(CRITERION,),
    )
    assertions = _extract(scope)
    assert assertions is not None
    verification = SpecVerifier(str(project), strict=True).verify_all(assertions, {0: True})
    summary = evaluation_summary_from_spec_verification(
        EvaluationSummary(
            final_approved=False,
            highest_stage_passed=1,
            execution_completion_status="completed",
        ),
        verification,
        seed,
    )
    assert summary is not None
    marker = tmp_path / "check-ran"
    command = (
        sys.executable,
        "-c",
        "import json,pathlib,sys; "
        f"pathlib.Path({str(marker)!r}).write_text('executed'); "
        f"assert json.loads(pathlib.Path({str(record)!r}).read_text())['iterations'] == 2; "
        f"sys.exit({0 if check_passes else 1})",
    )
    mechanical = AsyncMock(return_value=MechanicalConfig(test_command=command, working_dir=project))
    semantic = AsyncMock(
        return_value=Result.ok(
            (
                SemanticResult(
                    score=0.95,
                    ac_compliance=True,
                    goal_alignment=0.95,
                    drift_score=0.0,
                    uncertainty=0.0,
                    reasoning="Executed record check supplies the evidence.",
                ),
                [],
            )
        )
    )

    async def evaluate(seed, indices, **kwargs):
        return await evaluate_criteria_with_pipeline(
            seed,
            indices,
            **kwargs,
            llm_adapter=AsyncMock(),
            semantic_model="mock",
            detector_backend=None,
            stage1_enabled=True,
        )

    with (
        patch.object(pipeline_module, "_project_mechanical_config", mechanical),
        patch("ouroboros.evaluation.semantic.SemanticEvaluator.evaluate", semantic),
    ):
        decided = await GenerationCheckPackages(
            event_store=None, evaluate_criteria=evaluate
        ).decide(
            summary,
            seed,
            None,
            artifact=f"### Task 1: [COMPLETED]\nTerminal record: {record}",
            project_dir=str(project),
        )

    if scope == "source":
        mechanical.assert_not_awaited()
        assert not marker.exists()
        assert not decided.final_approved
        assert decided.ac_results[0].verification_method == "spec_verifier"
    else:
        assert summary.ac_results[0].ac_verdict_state == "not_evaluated"
        assert not summary.final_approved
        mechanical.assert_awaited_once()
        assert marker.read_text() == "executed"
        assert decided.final_approved is check_passes
        assert decided.ac_results[0].verification_method == "evaluation_pipeline"


@pytest.mark.parametrize("scope", ["other", None, 0, [], {}])
def test_invalid_execution_scope_rejects_extraction(scope: object) -> None:
    assert _extract(scope) is None


def test_legacy_assertions_default_to_source_scope() -> None:
    assertions = _extract("source")
    assert assertions is not None
    legacy = assertions[0].model_dump()
    legacy.pop("evidence_scope", None)
    assertion = type(assertions[0]).model_validate(legacy)
    assert assertion.evidence_scope == "source"


@pytest.mark.parametrize("record_case", ["false", "missing", "forged", "stale"])
async def test_execution_scope_does_not_accept_rejected_runtime_records(
    tmp_path: Path, record_case: str
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text("iterations = 2\n")
    observed = tmp_path / "observed-run.txt"
    observed.write_text("current-run completed 2 iterations\n")
    record = tmp_path / "terminal.json"
    payload = {
        "run_id": "old-run" if record_case == "stale" else "current-run",
        "iterations": 99 if record_case == "false" else 2,
        "observed": "invented result" if record_case == "forged" else observed.read_text(),
    }
    if record_case != "missing":
        record.write_text(json.dumps(payload))
    assertions = _extract()
    assert assertions is not None
    verification = SpecVerifier(str(project), strict=True).verify_all(assertions, {0: True})
    # Even matching source text cannot prove that a run produced this result.
    assert verification.reports[0].results[0].outcome is VerificationOutcome.SKIPPED
    seed = SimpleNamespace(
        metadata=SimpleNamespace(seed_id="seed-rejected-runtime-record"),
        goal="Verify this run's recorded result",
        constraints=(),
        acceptance_criteria=(CRITERION,),
    )
    summary = evaluation_summary_from_spec_verification(
        EvaluationSummary(
            final_approved=False, highest_stage_passed=1, execution_completion_status="completed"
        ),
        verification,
        seed,
    )
    marker = tmp_path / "check-ran"
    command = (
        sys.executable,
        "-c",
        "import json,pathlib; "
        f"pathlib.Path({str(marker)!r}).write_text('executed'); "
        f"record=json.loads(pathlib.Path({str(record)!r}).read_text()); "
        "assert record['run_id'] == 'current-run'; "
        "assert record['iterations'] == 2; "
        f"assert record['observed'] == pathlib.Path({str(observed)!r}).read_text()",
    )

    async def evaluate(seed, indices, **kwargs):
        return await evaluate_criteria_with_pipeline(
            seed,
            indices,
            **kwargs,
            llm_adapter=AsyncMock(),
            semantic_model="mock",
            detector_backend=None,
            stage1_enabled=True,
        )

    semantic = AsyncMock()
    with (
        patch.object(
            pipeline_module,
            "_project_mechanical_config",
            AsyncMock(return_value=MechanicalConfig(test_command=command, working_dir=project)),
        ),
        patch("ouroboros.evaluation.semantic.SemanticEvaluator.evaluate", semantic),
    ):
        decided = await GenerationCheckPackages(
            event_store=None, evaluate_criteria=evaluate
        ).decide(summary, seed, None, artifact=str(record), project_dir=str(project))
    assert marker.read_text() == "executed"
    assert decided.final_approved is False
    assert decided.ac_results[0].verification_method == "evaluation_pipeline"
    semantic.assert_not_awaited()


async def test_execution_skip_cannot_erase_source_failure_in_the_same_criterion(
    tmp_path: Path,
) -> None:
    (tmp_path / "app.py").write_text("iterations = 99\n")
    assertions = _extract()
    assert assertions is not None
    mixed = (*assertions, assertions[0].model_copy(update={"evidence_scope": "source"}))
    verification = SpecVerifier(str(tmp_path), strict=True).verify_all(mixed, {0: True})
    assert {row.outcome for row in verification.reports[0].results} == {
        VerificationOutcome.SKIPPED,
        VerificationOutcome.DISCREPANCY,
    }
    seed = SimpleNamespace(acceptance_criteria=(CRITERION,))
    summary = evaluation_summary_from_spec_verification(
        EvaluationSummary(
            final_approved=False, highest_stage_passed=1, execution_completion_status="completed"
        ),
        verification,
        seed,
    )
    pipeline = AsyncMock()
    decided = await GenerationCheckPackages(event_store=None, evaluate_criteria=pipeline).decide(
        summary, seed, None
    )
    pipeline.assert_not_awaited()
    assert not decided.final_approved
    assert decided.ac_results[0].verdict_is_authoritative


@pytest.mark.parametrize("stage1_enabled", [True, False])
async def test_execution_scope_without_executed_checks_cannot_approve(
    tmp_path: Path, stage1_enabled: bool
) -> None:
    assertions = _extract()
    assert assertions is not None
    verification = SpecVerifier(str(tmp_path), strict=True).verify_all(assertions, {0: True})
    seed = SimpleNamespace(
        metadata=SimpleNamespace(seed_id="seed-no-executed-check"),
        goal="Verify a run",
        constraints=(),
        acceptance_criteria=(CRITERION,),
    )
    summary = evaluation_summary_from_spec_verification(
        EvaluationSummary(
            final_approved=False, highest_stage_passed=1, execution_completion_status="completed"
        ),
        verification,
        seed,
    )
    evaluate = partial(
        evaluate_criteria_with_pipeline,
        llm_adapter=None,
        semantic_model="mock",
        detector_backend=None,
        stage1_enabled=stage1_enabled,
    )
    semantic = AsyncMock(
        return_value=Result.ok(
            (
                SemanticResult(
                    score=0.95,
                    ac_compliance=True,
                    goal_alignment=0.95,
                    drift_score=0.0,
                    uncertainty=0.0,
                    reasoning="Favorable model opinion is not executed evidence.",
                ),
                [],
            )
        )
    )
    with (
        patch.object(
            pipeline_module,
            "_project_mechanical_config",
            AsyncMock(return_value=MechanicalConfig(working_dir=tmp_path)),
        ),
        patch("ouroboros.evaluation.semantic.SemanticEvaluator.evaluate", semantic),
    ):
        decided = await GenerationCheckPackages(
            event_store=None, evaluate_criteria=evaluate
        ).decide(summary, seed, None, artifact="completed", project_dir=str(tmp_path))
    semantic.assert_awaited_once()
    assert not decided.final_approved
    assert decided.ac_results[0].ac_verdict_state == "not_evaluated"
