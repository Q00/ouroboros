"""EventStore ordering: package digest persisted before any actor starts."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError
import pytest

from ouroboros.boundary.binding import CheckTier
from ouroboros.boundary.events import (
    ACTOR_STARTED,
    ADMISSION_COMPLETED,
    CANDIDATE_VERIFIED,
    CHECK_PACKAGE_ENABLED,
    PACKAGE_FROZEN,
    BindingsPayload,
    ReconciliationPayload,
    ReferenceCheckPayload,
    RunContract,
    acceptance_reconciled_event,
    actor_started_event,
    admission_completed_event,
    binding_recorded_event,
    boundary_version_id,
    package_frozen_event,
    superseded_event,
)
from ouroboros.boundary.ledger import (
    BoundaryLeakError,
    BoundaryLedger,
    BoundaryOrderError,
    RecoveryBound,
    RecoveryOff,
    RecoveryUndecidable,
    admitted_exclusions,
    frozen_manifest,
    recovery_projection,
    verify_boundary_order,
)
from ouroboros.boundary.package import CheckPackageError, seal_package, seed_criterion_keys
from ouroboros.boundary.receipts import (
    AdmissionResult,
    CandidateVerification,
    write_receipt,
)
from ouroboros.events.base import BaseEvent
from ouroboros.persistence.event_store import EventStore

from .conftest import INPUT_DIGEST, REPRO_SCRIPT, SIGNATURE, build_package, make_seed
from .journal_fixtures import (
    admission_receipt,
    candidate_execution,
    criterion,
    decision_data,
    final_bindings,
    verification_receipt,
)
from .test_package_identity import held_out_package

CONTRACT = RunContract(check_timeout_seconds=120)
KEYS = seed_criterion_keys(make_seed())
"""The criteria of the ``package`` fixture (its frozen manifest's keys)."""


@pytest.fixture
async def store():
    event_store = EventStore("sqlite+aiosqlite:///:memory:")
    await event_store.initialize()
    yield event_store
    await event_store.close()


@pytest.fixture
def admission(base_checkout, package):
    return admission_receipt(package, base_checkout)


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
        "actor-1", ["task-1/V1"], workspace=workspace, runtime="codex", packages=[package]
    )

    events = await ledger.events("task-1/V1")
    assert [e.type for e in events] == [PACKAGE_FROZEN, ADMISSION_COMPLETED, ACTOR_STARTED]
    assert frozen.data["package_id"] == package.package_id
    assert frozen.data["seed_digest"] == package.seed_digest
    assert events[0].timestamp < events[-1].timestamp
    assert started[0].data["package_id"] == package.package_id
    assert verify_boundary_order(events) == ()
    # The journal carries ids and digests, never check code or argv.
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
    assert [e.data["package_id"] for e in started] == [package.package_id, None]


async def test_package_cannot_be_regenerated_after_seal(store, seed, package) -> None:
    ledger = BoundaryLedger(store)
    await ledger.record_package_frozen("task-1/V1", package)
    regenerated = seal_package(build_package(seed, repro_script=REPRO_SCRIPT + "# retry\n"))
    with pytest.raises(BoundaryOrderError, match="regenerated"):
        await ledger.record_package_frozen("task-1/V1", regenerated)


async def test_admission_must_cite_frozen_digest_and_is_single(
    store, seed, package, admission
) -> None:
    ledger = BoundaryLedger(store)
    other = seal_package(build_package(seed, repro_script=REPRO_SCRIPT + "# other\n"))
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
        await ledger.record_actor_started(
            "actor-1", ["task-1/V1"], workspace=workspace, packages=[package]
        )


async def test_candidate_verification_cites_the_frozen_package(
    store, tmp_path: Path, base_checkout, package, admission
) -> None:
    ledger = BoundaryLedger(store)
    await ledger.record_package_frozen("task-1/V1", package)
    await ledger.record_admission("task-1/V1", admission)
    await ledger.record_actor_started("actor-1", ["task-1/V1"])
    await ledger.record_bindings(
        "task-1/V1", package_id=package.package_id, payload=final_bindings(package)
    )
    verification = verification_receipt(package, base_checkout)
    event = await ledger.record_candidate_verification("task-1/V1", verification)

    assert event.type == CANDIDATE_VERIFIED
    assert event.data["verdict"] == "fail"
    assert verify_boundary_order(await ledger.events("task-1/V1")) == ()


def test_verify_boundary_order_flags_actor_before_seal(package) -> None:
    from ouroboros.boundary.events import actor_started_event, package_frozen_event

    actor = actor_started_event("b", actor_id="a", package_id=None, runtime=None)
    frozen = package_frozen_event("b", package)
    violations = verify_boundary_order([actor, frozen])
    assert any(item.startswith("actor started before the seal") for item in violations)


