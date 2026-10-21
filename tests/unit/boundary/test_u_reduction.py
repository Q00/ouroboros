"""Per-check admission, one replacement call, routing by admitted check, coverage."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ouroboros.boundary.acceptance import (
    ExistingOutcome,
    PackageCriterionStatus,
    VerificationCoverage,
    reconcile_acceptance,
)
from ouroboros.boundary.binding import CheckTier
from ouroboros.boundary.constructor import ConstructionOutcome
from ouroboros.boundary.coverage import why_excluded
from ouroboros.boundary.events import (
    ACTOR_STARTED,
    ADMISSION_COMPLETED,
    BOUNDARY_AGGREGATE_TYPE,
    CONSTRUCTION_FAILED,
    PACKAGE_FROZEN,
    REPLACEMENT_ABANDONED,
    SUPERSEDED,
)
from ouroboros.boundary.ledger import verify_boundary_order
from ouroboros.boundary.oracle_build import DECLARED_NOT_EXECUTABLE, package_from_reply
from ouroboros.boundary.package import seed_criterion_keys
from ouroboros.boundary.per_check import (
    NO_ADMITTED_REPRODUCTION_CHECK,
    PRESERVATION_FAILS_ON_BASE,
    REPRO_PASSES_ON_BASE,
)
from ouroboros.boundary.run_wiring import (
    CheckPackageSettings,
    prepare_check_package,
    render_preparation,
    verify_check_package,
)
from ouroboros.core.seed import Seed
from ouroboros.persistence.event_store import EventStore

from .clamp_fixtures import (
    BAD_PRESERVE_3,
    BAD_REPRO_2,
    BUGGY,
    FIXED,
    GOOD_PRESERVE_3,
    STALE_REASON,
    WILLING,
    _oracle,
    _seed,
)
from .clamp_fixtures import GOOD_REPRO_1 as _GOOD_REPRO_1


def _discriminating(oracle: dict[str, Any]) -> dict[str, Any]:
    """``oracle`` with a held-out case the base fails (above ``high``).

    Only a held-out case the base fails can verify a fix; a reproduction
    oracle whose every held-out case the base passes is excluded at
    admission (``held_out_not_discriminating``).
    """
    held = {
        "case_id": "held",
        "held_out": True,
        "args": {"value": 7, "low": -2, "high": 4},
        "expect": {"kind": "returns", "value": 4},
    }
    return {**oracle, "cases": [oracle["cases"][0], held]}


GOOD_REPRO_1 = _discriminating(_GOOD_REPRO_1)


class _Constructor:
    """``construct`` returns ``reply``; ``construct_replacements`` returns ``replacement``."""

    def __init__(
        self,
        seed: Seed,
        base: Path,
        reply: dict[str, Any],
        replacement: dict[str, Any] | None = None,
        *,
        replacement_fails: bool = False,
        replacements: bool = True,
    ) -> None:
        self.seed, self.base = seed, base
        self.reply, self.replacement = reply, replacement
        self.replacement_fails = replacement_fails
        self.construct_calls: list[dict[str, Any]] = []
        self.replacement_calls: list[dict[int, str]] = []
        if not replacements:
            self.construct_replacements = None  # type: ignore[assignment]

    def _outcome(self, reply: dict[str, Any]) -> ConstructionOutcome:
        package = package_from_reply(reply, self.seed, input_digest="1" * 64, generator="fake")
        return ConstructionOutcome(package, None, "1" * 64, "fake")

    async def construct(self, seed: Seed, base: Path, *, feedback=()):
        self.construct_calls.append({"feedback": list(feedback)})
        return self._outcome(self.reply)

    async def construct_replacements(self, seed: Seed, base: Path, *, targets):
        self.replacement_calls.append(dict(targets))
        if self.replacement_fails or self.replacement is None:
            return ConstructionOutcome(None, "constructor_timeout", "2" * 64, "fake")
        return self._outcome(self.replacement)


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


class _NoReplacement:
    """A constructor without a replacement call: the package admitted per check is final."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    async def construct(self, seed: Seed, base: Path, *, feedback=()) -> Any:
        return await self._inner.construct(seed, base, feedback=feedback)


