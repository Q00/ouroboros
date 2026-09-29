"""Records with no valid authority never enter an authoritative phase (round 2).

The gateway closes each record's schema; the reducer relates it to what the
journal already holds. A record the product could not have written in that
state is refused on write and flagged on replay: an incomplete final binding,
a decision that accepts what neither the package's recorded results nor the
existing verifier accepted, a frozen manifest that is not the package's
projection, an admission without its interpreter pin, a verification of
checks the bindings did not run, and a later version the product could not
have sealed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ouroboros.boundary.binding import Binding, CallKind, CheckTier
from ouroboros.boundary.events import (
    ACTOR_STARTED,
    BOUNDARY_AGGREGATE_TYPE,
    CONSTRUCTION_FAILED,
    BindingsPayload,
    ReconciliationPayload,
    ResumedPayload,
    RunContract,
    acceptance_reconciled_event,
    actor_started_event,
    admission_completed_event,
    binding_recorded_event,
    boundary_version_id,
    candidate_verified_event,
    construction_failed_event,
    package_frozen_event,
)
from ouroboros.boundary.ledger import (
    BoundaryLedger,
    BoundaryOrderError,
    Phase,
    RecoveryBound,
    RecoveryUndecidable,
    recovery_projection,
    verify_boundary_order,
    version_state,
)
from ouroboros.boundary.package import CheckPackage, seal_package
from ouroboros.boundary.receipts import CandidateVerdict, CheckStatus, PackageVerdict
from ouroboros.events.base import BaseEvent
from ouroboros.persistence.event_store import EventStore

from .conftest import REPRO_SCRIPT, build_package
from .journal_fixtures import (
    PIN,
    admission_receipt,
    candidate_execution,
    expected_execution,
    oracle_result,
    seed_for,
    settled_admission,
    unresolved_admission,
    verification_receipt,
)
from .test_package_identity import held_out_package

B = "task-1/V1"
TIERS = ("A", "A_prime", "U", "S", "C")
CONTRACT = RunContract(check_timeout_seconds=120)


@pytest.fixture
async def store():
    event_store = EventStore("sqlite+aiosqlite:///:memory:")
    await event_store.initialize()
    yield event_store
    await event_store.close()


def _criterion(index: int, key: str, status: str, **update: Any) -> dict[str, Any]:
    return {
        "root_ac_index": index,
        "criterion_key": key,
        "package_status": status,
        "tier": "U" if status in ("unverified", "uncovered") else "A",
        "reason": status,
        "failed_heldout_only": False,
        "binding": None,
        "existing_outcome": "succeeded",
        "existing_failure_class": None,
        "existing_accepted": True,
        "accepted": status in ("pass", "unverified", "uncovered"),
        "governed_by": "check_package",
        "declared_binding_pass": False,
        **update,
    }


def _decision(criteria: list[dict[str, Any]], **extra: Any) -> ReconciliationPayload:
    """A decision under the product's rule; its summary written out by hand."""
    statuses = [item["package_status"] for item in criteria]
    verdict = next((v for v in ("fail", "indeterminate", "pass") if v in statuses), "unverified")
    not_decided = [i for i in criteria if i["package_status"] in ("unverified", "uncovered")]
    unverified = [i for i in not_decided if i["governed_by"] != "existing_verifier"]
    loose = [i for i in unverified if i["governed_by"] != "execution"]
    coverage = (
        "low"
        if loose or (criteria and len(not_decided) / len(criteria) >= 0.5)
        else "partial"
        if not_decided
        else "full"
    )
    data = {
        "schema_version": "ouroboros.acceptance_reconciliation.v3",
        "run_accepted": bool(criteria) and all(i["accepted"] for i in criteria),
        "existing_run_accepted": True,
        "artifact_verdict": verdict,
        "verified_pass_count": statuses.count("pass"),
        "unverified_count": len(unverified),
        "criterion_count": len(criteria),
        "tier_summary": {tier: sum(1 for i in criteria if i["tier"] == tier) for tier in TIERS},
        "criteria": criteria,
        "legacy_decided_count": sum(1 for i in criteria if i["governed_by"] == "existing_verifier"),
        "verification_coverage": coverage,
        **extra,
    }
    return ReconciliationPayload.model_validate(data)


def _binding(check_id: str, key: str, tier: str, **update: Any) -> dict[str, Any]:
    return {
        "criterion_key": key,
        "check_id": check_id,
        "tier": tier,
        "binding_source": None,
        "binding": None,
        "status_hint": "run",
        "reason": "script_check",
        "declared": None,
        **update,
    }


def _script_bindings(package: CheckPackage, phase: str = "final") -> BindingsPayload:
    checks = [
        _binding(check.check_id, check.assertions[0].criterion_key, "S") for check in package.checks
    ]
    return BindingsPayload.model_validate({"phase": phase, "checks": checks})


async def _started(store: EventStore, package: CheckPackage, checkout: Path) -> BoundaryLedger:
    ledger = BoundaryLedger(store)
    await ledger.record_package_frozen(B, package, seed=seed_for(package))
    await ledger.record_admission(B, admission_receipt(package, checkout))
    await ledger.record_actor_started("actor-1", [B])
    return ledger


# --------------------------------------------------------------------------
# The bot's minimal reproduction, through public BoundaryLedger calls.


async def test_an_empty_final_binding_of_a_two_check_package_is_refused(
    store, package, base_checkout
) -> None:
    ledger = await _started(store, package, base_checkout)
    empty = BindingsPayload(phase="final", checks=())
    with pytest.raises(BoundaryOrderError):
        await ledger.record_bindings(B, package_id=package.package_id, payload=empty)
    # One check of two is incomplete too.
    one = BindingsPayload.model_validate(
        {"phase": "final", "checks": [_script_bindings(package).checks[0].model_dump()]}
    )
    with pytest.raises(BoundaryOrderError):
        await ledger.record_bindings(B, package_id=package.package_id, payload=one)
    await ledger.record_bindings(
        B, package_id=package.package_id, payload=_script_bindings(package)
    )


async def test_an_accepted_decision_without_any_authority_never_reaches_decided(
    store, package, base_checkout
) -> None:
    ledger = await _started(store, package, base_checkout)
    # The empty final binding, written around the ledger.
    empty = BindingsPayload(phase="final", checks=())
    await store.append(binding_recorded_event(B, package_id=package.package_id, payload=empty))
    # Every criterion unverified; the existing verifier rejected each one;
    # the package accepts them all anyway.
    unearned = _decision(
        [
            _criterion(index, key, "unverified", existing_accepted=False)
            for index, key in enumerate(package.criterion_keys)
        ]
    )
    assert unearned.run_accepted
    with pytest.raises(BoundaryOrderError):
        await ledger.record_acceptance_reconciled(
            B, package_id=package.package_id, reconciliation=unearned
        )
    await store.append(
        acceptance_reconciled_event(B, package_id=package.package_id, reconciliation=unearned)
    )
    events = await ledger.events(B)
    assert verify_boundary_order(events) != ()
    with pytest.raises(BoundaryOrderError):
        version_state(events)
    state = version_state(events[:3])
    assert state.phase is Phase.STARTED


# --------------------------------------------------------------------------
# The frozen manifest is exactly the package's projection.

_PROJECTION_FIELDS = (
    # Fields ``manifest_summary`` emits that the reducer itself never reads.
    "schema_version",
    "files",
    "base_file_count",
    "scratch_path_count",
)


def _enabled(run: str) -> list[BaseEvent]:
    event = BaseEvent(
        type="boundary.check_package.enabled",
        aggregate_type=BOUNDARY_AGGREGATE_TYPE,
        aggregate_id=run,
        data={"execution_id": run, "contract": CONTRACT.journal_data()},
    )
    return [event.model_copy(update={"timestamp": datetime(2026, 9, 29, tzinfo=UTC)})]