async def test_the_enabled_record_is_written_once_per_run_and_needs_an_execution_id(
    store: EventStore,
) -> None:
    ledger = BoundaryLedger(store)
    assert not await ledger.check_package_enabled("exec_enabled")
    event = await ledger.record_check_package_enabled("exec_enabled", CONTRACT)
    assert event.type == CHECK_PACKAGE_ENABLED and event.aggregate_id == "exec_enabled"
    assert await ledger.check_package_enabled("exec_enabled")
    with pytest.raises(BoundaryOrderError):
        await ledger.record_check_package_enabled("exec_enabled", CONTRACT)
    with pytest.raises(BoundaryOrderError):
        await ledger.record_check_package_enabled("", CONTRACT)


def test_a_payload_cannot_name_another_package_or_carry_undefined_fields(package) -> None:
    # The bot's probe: a caller-supplied payload overwrote the cited package id.
    for model in (BindingsPayload, ReconciliationPayload, ReferenceCheckPayload):
        with pytest.raises(ValidationError):
            model.model_validate({"package_id": "forged"})
    with pytest.raises(ValidationError):
        BindingsPayload.model_validate({"phase": "final", "checks": [], "package_id": "forged"})
    with pytest.raises(ValidationError):
        BindingsPayload.model_validate({"phase": "final", "checks": [], "held_out_value": 7})
    with pytest.raises(TypeError):
        binding_recorded_event(
            "b",
            package_id=package.package_id,
            payload={"phase": "final", "checks": [], "package_id": "forged"},  # type: ignore[arg-type]
        )
    event = binding_recorded_event(
        "b", package_id=package.package_id, payload=BindingsPayload(phase="final", checks=())
    )
    assert event.data["package_id"] == package.package_id


async def test_an_admission_receipt_for_another_seed_is_refused(store, package, admission) -> None:
    ledger = BoundaryLedger(store)
    await ledger.record_package_frozen("task-1/V1", package)
    foreign = admission.model_copy(update={"seed_digest": "f" * 64})
    with pytest.raises(BoundaryOrderError, match="different Seed"):
        await ledger.record_admission("task-1/V1", foreign)
    await ledger.record_admission("task-1/V1", admission)


async def test_a_superseded_version_accepts_no_actor_start(
    store, seed, base_checkout, package, admission
) -> None:
    # The bot's probe: V1 frozen and admitted, superseded by V2, then an
    # actor start on V1. The write is refused, and replay flags a journal
    # that holds one anyway.
    ledger = BoundaryLedger(store)
    await ledger.record_check_package_enabled("exec_s", CONTRACT)
    v1, v2 = boundary_version_id("exec_s", 1), boundary_version_id("exec_s", 2)
    await ledger.record_package_frozen(v1, package)
    await ledger.record_admission(v1, admission)
    successor = seal_package(build_package(seed, repro_script=REPRO_SCRIPT + "# v2\n"))
    await ledger.record_package_frozen(v2, successor)
    await ledger.record_admission(v2, admission_receipt(successor, base_checkout))
    await ledger.record_superseded(v1, superseded_by=v2, reason="replacement_checks")
    with pytest.raises(BoundaryOrderError, match="superseded"):
        await ledger.record_actor_started("exec_s", [v1])
    assert verify_boundary_order(await ledger.events(v1)) == ()
    await store.append(
        actor_started_event(v1, actor_id="exec_s", package_id=package.package_id, runtime=None)
    )
    violations = verify_boundary_order(await ledger.events(v1))
    assert f"{ACTOR_STARTED} recorded on a superseded boundary version" in violations
    await ledger.record_actor_started("exec_s", [v2])


def test_replay_flags_an_admission_before_the_seal(package, admission) -> None:
    violations = verify_boundary_order(
        [admission_completed_event("b", admission), package_frozen_event("b", package)]
    )
    assert any(item.startswith("admission recorded before the seal") for item in violations)


async def test_the_enabled_record_carries_the_run_contract(store: EventStore) -> None:
    ledger = BoundaryLedger(store)
    assert await ledger.run_contract("exec_contract") is None  # never on
    contract = RunContract(check_timeout_seconds=37)
    event = await ledger.record_check_package_enabled("exec_contract", contract)
    assert event.data["contract"] == {"check_timeout_seconds": 37}
    assert await ledger.run_contract("exec_contract") == contract
    with pytest.raises(TypeError):
        await ledger.record_check_package_enabled("exec_other", {"check_timeout_seconds": 1})  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        RunContract(check_timeout_seconds=0)
    with pytest.raises(ValueError):
        RunContract.model_validate({"check_timeout_seconds": 5, "extra": 1})


async def test_a_malformed_run_contract_is_refused_on_read(store: EventStore) -> None:
    from ouroboros.events.base import BaseEvent

    await store.append(
        BaseEvent(
            type=CHECK_PACKAGE_ENABLED,
            aggregate_type="boundary",
            aggregate_id="exec_bad",
            data={"execution_id": "exec_bad"},  # no contract: the run's settings are unknown
        )
    )
    with pytest.raises(BoundaryOrderError):
        await BoundaryLedger(store).run_contract("exec_bad")


