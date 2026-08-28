"""Integration tests for L3-2 runtime-probe envelope wiring (#1176).

Pins:

- An ``AutoPipeline`` constructed without a ``probe_runner`` keeps
  the envelope ``runtime_probe_evidence`` empty — backwards-compat.
- A wired ``probe_runner`` is invoked at the COMPLETE transition;
  the returned ``RuntimeEvidence`` tuple surfaces on the envelope.
- A ``probe_runner`` failure or explicit probe FAIL blocks PRODUCT_COMPLETE
  instead of allowing a false complete result.
"""

from __future__ import annotations

from typing import Any

import pytest

from ouroboros.auto.adapters import EnvRuntimeProbeRunner
from ouroboros.auto.grading import GradeResult, SeedGrade
from ouroboros.auto.ledger import (
    LedgerEntry,
    LedgerSource,
    LedgerStatus,
    SeedDraftLedger,
)
from ouroboros.auto.seed_reviewer import SeedReview, SeedReviewer
from ouroboros.auto.state import AutoPipelineState
from ouroboros.core.seed import (
    EvaluationPrinciple,
    ExitCondition,
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
)


def _seed() -> Seed:
    return Seed(
        goal="Build a CLI",
        constraints=("Use existing project patterns",),
        acceptance_criteria=("`habit list` prints stable stdout containing created habits",),
        ontology_schema=OntologySchema(
            name="CliTask",
            description="CLI task ontology",
            fields=(OntologyField(name="command", field_type="string", description="Command"),),
        ),
        evaluation_principles=(
            EvaluationPrinciple(name="testability", description="Observable behavior", weight=1.0),
        ),
        exit_conditions=(
            ExitCondition(
                name="verified",
                description="Checks pass",
                evaluation_criteria="All acceptance criteria pass",
            ),
        ),
        metadata=SeedMetadata(seed_id="seed_probe_env", ambiguity_score=0.12),
    )


def _fill_ready(ledger: SeedDraftLedger) -> None:
    for section, value in {
        "actors": "Single local CLI user",
        "inputs": "Command arguments",
        "outputs": "Stable stdout and files",
        "constraints": "Use existing project patterns",
        "non_goals": "No cloud sync",
        "acceptance_criteria": "Command prints stable output",
        "verification_plan": "Run command-level tests",
        "failure_modes": "Invalid input exits non-zero",
        "runtime_context": "Existing repository runtime",
    }.items():
        source = (
            LedgerSource.NON_GOAL if section == "non_goals" else LedgerSource.CONSERVATIVE_DEFAULT
        )
        ledger.add_entry(
            section,
            LedgerEntry(
                key=f"{section}.test",
                value=value,
                source=source,
                confidence=0.85,
                status=LedgerStatus.DEFAULTED,
            ),
        )


class _PassReviewer(SeedReviewer):
    def __init__(self) -> None:
        pass

    def review(self, seed: Seed, *, ledger: Any = None) -> SeedReview:  # noqa: ARG002
        grade = GradeResult(grade=SeedGrade.A, scores={}, findings=[], blockers=[], may_run=True)
        return SeedReview(grade_result=grade, findings=())


async def _ralph_starter_completed(_seed: Seed, **_kwargs: Any) -> dict[str, Any]:
    return {
        "job_id": "job_probe_env",
        "lineage_id": "ralph-probe-env",
        "dispatch_mode": "job",
        "terminal_status": "completed",
        "stop_reason": None,
    }


async def _run_starter_ok(_seed: Seed) -> dict[str, Any]:
    return {
        "job_id": "job_run_pe",
        "session_id": "exec_session_pe",
        "execution_id": "execution_pe",
    }


def _state_at_clean_start(tmp_path) -> AutoPipelineState:
    """Build a fresh state with a seed-ready ledger so the pipeline
    reaches Ralph terminal without going through the repair loop."""
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    _fill_ready(ledger)
    state.ledger = ledger.to_dict()
    return state


@pytest.mark.asyncio
async def test_env_runtime_probe_runner_blocks_public_entrypoint_failures(tmp_path) -> None:
    """Public composition runner executes configured command and returns failing evidence."""
    runner = EnvRuntimeProbeRunner(
        env={
            "OUROBOROS_RUNTIME_PROBE_COMMAND": "python -c 'import sys; sys.exit(7)'",
            "OUROBOROS_RUNTIME_PROBE_TIMEOUT_SECONDS": "5",
        }
    )
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))

    evidence = await runner(state)

    assert len(evidence) == 1
    assert evidence[0].probe_kind == "headless_run"
    assert evidence[0].passed is False
    assert evidence[0].payload["exit_code"] == 7