def _timed(events: list[BaseEvent]) -> list[BaseEvent]:
    start = datetime(2026, 9, 29, tzinfo=UTC)
    return [
        event.model_copy(update={"timestamp": start + timedelta(seconds=n + 1)})
        for n, event in enumerate(events)
    ]


def test_a_frozen_manifest_missing_projection_fields_makes_recovery_undecidable(
    package, base_checkout
) -> None:
    run = "run_manifest"
    version = boundary_version_id(run, 1)
    frozen = package_frozen_event(version, package)
    manifest = {k: v for k, v in frozen.data["manifest"].items() if k not in _PROJECTION_FIELDS}
    partial = frozen.model_copy(update={"data": {**frozen.data, "manifest": manifest}})
    rest = [
        admission_completed_event(version, admission_receipt(package, base_checkout)),
        actor_started_event(version, actor_id=run, package_id=package.package_id, runtime=None),
    ]
    genuine = recovery_projection(run, _enabled(run), {1: _timed([frozen, *rest])})
    assert isinstance(genuine, RecoveryBound)
    assert verify_boundary_order(_timed([partial, *rest])) != ()
    projection = recovery_projection(run, _enabled(run), {1: _timed([partial, *rest])})
    assert isinstance(projection, RecoveryUndecidable)


@pytest.mark.parametrize("oracles", [False, True], ids=["script_checks", "oracle"])
def test_the_manifest_model_is_exactly_what_manifest_summary_emits(seed, oracles: bool) -> None:
    from pydantic import ValidationError

    from ouroboros.boundary.events import ManifestRecord

    package = seal_package(held_out_package() if oracles else build_package(seed))
    summary = package.manifest_summary()
    record = ManifestRecord.model_validate(summary)
    assert record.model_dump(mode="json", exclude_unset=True) == summary
    if oracles:
        # Every field the model defines is one the projection emits.
        assert set(ManifestRecord.model_fields) == set(summary)
    for key in summary:
        with pytest.raises(ValidationError):
            ManifestRecord.model_validate({k: v for k, v in summary.items() if k != key})
    with pytest.raises(ValidationError):
        ManifestRecord.model_validate({**summary, "argv": ["python3", "x.py"]})


# --------------------------------------------------------------------------
# An admitted record carries both interpreter pins.


async def test_an_admitted_record_without_its_interpreter_pins_is_refused(
    store, package, base_checkout
) -> None:
    ledger = BoundaryLedger(store)
    await ledger.record_package_frozen(B, package, seed=seed_for(package))
    unpinned = admission_receipt(package, base_checkout).model_copy(
        update={"interpreter_sha256": None, "interpreter_realpath_sha256": None}
    )
    with pytest.raises(BoundaryOrderError):
        await ledger.record_admission(B, unpinned)
    await store.append(admission_completed_event(B, unpinned))
    await store.append(
        actor_started_event(B, actor_id="actor-1", package_id=package.package_id, runtime=None)
    )
    events = await ledger.events(B)
    assert verify_boundary_order(events) != ()
    with pytest.raises(BoundaryOrderError):
        version_state(events)


# --------------------------------------------------------------------------
# A decision records only the package statuses the recorded results support.


async def _script_verified(
    store: EventStore, package: CheckPackage, checkout: Path, *checks: Any
) -> BoundaryLedger:
    ledger = await _started(store, package, checkout)
    await ledger.record_bindings(
        B, package_id=package.package_id, payload=_script_bindings(package)
    )
    given = {check.check_id: check for check in checks}
    ran = tuple(
        given.get(check.check_id) or candidate_execution(check, met=True)
        for check in package.checks
    )
    verification = verification_receipt(package, checkout, ran)
    await ledger.record_candidate_verification(B, verification)
    return ledger


@pytest.mark.parametrize(
    "first",
    [
        # A failed check recorded as a criterion the package did not verify:
        # it would hand a package failure to the legacy verifier.
        {
            "package_status": "unverified",
            "tier": "U",
            "governed_by": "existing_verifier",
            "accepted": True,
        },
        # A pass the results do not show.
        {"package_status": "pass", "accepted": True},
    ],
    ids=["failure_handed_to_legacy", "pass_without_results"],
)
async def test_a_decision_the_results_do_not_support_is_refused(
    store, package, base_checkout, first: dict[str, Any]
) -> None:
    violated = candidate_execution(package.checks[0], met=False)
    ledger = await _script_verified(store, package, base_checkout, violated)
    keys = package.criterion_keys
    rest = [_criterion(1, keys[1], "indeterminate"), _criterion(2, keys[2], "uncovered")]
    forged = _decision([_criterion(0, keys[0], "fail", **first), *rest])
    with pytest.raises(BoundaryOrderError):
        await ledger.record_acceptance_reconciled(
            B, package_id=package.package_id, reconciliation=forged
        )
    genuine = _decision([_criterion(0, keys[0], "fail"), *rest])
    await ledger.record_acceptance_reconciled(
        B, package_id=package.package_id, reconciliation=genuine
    )
    assert verify_boundary_order(await ledger.events(B)) == ()


def _oracle_run(package: CheckPackage, passed: dict[str, bool]) -> Any:
    """The oracle check on a candidate: it met its role exactly when every case passed."""
    check = package.checks[0]
    every = len(passed) == 3 and all(passed.values())
    return expected_execution(check).model_copy(
        update={
            "status": CheckStatus.EXPECTED if every else CheckStatus.VIOLATED,
            "reason": "passed" if every else "reproduction_still_failing",
            "return_code": 0 if every else 1,
            "signature_seen": not every,
            "tier": CheckTier.A,
            # The product hands every bound oracle its binding: the harness says declared.
            "oracle_result": oracle_result(package, check.check_id, passed).model_copy(
                update={"binding_source": "declared"}
            ),
        }
    )


async def _oracle_verified(
    store: EventStore,
    checkout: Path,
    *,
    held_out_passed: bool | None = True,
    candidate: dict[str, bool] | None = None,
    base: dict[str, bool] | None = None,
) -> tuple[BoundaryLedger, CheckPackage]:
    """The one-oracle package verified on a candidate (held-out cases ``c2`` and ``c3``).

    ``base`` says which cases passed on the base at admission (default none).
    """
    package = seal_package(held_out_package())
    admission = admission_receipt(package, checkout)
    if base is not None:
        admitted = admission.checks[0].model_copy(
            update={"oracle_result": oracle_result(package, "oracle_1", base)}
        )
        admission = admission.model_copy(update={"checks": (admitted,)})
    ledger = BoundaryLedger(store)
    await ledger.record_package_frozen(B, package, seed=seed_for(package))
    await ledger.record_admission(B, admission)
    await ledger.record_actor_started("actor-1", [B])
    spec = package.oracle_for("oracle_1")
    assert spec is not None
    bound = _binding(
        "oracle_1",
        package.criterion_keys[0],
        "A",
        binding_source="default",
        binding=spec.default_binding.to_dict(),
        reason="default_binding_resolves",
    )
    payload = BindingsPayload.model_validate({"phase": "final", "checks": [bound]})
    await ledger.record_bindings(B, package_id=package.package_id, payload=payload)
    if candidate is None and held_out_passed is None:
        return ledger, package  # bound, not yet verified
    held = bool(held_out_passed)
    passed = candidate if candidate is not None else {"c1": True, "c2": held, "c3": held}
    run = _oracle_run(package, passed)
    verification = verification_receipt(package, checkout, (run,))
    await ledger.record_candidate_verification(B, verification)
    return ledger, package