async def test_the_enabled_record_comes_before_every_version_of_the_run(store, package) -> None:
    ledger = BoundaryLedger(store)
    v1 = boundary_version_id("exec_e", 1)
    with pytest.raises(BoundaryOrderError, match="enabled record"):
        await ledger.record_package_frozen(v1, package)
    # A version that reached the journal first (written around the ledger)
    # keeps the enabled record out, and replay against the run flags it.
    await store.append(package_frozen_event(v1, package))
    with pytest.raises(BoundaryOrderError, match="precede"):
        await ledger.record_check_package_enabled("exec_e", CONTRACT)
    assert "the run has no enabled record" in verify_boundary_order(
        await ledger.events(v1), run_events=await ledger.events("exec_e")
    )
    # A standalone boundary (not a version of a run) needs no enabled record.
    await ledger.record_package_frozen("task-9/V1", package)


async def test_the_enabled_record_sees_every_version_not_only_the_first(store, package) -> None:
    # #2458 round 3: only v1 was inspected, so a v2 written around the ledger
    # let the enabled record in, and replay then flagged a journal the write
    # path had accepted.
    ledger = BoundaryLedger(store)
    v2 = boundary_version_id("exec_gap", 2)
    await store.append(package_frozen_event(v2, package))
    assert list(await ledger.run_versions("exec_gap")) == [2]
    with pytest.raises(BoundaryOrderError, match="precede"):
        await ledger.record_check_package_enabled("exec_gap", CONTRACT)
    # Other runs' versions are not this run's.
    await store.append(package_frozen_event(boundary_version_id("exec_gap_other", 1), package))
    assert list(await ledger.run_versions("exec_gap")) == [2]
    await ledger.record_check_package_enabled("exec_fresh", CONTRACT)


async def test_a_version_is_superseded_only_by_a_later_version_of_its_own_run(
    store, seed, base_checkout, package, admission
) -> None:
    # The round-2 probe: run_b's version was accepted as the successor of
    # run_a's, and a version could point back to an earlier one.
    ledger = BoundaryLedger(store)
    a1, a2 = boundary_version_id("run_a", 1), boundary_version_id("run_a", 2)
    b1 = boundary_version_id("run_b", 1)
    for run in ("run_a", "run_b"):
        await ledger.record_check_package_enabled(run, CONTRACT)
    for version in (a1, a2, b1):
        await ledger.record_construction_failed(
            version, seed_digest=package.seed_digest, input_digest=INPUT_DIGEST, reason="x"
        )
    with pytest.raises(BoundaryOrderError, match="its own run"):
        await ledger.record_superseded(a1, superseded_by=b1, reason="x")
    with pytest.raises(BoundaryOrderError, match="later version"):
        await ledger.record_superseded(a2, superseded_by=a1, reason="x")
    with pytest.raises(BoundaryOrderError, match="version of a run"):
        await ledger.record_superseded("task-1/V1", superseded_by=a2, reason="x")
    await ledger.record_superseded(a1, superseded_by=a2, reason="x")
    # Replay applies the same rule to a journal written around the ledger.
    forged = superseded_event(
        a2, superseded_by=b1, package_id=None, successor_package_id=None, reason="x"
    )
    await store.append(forged)
    assert "a boundary version is superseded only within its own run" in verify_boundary_order(
        await ledger.events(a2)
    )


async def test_a_version_of_a_run_binds_only_that_runs_worker(store, package) -> None:
    ledger = BoundaryLedger(store)
    await ledger.record_check_package_enabled("run_a", CONTRACT)
    a1 = boundary_version_id("run_a", 1)
    await ledger.record_construction_failed(
        a1, seed_digest=package.seed_digest, input_digest=INPUT_DIGEST, reason="x"
    )
    with pytest.raises(BoundaryOrderError, match="that run's worker"):
        await ledger.record_actor_started("run_b", [a1])
    await ledger.record_actor_started("run_a", [a1])


async def test_a_candidate_verification_for_another_seed_is_refused(
    store, base_checkout, package, admission
) -> None:
    ledger = BoundaryLedger(store)
    await ledger.record_package_frozen("task-1/V1", package)
    await ledger.record_admission("task-1/V1", admission)
    await ledger.record_actor_started("actor-1", ["task-1/V1"])
    await ledger.record_bindings(
        "task-1/V1", package_id=package.package_id, payload=final_bindings(package)
    )
    verification = verification_receipt(package, base_checkout)
    foreign = verification.model_copy(update={"seed_digest": "f" * 64})
    with pytest.raises(BoundaryOrderError, match="different Seed"):
        await ledger.record_candidate_verification("task-1/V1", foreign)
    event = await ledger.record_candidate_verification("task-1/V1", verification)
    assert (event.data["seed_digest"], event.data["package_id"]) == (
        package.seed_digest,
        package.package_id,
    )


