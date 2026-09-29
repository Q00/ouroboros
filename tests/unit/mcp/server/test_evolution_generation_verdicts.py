"""Every Seed criterion of an evolve generation gets a verdict it can converge on.

v0.55.1 dev run (Claude family, lineage ralph-a5258be3...): generation 1 passed
criteria 0, 1 and 3 and failed criterion 2, a preservation criterion
("existing behavior still works"). Generation 2 froze 0, 1, 3 and worked on 2.
Its check package was built on generation 2's base, which already holds the
fix, so no reproduction check could fail there and every criterion came back
unverified; the source-scan verifier skipped every behavioral assertion; the
generation was NOT_EVALUATED on all four and the lineage never converged.

Now a frozen criterion carries its previous passing verdict, and a criterion
nothing decided is evaluated by the per-criterion pipeline, as
``ouroboros_evaluate`` does: executed checks grant, the review only withholds.
"""

from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from ouroboros.core.lineage import (
    ACResult,
    EvaluationSummary,
    GenerationPhase,
    GenerationRecord,
)
from ouroboros.core.seed import OntologySchema
from ouroboros.core.types import Result
from ouroboros.evaluation.mechanical import MechanicalConfig
from ouroboros.evaluation.models import SemanticResult
from ouroboros.evolution.focus import EvolutionFocus, call_evaluator
from ouroboros.mcp.server import evolution_check_package as package_module
from ouroboros.mcp.server import evolution_pipeline_evaluation as pipeline_module
from ouroboros.mcp.server.evolution_check_package import GenerationCheckPackages
from ouroboros.mcp.server.evolution_pipeline_evaluation import evaluate_criteria_with_pipeline
from ouroboros.mcp.server.spec_verification_adapter import apply_package_decisions

CRITERIA = (
    "1h30m parses to 5400",
    "30m1h raises ValueError",
    "existing behavior still works",
    "1h90m parses to 9000",
)


def _seed() -> Any:
    return SimpleNamespace(
        metadata=SimpleNamespace(seed_id="seed-gen2"),
        goal="Fix parse_duration",
        constraints=(),
        acceptance_criteria=CRITERIA,
    )


def _row(index: int, *, passed: bool, state: str = "evaluated", method: str = "x") -> ACResult:
    verdict = "pass" if passed else "fail"
    return ACResult(
        ac_index=index,
        ac_content=CRITERIA[index],
        passed=passed,
        evidence=f"{method} evidence",
        verification_method=method,
        ac_verdict_state=state,
        final_verdict=verdict,
        rendered_verdict=verdict.upper() if state == "evaluated" else "NOT_EVALUATED",
    )


def _skipped_summary() -> EvaluationSummary:
    """What the source-scan verifier reports for behavioral criteria: nothing decided."""
    return EvaluationSummary(
        final_approved=False,
        highest_stage_passed=2,
        score=0.0,
        failure_reason="source verification skipped for AC 1, 2, 3, 4",
        ac_results=tuple(
            _row(i, passed=False, state="not_evaluated", method="spec_verifier") for i in range(4)
        ),
        execution_completion_status="completed",
        approval_status="rejected",
    )


def _generation_one() -> GenerationRecord:
    return GenerationRecord(
        generation_number=1,
        seed_id="seed-gen1",
        ontology_snapshot=OntologySchema(name="o", description="d", fields=()),
        phase=GenerationPhase.COMPLETED,
        evaluation_summary=EvaluationSummary(
            final_approved=False,
            highest_stage_passed=2,
            ac_results=(
                _row(0, passed=True, method="formal_evaluation"),
                _row(1, passed=True, method="formal_evaluation"),
                _row(2, passed=False, method="formal_evaluation"),
                _row(3, passed=True, method="formal_evaluation"),
            ),
        ),
    )