async def test_a_verified_pass_needs_a_held_out_case_and_names_its_provenance(
    store, base_checkout
) -> None:
    ledger, package = await _oracle_verified(store, base_checkout, held_out_passed=True)
    (key,) = package.criterion_keys
    declared = _decision([_criterion(0, key, "pass", declared_binding_pass=True)])
    with pytest.raises(BoundaryOrderError):
        await ledger.record_acceptance_reconciled(
            B, package_id=package.package_id, reconciliation=declared
        )
    await ledger.record_acceptance_reconciled(
        B, package_id=package.package_id, reconciliation=_decision([_criterion(0, key, "pass")])
    )


async def test_a_pass_without_a_held_out_case_is_refused(store, base_checkout) -> None:
    ledger, package = await _oracle_verified(store, base_checkout, held_out_passed=False)
    (key,) = package.criterion_keys
    with pytest.raises(BoundaryOrderError):
        await ledger.record_acceptance_reconciled(
            B,
            package_id=package.package_id,
            reconciliation=_decision([_criterion(0, key, "pass")]),
        )
    # The held-out cases failed: the check did not meet its role.
    await ledger.record_acceptance_reconciled(
        B,
        package_id=package.package_id,
        reconciliation=_decision([_criterion(0, key, "fail")]),
    )


def test_a_decision_whose_summary_disagrees_with_its_criteria_is_refused(package) -> None:
    keys = package.criterion_keys
    genuine = _decision([_criterion(i, k, "indeterminate") for i, k in enumerate(keys)])
    for update in (
        {"verified_pass_count": 1},
        {"artifact_verdict": "pass"},
        {"unverified_count": 2},
    ):
        with pytest.raises(ValueError):
            ReconciliationPayload.model_validate({**genuine.model_dump(mode="json"), **update})


async def test_a_decision_outside_the_products_rule_is_not_journaled(
    store, package, base_checkout
) -> None:
    violated = candidate_execution(package.checks[0], met=False)
    ledger = await _script_verified(store, package, base_checkout, violated)
    keys = package.criterion_keys
    criteria = [
        _criterion(0, keys[0], "fail"),
        _criterion(1, keys[1], "indeterminate"),
        _criterion(2, keys[2], "uncovered"),
    ]
    legacy_off = _decision(criteria, schema_version="ouroboros.acceptance_reconciliation.v2")
    with pytest.raises(BoundaryOrderError):
        await ledger.record_acceptance_reconciled(
            B, package_id=package.package_id, reconciliation=legacy_off
        )


# --------------------------------------------------------------------------
# Bindings and verifications the product could not write.


async def test_a_binding_tier_the_product_cannot_assign_is_refused(
    store, package, base_checkout
) -> None:
    ledger = await _started(store, package, base_checkout)
    good = _script_bindings(package).model_dump()["checks"]
    spec = {
        "criterion_key": package.criterion_keys[0],
        "symbol": "calc.add",
        "call_kind": "function",
    }
    for edit in (
        {"tier": "A", "binding_source": "default", "binding": spec},  # a script as tier A
        {"tier": "C", "status_hint": "excluded"},  # excluded though admitted
        {"criterion_key": package.criterion_keys[2]},  # a criterion the check does not link
    ):
        forged = BindingsPayload.model_validate(
            {"phase": "final", "checks": [{**good[0], **edit}, good[1]]}
        )
        with pytest.raises(BoundaryOrderError):
            await ledger.record_bindings(B, package_id=package.package_id, payload=forged)


async def test_a_rerun_of_a_check_that_met_its_role_is_refused(
    store, package, base_checkout
) -> None:
    ledger = await _started(store, package, base_checkout)
    admitted = [
        _binding(package.checks[0].check_id, package.criterion_keys[0], "S"),
        _binding(
            package.checks[1].check_id,
            package.criterion_keys[1],
            "S",
        ),
    ]
    await ledger.record_bindings(
        B,
        package_id=package.package_id,
        payload=BindingsPayload.model_validate({"phase": "final", "checks": admitted}),
    )
    expected = [candidate_execution(check, met=True) for check in package.checks]
    receipt = verification_receipt(package, base_checkout, tuple(expected))
    await ledger.record_candidate_verification(B, receipt)
    # A re-run of a check that already met its role.
    rerun = verification_receipt(
        package, base_checkout, tuple(expected[:1]), selection=(expected[0].check_id,)
    )
    with pytest.raises(BoundaryOrderError):
        await ledger.record_candidate_verification(B, rerun)


# --------------------------------------------------------------------------
# A later version the product could not have written (the M3 probe, ported).


async def _bound_run(store: EventStore, package: CheckPackage, checkout: Path, run: str):
    ledger = BoundaryLedger(store)
    await ledger.record_check_package_enabled(run, CONTRACT)
    v1 = boundary_version_id(run, 1)
    await ledger.record_package_frozen(v1, package, seed=seed_for(package))
    await ledger.record_admission(v1, admission_receipt(package, checkout))
    await ledger.record_actor_started(run, [v1])
    return ledger


async def _project(ledger: BoundaryLedger, run: str):
    return recovery_projection(run, await ledger.events(run), await ledger.run_versions(run))


async def test_bare_records_on_a_later_version_make_recovery_undecidable(
    store, package, base_checkout
) -> None:
    run = "exec_m3"
    ledger = await _bound_run(store, package, base_checkout, run)
    assert isinstance(await _project(ledger, run), RecoveryBound)
    for event_type in (CONSTRUCTION_FAILED, ACTOR_STARTED):
        await store.append(
            BaseEvent(
                type=event_type,
                aggregate_type=BOUNDARY_AGGREGATE_TYPE,
                aggregate_id=boundary_version_id(run, 2),
                data={},
            )
        )
    assert isinstance(await _project(ledger, run), RecoveryUndecidable)


async def test_a_version_sealed_after_the_worker_started_makes_recovery_undecidable(
    store, package, base_checkout
) -> None:
    run = "exec_m3_sealed"
    ledger = await _bound_run(store, package, base_checkout, run)
    await store.append(
        construction_failed_event(
            boundary_version_id(run, 2),
            seed_digest=package.seed_digest,
            input_digest="1" * 64,
            reason="parse",
        )
    )
    assert isinstance(await _project(ledger, run), RecoveryUndecidable)


async def test_a_gap_in_the_versions_makes_recovery_undecidable(
    store, package, base_checkout
) -> None:
    run = "exec_m3_gap"
    ledger = await _bound_run(store, package, base_checkout, run)
    await store.append(package_frozen_event(boundary_version_id(run, 3), package))
    assert isinstance(await _project(ledger, run), RecoveryUndecidable)


async def test_only_a_superseded_predecessor_may_stand_before_the_bound_version(
    store, seed, package, base_checkout
) -> None:
    run = "exec_chain"
    ledger = BoundaryLedger(store)
    await ledger.record_check_package_enabled(run, CONTRACT)
    v1, v2 = boundary_version_id(run, 1), boundary_version_id(run, 2)
    successor = seal_package(build_package(seed, repro_script=REPRO_SCRIPT + "# v2\n"))
    await ledger.record_package_frozen(v1, package, seed=seed_for(package))
    await ledger.record_admission(v1, admission_receipt(package, base_checkout))
    await ledger.record_package_frozen(v2, successor, seed=seed_for(successor))
    await ledger.record_admission(v2, admission_receipt(successor, base_checkout))
    await ledger.record_actor_started(run, [v2])
    # v1 was never superseded: not a history the product writes.
    assert isinstance(await _project(ledger, run), RecoveryUndecidable)
    await ledger.record_superseded(v1, superseded_by=v2, reason="replacement_checks")
    assert isinstance(await _project(ledger, run), RecoveryBound)