async def _prepare(
    store: EventStore,
    repo: Path,
    tmp_path: Path,
    constructor: Any,
    seed: Seed,
    *,
    replacement: bool = True,
) -> Any:
    return await prepare_check_package(
        seed,
        event_store=store,
        constructor=constructor if replacement else _NoReplacement(constructor),
        execution_id="exec_u",
        base_checkout=repo,
        worker_workspace=repo,
        runtime_label="codex",
        settings=CheckPackageSettings(enabled=True),
        store_dir=tmp_path / "store",
    )


async def _types(store: EventStore, boundary_id: str) -> list[str]:
    return [event.type for event in await store.replay(BOUNDARY_AGGREGATE_TYPE, boundary_id)]


# ----------------------------------------------------------------------
# per-check admission


async def test_each_exclusion_reason_excludes_only_its_check(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    seed = _seed()
    reply = {"oracles": [GOOD_REPRO_1, BAD_REPRO_2, BAD_PRESERVE_3]}
    state = await _prepare(
        store,
        repo,
        tmp_path,
        _Constructor(seed, repo, reply),
        seed,
        replacement=False,
    )
    assert state.admitted and state.boundary_id == "exec_u/check_package/v1"
    assert state.admission.excluded_checks == {
        "oracle_2": REPRO_PASSES_ON_BASE,
        "oracle_3": PRESERVATION_FAILS_ON_BASE,
    }
    assert state.admission.check_tiers == {"oracle_1": "A", "oracle_2": "C", "oracle_3": "C"}
    assert state.exclusions == (
        ("exec_u/check_package/v1", "oracle_2", REPRO_PASSES_ON_BASE),
        ("exec_u/check_package/v1", "oracle_3", PRESERVATION_FAILS_ON_BASE),
    )
    # The journal records ids and reasons, never values.
    events = await store.replay(BOUNDARY_AGGREGATE_TYPE, state.boundary_id)
    admitted = next(event for event in events if event.type == ADMISSION_COMPLETED)
    assert admitted.data["verdict"] == "admitted"
    assert admitted.data["excluded_checks"] == state.admission.excluded_checks
    assert verify_boundary_order(events) == ()
    assert any("Checks excluded at admission" in line for line in render_preparation(state))
    # After the fix: the admitted check decides criterion 1, the others are uncovered.
    (repo / "mathutils.py").write_text(FIXED)
    verdict = await verify_check_package(
        state,
        event_store=store,
        candidate_checkout=repo,
    )
    keys = seed_criterion_keys(seed)
    assert [(verdict.verdicts[k].status, verdict.verdicts[k].reason) for k in keys] == [
        (PackageCriterionStatus.PASS, "passed"),
        (PackageCriterionStatus.UNCOVERED, f"uncovered:{REPRO_PASSES_ON_BASE}"),
        (PackageCriterionStatus.UNCOVERED, f"uncovered:{PRESERVATION_FAILS_ON_BASE}"),
    ]
    # The CLI's "Criteria without a check" line counts them too.
    assert set(verdict.uncovered) == {keys[1], keys[2]}
    # The excluded checks never ran on the candidate.
    ran = {
        check["check_id"]
        for check in _verified(await store.replay(BOUNDARY_AGGREGATE_TYPE, state.boundary_id))
    }
    assert ran == {"oracle_1"}


def _verified(events: list[Any]) -> list[dict[str, Any]]:
    return next(e for e in events if e.type == "boundary.candidate.verified").data["checks"]


async def test_a_criterion_keeps_authority_while_one_admitted_check_covers_it(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    seed = _seed("clamp(15, 0, 10) returns 10")
    extra = _oracle(1, "oracle_1b", "reproduction", (5, 0, 10), 5)  # passes on base: oracle_1_2
    state = await _prepare(
        store,
        repo,
        tmp_path,
        _Constructor(seed, repo, {"oracles": [GOOD_REPRO_1, extra]}),
        seed,
    )
    assert state.admission.excluded_checks == {"oracle_1_2": REPRO_PASSES_ON_BASE}
    assert state.replacement_calls == 0  # nothing left without an admitted check
    (repo / "mathutils.py").write_text(FIXED)
    verdict = await verify_check_package(state, event_store=store, candidate_checkout=repo)
    (item,) = verdict.verdicts.values()
    assert item.status is PackageCriterionStatus.PASS and item.tier is CheckTier.A


async def test_losing_the_last_reproduction_check_leaves_the_criterion_uncovered(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    seed = _seed("clamp(5, 0, 10) returns 5")
    repro = _oracle(1, "oracle_r", "reproduction", (5, 0, 10), 5)  # passes on base: oracle_1
    keep = _oracle(1, "oracle_p", "preservation", (-5, 0, 10), 0)  # passes on base: oracle_1_2
    state = await _prepare(
        store,
        repo,
        tmp_path,
        _Constructor(seed, repo, {"oracles": [repro, keep]}),
        seed,
        replacement=False,
    )
    assert state.admitted
    assert state.admission.excluded_checks == {"oracle_1": REPRO_PASSES_ON_BASE}
    (repo / "mathutils.py").write_text(FIXED)
    verdict = await verify_check_package(state, event_store=store, candidate_checkout=repo)
    (item,) = verdict.verdicts.values()
    assert item.status is PackageCriterionStatus.UNCOVERED
    assert item.reason == f"uncovered:{NO_ADMITTED_REPRODUCTION_CHECK}"


# ----------------------------------------------------------------------
# One replacement call (product policy)


async def test_one_replacement_call_supersedes_the_version_before_dispatch(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    seed = _seed()
    constructor = _Constructor(
        seed,
        repo,
        {"oracles": [GOOD_REPRO_1, BAD_REPRO_2, GOOD_PRESERVE_3]},
        {"oracles": [_discriminating(_oracle(2, "r2_repro", "reproduction", (20, 0, 10), 10))]},
    )
    state = await _prepare(store, repo, tmp_path, constructor, seed)
    assert constructor.replacement_calls == [
        {2: "its reproduction check passes on the base code, so it does not reproduce the bug"}
    ]
    assert state.boundary_id == "exec_u/check_package/v2"
    assert state.replacement_calls == 1 and state.replacement_outcome == "admitted"
    assert state.admitted and state.admission.excluded_checks is None
    assert {check.check_id for check in state.package.checks} == {
        "oracle_1",
        "oracle_2",  # the replacement's oracle for criterion 2
        "oracle_3",
    }
    v1 = await store.replay(BOUNDARY_AGGREGATE_TYPE, "exec_u/check_package/v1")
    v2 = await store.replay(BOUNDARY_AGGREGATE_TYPE, "exec_u/check_package/v2")
    assert [e.type for e in v1] == [PACKAGE_FROZEN, ADMISSION_COMPLETED, SUPERSEDED]
    assert [e.type for e in v2] == [PACKAGE_FROZEN, ADMISSION_COMPLETED, ACTOR_STARTED]
    superseded = v1[-1]
    assert superseded.data["superseded_by"] == "exec_u/check_package/v2"
    assert superseded.data["reason"] == "replacement_checks"
    # The new package id is recorded before the worker start.
    assert v2[0].timestamp < v2[-1].timestamp
    assert "package_id" in v2[0].data
    assert verify_boundary_order(v1) == () and verify_boundary_order(v2) == ()
    (repo / "mathutils.py").write_text(FIXED)
    verdict = await verify_check_package(state, event_store=store, candidate_checkout=repo)
    # Criterion 3 has a preservation check only: it passes, and verifies nothing.
    assert [(v.status, v.reason) for v in verdict.verdicts.values()] == [
        (PackageCriterionStatus.PASS, "passed"),
        (PackageCriterionStatus.PASS, "passed"),
        (PackageCriterionStatus.UNVERIFIED, "no_reproduction_check"),
    ]
    assert state.replacement_calls == 1 and len(state.exclusions) == 1


async def test_a_failed_replacement_call_keeps_the_admitted_version(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    seed = _seed()
    constructor = _Constructor(
        seed,
        repo,
        {"oracles": [GOOD_REPRO_1, BAD_REPRO_2, GOOD_PRESERVE_3]},
        replacement_fails=True,
    )
    state = await _prepare(store, repo, tmp_path, constructor, seed)
    assert len(constructor.replacement_calls) == 1
    assert state.boundary_id == "exec_u/check_package/v1" and state.admitted
    assert state.replacement_calls == 1 and state.replacement_outcome == "construction_failed"
    # The failed replacement is closed in favor of v1: a version is
    # superseded only by a later one, never by the version before it.
    assert await _types(store, "exec_u/check_package/v2") == [
        CONSTRUCTION_FAILED,
        REPLACEMENT_ABANDONED,
    ]
    assert await _types(store, "exec_u/check_package/v1") == [
        PACKAGE_FROZEN,
        ADMISSION_COMPLETED,
        ACTOR_STARTED,
    ]
    assert any("v2" in line for line in render_preparation(state) if "not bound" in line)


# ----------------------------------------------------------------------
# Routing: an admitted check decides; labels and reasons never route


async def test_an_uncovered_reason_never_skips_the_replacement_call(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    """A criterion left uncovered with any reason is a replacement target.

    Still uncovered after the replacement call, it is uncovered in the
    verdict, the legacy verifier decides it, and coverage counts it.
    """
    seed = _seed()
    reply = {
        "oracles": [GOOD_REPRO_1, GOOD_PRESERVE_3],
        "uncovered": [{"criterion": 2, "reason": STALE_REASON}],
        "labels": [{"criterion": 2, "kind": "context", "evidence_span": "returns 5"}],
    }
    replacement = {"uncovered": [{"criterion": 2, "reason": STALE_REASON}]}
    constructor = _Constructor(seed, repo, reply, replacement)
    state = await _prepare(store, repo, tmp_path, constructor, seed)
    assert len(constructor.construct_calls) == 1
    assert constructor.replacement_calls == [{2: why_excluded(DECLARED_NOT_EXECUTABLE)}]
    assert state.replacement_calls == 1 and state.replacement_outcome == "construction_failed"
    assert state.admitted and state.boundary_id == "exec_u/check_package/v1"
    keys = seed_criterion_keys(seed)
    assert {item.criterion_key for item in state.package.uncovered} == {keys[1]}
    assert not any("labels" in line.lower() for line in render_preparation(state))
    (repo / "mathutils.py").write_text(FIXED)
    verdict = await verify_check_package(state, event_store=store, candidate_checkout=repo)
    assert verdict.uncovered == (keys[1],)
    assert [verdict.verdicts[k].status for k in keys] == [
        PackageCriterionStatus.PASS,
        PackageCriterionStatus.UNCOVERED,
        PackageCriterionStatus.UNVERIFIED,  # preservation only
    ]
    rejected = {1: ExistingOutcome(1, "failed", "failed", "failed")}
    decided = reconcile_acceptance(
        keys,
        verdict.verdicts,
        rejected,
        existing_run_accepted=True,
        legacy_decides_unverified=True,
    )
    assert [(d.accepted, d.governed_by.value) for d in decided.decisions] == [
        (True, "check_package"),
        (False, "existing_verifier"),
        (True, "check_package"),
    ]
    assert [d.criterion_key for d in decided.not_package_decided] == [keys[1], keys[2]]
    assert decided.coverage is VerificationCoverage.LOW
    assert state.replacement_calls == 1 and state.exclusions == ()


async def test_a_criterion_with_a_working_check_is_never_stripped(
    store: EventStore, repo: Path, tmp_path: Path
) -> None:
    """A label in the reply (any kind) never removes a check the constructor wrote."""
    seed = _seed("clamp(15, 0, 10) returns 10", WILLING)
    keep = _oracle(2, "oracle_2", "preservation", (-5, 0, 10), 0)
    reply = {
        "oracles": [GOOD_REPRO_1, keep],
        "labels": [
            {"criterion": 1, "kind": "behavior", "evidence_span": "returns 10"},
            {"criterion": 2, "kind": "context", "evidence_span": "willing to assist"},
        ],
    }
    constructor = _Constructor(seed, repo, reply)
    state = await _prepare(store, repo, tmp_path, constructor, seed)
    assert len(constructor.construct_calls) == 1
    assert {check.check_id for check in state.package.checks} == {"oracle_1", "oracle_2"}
    assert state.package.uncovered == ()
    assert constructor.replacement_calls == [] and state.replacement_calls == 0
    (repo / "mathutils.py").write_text(FIXED)
    verdict = await verify_check_package(state, event_store=store, candidate_checkout=repo)
    assert verdict.uncovered == ()
    # Kept, and run: the preservation check passes (unverified, never stripped).
    assert [(v.status, v.reason) for v in verdict.verdicts.values()] == [
        (PackageCriterionStatus.PASS, "passed"),
        (PackageCriterionStatus.UNVERIFIED, "no_reproduction_check"),
    ]


# ----------------------------------------------------------------------
# Coverage flag and flag-off identity