def _record(
    status: str = "unverified", governed_by: str = "existing_verifier", *, accepted: bool = True
) -> Any:
    """A recorded criterion decision: the package's status and the existing verifier's verdict."""
    return SimpleNamespace(
        package_status=status,
        governed_by=governed_by,
        reason="no_reproduction_check" if status == "unverified" else "held_out",
        existing_accepted=accepted,
        existing_outcome="succeeded" if accepted else "failed",
        existing_failure_class=None if accepted else "verification_failed",
    )


FOCUS = EvolutionFocus(active_ac_indices=(2,), frozen_ac_indices=(0, 1, 3), reason="focused")


def test_a_frozen_criterion_carries_its_previous_passing_verdict() -> None:
    carried = FOCUS.carried_verdicts(_generation_one())
    assert sorted(carried) == [0, 1, 3]
    assert all(row.authoritative_pass for row in carried.values())
    assert carried[0].verification_method == "formal_evaluation"
    assert carried[0].evidence.startswith("carried from generation 1:")
    # An active criterion or a previous failure is never carried.
    failing = EvolutionFocus(active_ac_indices=(), frozen_ac_indices=(2,), reason="x")
    assert failing.carried_verdicts(_generation_one()) == {}
    assert FOCUS.carried_verdicts(None) == {}


def test_a_criterion_the_package_could_not_evaluate_takes_the_existing_verifier_verdict() -> None:
    """The run's recorded existing-verifier verdict decides it, as ``ooo run`` does."""
    spec = _skipped_summary().model_copy(
        update={
            "ac_results": (
                _row(0, passed=False, state="not_evaluated", method="spec_verifier"),
                _row(1, passed=False, method="spec_verifier"),
                _row(2, passed=False, state="not_evaluated", method="spec_verifier"),
                _row(3, passed=False, state="not_evaluated", method="spec_verifier"),
            )
        }
    )
    decisions = (
        _record("fail", "check_package"),
        _record(accepted=True),
        _record(accepted=True),
        _record("indeterminate", "check_package", accepted=False),
    )
    resolved = apply_package_decisions(
        spec, decisions, _seed(), carried=FOCUS.carried_verdicts(_generation_one())
    )
    # 0: the package failed it; 1: a spec-verifier failure rejects it even though the existing
    # verifier accepted it; 2: the existing verifier accepted it; 3: the package could not
    # evaluate it and the existing verifier rejected it, which a carried pass does not lift.
    assert [row.verification_method for row in resolved.ac_results] == [
        "check_package",
        "spec_verifier",
        "existing_verifier",
        "existing_verifier",
    ]
    assert [row.rendered_verdict for row in resolved.ac_results] == ["FAIL", "FAIL", "PASS", "FAIL"]
    assert resolved.final_approved is False


async def test_the_dev_run_lineage_is_approved_by_the_recorded_verifier_verdicts(
    monkeypatch: Any,
) -> None:
    """Run 11: the package verified nothing, the existing verifier accepted every criterion."""

    async def _decisions(store: Any, execution_id: str, seed: Any) -> tuple[Any, ...]:
        return (_record(), _record(), _record(), _record(governed_by="check_package"))

    asked: list[tuple[int, ...]] = []

    async def _pipeline(seed: Any, indices: tuple[int, ...], **kwargs: Any) -> dict[int, ACResult]:
        asked.append(indices)
        return {}

    monkeypatch.setattr(package_module, "recorded_criterion_decisions", _decisions)
    packages = GenerationCheckPackages(event_store=None, evaluate_criteria=_pipeline)  # type: ignore[arg-type]
    decided = await packages.decide(_skipped_summary(), _seed(), "evolve:lin:generation:1")
    assert asked == []
    assert decided.final_approved is True
    assert {row.verification_method for row in decided.ac_results} == {"existing_verifier"}