async def test_a_supersession_naming_another_successor_package_is_undecidable(
    store, seed, package, base_checkout
) -> None:
    from ouroboros.boundary.events import superseded_event

    run = "exec_chain_forged"
    ledger = BoundaryLedger(store)
    await ledger.record_check_package_enabled(run, CONTRACT)
    v1, v2 = boundary_version_id(run, 1), boundary_version_id(run, 2)
    successor = seal_package(build_package(seed, repro_script=REPRO_SCRIPT + "# v2\n"))
    await ledger.record_package_frozen(v1, package, seed=seed_for(package))
    await ledger.record_package_frozen(v2, successor, seed=seed_for(successor))
    await ledger.record_admission(v2, admission_receipt(successor, base_checkout))
    await store.append(
        superseded_event(
            v1,
            superseded_by=v2,
            package_id=package.package_id,
            successor_package_id=package.package_id,
            reason="replacement_checks",
        )
    )
    await ledger.record_actor_started(run, [v2])
    assert isinstance(await _project(ledger, run), RecoveryUndecidable)


# --------------------------------------------------------------------------
# The sweep: each record type once more, for what the product never writes.


async def test_an_a_prime_binding_without_its_valid_declaration_is_refused(
    store, base_checkout
) -> None:
    package = seal_package(held_out_package())
    (key,) = package.criterion_keys
    admission = unresolved_admission(package, base_checkout)
    ledger = BoundaryLedger(store)
    await ledger.record_package_frozen(B, package, seed=seed_for(package))
    await ledger.record_admission(B, admission)
    await ledger.record_actor_started("actor-1", [B])
    binding = {"criterion_key": key, "symbol": "mathutils.clamp", "call_kind": "function"}
    declared = {
        "criterion_key": key,
        "valid": True,
        "indeterminate": False,
        "reason": "declared_binding_resolves",
        "binding": binding,
        "base_run": None,
    }
    a_prime = _binding(
        "oracle_1",
        key,
        "A_prime",
        binding_source="declared",
        binding=binding,
        reason="declared_binding_resolves",
    )
    for forged in (
        a_prime,  # no declaration recorded
        {**a_prime, "declared": {**declared, "valid": False}},  # the declaration was invalid
        {**a_prime, "tier": "A", "binding_source": "default", "declared": declared},
    ):
        payload = BindingsPayload.model_validate({"phase": "final", "checks": [forged]})
        with pytest.raises(BoundaryOrderError):
            await ledger.record_bindings(B, package_id=package.package_id, payload=payload)
    genuine = BindingsPayload.model_validate(
        {"phase": "final", "checks": [{**a_prime, "declared": declared}]}
    )
    await ledger.record_bindings(B, package_id=package.package_id, payload=genuine)


def _reference(excluded: list[dict[str, Any]], uncovered: list[dict[str, Any]]) -> Any:
    from ouroboros.boundary.events import ReferenceCheckPayload

    return ReferenceCheckPayload.model_validate(
        {
            "schema_version": "ouroboros.reference_check.v2",
            "excluded_cases": excluded,
            "uncovered": uncovered,
        }
    )


async def test_a_reference_check_is_expressed_against_the_frozen_package(store) -> None:
    # A frozen package with one oracle (criterion 1) and scripts for the rest;
    # criterion 3 is uncovered (as when the reference check dropped its oracle).
    from pydantic import ValidationError

    package = seal_package(held_out_package())
    (key,) = package.criterion_keys
    ledger = BoundaryLedger(store)
    await ledger.record_package_frozen(B, package, seed=seed_for(package))
    kept = {"check_id": "oracle_1", "excluded_count": 1, "reason": "oracle_inconsistent"}
    for shape in (
        # Pre-rebuild case ids name nothing in the frozen package.
        {**{k: v for k, v in kept.items() if k != "excluded_count"}, "case_ids": ["c2"]},
        {**kept, "excluded_count": 0},
        {**kept, "reason": "because"},
    ):
        with pytest.raises(ValidationError):
            _reference([shape], [])
    with pytest.raises(ValidationError):
        _reference([kept, kept], [])
    # A kept oracle's criterion is covered: it cannot also be uncovered.
    covered = [{"criterion_key": key, "reason": "reference_unavailable"}]
    with pytest.raises(BoundaryOrderError, match="covers"):
        await ledger.record_reference_checked(
            B, package_id=package.package_id, payload=_reference([kept], covered)
        )
    await ledger.record_reference_checked(
        B, package_id=package.package_id, payload=_reference([kept], [])
    )


async def test_a_reference_check_excluding_cases_of_a_script_check_is_refused(
    store, package
) -> None:
    ledger = BoundaryLedger(store)
    await ledger.record_package_frozen(B, package, seed=seed_for(package))
    script = {
        "check_id": package.checks[0].check_id,
        "excluded_count": 1,
        "reason": "oracle_inconsistent",
    }
    with pytest.raises(BoundaryOrderError):
        await ledger.record_reference_checked(
            B, package_id=package.package_id, payload=_reference([script], [])
        )
    # A criterion with no check left (its oracle dropped) is uncovered with its reason.
    uncovered = [{"criterion_key": package.criterion_keys[2], "reason": "reference_unavailable"}]
    await ledger.record_reference_checked(
        B, package_id=package.package_id, payload=_reference([], uncovered)
    )


def test_an_existing_acceptance_of_a_failed_outcome_is_not_journaled() -> None:
    from ouroboros.boundary.events import ReconciledRecord

    item = _criterion(
        0,
        "k",
        "unverified",
        governed_by="existing_verifier",
        existing_outcome="failed",
        existing_accepted=True,
    )
    data = _decision([item]).model_dump(mode="json")
    with pytest.raises(ValueError):
        ReconciledRecord.model_validate({**data, "package_id": None})
    ok = {**item, "existing_outcome": "succeeded"}
    ReconciledRecord.model_validate({**_decision([ok]).model_dump(mode="json"), "package_id": None})


async def test_a_held_out_case_the_base_already_passed_supports_no_pass(
    store, base_checkout
) -> None:
    # B2: c2 passed on the base at admission, c3 failed there. A receipt that
    # claims the check met its role while only c2 passed is one the harness
    # never writes (a met role passes every case); the run that failed c3
    # supports no pass.
    ledger, package = await _oracle_verified(
        store, base_checkout, held_out_passed=None, base={"c2": True}
    )
    (key,) = package.criterion_keys
    only_c2 = {"c1": True, "c2": True, "c3": False}
    claimed = _oracle_run(package, only_c2).model_copy(
        update={"status": CheckStatus.EXPECTED, "reason": "passed"}
    )
    with pytest.raises(BoundaryOrderError, match="recorded run|harness"):
        await ledger.record_candidate_verification(
            B, _run_on(package, base_checkout, claimed, X, X)
        )
    await ledger.record_candidate_verification(
        B, _run_on(package, base_checkout, _oracle_run(package, only_c2), X, X)
    )
    with pytest.raises(BoundaryOrderError):
        await ledger.record_acceptance_reconciled(
            B,
            package_id=package.package_id,
            reconciliation=_decision([_criterion(0, key, "pass")]),
        )


async def test_a_held_out_case_the_base_failed_supports_a_pass(store, base_checkout) -> None:
    ledger, package = await _oracle_verified(
        store, base_checkout, candidate={"c1": True, "c2": True, "c3": True}, base={"c2": True}
    )
    (key,) = package.criterion_keys
    await ledger.record_acceptance_reconciled(
        B, package_id=package.package_id, reconciliation=_decision([_criterion(0, key, "pass")])
    )