def test_receipts_are_closed_and_name_their_seed(base_checkout, package) -> None:
    # The round-2 probe: a supplied seed_digest was silently dropped.
    verification = verification_receipt(package, base_checkout)
    data = verification.model_dump()
    data["package_sha256"] = package.sha256
    with pytest.raises(ValidationError):
        CandidateVerification.model_validate({**data, "unknown": 1})
    with pytest.raises(ValidationError):
        CandidateVerification.model_validate({k: v for k, v in data.items() if k != "seed_digest"})
    with pytest.raises(ValidationError):
        CandidateVerification.model_validate({**data, "seed_digest": "not a digest"})
    admission = admission_receipt(package, base_checkout).model_dump()
    admission["package_sha256"] = package.sha256
    with pytest.raises(ValidationError):
        AdmissionResult.model_validate({**admission, "unknown": 1})
    with pytest.raises(ValidationError):
        AdmissionResult.model_validate({k: v for k, v in admission.items() if k != "package_id"})
    assert verification.event_summary()["seed_digest"] == package.seed_digest


async def test_an_undecided_resume_is_recorded_only_for_a_run_that_was_on(store) -> None:
    from ouroboros.boundary.events import ResumedPayload

    ledger = BoundaryLedger(store)
    payload = ResumedPayload.model_validate(
        decision_data([], source="none", reason="boundary_record_missing")
    )
    with pytest.raises(BoundaryOrderError, match="was on"):
        await ledger.record_resumed_undecided("run_x", payload=payload)
    await ledger.record_check_package_enabled("run_x", CONTRACT)
    await ledger.record_resumed_undecided("run_x", payload=payload)


def _decision(*statuses: str, undecided: str | None = None) -> ReconciliationPayload:
    """One status per criterion of the frozen manifest (one status: the same for all)."""
    statuses = statuses * len(KEYS) if len(statuses) == 1 else statuses
    extra = {} if undecided is None else {"undecided_reason": undecided}
    return ReconciliationPayload.model_validate(
        decision_data(
            [
                criterion(index, key, status)
                for index, (key, status) in enumerate(zip(KEYS, statuses, strict=True))
            ],
            **extra,
        )
    )


async def _started(store, package, admission) -> BoundaryLedger:
    ledger = BoundaryLedger(store)
    await ledger.record_package_frozen("task-1/V1", package)
    await ledger.record_admission("task-1/V1", admission)
    await ledger.record_actor_started("actor-1", ["task-1/V1"])
    return ledger


async def test_a_verified_decision_needs_the_candidate_verification_of_runnable_bindings(
    store, package, admission, base_checkout
) -> None:
    # The #2465 probe: final bindings with a runnable (tier A) check, then the
    # verification raised; a reconciliation claiming a verified status must
    # not follow the bindings alone. Refused on write, flagged on replay.
    ledger = await _started(store, package, admission)
    await ledger.record_bindings(
        "task-1/V1", package_id=package.package_id, payload=final_bindings(package)
    )
    for status in ("pass", "fail", "unverified"):
        with pytest.raises(BoundaryOrderError, match="acceptance must cite a verification"):
            await ledger.record_acceptance_reconciled(
                "task-1/V1", package_id=package.package_id, reconciliation=_decision(status)
            )
    forged = acceptance_reconciled_event(
        "task-1/V1", package_id=package.package_id, reconciliation=_decision("pass")
    )
    await store.append(forged)
    assert any(
        "acceptance must cite a verification" in item
        for item in verify_boundary_order(await ledger.events("task-1/V1"))
    )


async def test_an_undecided_decision_after_a_failed_verification_is_recorded(
    store, package, admission
) -> None:
    # The authority's fail-closed shape: covered criteria indeterminate, stated
    # as undecided. It needs no candidate verification, and cannot smuggle a
    # verified status.
    ledger = await _started(store, package, admission)
    await ledger.record_bindings(
        "task-1/V1", package_id=package.package_id, payload=final_bindings(package)
    )
    with pytest.raises(ValueError, match="undecided decision carries only"):
        _decision("pass", undecided="authority_error:OSError")
    await ledger.record_acceptance_reconciled(
        "task-1/V1",
        package_id=package.package_id,
        reconciliation=_decision("indeterminate", undecided="authority_error:OSError"),
    )
    assert verify_boundary_order(await ledger.events("task-1/V1")) == ()


