"""EventStore ordering: package digest persisted before any actor starts."""

from __future__ import annotations

from pathlib import Path

import pytest

from ouroboros.boundary import (
    ArtifactRef,
    BoundaryLeakError,
    BoundaryLedger,
    BoundaryOrderError,
    admit_check_package,
    select_incumbent,
    tree_digest,
    verify_boundary_order,
    verify_candidate,
)
from ouroboros.boundary.events import (
    ACTOR_STARTED,
    ADMISSION_COMPLETED,
    PACKAGE_FROZEN,
    SELECTION_DECIDED,
)
from ouroboros.persistence.event_store import EventStore

from .conftest import INPUT_DIGEST, REPRO_SCRIPT, SIGNATURE, build_package


@pytest.fixture
async def store():
    event_store = EventStore("sqlite+aiosqlite:///:memory:")
    await event_store.initialize()
    yield event_store
    await event_store.close()


@pytest.fixture
async def admission(tmp_path: Path, base_checkout, package):
    return await admit_check_package(package, base_checkout, work_dir=tmp_path / "adm")


async def test_package_hash_event_precedes_actor_start(
    store, tmp_path: Path, seed, package, admission
) -> None:
    ledger = BoundaryLedger(store)
    frozen = await ledger.record_package_frozen("task-1/V1", package, seed=seed)
    await ledger.record_admission("task-1/V1", admission)
    workspace = tmp_path / "worker"
    workspace.mkdir()
    (workspace / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    started = await ledger.record_actor_started(
        "actor-1", ["task-1/V1"], workspace=workspace, runtime="codex"
    )

    events = await ledger.events("task-1/V1")
    assert [e.type for e in events] == [PACKAGE_FROZEN, ADMISSION_COMPLETED, ACTOR_STARTED]
    assert frozen.data["package_sha256"] == package.sha256
    assert len(frozen.data["package_sha256"]) == 64
    assert events[0].timestamp < events[-1].timestamp
    assert started[0].data["package_sha256"] == package.sha256
    assert verify_boundary_order(events) == ()
    # The journal carries digests, never check code or argv.
    journal = repr([e.data for e in events])
    assert SIGNATURE not in journal
    assert REPRO_SCRIPT not in journal


async def test_actor_cannot_start_before_seal_or_admission(store, package) -> None:
    ledger = BoundaryLedger(store)
    with pytest.raises(BoundaryOrderError, match="sealed"):
        await ledger.record_actor_started("actor-1", ["task-1/V1"])
    await ledger.record_package_frozen("task-1/V1", package)
    with pytest.raises(BoundaryOrderError, match="admission"):
        await ledger.record_actor_started("actor-1", ["task-1/V1"])
    assert all(e.type != ACTOR_STARTED for e in await ledger.events("task-1/V1"))


async def test_actor_waits_for_every_bound_boundary(store, package, admission) -> None:
    ledger = BoundaryLedger(store)
    await ledger.record_package_frozen("task-1/V0", package)
    await ledger.record_admission("task-1/V0", admission)
    with pytest.raises(BoundaryOrderError):
        await ledger.record_actor_started("actor-1", ["task-1/V0", "task-1/V1"])
    await ledger.record_construction_failed(
        "task-1/V1", seed_digest=package.seed_digest, input_digest=INPUT_DIGEST, reason="parse"
    )
    started = await ledger.record_actor_started("actor-1", ["task-1/V0", "task-1/V1"])
    assert [e.data["package_sha256"] for e in started] == [package.sha256, None]


async def test_package_cannot_be_regenerated_after_seal(store, seed, package) -> None:
    ledger = BoundaryLedger(store)
    await ledger.record_package_frozen("task-1/V1", package)
    regenerated = build_package(seed, repro_script=REPRO_SCRIPT + "# retry\n")
    with pytest.raises(BoundaryOrderError, match="regenerated"):
        await ledger.record_package_frozen("task-1/V1", regenerated)


async def test_admission_must_cite_frozen_digest_and_is_single(
    store, seed, package, admission
) -> None:
    ledger = BoundaryLedger(store)
    other = build_package(seed, repro_script=REPRO_SCRIPT + "# other\n")
    await ledger.record_package_frozen("task-1/V1", other)
    with pytest.raises(BoundaryOrderError, match="different package"):
        await ledger.record_admission("task-1/V1", admission)

    await ledger.record_package_frozen("task-2/V1", package)
    await ledger.record_admission("task-2/V1", admission)
    with pytest.raises(BoundaryOrderError, match="already recorded"):
        await ledger.record_admission("task-2/V1", admission)


async def test_workspace_with_generated_check_code_is_refused(
    store, tmp_path: Path, package, admission
) -> None:
    ledger = BoundaryLedger(store)
    await ledger.record_package_frozen("task-1/V1", package)
    await ledger.record_admission("task-1/V1", admission)
    workspace = tmp_path / "worker"
    workspace.mkdir()
    (workspace / "hidden_copy.py").write_text(REPRO_SCRIPT)
    with pytest.raises(BoundaryLeakError):
        await ledger.record_actor_started("actor-1", ["task-1/V1"], workspace=workspace)


async def test_selection_event_records_reason_and_digests(
    store, tmp_path: Path, base_checkout, package, admission
) -> None:
    ledger = BoundaryLedger(store)
    await ledger.record_package_frozen("task-1/V1", package)
    await ledger.record_admission("task-1/V1", admission)
    await ledger.record_actor_started("actor-1", ["task-1/V1"])
    verification = await verify_candidate(package, base_checkout, work_dir=tmp_path / "v")
    await ledger.record_candidate_verification("task-1/V1", verification)
    incumbent = ArtifactRef(
        artifact_id="incumbent", tree_digest="a" * 64, seed_digest=package.seed_digest
    )
    candidate = ArtifactRef(
        artifact_id="repair",
        tree_digest=tree_digest(base_checkout),
        seed_digest=package.seed_digest,
    )
    decision = select_incumbent(
        incumbent=incumbent,
        candidate=candidate,
        package=package,
        admission=admission,
        verification=verification,
    )
    event = await ledger.record_selection("task-1/V1", decision)

    assert event.type == SELECTION_DECIDED
    assert event.data["reason"] == "candidate_failed"
    assert event.data["selected"]["tree_digest"] == "a" * 64
    assert event.data["candidate"]["tree_digest"] == candidate.tree_digest
    assert event.data["package_sha256"] == package.sha256
    assert verify_boundary_order(await ledger.events("task-1/V1")) == ()


def test_verify_boundary_order_flags_actor_before_seal(package) -> None:
    from ouroboros.boundary.events import actor_started_event, package_frozen_event

    actor = actor_started_event("b", actor_id="a", package_sha256=None, runtime=None)
    frozen = package_frozen_event("b", package)
    violations = verify_boundary_order([actor, frozen])
    assert "actor started before the seal" in violations