# --------------------------------------------------------------------------
# A resumed decision is judged by the resume's own recorded run.


async def test_a_resumed_pass_needs_the_resumes_own_recorded_verification(
    store, base_checkout
) -> None:
    ledger, package = await _oracle_verified(store, base_checkout)
    (key,) = package.criterion_keys
    passed = _decision([_criterion(0, key, "pass")])
    await ledger.record_acceptance_reconciled(
        B, package_id=package.package_id, reconciliation=passed
    )
    resumed = ResumedPayload.model_validate({**passed.model_dump(mode="json"), "source": "live"})
    # No resumed bindings and verification: the resume verified nothing.
    with pytest.raises(BoundaryOrderError):
        await ledger.record_acceptance_resumed(B, package_id=package.package_id, payload=resumed)
    undecided = ResumedPayload.model_validate(
        {
            **_decision([_criterion(0, key, "indeterminate")]).model_dump(mode="json"),
            "source": "none",
        }
    )
    await ledger.record_acceptance_resumed(B, package_id=package.package_id, payload=undecided)
    # The resume's own bindings and verification support its pass.
    bindings = await ledger.events(B)
    final = next(e for e in bindings if e.type == "boundary.binding.recorded")
    data = {k: v for k, v in final.data.items() if k != "package_id"}
    again = BindingsPayload.model_validate({**data, "phase": "resumed"})
    await ledger.record_bindings(B, package_id=package.package_id, payload=again)
    run = _oracle_run(package, _PASSING)
    verification = verification_receipt(package, base_checkout, (run,))
    await ledger.record_candidate_verification(B, verification)
    await ledger.record_acceptance_resumed(B, package_id=package.package_id, payload=resumed)
    assert verify_boundary_order(await ledger.events(B)) == ()


# --------------------------------------------------------------------------
# Exclusion reasons say what the base run showed.


def _not_discriminating(package: CheckPackage, checkout: Path, base: dict[str, bool]) -> Any:
    admission = admission_receipt(package, checkout)
    excluded = admission.checks[0].model_copy(
        update={
            "status": CheckStatus.VIOLATED,
            "reason": "held_out_not_discriminating",
            "oracle_result": oracle_result(package, "oracle_1", base),
        }
    )
    return admission.model_copy(
        update={
            "checks": (excluded,),
            "check_tiers": {"oracle_1": CheckTier.C},
            "excluded_checks": {"oracle_1": "held_out_not_discriminating"},
        }
    )


def test_a_non_discriminating_oracle_is_excluded_for_that_reason(base_checkout) -> None:
    from ouroboros.boundary.ledger import admitted_exclusions, frozen_manifest

    package = seal_package(held_out_package())
    (key,) = package.criterion_keys
    frozen = package_frozen_event(B, package).data
    # A second, preservation script check keeps the package admitted.
    keep = {
        "check_id": "script_1_1",
        "role": "preservation",
        "criterion_keys": [key],
        "assertion_ids": ["script_1_1.a1"],
    }
    manifest = frozen_manifest(
        {
            **frozen,
            "manifest": {**frozen["manifest"], "checks": [*frozen["manifest"]["checks"], keep]},
        }
    )

    def admitted(base: dict[str, bool]) -> dict[str, Any]:
        data = admission_completed_event(B, _not_discriminating(package, base_checkout, base)).data
        kept = {
            **data["checks"][0],
            "check_id": "script_1_1",
            "role": "preservation",
            "reason": "preservation_passed",
            "status": "expected",
        }
        kept.pop("oracle_result", None)
        kept.pop("tier", None)
        return {
            **data,
            "checks": [*data["checks"], kept],
            "check_tiers": {**data["check_tiers"], "script_1_1": "S"},
        }

    assert admitted_exclusions(manifest, admitted({"c2": True, "c3": True})) == {"oracle_1"}
    # A held-out case the base failed: the oracle discriminates, the reason is false.
    with pytest.raises(BoundaryOrderError, match="not excluded for its role"):
        admitted_exclusions(manifest, admitted({"c2": True}))


# --------------------------------------------------------------------------
# The frozen manifest's criteria are in Seed order, the order decisions index.


async def test_a_decision_whose_root_index_names_another_criterion_is_refused(
    store, package, base_checkout
) -> None:
    violated = candidate_execution(package.checks[0], met=False)
    ledger = await _script_verified(store, package, base_checkout, violated)
    keys = package.criterion_keys
    swapped = _decision(
        [
            _criterion(1, keys[0], "fail"),
            _criterion(0, keys[1], "indeterminate"),
            _criterion(2, keys[2], "uncovered"),
        ]
    )
    with pytest.raises(BoundaryOrderError, match="root index"):
        await ledger.record_acceptance_reconciled(
            B, package_id=package.package_id, reconciliation=swapped
        )


def test_the_base_failing_rule_reads_the_receipt_and_its_journal_form_alike(
    base_checkout,
) -> None:
    from ouroboros.boundary.per_check import base_failing_held_out, held_out_all_passed
    from ouroboros.boundary.receipts import AdmissionJournal

    package = seal_package(held_out_package())
    live = admission_receipt(package, base_checkout)
    base = live.checks[0].model_copy(
        update={"oracle_result": oracle_result(package, "oracle_1", {"c2": True})}
    )
    live = live.model_copy(update={"checks": (base,)})
    journal = AdmissionJournal.model_validate(live.event_summary())
    for checks in (live.checks, journal.checks):
        assert base_failing_held_out(checks, set()) == {"oracle_1": frozenset({"c3"})}
        assert base_failing_held_out(checks, {"oracle_1"}) == {}
        assert not held_out_all_passed(checks[0].oracle_result)
    assert held_out_all_passed(oracle_result(package, "oracle_1", {"c2": True, "c3": True}))
    assert not held_out_all_passed(None)


# --------------------------------------------------------------------------
# Trust: a verification the product would distrust verifies nothing.


def _run_on(package: CheckPackage, checkout: Path, run: Any, before: str, after: str) -> Any:
    """A verification receipt as the product writes it: its mutation flag from its evidence."""
    selection = (run.check_id,)
    return verification_receipt(
        package, checkout, (run,), before=before, after=after, selection=selection
    )


_PASSING = {"c1": True, "c2": True, "c3": True}
X, Y = "1" * 64, "2" * 64


async def _pass_refused(ledger: BoundaryLedger, package: CheckPackage) -> None:
    (key,) = package.criterion_keys
    passed = _decision([_criterion(0, key, "pass")])
    with pytest.raises(BoundaryOrderError, match="does not show"):
        await ledger.record_acceptance_reconciled(
            B, package_id=package.package_id, reconciliation=passed
        )
    undecided = _decision([_criterion(0, key, "indeterminate")])
    await ledger.record_acceptance_reconciled(
        B, package_id=package.package_id, reconciliation=undecided
    )


async def test_a_pass_on_a_candidate_tree_that_changed_under_verification_is_refused(
    store, base_checkout
) -> None:
    ledger, package = await _oracle_verified(store, base_checkout, held_out_passed=None)
    run = _oracle_run(package, _PASSING)
    changed = _run_on(package, base_checkout, run, X, Y)
    # The round 3 probe: the tree changed but the receipt flags no mutation.
    with pytest.raises(BoundaryOrderError, match="mutation evidence"):
        await ledger.record_candidate_verification(
            B, changed.model_copy(update={"protected_bytes_mutated": False})
        )
    await ledger.record_candidate_verification(B, changed)
    await _pass_refused(ledger, package)