async def test_an_undecided_decision_that_accepts_an_indeterminate_criterion_is_refused(
    store, package, admission
) -> None:
    # #2465 round 2: the undecided exception must not admit an accepted
    # indeterminate criterion. The payload model refuses it, and a payload
    # built without validation is refused on write and flagged on replay.
    ledger = await _started(store, package, admission)
    await ledger.record_bindings(
        "task-1/V1", package_id=package.package_id, payload=final_bindings(package)
    )
    valid = _decision("indeterminate", undecided="authority_error:OSError")
    accepted = valid.criteria[0].model_copy(update={"accepted": True})
    raw = valid.model_dump(mode="json")
    raw["criteria"][0]["accepted"] = True
    raw["run_accepted"] = True
    with pytest.raises(ValueError, match="acceptance disagrees"):
        ReconciliationPayload.model_validate(raw)
    smuggled = ReconciliationPayload.model_construct(
        **{**dict(valid), "criteria": (accepted,), "run_accepted": True}
    )
    with pytest.raises(BoundaryOrderError, match="disagrees with the statuses"):
        await ledger.record_acceptance_reconciled(
            "task-1/V1", package_id=package.package_id, reconciliation=smuggled
        )
    await store.append(
        acceptance_reconciled_event(
            "task-1/V1", package_id=package.package_id, reconciliation=smuggled
        )
    )
    assert any(
        "disagrees with the statuses" in item
        for item in verify_boundary_order(await ledger.events("task-1/V1"))
    )


async def test_bindings_with_no_runnable_check_may_be_followed_by_an_unrun_decision(
    store, base_checkout
) -> None:
    # An oracle whose default binding did not resolve on the base (admitted
    # tier U) and no declared binding: nothing runs, the criterion is unverified.
    package = seal_package(held_out_package())
    (key,) = package.criterion_keys
    admission = admission_receipt(package, base_checkout)
    admission = admission.model_copy(update={"check_tiers": {"oracle_1": CheckTier.U}})
    ledger = await _started(store, package, admission)
    unbound = {
        "criterion_key": key,
        "check_id": "oracle_1",
        "tier": "U",
        "binding_source": None,
        "binding": None,
        "status_hint": "unverified",
        "reason": "no_binding",
        "declared": None,
    }
    payload = BindingsPayload.model_validate({"phase": "final", "checks": [unbound]})
    await ledger.record_bindings("task-1/V1", package_id=package.package_id, payload=payload)

    def decided(status: str) -> ReconciliationPayload:
        return ReconciliationPayload.model_validate(decision_data([criterion(0, key, status)]))

    with pytest.raises(BoundaryOrderError, match="claims a verified status"):
        await ledger.record_acceptance_reconciled(
            "task-1/V1", package_id=package.package_id, reconciliation=decided("pass")
        )
    await ledger.record_acceptance_reconciled(
        "task-1/V1", package_id=package.package_id, reconciliation=decided("unverified")
    )


async def test_a_verified_decision_follows_the_candidate_verification(
    store, package, admission, base_checkout
) -> None:
    ledger = await _started(store, package, admission)
    await ledger.record_bindings(
        "task-1/V1", package_id=package.package_id, payload=final_bindings(package)
    )
    repro = candidate_execution(package.checks[0], met=False)
    verification = verification_receipt(package, base_checkout).model_copy(
        update={"checks": (repro,)}
    )
    await ledger.record_candidate_verification("task-1/V1", verification)
    # The repro check failed; the preservation check did not run (undecided);
    # the third criterion has no check.
    await ledger.record_acceptance_reconciled(
        "task-1/V1",
        package_id=package.package_id,
        reconciliation=_decision("fail", "indeterminate", "uncovered"),
    )
    assert verify_boundary_order(await ledger.events("task-1/V1")) == ()


# --------------------------------------------------------------------------
# What admission can write: the reducer refuses an admitted record it could not.


def _manifest_event_data(
    checks: list[dict[str, Any]], oracles: list[dict[str, Any]], *, keys=("k1", "k2")
) -> dict[str, Any]:
    return {
        "package_id": "1" * 64,
        "seed_digest": "2" * 64,
        "record_sha256": "a" * 64,
        "manifest": {
            "schema_version": "ouroboros.check_package.v2",
            "package_id": "1" * 64,
            "seed_digest": "2" * 64,
            "criterion_keys": list(keys),
            "checks": checks,
            "files": [],
            "base_file_count": 0,
            "scratch_path_count": 0,
            "uncovered": [],
            "binding_grammar": "g",
            "oracles": oracles,
        },
    }


def _check(check_id: str, role: str, *keys: str) -> dict[str, Any]:
    """A manifest check with the product's ids (an oracle has three cases)."""
    oracle = check_id.startswith("oracle_")
    return {
        "check_id": check_id,
        "role": role,
        "criterion_keys": list(keys),
        "assertion_ids": [f"{check_id}.c{n}" for n in (1, 2, 3)] if oracle else [f"{check_id}.a1"],
    }


