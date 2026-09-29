"""The advisory judge sees what the product executed, each check with its scope.

v0.55.1 dev run (Claude family): Stage 1 executed the project test command and
it passed, but the Stage 2 judge, which runs in the read-only evaluation tool
envelope, could not run it itself; the prompt carried no executed evidence, the
artifact said "executed_unverified", and the judge withheld a preservation
criterion. The codex judge, which can execute, approved the same kind of work.
Stage 2 now receives Stage 1's executed checks, so the verdict no longer
depends on whether a backend's judge can execute.
"""

from __future__ import annotations

import json
from typing import Any

from ouroboros.core.types import Result
from ouroboros.evaluation.executed_evidence import render_executed_evidence
from ouroboros.evaluation.models import (
    CheckResult,
    CheckType,
    EvaluationContext,
    MechanicalResult,
)
from ouroboros.evaluation.pipeline import EvaluationPipeline, PipelineConfig
from ouroboros.evaluation.semantic import SemanticConfig
from ouroboros.providers.base import CompletionResponse, UsageInfo

_COMMAND = ["python", "-m", "pytest", "-q"]


def _test_check(*, passed: bool = True) -> CheckResult:
    return CheckResult(
        check_type=CheckType.TEST,
        passed=passed,
        message="Check test passed" if passed else "Check test failed (exit code 1)",
        executed=True,
        details={
            "command": _COMMAND,
            "return_code": 0 if passed else 1,
            "stdout_tail": "20 passed in 0.01s" if passed else "1 failed, 19 passed",
        },
    )


def _skipped(check_type: CheckType) -> CheckResult:
    return CheckResult(
        check_type=check_type,
        passed=True,
        message=f"Check {check_type.value} skipped (no command configured)",
        details={"skipped": True},
    )


def _package(passed: bool) -> CheckResult:
    status = "pass" if passed else "fail"
    return CheckResult(
        CheckType.CHECK_PACKAGE, passed, f"check package {status} (passed)", executed=True
    )


class _Judge:
    """An LLM adapter that records the prompt and withholds."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def complete(self, messages: list[Any], config: Any) -> Any:
        self.prompts.append(messages[-1].content)
        return Result.ok(
            CompletionResponse(
                content=json.dumps(
                    {
                        "score": 0.6,
                        "ac_compliance": False,
                        "goal_alignment": 0.9,
                        "drift_score": 0.1,
                        "uncertainty": 0.2,
                        "reasoning": "withheld",
                        "questions_used": ["q"],
                        "evidence": ["e"],
                    }
                ),
                model="judge",
                usage=UsageInfo(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            )
        )


async def _judge_prompt(
    stage1: MechanicalResult, recorded: tuple[CheckResult, ...] = ()
) -> tuple[str, Any]:
    judge = _Judge()
    pipeline = EvaluationPipeline(
        llm_adapter=judge,  # type: ignore[arg-type]
        config=PipelineConfig(
            semantic=SemanticConfig(model="judge"),
            stage3_enabled=False,
        ),
    )
    context = EvaluationContext(
        execution_id="exec_1",
        seed_id="seed_1",
        current_ac="Existing behavior still works",
        artifact="Verification Status: executed_unverified",
        recorded_checks=recorded,
    )
    result = await pipeline.evaluate(context, stage1_result=stage1)
    assert result.is_ok, result
    assert len(judge.prompts) == 1
    return judge.prompts[0], result.value


async def test_the_judge_sees_the_executed_test_command_for_a_criterion_with_no_package_row() -> (
    None
):
    stage1 = MechanicalResult(
        passed=True, checks=(_skipped(CheckType.LINT), _test_check(), _skipped(CheckType.BUILD))
    )
    prompt, evaluation = await _judge_prompt(stage1)
    assert "## EXECUTED EVIDENCE (run by Ouroboros, not by you)" in prompt
    assert "scope: the whole project, not this criterion alone" in prompt
    assert "- test: `python -m pytest -q` ran, exit code 0, passed" in prompt
    assert "20 passed in 0.01s" in prompt
    # Checks that did not run are not presented as evidence.
    assert "lint:" not in prompt and "build:" not in prompt
    assert "Check package decision" not in prompt
    # The judge still only withholds: its withholding stands.
    assert evaluation.final_approved is False


async def test_a_package_decision_is_shown_as_evidence_for_this_criterion_only() -> None:
    stage1 = MechanicalResult(passed=True, checks=(_test_check(),))
    prompt, _ = await _judge_prompt(stage1, recorded=(_package(True),))
    assert "scope: this criterion only" in prompt
    assert "- pass: check package pass (passed)" in prompt


def test_nothing_executed_renders_no_section() -> None:
    assert render_executed_evidence(None) == ""
    skipped_only = MechanicalResult(passed=True, checks=(_skipped(CheckType.TEST),))
    assert render_executed_evidence(skipped_only) == ""


def test_a_failed_or_timed_out_command_is_shown_as_it_ran() -> None:
    failed = render_executed_evidence(
        MechanicalResult(passed=False, checks=(_test_check(passed=False),))
    )
    assert "ran, exit code 1, failed" in failed
    assert "1 failed, 19 passed" in failed
    timed_out = CheckResult(
        CheckType.TEST,
        False,
        "Check test timed out after 60s",
        executed=True,
        details={"timed_out": True, "command": _COMMAND},
    )
    rendered = render_executed_evidence(MechanicalResult(passed=False, checks=(timed_out,)))
    assert "ran, timed out, failed" in rendered
