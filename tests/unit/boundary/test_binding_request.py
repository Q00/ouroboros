"""A criterion that needs a late binding is asked once for its entry point.

In the final product smoke (feature-wrong) the worker declared no
``entry_points``, so a wrong new-feature implementation ended unverified
(tier U, ``no_binding``) with exit 0. The sticky rule keeps a declaration
from being withdrawn; this keeps one from never being made: the gate sends
one declaration-only repair within the retry budget, then binding admission
and verification run again.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from ouroboros.boundary.acceptance import PackageCriterionStatus
from ouroboros.boundary.authority import (
    NO_BINDING_AFTER_REQUEST,
    NO_BINDING_BUDGET_EXHAUSTED,
    CheckPackageAuthority,
)
from ouroboros.boundary.binding import CheckTier
from ouroboros.boundary.package import seed_criterion_keys
from ouroboros.orchestrator.parallel_executor_models import ParallelExecutionResult
from ouroboros.persistence.event_store import EventStore
from tests.unit.boundary.test_authority_oracle import (
    BAD_MIX,
    BUGGY,
    FIXED,
    GOOD_MIX,
    MIX_ENTRY,
    _authority,
    _batch,
    _executor,
    _legacy_rejected,
)

REQUEST = "could not find the entry point of this criterion"


@pytest.fixture
async def store():
    event_store = EventStore("sqlite+aiosqlite:///:memory:")
    await event_store.initialize()
    yield event_store
    await event_store.close()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "mathutils.py").write_text(BUGGY)
    return root


async def _run(
    store: EventStore,
    repo: Path,
    tmp_path: Path,
    *,
    implementation: str,
    declares_after_request: bool,
    retries: int = 2,
) -> tuple[Any, Any, list[dict[int, str]], list[Any]]:
    """Criterion 2 (``mathutils.interpolate`` does not exist): no declaration at first."""
    seed, authority = await _authority(store, repo, tmp_path)
    (repo / "mathutils.py").write_text(FIXED + implementation)
    executor = _executor(repo, retries=retries)
    authority.install(executor)
    prompts: list[dict[int, str]] = []

    async def fake_batch(**kwargs: Any) -> list[Any]:
        prompts.append(dict(kwargs.get("retry_prompts") or {}))
        entry = MIX_ENTRY if declares_after_request and len(prompts) > 1 else None
        return [replace(_legacy_rejected(1, entry=entry), retry_attempt=len(prompts) - 1)]

    executor._execute_ac_batch = fake_batch  # type: ignore[method-assign]
    results = await _batch(executor, seed, [1])
    parallel = ParallelExecutionResult(
        results=(_legacy_rejected(0), results[0], _legacy_rejected(2)),
        success_count=2 + int(results[0].success),
        failure_count=int(not results[0].success),
    )
    await authority(seed=seed, execution_id="exec_oracle", parallel_result=parallel)
    return seed, authority, prompts, results


def _verdict(seed: Any, authority: Any) -> Any:
    return authority.outcome.verdict.verdicts[seed_criterion_keys(seed)[1]]


async def test_a_wrong_implementation_declared_after_the_request_fails(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    seed, authority, prompts, results = await _run(
        store, repo, tmp_path, implementation=BAD_MIX, declares_after_request=True
    )
    request = prompts[1][1]
    assert REQUEST in request and "entry_points" in request
    assert seed_criterion_keys(seed)[1] in request
    # Nothing about the oracle: no case, input value or expected value.
    assert "0.5" not in request and "expected" not in request
    # The declaration was validated and the oracle failed through it.
    assert [entry["status"] for entry in authority.gate.log][:2] == ["unverified", "fail"]
    assert "expected 5, observed 9.5" in prompts[2][1]
    assert results[0].success is False
    item = _verdict(seed, authority)
    assert (item.status, item.tier) == (PackageCriterionStatus.FAIL, CheckTier.A_PRIME)
    assert not authority.outcome.reconciliation.run_accepted


async def test_a_correct_implementation_declared_after_the_request_passes_as_a_prime(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    seed, authority, prompts, results = await _run(
        store, repo, tmp_path, implementation=GOOD_MIX, declares_after_request=True
    )
    assert len(prompts) == 2 and REQUEST in prompts[1][1]
    assert results[0].success is True
    item = _verdict(seed, authority)
    assert (item.status, item.tier) == (PackageCriterionStatus.PASS, CheckTier.A_PRIME)


async def _no_declaration_after_the_request_stays_unverified_scenario(
    store: EventStore, repo: Path, tmp_path: Path
) -> CheckPackageAuthority:
    seed, authority, prompts, results = await _run(
        store, repo, tmp_path, implementation=BAD_MIX, declares_after_request=False
    )
    # Asked exactly once; the next attempt is not asked again.
    assert len(prompts) == 2 and sum(REQUEST in text for p in prompts for text in p.values()) == 1
    item = _verdict(seed, authority)
    assert item.status.is_unverified and item.reason == NO_BINDING_AFTER_REQUEST
    assert len(authority.binding_requested) == 1
    return authority


async def test_no_declaration_after_the_request_stays_unverified(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    await _no_declaration_after_the_request_stays_unverified_scenario(store, repo, tmp_path)


async def test_no_retry_left_records_budget_exhausted(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    seed, authority, prompts, _results = await _run(
        store, repo, tmp_path, implementation=BAD_MIX, declares_after_request=True, retries=0
    )
    assert len(prompts) == 1
    item = _verdict(seed, authority)
    assert item.status.is_unverified and item.reason == NO_BINDING_BUDGET_EXHAUSTED
    assert authority.binding_requested == set()