def _oracle(check_id: str, key: str, held_out: int) -> dict[str, Any]:
    return {
        "check_id": check_id,
        "criterion_key": key,
        "call_kind": "function",
        "param_count": 1,
        "target_named_in_criterion": False,
        "case_count": 3,
        "held_out_count": held_out,
    }


# The base run of a check (role, excluded): its reason and whether it passed.
_BASE_REASON = {
    ("reproduction", False): "reached_failing_assertion",
    ("reproduction", True): "reproduction_passed_on_base",
    ("preservation", False): "preservation_passed",
    ("preservation", True): "preservation_failed",
}
_BASE_PASSED = {
    ("reproduction", False): False,
    ("reproduction", True): True,
    ("preservation", False): True,
    ("preservation", True): False,
}
_EXCLUSION = {"reproduction": "repro_passes_on_base", "preservation": "preservation_fails_on_base"}


def _admission_data(frozen: dict[str, Any], excluded: tuple[str, ...] = ()) -> dict[str, Any]:
    """The admission record ``admit_check_package`` writes for the frozen manifest."""
    roles = {check["check_id"]: check["role"] for check in frozen["manifest"]["checks"]}
    oracles = {oracle["check_id"]: oracle for oracle in frozen["manifest"]["oracles"]}
    return {
        "schema_version": "ouroboros.check_admission.v2",
        "package_id": frozen["package_id"],
        "seed_digest": frozen["seed_digest"],
        "verdict": "admitted",
        "reasons": [],
        "timeout_seconds": 120,
        "started_at": "2026-09-28T00:00:00+00:00",
        "completed_at": "2026-09-28T00:00:01+00:00",
        "protected_bytes_mutated": False,
        "base_tree_digest": "b" * 64,
        "base_tree_digest_after": "b" * 64,
        "interpreter_sha256": "d" * 64,
        "interpreter_realpath_sha256": "d" * 64,
        "check_tiers": {
            check_id: "C" if check_id in excluded else "A" if check_id in oracles else "S"
            for check_id in roles
        },
        "excluded_checks": {check_id: _EXCLUSION[roles[check_id]] for check_id in excluded} or None,
        "checks": [
            {
                "check_id": check_id,
                "role": role,
                "status": "violated" if check_id in excluded else "expected",
                "reason": _BASE_REASON[role, check_id in excluded],
                "cwd": ".",
                "return_code": 0 if _BASE_PASSED[role, check_id in excluded] else 1,
                "timed_out": False,
                "duration_seconds": 0.0,
                # A failure signature on the base: an oracle's exit code 1, a
                # reproduction script reaching its signature.
                "signature_seen": not _BASE_PASSED[role, check_id in excluded]
                and (check_id in oracles or role == "reproduction"),
                "stdout_sha256": "0" * 64,
                "stderr_sha256": "0" * 64,
                "protected_digest_before": "0" * 64,
                "protected_digest_after": "0" * 64,
                "mutated_paths": [],
                "scratch_outputs": [],
                "undeclared_outputs": [],
                **(
                    {
                        "oracle_result": _base_result(
                            oracles[check_id], _BASE_PASSED[role, check_id in excluded]
                        )
                    }
                    if check_id in oracles
                    else {}
                ),
            }
            for check_id, role in roles.items()
        ],
    }


def _base_result(oracle: dict[str, Any], passed: bool) -> dict[str, Any]:
    """An oracle's base run: its last ``held_out_count`` cases held out, all ``passed``."""
    count, held = oracle["case_count"], oracle["held_out_count"]
    return {
        "binding_source": "default",
        "resolve": "ok",
        "cases": [
            {"case_id": f"c{n}", "held_out": n > count - held, "passed": passed}
            for n in range(1, count + 1)
        ],
    }


def _two_repros() -> dict[str, Any]:
    return _manifest_event_data(
        [_check("oracle_1", "reproduction", "k1"), _check("oracle_2", "reproduction", "k2")],
        [_oracle("oracle_1", "k1", 1), _oracle("oracle_2", "k2", 1)],
    )


def _set_tier(check_id: str, tier: str) -> Any:
    return lambda a: a["check_tiers"].update({check_id: tier})


def _result(check_id: str, **update: Any) -> Any:
    def edit(admission: dict[str, Any]) -> None:
        for item in admission["checks"]:
            if item["check_id"] == check_id:
                item.update(update)

    return edit


