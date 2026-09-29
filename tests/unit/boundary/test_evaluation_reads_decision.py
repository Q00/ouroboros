"""Formal evaluation reads the check package decision the run recorded.

The decision comes from the real product path (``test_journal_roundtrip``):
nothing here is built as data. A package pass is executed evidence for its
criterion only, a package fail an executed failure for its criterion only,
and a run with no admitted decision gives no evidence.
"""

from __future__ import annotations

from pathlib import Path

from ouroboros.boundary.decision import (
    recorded_criterion_decisions,
    recorded_execution_for_seed,
)
from ouroboros.core.seed import AcceptanceCriterionSpec, ac_text
from ouroboros.evaluation.mechanical import MechanicalConfig
from ouroboros.evaluation.models import (
    CheckResult,
    CheckType,
    EvaluationContext,
    MechanicalDisposition,
)
from ouroboros.evaluation.pipeline import EvaluationPipeline, PipelineConfig
from ouroboros.mcp.tools.evaluation_package_evidence import (
    evidence_for_criteria,
    recorded_checks_by_position,
)
from ouroboros.persistence.event_store import EventStore

from .clamp_fixtures import FIXED, _seed
from .test_journal_roundtrip import (
    EXECUTION_ID,
    WRONG,
    _Constructor,
    _prepare,
    _reply,
    _run,
    repo,  # noqa: F401 (fixture)
    store,  # noqa: F401 (fixture)
)


async def _stage1(
    index: int, recorded: tuple[tuple[CheckResult, ...], ...], working_dir: Path
) -> MechanicalDisposition:
    """Stage 1 for one criterion of a project with no configured command check."""
    pipeline = EvaluationPipeline(
        llm_adapter=None,  # type: ignore[arg-type]  (Stage 2 and 3 are off)
        config=PipelineConfig(
            mechanical=MechanicalConfig(working_dir=working_dir),
            stage2_enabled=False,
            stage3_enabled=False,
        ),
    )
    context = EvaluationContext(
        execution_id=EXECUTION_ID,
        seed_id="seed",
        current_ac=_acs()[index],
        artifact="",
        recorded_checks=recorded[index],
    )
    result = await pipeline.evaluate(context)
    assert result.is_ok, result
    stage1 = result.value.stage1_result
    assert stage1 is not None
    return stage1.disposition


def _acs() -> list[str]:
    return [ac_text(criterion).strip() for criterion in _seed().acceptance_criteria]


async def test_a_package_pass_is_executed_evidence_for_its_criterion_only(
    store: EventStore,  # noqa: F811
    repo: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    seed, _state, authority = await _prepare(store, repo, tmp_path, _Constructor(_reply()))
    (repo / "mathutils.py").write_text(FIXED)
    await _run(seed, authority, gate=True)
    recorded = await recorded_checks_by_position(store, EXECUTION_ID, seed)
    # criterion 1 is a verified pass; criteria 2 and 3 are unverified (no evidence).
    assert await _stage1(0, recorded, repo) is MechanicalDisposition.EXECUTED_PASS
    assert await _stage1(1, recorded, repo) is MechanicalDisposition.NO_EVIDENCE
    assert await _stage1(2, recorded, repo) is MechanicalDisposition.NO_EVIDENCE


async def test_a_package_fail_is_an_executed_failure_for_its_criterion_only(
    store: EventStore,  # noqa: F811
    repo: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    seed, _state, authority = await _prepare(store, repo, tmp_path, _Constructor(_reply()))
    (repo / "mathutils.py").write_text(WRONG)
    await _run(seed, authority, gate=True)
    recorded = await recorded_checks_by_position(store, EXECUTION_ID, seed)
    assert await _stage1(2, recorded, repo) is MechanicalDisposition.EXECUTED_FAIL
    assert await _stage1(1, recorded, repo) is MechanicalDisposition.NO_EVIDENCE


async def test_no_recorded_decision_is_no_evidence(
    store: EventStore,  # noqa: F811
    repo: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    seed, _state, _authority = await _prepare(store, repo, tmp_path, _Constructor(_reply()))
    # The worker started but nothing was decided yet.
    assert await recorded_criterion_decisions(store, EXECUTION_ID, seed) == ()
    assert await recorded_checks_by_position(store, EXECUTION_ID, seed) == ()
    # A run the check package never touched has no decision either.
    assert await recorded_checks_by_position(store, "exec_other", seed) == ()


async def test_a_decision_for_another_seed_is_no_evidence(
    store: EventStore,  # noqa: F811
    repo: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    seed, _state, authority = await _prepare(store, repo, tmp_path, _Constructor(_reply()))
    (repo / "mathutils.py").write_text(FIXED)
    await _run(seed, authority, gate=True)
    reordered = seed.model_copy(
        update={"acceptance_criteria": tuple(reversed(seed.acceptance_criteria))}
    )
    assert await recorded_criterion_decisions(store, EXECUTION_ID, reordered) == ()


async def test_a_decision_for_a_seed_with_the_same_keys_but_another_digest_is_no_evidence(
    store: EventStore,  # noqa: F811
    repo: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    seed, _state, authority = await _prepare(store, repo, tmp_path, _Constructor(_reply()))
    (repo / "mathutils.py").write_text(FIXED)
    await _run(seed, authority, gate=True)
    assert await recorded_criterion_decisions(store, EXECUTION_ID, seed) != ()
    other = seed.model_copy(update={"goal": "another goal"})
    assert await recorded_criterion_decisions(store, EXECUTION_ID, other) == ()


def test_criteria_with_the_same_text_keep_their_own_evidence() -> None:
    seed = _seed().model_copy(
        update={
            "acceptance_criteria": (
                AcceptanceCriterionSpec(
                    description="clamp works", semantic_ac_key="ac_" + "1" * 16
                ),
                AcceptanceCriterionSpec(
                    description="clamp works", semantic_ac_key="ac_" + "2" * 16
                ),
            )
        }
    )
    passed = CheckResult(CheckType.CHECK_PACKAGE, True, "pass", executed=True)
    failed = CheckResult(CheckType.CHECK_PACKAGE, False, "fail", executed=True)
    evaluated = ("clamp works", "clamp works")
    assert evidence_for_criteria(evaluated, seed, ((failed,), (passed,))) == ((failed,), (passed,))
    # Criteria that are not the Seed's own, in order, get no evidence at all.
    assert evidence_for_criteria(("clamp works",), seed, ((failed,), (passed,))) == ((),)


async def test_the_journal_names_the_run_a_seed_was_frozen_for(
    store: EventStore,  # noqa: F811
    repo: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    seed, _state, _authority = await _prepare(store, repo, tmp_path, _Constructor(_reply()))
    assert await recorded_execution_for_seed(store, seed) == EXECUTION_ID
    other = seed.model_copy(update={"goal": "another goal"})
    assert await recorded_execution_for_seed(store, other) is None