async def test_with_no_recorded_decision_carried_verdicts_and_the_pipeline_decide() -> None:
    """The package was off: 0, 1, 3 carried, 2 evaluated by the pipeline, generation approved."""
    asked: list[tuple[int, ...]] = []

    async def _pipeline(seed: Any, indices: tuple[int, ...], **kwargs: Any) -> dict[int, ACResult]:
        asked.append(indices)
        assert kwargs == {"artifact": "report", "project_dir": "/work"}
        return {2: _row(2, passed=True, method="evaluation_pipeline")}

    packages = GenerationCheckPackages(event_store=None, evaluate_criteria=_pipeline)  # type: ignore[arg-type]
    decided = await packages.decide(
        _skipped_summary(),
        _seed(),
        None,
        carried=FOCUS.carried_verdicts(_generation_one()),
        artifact="report",
        project_dir="/work",
    )
    assert asked == [(2,)]
    assert decided.final_approved is True
    assert all(row.authoritative_pass for row in decided.ac_results)


async def test_without_carried_verdicts_every_undecided_criterion_is_evaluated() -> None:
    asked: list[tuple[int, ...]] = []

    async def _pipeline(seed: Any, indices: tuple[int, ...], **kwargs: Any) -> dict[int, ACResult]:
        asked.append(indices)
        return {}

    packages = GenerationCheckPackages(event_store=None, evaluate_criteria=_pipeline)  # type: ignore[arg-type]
    decided = await packages.decide(_skipped_summary(), _seed(), None)
    assert asked == [(0, 1, 2, 3)]
    assert decided.final_approved is False


def _semantic(*, approve: bool) -> SemanticResult:
    return SemanticResult(
        score=0.9 if approve else 0.4,
        ac_compliance=approve,
        goal_alignment=0.9,
        drift_score=0.1,
        uncertainty=0.1,
        reasoning="ok" if approve else "withheld",
        questions_used=("q",),
        evidence=("e",),
    )


async def _evaluate(
    tmp_path: Path, *, approve: bool, command: tuple[str, ...] | None
) -> tuple[dict[int, ACResult], int]:
    config = MechanicalConfig(test_command=command, working_dir=tmp_path)
    semantic = AsyncMock(return_value=Result.ok((_semantic(approve=approve), [])))
    with (
        patch("ouroboros.evaluation.semantic.SemanticEvaluator.evaluate", semantic),
        patch.object(pipeline_module, "_project_mechanical_config", AsyncMock(return_value=config)),
    ):
        rows = await evaluate_criteria_with_pipeline(
            _seed(),
            (2, 3),
            artifact="report",
            project_dir=str(tmp_path),
            llm_adapter=None,
            semantic_model="judge",
            detector_backend=None,
            stage1_enabled=True,
        )
    ran = tmp_path / "ran"
    return rows, len(ran.read_text()) if ran.exists() else 0


def _counting_command(tmp_path: Path, exit_code: int = 0) -> tuple[str, ...]:
    script = f"open({str(tmp_path / 'ran')!r}, 'a').write('x'); raise SystemExit({exit_code})"
    return (sys.executable, "-c", script)


async def test_an_executed_passing_check_and_a_clean_review_approve_each_criterion(
    tmp_path: Path,
) -> None:
    rows, runs = await _evaluate(tmp_path, approve=True, command=_counting_command(tmp_path))
    assert sorted(rows) == [2, 3]
    assert all(row.authoritative_pass for row in rows.values())
    assert rows[2].verification_method == "evaluation_pipeline"
    # The project's command checks run once and are shared by every criterion.
    assert runs == 1


async def test_the_review_can_only_withhold(tmp_path: Path) -> None:
    rows, _ = await _evaluate(tmp_path, approve=False, command=_counting_command(tmp_path))
    assert [row.rendered_verdict for row in rows.values()] == ["FAIL", "FAIL"]
    assert not any(row.authoritative_pass for row in rows.values())


async def test_an_executed_failure_rejects(tmp_path: Path) -> None:
    rows, _ = await _evaluate(tmp_path, approve=True, command=_counting_command(tmp_path, 1))
    assert [row.rendered_verdict for row in rows.values()] == ["FAIL", "FAIL"]