IMPOSSIBLE_ADMISSIONS = {
    "protected_bytes_mutated": lambda a: a.update(protected_bytes_mutated=True),
    "base_changed": lambda a: a.update(base_tree_digest_after="c" * 64),
    "incomplete_tiers": lambda a: a["check_tiers"].pop("oracle_2"),
    "no_tiers": lambda a: a.pop("check_tiers"),
    "tier_after_admission": _set_tier("oracle_2", "A_prime"),
    "unknown_tier_check": _set_tier("ghost", "C"),
    "tier_c_not_excluded": _set_tier("oracle_2", "C"),
    "excluded_without_tier_c": lambda a: a.update(
        excluded_checks={**a["excluded_checks"], "oracle_2": "x"}
    ),
    "every_check_excluded": lambda a: a.update(check_tiers=dict.fromkeys(a["check_tiers"], "C")),
    "duplicate_result": lambda a: a["checks"].append(dict(a["checks"][0])),
    "missing_result": lambda a: a["checks"].pop(),
    "role_differs_from_manifest": _result("oracle_2", role="preservation"),
    "excluded_check_passed": _result("oracle_1", status="expected"),
    "excluded_for_another_role_reason": _result("oracle_1", reason="preservation_failed"),
    "exclusion_reason_of_another_role": lambda a: a["excluded_checks"].update(
        oracle_1="preservation_fails_on_base"
    ),
    "admitted_check_violated": _result(
        "oracle_2", status="violated", reason="reproduction_passed_on_base"
    ),
    "admitted_check_undecided": _result("oracle_2", status="indeterminate"),
}


def _event_at(event_type: str, aggregate: str, data: dict[str, Any], second: int) -> BaseEvent:
    return BaseEvent(
        type=event_type,
        aggregate_type="boundary",
        aggregate_id=aggregate,
        data=data,
        timestamp=datetime(2026, 9, 28, 0, 0, second, tzinfo=UTC),
    )


def _journal(frozen: dict[str, Any], admission: dict[str, Any]) -> list[BaseEvent]:
    version = boundary_version_id("run_p", 1)
    return [
        _event_at(PACKAGE_FROZEN, version, frozen, 1),
        _event_at(ADMISSION_COMPLETED, version, admission, 2),
        _event_at(
            ACTOR_STARTED,
            version,
            {"actor_id": "run_p", "package_id": frozen["package_id"], "runtime": None},
            3,
        ),
    ]


def _enabled() -> list[BaseEvent]:
    return [
        _event_at(
            CHECK_PACKAGE_ENABLED,
            "run_p",
            {"execution_id": "run_p", "contract": CONTRACT.journal_data()},
            0,
        )
    ]


def test_an_admission_record_admission_writes_is_accepted() -> None:
    frozen = _two_repros()
    admission = _admission_data(frozen, ("oracle_1",))
    assert admitted_exclusions(frozen_manifest(frozen), admission) == frozenset({"oracle_1"})
    assert verify_boundary_order(_journal(frozen, admission), run_events=_enabled()) == ()


@pytest.mark.parametrize(
    "edit", list(IMPOSSIBLE_ADMISSIONS.values()), ids=list(IMPOSSIBLE_ADMISSIONS)
)
def test_an_admission_record_admission_could_not_write_is_refused_and_flagged(
    edit: Any,
) -> None:
    frozen = _two_repros()
    admission = _admission_data(frozen, ("oracle_1",))
    edit(admission)
    with pytest.raises(BoundaryOrderError):
        admitted_exclusions(frozen_manifest(frozen), admission)
    # The same rule on write (the reducer) and on replay.
    assert verify_boundary_order(_journal(frozen, admission), run_events=_enabled()) != ()


def _rename_oracle(frozen: dict[str, Any], check_id: str) -> None:
    """Rename the first oracle check consistently (check, its assertions, its oracle)."""
    manifest = frozen["manifest"]
    manifest["checks"][0] = _check(check_id, "reproduction", "k1")
    manifest["checks"][0]["assertion_ids"] = [f"{check_id}.c{n}" for n in (1, 2, 3)]
    manifest["oracles"][0] = _oracle(check_id, "k1", 1)


@pytest.mark.parametrize(
    "edit",
    [
        lambda f: f.pop("record_sha256"),
        lambda f: f.pop("manifest"),
        lambda f: f["manifest"].update(package_id="q" * 64),
        lambda f: f["manifest"].update(checks=f["manifest"]["checks"][:1]),
        lambda f: f["manifest"].update(oracles=[_oracle("ghost", "k1", 1)]),
        lambda f: f["manifest"].update(oracles=[_oracle("oracle_1", "k1", -1)]),
        lambda f: f["manifest"]["checks"][0].update(role="other"),
        # A check named by the constructor, not by the product's mint.
        lambda f: _rename_oracle(f, "fix_c2_is_minus_2"),
        lambda f: _rename_oracle(f, "oracle_5"),
        lambda f: f["manifest"]["checks"][0].update(
            assertion_ids=["oracle_1.c1", "oracle_1.c2", "oracle_1.held_minus_2"]
        ),
    ],
    ids=[
        "no_record_digest",
        "no_manifest",
        "manifest_names_another_package",
        "shortened",
        "unknown_oracle",
        "held_out_count",
        "unknown_role",
        "hand_named_check",
        "oracle_id_of_another_criterion",
        "hand_named_assertion",
    ],
)
def test_a_frozen_record_the_product_could_not_write_is_refused(edit: Any) -> None:
    frozen = _two_repros()
    edit(frozen)
    with pytest.raises(BoundaryOrderError):
        frozen_manifest(frozen)


