"""Every evolve generation runs its check package, and its verdicts decide what they cover.

v0.55.0 dev run: generations 2 and 3 of a lineage ran with no check package
(no ``boundary.*`` event after generation 1), and the evolve evaluator's
source-scan spec verifier skips every behavioral assertion, so each behavioral
criterion stayed "source verification skipped" and the lineage could never
converge on a bug fix.

The evolve loop names each generation's run with one deterministic execution
id and gives it to both the executor and the evaluator, so a generation reads
its own run's decision, in the same process or after a resume, and never
another run's with the same Seed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ouroboros.core.lineage import ACResult, EvaluationSummary, OntologyLineage
from ouroboros.core.seed import (
    EvaluationPrinciple,
    ExitCondition,
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
)
from ouroboros.core.types import Result
from ouroboros.events.lineage import lineage_created
from ouroboros.evolution import loop_support
from ouroboros.evolution.loop import EvolutionaryLoop
from ouroboros.evolution.projector import LineageProjector
from ouroboros.mcp.server import evolution_check_package as module
from ouroboros.mcp.server.evolution_check_package import GenerationCheckPackages
from ouroboros.mcp.server.spec_verification_adapter import apply_package_decisions
from ouroboros.persistence.event_store import EventStore


def _seed(seed_id: str = "seed-gen2") -> Any:
    return SimpleNamespace(
        metadata=SimpleNamespace(seed_id=seed_id),
        acceptance_criteria=("parse_duration('1h30m') returns 5400", "negative input is refused"),
    )


def _decision(status: str, governed_by: str = "check_package", *, accepted: bool = False) -> Any:
    return SimpleNamespace(
        package_status=status,
        governed_by=governed_by,
        reason="held_out",
        existing_accepted=accepted,
        existing_outcome="succeeded" if accepted else "failed",
        existing_failure_class=None,
        accepted=(status == "pass") if governed_by == "check_package" else accepted,
    )


def _skipped_summary(*, rows: bool = True) -> EvaluationSummary:
    """What the source-scan verifier reports for behavioral criteria: nothing decided."""
    results = tuple(
        ACResult(
            ac_index=index,
            ac_content=text,
            passed=False,
            score=0.0,
            evidence="source verification skipped",
            verification_method="spec_verifier",
            ac_verdict_state="not_evaluated",
            final_verdict="fail",
            rendered_verdict="NOT_EVALUATED",
        )
        for index, text in enumerate(_seed().acceptance_criteria)
    )
    return EvaluationSummary(
        final_approved=False,
        highest_stage_passed=2,
        score=0.0,
        failure_reason="source verification skipped for AC 1, 2",
        ac_results=results if rows else (),
        execution_completion_status="completed",
        approval_status="rejected",
    )


def test_package_passes_approve_criteria_the_spec_verifier_skipped() -> None:
    summary = apply_package_decisions(
        _skipped_summary(), (_decision("pass"), _decision("pass")), _seed()
    )
    assert summary.final_approved is True
    assert [r.verification_method for r in summary.ac_results] == ["check_package"] * 2


def test_a_package_fail_rejects_only_its_criterion() -> None:
    summary = apply_package_decisions(
        _skipped_summary(), (_decision("pass"), _decision("fail")), _seed()
    )
    assert summary.final_approved is False
    assert [r.rendered_verdict for r in summary.ac_results] == ["PASS", "FAIL"]


@pytest.mark.parametrize(("accepted", "verdict"), [(True, "PASS"), (False, "FAIL")])
def test_a_criterion_the_package_did_not_decide_takes_the_existing_verifier_verdict(
    accepted: bool, verdict: str
) -> None:
    summary = apply_package_decisions(
        _skipped_summary(),
        (_decision("pass"), _decision("unverified", "existing_verifier", accepted=accepted)),
        _seed(),
    )
    assert summary.final_approved is accepted
    assert [r.rendered_verdict for r in summary.ac_results] == ["PASS", verdict]
    assert summary.ac_results[1].verification_method == "existing_verifier"


def test_an_indeterminate_package_criterion_the_verifier_rejected_is_never_approved() -> None:
    summary = apply_package_decisions(
        _skipped_summary(rows=False),
        (_decision("pass"), _decision("indeterminate")),
        _seed(),
    )
    assert summary.final_approved is False
    assert [r.rendered_verdict for r in summary.ac_results] == ["PASS", "FAIL"]


def test_no_decision_leaves_the_verdicts_unchanged() -> None:
    original = _skipped_summary()
    resolved = apply_package_decisions(original, (), _seed())
    assert resolved.ac_results == original.ac_results
    assert resolved.final_approved is False


def test_an_aggregate_approval_with_no_criterion_verdict_is_not_approved() -> None:
    """A rowless summary approved in aggregate proves no Seed criterion."""
    rowless = _skipped_summary(rows=False).model_copy(
        update={"final_approved": True, "approval_status": "approved"}
    )
    resolved = apply_package_decisions(rowless, (), _seed())
    assert resolved.final_approved is False
    assert [r.rendered_verdict for r in resolved.ac_results] == ["NOT_EVALUATED"] * 2


class _Runner:
    def __init__(self, order: list[str]) -> None:
        self.order = order

    async def execute_seed(self, **kwargs: Any) -> Any:
        self.order.append(f"execute:{kwargs['execution_id']}")
        return Result.ok(SimpleNamespace(success=True, summary={}))


async def test_each_generation_prepares_its_package_before_its_worker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    order: list[str] = []

    async def _prepare(self: Any, runner: Any, seed: Any, **kwargs: Any) -> list[str]:
        order.append(f"prepare:{kwargs['execution_id']}")
        return []

    monkeypatch.setattr(module.CheckPackageRun, "prepare", _prepare)
    packages = GenerationCheckPackages(event_store=None)  # type: ignore[arg-type]
    for generation in ("seed-gen1", "seed-gen2"):
        await packages.execute(
            _Runner(order),
            _seed(generation),
            execution_id=f"exec_{generation}",
            worker_dir=tmp_path,
            runtime_backend="codex",
            model=None,
            parallel=True,
            externally_satisfied_acs=None,
        )
    assert order == [
        "prepare:exec_seed-gen1",
        "execute:exec_seed-gen1",
        "prepare:exec_seed-gen2",
        "execute:exec_seed-gen2",
    ]

    seen: list[str] = []

    async def _decisions(store: Any, execution_id: str, seed: Any) -> tuple[Any, ...]:
        seen.append(execution_id)
        return (_decision("pass"), _decision("pass"))

    monkeypatch.setattr(module, "recorded_criterion_decisions", _decisions)
    decided = await packages.decide(_skipped_summary(), _seed("seed-gen2"), "exec_seed-gen2")
    assert seen == ["exec_seed-gen2"]
    assert decided.final_approved is True


async def test_two_runs_of_one_seed_each_keep_their_own_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = {
        "evolve:lin_a:generation:2": (_decision("pass"), _decision("pass")),
        "evolve:lin_b:generation:2": (_decision("fail"), _decision("fail")),
    }

    async def _decisions(store: Any, execution_id: str, seed: Any) -> tuple[Any, ...]:
        return recorded[execution_id]

    monkeypatch.setattr(module, "recorded_criterion_decisions", _decisions)
    packages = GenerationCheckPackages(event_store=None)  # type: ignore[arg-type]
    seed = _seed("seed-shared")
    lineage_a = await packages.decide(_skipped_summary(), seed, "evolve:lin_a:generation:2")
    lineage_b = await packages.decide(_skipped_summary(), seed, "evolve:lin_b:generation:2")
    assert lineage_a.final_approved is True
    assert [r.rendered_verdict for r in lineage_b.ac_results] == ["FAIL", "FAIL"]


async def test_no_named_run_is_no_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _decisions(store: Any, execution_id: str, seed: Any) -> tuple[Any, ...]:
        raise AssertionError("no run was named, so no journal is read")

    monkeypatch.setattr(module, "recorded_criterion_decisions", _decisions)
    packages = GenerationCheckPackages(event_store=None)  # type: ignore[arg-type]
    original = _skipped_summary()
    resolved = await packages.decide(original, _seed(), None)
    assert resolved.ac_results == original.ac_results
    assert resolved.final_approved is False


def _real_seed() -> Seed:
    return Seed(
        goal="Fix parse_duration",
        task_type="code",
        constraints=("c1",),
        acceptance_criteria=("AC0", "AC1"),
        ontology_schema=OntologySchema(
            name="o",
            description="d",
            fields=(OntologyField(name="f", field_type="entity", description="a field"),),
        ),
        evaluation_principles=(
            EvaluationPrinciple(name="completeness", description="done", weight=1.0),
        ),
        exit_conditions=(ExitCondition(name="done", description="d", evaluation_criteria="100%"),),
        metadata=SeedMetadata(seed_id="seed_identity", ambiguity_score=0.1),
    )


async def test_a_resumed_generation_is_evaluated_under_its_own_run(tmp_path: Path) -> None:
    """The process that evaluates after a resume names the run the executor ran."""
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'events.db'}")
    await store.initialize()
    seed = _real_seed()
    await store.append(lineage_created("lin_identity", seed.goal))
    calls: list[tuple[str, str | None]] = []

    async def executor(seed: Any, *, parallel: bool = True, execution_id: str | None = None) -> Any:
        calls.append(("execute", execution_id))
        return Result.ok(SimpleNamespace(final_message="done", summary={}, success=True))

    async def dies_while_evaluating(
        seed: Any, output: str | None, *, execution_id: str | None = None
    ) -> Any:
        raise asyncio.CancelledError

    async def evaluator(seed: Any, output: str | None, *, execution_id: str | None = None) -> Any:
        calls.append(("evaluate", execution_id))
        return _skipped_summary()

    first = EvolutionaryLoop(event_store=store, executor=executor, evaluator=dies_while_evaluating)
    with pytest.raises(asyncio.CancelledError):
        await first._run_generation_with_watchdog(
            lineage=OntologyLineage(lineage_id="lin_identity", goal=seed.goal),
            generation_number=1,
            current_seed=seed,
        )

    # A new process replays the lineage and resumes after execution.
    lineage = LineageProjector().project(await store.replay_lineage("lin_identity"))
    assert lineage is not None
    resumed = EvolutionaryLoop(event_store=store, executor=executor, evaluator=evaluator)
    result = await resumed._run_generation_with_watchdog(
        lineage=lineage,
        generation_number=1,
        current_seed=seed,
        resume_after_phase="executing",
    )
    assert result.is_ok
    run = loop_support.generation_execution_id("lin_identity", 1)
    assert calls == [("execute", run), ("evaluate", run)]
    await store.close()
