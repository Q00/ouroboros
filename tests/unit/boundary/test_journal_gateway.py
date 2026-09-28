"""One journal gateway: every record is closed-schema and bound to the frozen identities.

Replay (``verify_boundary_order``, ``recovery_projection``) and every write go
through ``events.validate_record`` before any transition or projection, so a
record the product could not have written grants nothing: a partial binding,
a verification without its receipt, a decision for a criterion the frozen
manifest does not name, or a malformed run-level record.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError
import pytest

from ouroboros.boundary.binding import CheckTier
from ouroboros.boundary.events import (
    ACCEPTANCE_RECONCILED,
    ACCEPTANCE_RESUMED,
    ACTOR_STARTED,
    BINDING_RECORDED,
    CANDIDATE_VERIFIED,
    CHECK_PACKAGE_ENABLED,
    BindingsPayload,
    ReconciliationPayload,
    RunContract,
    actor_started_event,
    admission_completed_event,
    binding_recorded_event,
    boundary_version_id,
    candidate_verified_event,
    package_frozen_event,
    validate_record,
)
from ouroboros.boundary.ledger import (
    BoundaryLedger,
    BoundaryOrderError,
    RecoveryBound,
    RecoveryUndecidable,
    recovery_projection,
    verify_boundary_order,
    version_state,
)
from ouroboros.boundary.oracle import CaseResult, OracleResult
from ouroboros.boundary.package import CheckPackage
from ouroboros.boundary.receipts import PackageVerdict
from ouroboros.events.base import BaseEvent
from ouroboros.persistence.event_store import EventStore

from .journal_fixtures import (
    admission_receipt,
    criterion,
    decision_data,
    expected_execution,
    final_bindings,
    seed_for,
    verification_receipt,
)

RUN = "run_gateway"
VERSION = boundary_version_id(RUN, 1)
T0 = datetime(2026, 9, 29, tzinfo=UTC)
CONTRACT = RunContract(check_timeout_seconds=120)


@pytest.fixture
async def store():
    event_store = EventStore("sqlite+aiosqlite:///:memory:")
    await event_store.initialize()
    yield event_store
    await event_store.close()


def _timed(events: list[BaseEvent]) -> list[BaseEvent]:
    return [
        event.model_copy(update={"timestamp": T0 + timedelta(seconds=n + 1)})
        for n, event in enumerate(events)
    ]


def _planted(event_type: str, data: dict[str, Any], aggregate: str = VERSION) -> BaseEvent:
    return BaseEvent(type=event_type, aggregate_type="boundary", aggregate_id=aggregate, data=data)


def _enabled() -> list[BaseEvent]:
    event = _planted(
        CHECK_PACKAGE_ENABLED, {"execution_id": RUN, "contract": CONTRACT.journal_data()}, RUN
    )
    return [event.model_copy(update={"timestamp": T0})]


def _started(package: CheckPackage, checkout: Path) -> list[BaseEvent]:
    """Freeze, admission and worker start, each written by its factory."""
    return [
        package_frozen_event(VERSION, package),
        admission_completed_event(VERSION, admission_receipt(package, checkout)),
        actor_started_event(VERSION, actor_id=RUN, package_id=package.package_id, runtime=None),
    ]


def _final_bindings(package: CheckPackage) -> BaseEvent:
    return binding_recorded_event(
        VERSION, package_id=package.package_id, payload=final_bindings(package)
    )


def _decision(keys: tuple[str, ...], *statuses: str, **update: Any) -> dict[str, Any]:
    """A decision the reconciliation rule can write (by default every criterion a pass)."""
    statuses = statuses or ("pass",) * len(keys)
    return decision_data(
        [
            criterion(index, key, status, **update)
            for index, (key, status) in enumerate(zip(keys, statuses, strict=True))
        ]
    )


# --------------------------------------------------------------------------
# The bot's probe: records the product could not write are flagged on replay.


def test_a_planted_partial_final_binding_is_flagged(package, base_checkout) -> None:
    planted = _planted(
        BINDING_RECORDED,
        {"phase": "final", "checks": [{"tier": "A"}], "package_id": package.package_id},
    )
    journal = _timed([*_started(package, base_checkout), planted])
    assert verify_boundary_order(journal) != ()


def test_a_planted_verification_with_only_package_and_seed_ids_is_flagged(
    package, base_checkout
) -> None:
    planted = _planted(
        CANDIDATE_VERIFIED,
        {"package_id": package.package_id, "seed_digest": package.seed_digest},
    )
    journal = _timed([*_started(package, base_checkout), _final_bindings(package), planted])
    assert verify_boundary_order(journal) != ()


def test_an_accepted_decision_for_a_criterion_the_manifest_lacks_is_flagged(
    package, base_checkout
) -> None:
    verified = [
        *_started(package, base_checkout),
        _final_bindings(package),
        candidate_verified_event(VERSION, verification_receipt(package, base_checkout)),
    ]
    decided = _planted(
        ACCEPTANCE_RECONCILED, {**_decision(("k",)), "package_id": package.package_id}
    )
    assert verify_boundary_order(_timed(verified)) == ()
    violations = verify_boundary_order(_timed([*verified, decided]))
    assert any("frozen manifest" in problem for problem in violations)
    # A decision over exactly the manifest's criteria, with the statuses the
    # (empty, so untrusted) verification supports, is recorded.
    supported = _decision(package.criterion_keys, "indeterminate", "indeterminate", "uncovered")
    genuine = _planted(ACCEPTANCE_RECONCILED, {**supported, "package_id": package.package_id})
    assert verify_boundary_order(_timed([*verified, genuine])) == ()


def test_bindings_naming_a_check_the_package_lacks_are_flagged(package, base_checkout) -> None:
    payload = BindingsPayload.model_validate(
        {
            "phase": "final",
            "checks": [
                {
                    "criterion_key": package.criterion_keys[0],
                    "check_id": "c1",
                    "tier": "A",
                    "binding_source": None,
                    "binding": None,
                    "status_hint": None,
                    "reason": "r",
                    "declared": None,
                }
            ],
        }
    )
    forged = binding_recorded_event(VERSION, package_id=package.package_id, payload=payload)
    assert verify_boundary_order(_timed([*_started(package, base_checkout), forged])) != ()


# --------------------------------------------------------------------------
# Run-level records: a malformed or impossible one makes recovery undecidable.


def _versions(package: CheckPackage, checkout: Path) -> dict[int, list[BaseEvent]]:
    return {1: _timed(_started(package, checkout))}


def _undecided_resume(keys: tuple[str, ...]) -> dict[str, Any]:
    reason = "boundary_record_missing"
    return {
        **_decision(keys, *("indeterminate",) * len(keys), reason=reason),
        "source": "none",
        "held_out_checks": [],
        "reason": reason,
        "package_id": None,
    }


def _run_resumed(data: dict[str, Any]) -> BaseEvent:
    event = _planted(ACCEPTANCE_RESUMED, data, RUN)
    return event.model_copy(update={"timestamp": T0 + timedelta(minutes=5)})


@pytest.mark.parametrize(
    "data",
    [
        {"source": "memory"},
        {"package_id": None},
        "cites_a_package",
        "claims_a_pass",
        "names_held_out_checks",
    ],
    ids=["partial", "empty", "cites_a_package", "claims_a_pass", "names_held_out_checks"],
)
def test_a_malformed_run_level_resume_makes_recovery_undecidable(
    package, base_checkout, data: Any
) -> None:
    versions = _versions(package, base_checkout)
    assert isinstance(recovery_projection(RUN, _enabled(), versions), RecoveryBound)
    keys = package.criterion_keys
    if data == "cites_a_package":
        data = {**_undecided_resume(keys), "package_id": package.package_id}
    elif data == "claims_a_pass":
        data = {**_undecided_resume(keys), **_decision(keys)}
    elif data == "names_held_out_checks":
        data = {**_undecided_resume(keys), "held_out_checks": [package.checks[0].check_id]}
    projection = recovery_projection(RUN, [*_enabled(), _run_resumed(data)], versions)
    assert isinstance(projection, RecoveryUndecidable)


def test_a_well_formed_run_level_resume_on_a_usable_boundary_makes_recovery_undecidable(
    package, base_checkout
) -> None:
    # The product records a run-level resume only when recovery was already
    # undecidable; appended to a usable history it is one it could not write.
    resumed = _run_resumed(_undecided_resume(package.criterion_keys))
    validate_record(resumed, None)
    versions = _versions(package, base_checkout)
    assert isinstance(recovery_projection(RUN, _enabled(), versions), RecoveryBound)
    projection = recovery_projection(RUN, [*_enabled(), resumed], versions)
    assert isinstance(projection, RecoveryUndecidable)


async def test_a_run_level_resume_is_refused_while_the_boundary_is_usable(
    package, base_checkout
) -> None:
    from ouroboros.boundary.events import ResumedPayload

    store = EventStore("sqlite+aiosqlite:///:memory:")
    await store.initialize()
    try:
        ledger = BoundaryLedger(store)
        await ledger.record_check_package_enabled(RUN, CONTRACT)
        await ledger.record_package_frozen(VERSION, package, seed=seed_for(package))
        await ledger.record_admission(VERSION, admission_receipt(package, base_checkout))
        await ledger.record_actor_started(RUN, [VERSION])
        data = _undecided_resume(package.criterion_keys)
        payload = ResumedPayload.model_validate(
            {k: v for k, v in data.items() if k != "package_id"}
        )
        with pytest.raises(BoundaryOrderError, match="unusable"):
            await ledger.record_resumed_undecided(RUN, payload=payload)
    finally:
        await store.close()


def test_a_malformed_enabled_record_makes_recovery_undecidable(package, base_checkout) -> None:
    enabled = _enabled()[0]
    extra = enabled.model_copy(update={"data": {**enabled.data, "note": "x"}})
    other_run = enabled.model_copy(update={"data": {**enabled.data, "execution_id": "other"}})
    for record in (extra, other_run):
        projection = recovery_projection(RUN, [record], _versions(package, base_checkout))
        assert isinstance(projection, RecoveryUndecidable)


def test_a_version_holding_another_aggregate_makes_recovery_undecidable(
    package, base_checkout
) -> None:
    # Another run's well-formed version, handed in as this run's v1.
    other = boundary_version_id("run_other", 1)
    moved = [
        package_frozen_event(other, package),
        admission_completed_event(other, admission_receipt(package, base_checkout)),
        actor_started_event(
            other, actor_id="run_other", package_id=package.package_id, runtime=None
        ),
    ]
    assert verify_boundary_order(_timed(moved)) == ()
    projection = recovery_projection(RUN, _enabled(), {1: _timed(moved)})
    assert isinstance(projection, RecoveryUndecidable)


# --------------------------------------------------------------------------
# Genuine receipts pass the gateway (it never refuses what the product writes).


def test_every_receipt_the_product_writes_passes_the_gateway(package, base_checkout) -> None:
    frozen = package_frozen_event(VERSION, package)
    identity = version_state([frozen]).identity
    rejected = admission_receipt(package, base_checkout, PackageVerdict.REJECTED)
    admitted = admission_receipt(package, base_checkout)
    check = package.checks[0]
    oracle = OracleResult(
        check_id="oracle_1",
        criterion_key=check.assertions[0].criterion_key,
        binding_source="default",
        symbol="add",
        call_kind="function",
        resolve="ok",
        cases=(CaseResult(case_id="c1", held_out=True, passed=True, detail="secret"),),
    )
    with_oracle = admitted.model_copy(
        update={
            "checks": (
                expected_execution(check).model_copy(
                    update={"tier": CheckTier.A, "oracle_result": oracle}
                ),
                *admitted.checks[1:],
            )
        }
    )
    for receipt in (rejected, admitted, with_oracle):
        validate_record(admission_completed_event(VERSION, receipt), identity)
    verification = verification_receipt(package, base_checkout)
    validate_record(candidate_verified_event(VERSION, verification), identity)


# --------------------------------------------------------------------------
# Follow-up: a batch of actor starts is validated cumulatively before one append.


async def test_a_duplicate_boundary_in_one_start_batch_is_refused_before_append(
    store, package, base_checkout
) -> None:
    ledger = BoundaryLedger(store)
    await ledger.record_package_frozen("b", package, seed=seed_for(package))
    await ledger.record_admission("b", admission_receipt(package, base_checkout))
    with pytest.raises(BoundaryOrderError, match="already started"):
        await ledger.record_actor_started("a", ["b", "b"])
    events = await ledger.events("b")
    assert all(event.type != ACTOR_STARTED for event in events)
    assert verify_boundary_order(events) == ()


# --------------------------------------------------------------------------
# The decisive authority of a pass is its own field; the display tier has none.


def _one(**update: Any) -> dict[str, Any]:
    return _decision(("k",), **update)


def test_a_declared_binding_pass_is_recorded_whatever_the_display_tier() -> None:
    # An A' oracle plus an advisory script shows tier S; the flag keeps the provenance.
    record = ReconciliationPayload.model_validate(
        _one(
            tier="S",
            declared_binding_pass=True,
            governed_by="existing_verifier",
            existing_accepted=False,
            accepted=False,
        )
    )
    assert record.criteria[0].declared_binding_pass is True


@pytest.mark.parametrize(
    "update",
    [
        # The display tier alone never lets the existing verifier decide a pass.
        {"tier": "A_prime", "governed_by": "existing_verifier", "declared_binding_pass": False},
        # A declared-binding pass never overrules a legacy rejection.
        {"declared_binding_pass": True, "existing_accepted": False, "accepted": True},
        # Only a pass can rest on a declared binding.
        {"declared_binding_pass": True, "package_status": "fail", "accepted": False},
    ],
    ids=["display_tier_has_no_authority", "overrules_a_rejection", "not_a_pass"],
)
def test_a_decision_the_provenance_rule_forbids_is_refused(update: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ReconciliationPayload.model_validate(_one(**update))


def test_the_provenance_flag_is_required() -> None:
    data = _one()
    del data["criteria"][0]["declared_binding_pass"]
    with pytest.raises(ValidationError):
        ReconciliationPayload.model_validate(data)