def _scripts(*checks: tuple[str, tuple[str, ...]]) -> dict[str, Any]:
    """A manifest of script checks only (check id, linked criteria), keys k1..k3."""
    return _manifest_event_data(
        [_check(check_id, "reproduction", *keys) for check_id, keys in checks],
        [],
        keys=("k1", "k2", "k3"),
    )


def test_manifest_ids_are_the_dense_sequence_the_package_mints() -> None:
    # Numbered by the criterion a check first links, counted 1, 2, ... per
    # criterion in package order; a check linking two criteria matches one.
    genuine = _scripts(
        ("script_1_1", ("k1",)), ("script_1_2", ("k1", "k2")), ("script_3_1", ("k3",))
    )
    for data in (genuine, _scripts(("script_2_1", ("k1", "k2")), ("script_3_1", ("k3",)))):
        data["manifest"].pop("binding_grammar")
        data["manifest"].pop("oracles")
        frozen_manifest(data)
    for forged in (
        _scripts(("script_1_2", ("k1",)), ("script_3_1", ("k2", "k3"))),  # a gap
        _scripts(("script_1_1", ("k1",)), ("script_1_1", ("k1", "k2")), ("script_3_1", ("k3",))),
        _scripts(("script_2_1", ("k1",)), ("script_3_1", ("k2", "k3"))),  # an unlinked number
    ):
        with pytest.raises(BoundaryOrderError):
            frozen_manifest(forged)


# --------------------------------------------------------------------------
# The recovery projection (``resume`` consumes it; ``test_resume`` covers the
# end to end journal matrix).


def test_the_projection_applies_admission_exclusions_and_the_reproduction_rule() -> None:
    frozen = _manifest_event_data(
        [
            _check("oracle_1", "reproduction", "k1"),
            _check("script_1_1", "preservation", "k1"),
            _check("script_2_1", "reproduction", "k2"),
            _check("oracle_2", "reproduction", "k2"),
        ],
        [
            _oracle("oracle_1", "k1", 2),
            _oracle("oracle_2", "k2", 3),
        ],
    )
    # oracle_1 excluded: k1 keeps only a preservation check, so it is not covered;
    # oracle_2 excluded: k2 stays covered by script_2_1, a script check (no held-out case).
    events = _journal(frozen, _admission_data(frozen, ("oracle_1", "oracle_2")))
    projection = recovery_projection("run_p", _enabled(), {1: events})
    assert isinstance(projection, RecoveryBound)
    assert projection.covered == frozenset({"k2"})
    assert projection.held_out_checks == frozenset()
    assert projection.contract == CONTRACT
    admitted = recovery_projection(
        "run_p", _enabled(), {1: _journal(frozen, _admission_data(frozen))}
    )
    assert isinstance(admitted, RecoveryBound)
    assert admitted.covered == frozenset({"k1", "k2"})
    assert admitted.held_out_checks == frozenset({"oracle_1", "oracle_2"})


def test_the_projection_is_off_only_without_any_record_of_the_run() -> None:
    assert isinstance(recovery_projection("run_p", [], {}), RecoveryOff)
    frozen = _two_repros()
    journal = _journal(frozen, _admission_data(frozen))
    # Versions without the enabled record, the enabled record without versions.
    assert isinstance(recovery_projection("run_p", [], {1: journal}), RecoveryUndecidable)
    assert isinstance(recovery_projection("run_p", _enabled(), {}), RecoveryUndecidable)
    assert isinstance(recovery_projection("run_p", _enabled(), {2: journal}), RecoveryUndecidable)


def test_the_projection_needs_the_interpreter_pin() -> None:
    frozen = _two_repros()
    admission = _admission_data(frozen)
    del admission["interpreter_realpath_sha256"]
    projection = recovery_projection("run_p", _enabled(), {1: _journal(frozen, admission)})
    assert isinstance(projection, RecoveryUndecidable)


def test_a_second_worker_start_on_a_version_is_refused() -> None:
    frozen = _two_repros()
    journal = _journal(frozen, _admission_data(frozen))
    again = journal[-1].model_copy(
        update={"id": "second", "timestamp": datetime(2026, 9, 28, 0, 0, 4, tzinfo=UTC)}
    )
    assert verify_boundary_order([*journal, again], run_events=_enabled()) != ()


def test_a_file_planted_at_a_receipt_target_never_stands_in_for_the_receipt(
    tmp_path: Path, admission
) -> None:
    stored = write_receipt(admission, tmp_path)
    assert write_receipt(admission, tmp_path) == stored  # the same bytes: confirmed
    stored.write_bytes(b"corrupt")
    with pytest.raises(CheckPackageError, match="different file"):
        write_receipt(admission, tmp_path)