def _timed_out(package: CheckPackage) -> Any:
    return expected_execution(package.checks[0]).model_copy(
        update={
            "status": CheckStatus.INDETERMINATE,
            "reason": "timeout",
            "timed_out": True,
            "return_code": None,
            "signature_seen": False,
            "tier": CheckTier.A,
        }
    )


@pytest.mark.parametrize(
    ("first", "rerun"),
    [((X, X), (X, Y)), ((X, X), (Y, Y)), ((X, Y), (Y, Y))],
    ids=["rerun_tree_changed", "rerun_on_another_candidate", "first_tree_changed"],
)
async def test_a_rerun_pass_on_a_changed_or_other_candidate_is_refused(
    store, base_checkout, first: tuple[str, str], rerun: tuple[str, str]
) -> None:
    ledger, package = await _oracle_verified(store, base_checkout, held_out_passed=None)
    await ledger.record_candidate_verification(
        B, _run_on(package, base_checkout, _timed_out(package), *first)
    )
    run = _oracle_run(package, _PASSING)
    await ledger.record_candidate_verification(B, _run_on(package, base_checkout, run, *rerun))
    await _pass_refused(ledger, package)


async def test_a_rerun_pass_on_the_same_unchanged_candidate_is_recorded(
    store, base_checkout
) -> None:
    ledger, package = await _oracle_verified(store, base_checkout, held_out_passed=None)
    await ledger.record_candidate_verification(
        B, _run_on(package, base_checkout, _timed_out(package), X, X)
    )
    run = _oracle_run(package, _PASSING)
    await ledger.record_candidate_verification(B, _run_on(package, base_checkout, run, X, X))
    (key,) = package.criterion_keys
    await ledger.record_acceptance_reconciled(
        B, package_id=package.package_id, reconciliation=_decision([_criterion(0, key, "pass")])
    )


async def test_a_pass_whose_check_changed_protected_bytes_is_refused(store, base_checkout) -> None:
    ledger, package = await _oracle_verified(store, base_checkout, held_out_passed=None)
    changed = {"protected_digest_after": "9" * 64}
    silent = _oracle_run(package, _PASSING).model_copy(update=changed)
    # A changed protected digest with no changed path: contradictory evidence.
    with pytest.raises(BoundaryOrderError, match="mutation evidence"):
        await ledger.record_candidate_verification(B, _run_on(package, base_checkout, silent, X, X))
    mutated = silent.model_copy(
        update={
            "mutated_paths": ("calc.py",),
            "status": CheckStatus.INDETERMINATE,
            "reason": "protected_bytes_mutated",
        }
    )
    await ledger.record_candidate_verification(B, _run_on(package, base_checkout, mutated, X, X))
    await _pass_refused(ledger, package)


async def test_a_verification_through_another_binding_than_the_bound_one_is_refused(
    store, base_checkout
) -> None:
    ledger, package = await _oracle_verified(store, base_checkout, held_out_passed=None)
    (key,) = package.criterion_keys
    other = Binding(criterion_key=key, symbol="mathutils.other", call_kind=CallKind.FUNCTION)
    run = _oracle_run(package, _PASSING).model_copy(update={"binding": other})
    with pytest.raises(BoundaryOrderError):
        await ledger.record_candidate_verification(B, _run_on(package, base_checkout, run, X, X))
    receipt = _run_on(package, base_checkout, _oracle_run(package, _PASSING), X, X)
    with pytest.raises(BoundaryOrderError):
        await ledger.record_candidate_verification(
            B, receipt.model_copy(update={"bindings": {"oracle_1": other}})
        )


# --------------------------------------------------------------------------
# An oracle result is one the harness and oracle_run can write.


async def test_passing_cases_on_an_unresolved_target_are_refused(store, base_checkout) -> None:
    # The bot's probe: an import_error run whose held-out cases "passed", its
    # source "declared" though no binding was handed to the run.
    ledger, package = await _oracle_verified(store, base_checkout, held_out_passed=None)
    (key,) = package.criterion_keys
    run = _oracle_run(package, _PASSING)
    assert run.oracle_result is not None
    for forged in (
        run.oracle_result.model_copy(update={"resolve": "import_error"}),
        # The run was handed its binding, yet the harness says it used the default.
        run.oracle_result.model_copy(update={"binding_source": "default"}),
    ):
        receipt = _run_on(
            package, base_checkout, run.model_copy(update={"oracle_result": forged}), X, X
        )
        with pytest.raises(BoundaryOrderError, match="harness"):
            await ledger.record_candidate_verification(B, receipt)
    await ledger.record_candidate_verification(B, _run_on(package, base_checkout, run, X, X))
    await ledger.record_acceptance_reconciled(
        B, package_id=package.package_id, reconciliation=_decision([_criterion(0, key, "pass")])
    )


async def test_a_run_through_a_handed_binding_reports_it_as_declared(store, base_checkout) -> None:
    # The product hands every bound oracle its binding (the default one for
    # tier A), so the harness reports it as declared.
    ledger, package = await _oracle_verified(store, base_checkout, held_out_passed=None)
    spec = package.oracle_for("oracle_1")
    assert spec is not None
    run = _oracle_run(package, _PASSING)
    assert run.oracle_result is not None
    handed = run.model_copy(
        update={
            "oracle_result": run.oracle_result.model_copy(update={"binding_source": "declared"})
        }
    )
    receipt = _run_on(package, base_checkout, handed, X, X).model_copy(
        update={"bindings": {"oracle_1": spec.default_binding}}
    )
    await ledger.record_candidate_verification(B, receipt)


@pytest.mark.parametrize(
    "edit",
    ["other_case_ids", "held_out_moved"],
)
async def test_a_case_set_other_than_the_admitted_one_is_refused(
    store, base_checkout, edit: str
) -> None:
    ledger, package = await _oracle_verified(store, base_checkout, held_out_passed=None)
    run = _oracle_run(package, _PASSING)
    assert run.oracle_result is not None
    cases = list(run.oracle_result.cases)
    if edit == "other_case_ids":
        cases[2] = cases[2].model_copy(update={"case_id": "c4"})
    elif edit == "held_out_moved":
        # Same count, but c1 held out and c3 visible: not the cases admission ran.
        cases[0] = cases[0].model_copy(update={"held_out": True})
        cases[2] = cases[2].model_copy(update={"held_out": False})
    result = run.oracle_result.model_copy(update={"cases": tuple(cases)})
    forged = run.model_copy(update={"oracle_result": result})
    with pytest.raises(BoundaryOrderError):
        await ledger.record_candidate_verification(B, _run_on(package, base_checkout, forged, X, X))


async def test_an_admission_whose_oracle_result_contradicts_its_status_is_refused(
    store, base_checkout
) -> None:
    package = seal_package(held_out_package())
    admission = admission_receipt(package, base_checkout)
    # "Reached the failing assertion" on the base, yet every case passed there.
    every = admission.checks[0].model_copy(
        update={
            "oracle_result": oracle_result(
                package, "oracle_1", {"c1": True, "c2": True, "c3": True}
            )
        }
    )
    ledger = BoundaryLedger(store)
    await ledger.record_package_frozen(B, package, seed=seed_for(package))
    with pytest.raises(BoundaryOrderError, match="recorded run|harness"):
        await ledger.record_admission(B, admission.model_copy(update={"checks": (every,)}))
    await ledger.record_admission(B, admission)