async def test_no_executed_check_is_not_evaluated(tmp_path: Path) -> None:
    rows, _ = await _evaluate(tmp_path, approve=True, command=None)
    assert [row.ac_verdict_state for row in rows.values()] == ["not_evaluated"] * 2
    assert not any(row.authoritative_pass for row in rows.values())


async def test_carried_verdicts_reach_only_an_evaluator_that_reads_them() -> None:
    carried = FOCUS.carried_verdicts(_generation_one())
    seen: dict[str, Any] = {}

    async def reads(seed: Any, output: str | None, *, carried_ac_results: Any = None) -> Any:
        seen["carried"] = carried_ac_results
        return _skipped_summary()

    async def ignores(seed: Any, output: str | None) -> Any:
        return _skipped_summary()

    await call_evaluator(reads, _seed(), "out", None, carried)
    assert seen["carried"] == carried
    await call_evaluator(ignores, _seed(), "out", "evolve:lin:generation:2", carried)


async def _server_evaluator(store: Any, monkeypatch: Any, **patches: Any) -> Any:
    """The evolve evaluator the MCP server actually wires into its loop."""
    import ouroboros.evolution.loop as loop_module
    from ouroboros.mcp.server import adapter

    captured: dict[str, Any] = {}

    class _Loop(loop_module.EvolutionaryLoop):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            captured.update(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(loop_module, "EvolutionaryLoop", _Loop)
    for name, value in patches.items():
        monkeypatch.setattr(adapter, name, value)
    adapter.create_ouroboros_server(event_store=store)
    return captured["evaluator"]


async def test_an_approved_no_marker_fallback_still_resolves_every_criterion(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """A recorded package failure rejects the generation even when the fallback approved."""
    from ouroboros.persistence.event_store import EventStore

    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'events.db'}")
    await store.initialize()

    async def _approved_fallback(**kwargs: Any) -> EvaluationSummary:
        return EvaluationSummary(
            final_approved=True,
            highest_stage_passed=2,
            execution_completion_status="completed",
            approval_status="approved",
        )

    async def _pipeline(seed: Any, indices: tuple[int, ...], **kwargs: Any) -> dict[int, ACResult]:
        return {index: _row(index, passed=True, method="evaluation_pipeline") for index in indices}

    async def _decisions(store: Any, execution_id: str, seed: Any) -> tuple[Any, ...]:
        return (_record(), _record("fail", "check_package"), _record(), _record())

    evaluator = await _server_evaluator(
        store,
        monkeypatch,
        evaluate_generation_with_pipeline=_approved_fallback,
        evaluate_criteria_with_pipeline=_pipeline,
    )
    monkeypatch.setattr(package_module, "recorded_criterion_decisions", _decisions)
    summary = await evaluator(
        _seed(), "worker report with no task markers", execution_id="evolve:lin:generation:2"
    )
    assert summary.final_approved is False
    assert [row.rendered_verdict for row in summary.ac_results] == ["PASS", "FAIL", "PASS", "PASS"]
    assert summary.ac_results[1].verification_method == "check_package"
    await store.close()


async def test_an_approved_rowless_fallback_the_pipeline_could_not_evaluate_is_not_approved() -> (
    None
):
    """The reviewer's reproduction: no package decision, no carried verdict, pipeline returns nothing."""

    async def _nothing(seed: Any, indices: tuple[int, ...], **kwargs: Any) -> dict[int, ACResult]:
        return {}

    one = SimpleNamespace(metadata=SimpleNamespace(seed_id="s"), acceptance_criteria=(CRITERIA[0],))
    approved = EvaluationSummary(
        final_approved=True,
        highest_stage_passed=2,
        execution_completion_status="completed",
        approval_status="approved",
    )
    packages = GenerationCheckPackages(event_store=None, evaluate_criteria=_nothing)  # type: ignore[arg-type]
    decided = await packages.decide(approved, one, None)
    assert decided.final_approved is False
    assert [row.rendered_verdict for row in decided.ac_results] == ["NOT_EVALUATED"]
