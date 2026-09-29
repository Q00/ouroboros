"""Every evolve generation runs its check package, and its verdicts decide what they cover.

v0.55.0 dev run: generations 2 and 3 of a lineage ran with no check package
(no ``boundary.*`` event after generation 1), and the evolve evaluator's
source-scan spec verifier skips every behavioral assertion, so each behavioral
criterion stayed "source verification skipped" and the lineage could never
converge on a bug fix.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ouroboros.core.lineage import ACResult, EvaluationSummary
from ouroboros.core.types import Result
from ouroboros.mcp.server import evolution_check_package as module
from ouroboros.mcp.server.evolution_check_package import GenerationCheckPackages
from ouroboros.mcp.server.spec_verification_adapter import apply_package_decisions


def _seed(seed_id: str = "seed-gen2") -> Any:
    return SimpleNamespace(
        metadata=SimpleNamespace(seed_id=seed_id),
        acceptance_criteria=("parse_duration('1h30m') returns 5400", "negative input is refused"),
    )


def _decision(status: str, governed_by: str = "check_package") -> Any:
    return SimpleNamespace(package_status=status, governed_by=governed_by, reason="held_out")


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


def test_a_criterion_the_package_did_not_decide_keeps_the_verifier_verdict() -> None:
    summary = apply_package_decisions(
        _skipped_summary(),
        (_decision("pass"), _decision("unverified", governed_by="existing_verifier")),
        _seed(),
    )
    assert summary.final_approved is False
    assert [r.rendered_verdict for r in summary.ac_results] == ["PASS", "NOT_EVALUATED"]


def test_a_criterion_with_no_verdict_row_is_never_approved() -> None:
    summary = apply_package_decisions(
        _skipped_summary(rows=False),
        (_decision("pass"), _decision("indeterminate")),
        _seed(),
    )
    assert summary.final_approved is False
    assert [r.rendered_verdict for r in summary.ac_results] == ["PASS", "NOT_EVALUATED"]


def test_no_decision_leaves_the_summary_unchanged() -> None:
    original = _skipped_summary()
    assert apply_package_decisions(original, (), _seed()) is original


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
    decided = await packages.decide(_skipped_summary(), _seed("seed-gen2"))
    assert seen == ["exec_seed-gen2"]
    assert decided.final_approved is True


async def test_a_generation_evaluated_after_a_resume_finds_its_run_in_the_journal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packages = GenerationCheckPackages(event_store=None)  # type: ignore[arg-type]
    seen: list[str] = []

    async def _run_for_seed(store: Any, seed: Any) -> str:
        return "exec_recovered"

    async def _decisions(store: Any, execution_id: str, seed: Any) -> tuple[Any, ...]:
        seen.append(execution_id)
        return (_decision("pass"), _decision("pass"))

    monkeypatch.setattr(module, "recorded_execution_for_seed", _run_for_seed)
    monkeypatch.setattr(module, "recorded_criterion_decisions", _decisions)
    decided = await packages.decide(_skipped_summary(), _seed("seed-resumed"))
    assert seen == ["exec_recovered"]
    assert decided.final_approved is True