async def test_a_script_check_reporting_an_oracle_result_is_refused(
    store, package, base_checkout
) -> None:
    ledger = await _started(store, package, base_checkout)
    await ledger.record_bindings(
        B, package_id=package.package_id, payload=_script_bindings(package)
    )
    foreign = oracle_result(
        seal_package(held_out_package()), "oracle_1", {"c1": True, "c2": True, "c3": True}
    )
    script = candidate_execution(package.checks[0], met=True).model_copy(
        update={"oracle_result": foreign}
    )
    other = candidate_execution(package.checks[1], met=True)
    receipt = verification_receipt(package, base_checkout, (script, other))
    with pytest.raises(BoundaryOrderError, match="script check"):
        await ledger.record_candidate_verification(B, receipt)


# --------------------------------------------------------------------------
# A check's status is the one the product computes from what its run recorded.


async def test_all_passing_cases_labeled_violated_never_decide_a_fail(store, base_checkout) -> None:
    # The round 5 probe: every case passed (exit 0), yet the check claims it
    # was violated, and a fail decision follows.
    ledger, package = await _oracle_verified(store, base_checkout, held_out_passed=None)
    (key,) = package.criterion_keys
    run = _oracle_run(package, _PASSING)
    relabeled = run.model_copy(
        update={"status": CheckStatus.VIOLATED, "reason": "reproduction_still_failing"}
    )
    receipt = _run_on(package, base_checkout, relabeled, X, X)
    with pytest.raises(BoundaryOrderError, match="recorded run"):
        await ledger.record_candidate_verification(B, receipt)
    # Written around the ledger, with the fail decision it would allow: replay flags both.
    await store.append(candidate_verified_event(B, receipt))
    failed = _decision([_criterion(0, key, "fail")])
    await store.append(
        acceptance_reconciled_event(B, package_id=package.package_id, reconciliation=failed)
    )
    violations = verify_boundary_order(await ledger.events(B))
    assert any("recorded run" in problem for problem in violations)
    with pytest.raises(BoundaryOrderError):
        version_state(await ledger.events(B))


@pytest.mark.parametrize(
    ("update", "why"),
    [
        # Failing cases (exit 1) labeled as met.
        ({"status": CheckStatus.EXPECTED, "reason": "passed"}, "failing_labeled_expected"),
        # An observed failure labeled undecided (it was decided).
        (
            {"status": CheckStatus.INDETERMINATE, "reason": "failure_signature_absent"},
            "decided_labeled_indeterminate",
        ),
        # A timed-out run labeled as an observed failure.
        ({"timed_out": True}, "timeout_labeled_violated"),
        # A run with no observation labeled as a verdict.
        (
            {"return_code": None, "signature_seen": False, "oracle_result": None},
            "nothing_observed_labeled_violated",
        ),
        # Nothing observed, but for a reason only an observation gives.
        (
            {
                "status": CheckStatus.INDETERMINATE,
                "reason": "timeout",
                "return_code": None,
                "signature_seen": False,
                "oracle_result": None,
            },
            "unobserved_with_an_observed_reason",
        ),
    ],
    ids=[
        "failing_labeled_expected",
        "decided_labeled_indeterminate",
        "timeout_labeled_violated",
        "nothing_observed_labeled_violated",
        "unobserved_with_an_observed_reason",
    ],
)
async def test_an_oracle_status_other_than_the_recorded_run_shows_is_refused(
    store, base_checkout, update: dict[str, Any], why: str
) -> None:
    ledger, package = await _oracle_verified(store, base_checkout, held_out_passed=None)
    failing = _oracle_run(package, {"c1": True, "c2": False, "c3": False})
    with pytest.raises(BoundaryOrderError):
        await ledger.record_candidate_verification(
            B, _run_on(package, base_checkout, failing.model_copy(update=update), X, X)
        )
    await ledger.record_candidate_verification(B, _run_on(package, base_checkout, failing, X, X))


async def test_an_unobserved_run_is_indeterminate_for_its_recorded_reason(
    store, base_checkout
) -> None:
    ledger, package = await _oracle_verified(store, base_checkout, held_out_passed=None)
    unavailable = _oracle_run(package, _PASSING).model_copy(
        update={
            "status": CheckStatus.INDETERMINATE,
            "reason": "sandbox_unavailable",
            "return_code": None,
            "signature_seen": False,
            "oracle_result": None,
        }
    )
    await ledger.record_candidate_verification(
        B, _run_on(package, base_checkout, unavailable, X, X)
    )


@pytest.mark.parametrize("met", [True, False])
async def test_a_script_status_other_than_its_exit_code_shows_is_refused(
    store, package, base_checkout, met: bool
) -> None:
    ledger = await _started(store, package, base_checkout)
    await ledger.record_bindings(
        B, package_id=package.package_id, payload=_script_bindings(package)
    )
    run = candidate_execution(package.checks[0], met=met)
    flipped = run.model_copy(
        update={
            "status": CheckStatus.VIOLATED if met else CheckStatus.EXPECTED,
            "reason": "reproduction_still_failing" if met else "passed",
        }
    )
    other = candidate_execution(package.checks[1], met=True)
    forged = verification_receipt(package, base_checkout, (run, other)).model_copy(
        update={"checks": (flipped, other)}
    )
    with pytest.raises(BoundaryOrderError, match="recorded run"):
        await ledger.record_candidate_verification(B, forged)
    await ledger.record_candidate_verification(
        B, verification_receipt(package, base_checkout, (run, other))
    )


# --------------------------------------------------------------------------
# One mutation rule for admission and verification; the freeze needs its Seed.


async def test_an_admission_whose_check_changed_protected_bytes_is_not_admitted(
    store, package, base_checkout
) -> None:
    # The round 1 (#2476) probe: one check's protected digest changed, the
    # receipt still admitted. Silent evidence is contradictory; stated
    # evidence makes the check indeterminate and the receipt mutated.
    ledger = BoundaryLedger(store)
    await ledger.record_package_frozen(B, package, seed=seed_for(package))
    admission = admission_receipt(package, base_checkout)
    changed = admission.checks[0].model_copy(update={"protected_digest_after": "9" * 64})
    silent = admission.model_copy(update={"checks": (changed, *admission.checks[1:])})
    with pytest.raises(BoundaryOrderError, match="mutation evidence"):
        await ledger.record_admission(B, silent)
    stated = changed.model_copy(
        update={
            "mutated_paths": ("calc.py",),
            "status": CheckStatus.INDETERMINATE,
            "reason": "protected_bytes_mutated",
        }
    )
    mutated = admission.model_copy(
        update={"checks": (stated, *admission.checks[1:]), "protected_bytes_mutated": True}
    )
    with pytest.raises(BoundaryOrderError):
        await ledger.record_admission(B, mutated)
    events = await ledger.events(B)
    assert [event.type for event in events] == ["boundary.check_package.frozen"]
    await store.append(admission_completed_event(B, silent))
    assert verify_boundary_order(await ledger.events(B)) != ()


async def test_a_package_is_frozen_only_for_the_seed_it_was_built_for(store, package, seed) -> None:
    from ouroboros.boundary.package import CheckPackageError

    from .test_package_identity import _seed

    ledger = BoundaryLedger(store)
    with pytest.raises(CheckPackageError):
        await ledger.record_package_frozen(B, package, seed=_seed())
    with pytest.raises(TypeError):
        await ledger.record_package_frozen(B, package)  # type: ignore[call-arg]
    assert await ledger.events(B) == []
    await ledger.record_package_frozen(B, package, seed=seed)


# --------------------------------------------------------------------------
# Every field that follows from other recorded fields is checked against them.


async def test_an_admitted_receipt_relabelled_rejected_is_refused(
    store, package, base_checkout
) -> None:
    # The round 2 (#2476) probe: two expected checks, an unchanged base, no
    # reasons, and only the verdict changed to rejected.
    ledger = BoundaryLedger(store)
    await ledger.record_package_frozen(B, package, seed=seed_for(package))
    admitted = admission_receipt(package, base_checkout)
    relabelled = admitted.model_copy(update={"verdict": PackageVerdict.REJECTED})
    with pytest.raises(BoundaryOrderError, match="verdict"):
        await ledger.record_admission(B, relabelled)
    assert [e.type for e in await ledger.events(B)] == ["boundary.check_package.frozen"]
    await store.append(admission_completed_event(B, relabelled))
    assert any("verdict" in problem for problem in verify_boundary_order(await ledger.events(B)))


def _admission_variants(package: CheckPackage, checkout: Path) -> dict[str, Any]:
    admitted = admission_receipt(package, checkout)
    rejected = admission_receipt(package, checkout, PackageVerdict.REJECTED)
    return {
        "rejected_relabelled_admitted": rejected.model_copy(
            update={
                "verdict": PackageVerdict.ADMITTED,
                "interpreter_sha256": PIN,
                "interpreter_realpath_sha256": PIN,
            }
        ),
        "rejected_relabelled_indeterminate": rejected.model_copy(
            update={"verdict": PackageVerdict.INDETERMINATE}
        ),
        "reason_dropped": rejected.model_copy(update={"reasons": rejected.reasons[1:]}),
        "reason_added": admitted.model_copy(update={"reasons": ("timeout:script_1_1",)}),
        "reasons_reordered": rejected.model_copy(update={"reasons": rejected.reasons[::-1]}),
        "precondition_with_checks": admitted.model_copy(
            update={"reasons": ("package_path_collision:x",)}
        ),
        "tier_restated": admitted.model_copy(
            update={"check_tiers": dict.fromkeys(admitted.check_tiers or {}, CheckTier.U)}
        ),
        "exclusion_invented": admitted.model_copy(
            update={"excluded_checks": {"script_1_1": "repro_passes_on_base"}}
        ),
        "ends_before_it_starts": admitted.model_copy(
            update={"completed_at": admitted.started_at - timedelta(seconds=1)}
        ),
    }


@pytest.mark.parametrize(
    "variant",
    [
        "rejected_relabelled_admitted",
        "rejected_relabelled_indeterminate",
        "reason_dropped",
        "reason_added",
        "reasons_reordered",
        "precondition_with_checks",
        "tier_restated",
        "exclusion_invented",
        "ends_before_it_starts",
    ],
)
async def test_an_admission_receipt_its_checks_do_not_give_is_refused(
    store, package, base_checkout, variant: str
) -> None:
    ledger = BoundaryLedger(store)
    await ledger.record_package_frozen(B, package, seed=seed_for(package))
    forged = _admission_variants(package, base_checkout)[variant]
    with pytest.raises((BoundaryOrderError, ValueError)):
        await ledger.record_admission(B, forged)
    assert [e.type for e in await ledger.events(B)] == ["boundary.check_package.frozen"]


async def test_a_package_whose_every_check_is_excluded_stays_rejected(
    store, package, base_checkout
) -> None:
    from ouroboros.boundary.per_check import ALL_CHECKS_EXCLUDED

    admitted = admission_receipt(package, base_checkout)
    passed_on_base = admitted.checks[0].model_copy(
        update={
            "status": CheckStatus.VIOLATED,
            "reason": "reproduction_passed_on_base",
            "return_code": 0,
            "signature_seen": False,
        }
    )
    failed_on_base = admitted.checks[1].model_copy(
        update={"status": CheckStatus.VIOLATED, "reason": "preservation_failed", "return_code": 1}
    )
    genuine = settled_admission(
        package, admitted.model_copy(update={"checks": (passed_on_base, failed_on_base)})
    )
    assert genuine.verdict is PackageVerdict.REJECTED
    assert genuine.reasons[-1] == ALL_CHECKS_EXCLUDED
    ledger = BoundaryLedger(store)
    await ledger.record_package_frozen(B, package, seed=seed_for(package))
    with pytest.raises(BoundaryOrderError):
        await ledger.record_admission(
            B, genuine.model_copy(update={"reasons": genuine.reasons[:-1]})
        )
    await ledger.record_admission(B, genuine)


@pytest.mark.parametrize(
    "edit",
    ["verdict_pass_over_a_failure", "verdict_fail_over_passes", "reason_dropped"],
)
async def test_a_verification_receipt_its_checks_do_not_give_is_refused(
    store, package, base_checkout, edit: str
) -> None:
    ledger = await _started(store, package, base_checkout)
    await ledger.record_bindings(
        B, package_id=package.package_id, payload=_script_bindings(package)
    )
    failing = verification_receipt(
        package,
        base_checkout,
        (
            candidate_execution(package.checks[0], met=False),
            candidate_execution(package.checks[1], met=True),
        ),
    )
    passing = verification_receipt(
        package,
        base_checkout,
        tuple(candidate_execution(check, met=True) for check in package.checks),
    )
    forged = {
        "verdict_pass_over_a_failure": failing.model_copy(
            update={"verdict": CandidateVerdict.PASS}
        ),
        "verdict_fail_over_passes": passing.model_copy(update={"verdict": CandidateVerdict.FAIL}),
        "reason_dropped": failing.model_copy(update={"reasons": ()}),
    }[edit]
    with pytest.raises(BoundaryOrderError):
        await ledger.record_candidate_verification(B, forged)
    await ledger.record_candidate_verification(B, failing)


async def test_a_verification_selects_every_runnable_check_with_its_binding(
    store, base_checkout
) -> None:
    ledger, package = await _oracle_verified(store, base_checkout, held_out_passed=None)
    run = _oracle_run(package, _PASSING)
    genuine = verification_receipt(package, base_checkout, (run,))
    for forged in (
        genuine.model_copy(update={"bindings": None}),  # the binding handed is not recorded
        genuine.model_copy(update={"check_tiers": None}),  # no selection recorded
    ):
        with pytest.raises(BoundaryOrderError):
            await ledger.record_candidate_verification(B, forged)
    await ledger.record_candidate_verification(B, genuine)


async def test_bindings_state_the_reason_and_attempt_their_assignment_gives(
    store, package, base_checkout
) -> None:
    ledger = await _started(store, package, base_checkout)
    good = _script_bindings(package).model_dump()["checks"]
    repair = [c for c in good if c["check_id"] == package.checks[0].check_id]
    for forged in (
        {"phase": "final", "checks": [{**good[0], "reason": "because"}, good[1]]},
        {"phase": "final", "checks": good, "root_ac_index": 0, "retry_attempt": 0},
        {"phase": "repair", "checks": repair},  # no attempt named
        {"phase": "repair", "checks": good, "root_ac_index": 0, "retry_attempt": 0},
    ):
        with pytest.raises(BoundaryOrderError):
            await ledger.record_bindings(
                B, package_id=package.package_id, payload=BindingsPayload.model_validate(forged)
            )
    await ledger.record_bindings(
        B,
        package_id=package.package_id,
        payload=BindingsPayload.model_validate(
            {"phase": "repair", "checks": repair, "root_ac_index": 0, "retry_attempt": 0}
        ),
    )


def test_a_manifest_whose_schema_or_files_disagree_with_its_oracles_is_refused() -> None:
    from pydantic import ValidationError

    from ouroboros.boundary.events import ManifestRecord

    summary = seal_package(held_out_package()).manifest_summary()
    ManifestRecord.model_validate(summary)
    without_data = [item for item in summary["files"] if item["kind"] != "oracle_data"]
    for forged in (
        {**summary, "schema_version": "ouroboros.check_package.v1"},
        {**summary, "binding_grammar": "another.grammar"},
        {**summary, "files": without_data},
    ):
        with pytest.raises(ValidationError):
            ManifestRecord.model_validate(forged)
