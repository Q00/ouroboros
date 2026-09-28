"""The boundary lifecycle as one fail-closed matrix (the #2458 round-4 reset evidence).

Two parts:

- every (phase, record) pair of a boundary version, reached through records
  the product writes, is either allowed or refused on write and flagged on
  replay, exactly as ``ledger.TRANSITIONS`` says;
- the four authority transitions round 4 reproduced are each refused before
  any authority or persistence is published: a tier claim a script check
  cannot have, a worker start on a rejected admission (and a verification
  before any worker start), a file planted at a record target, and a
  constructor reply whose criterion number is out of domain.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ouroboros.boundary.binding import CheckTier
from ouroboros.boundary.events import (
    ACCEPTANCE_RECONCILED,
    ACCEPTANCE_RESUMED,
    ACTOR_STARTED,
    ADMISSION_COMPLETED,
    BINDING_RECORDED,
    CANDIDATE_VERIFIED,
    CONSTRUCTION_FAILED,
    PACKAGE_FROZEN,
    REFERENCE_CHECKED,
    SUPERSEDED,
    BindingsPayload,
    ReconciliationPayload,
    ReferenceCheckPayload,
    ResumedPayload,
    acceptance_reconciled_event,
    acceptance_resumed_event,
    actor_started_event,
    admission_completed_event,
    binding_recorded_event,
    boundary_version_id,
    candidate_verified_event,
    construction_failed_event,
    package_frozen_event,
    reference_checked_event,
    superseded_event,
)
from ouroboros.boundary.ledger import (
    TRANSITIONS,
    VERSION_RECORDS,
    BoundaryOrderError,
    Phase,
    VersionState,
    admitted_exclusions,
    advance,
    frozen_manifest,
    verify_boundary_order,
    version_state,
)
from ouroboros.boundary.oracle_build import ReplyError, ReplyFailure, normalize_reply
from ouroboros.boundary.package import (
    CheckPackage,
    CheckPackageError,
    seal_package,
    write_package_record,
)
from ouroboros.boundary.receipts import CheckStatus, PackageVerdict
from ouroboros.events.base import BaseEvent

from .clamp_fixtures import _oracle
from .conftest import build_package
from .journal_fixtures import (
    admission_receipt,
    criterion,
    decision_data,
    expected_execution,
    final_bindings,
    verification_receipt,
)

RUN = "run_matrix"
BOUNDARY = boundary_version_id(RUN, 1)
SUCCESSOR = boundary_version_id(RUN, 2)
T0 = datetime(2026, 9, 28, tzinfo=UTC)


def _bindings(phase: str, package: CheckPackage) -> BindingsPayload:
    return final_bindings(package, phase)


def _undecided(package: CheckPackage) -> dict[str, Any]:
    """A decision the package could not make: every criterion indeterminate, none accepted."""
    reason = "authority_error:OSError"
    return decision_data(
        [
            criterion(index, key, "indeterminate", reason=reason)
            for index, key in enumerate(package.criterion_keys)
        ],
        undecided_reason=reason,
    )


class _Records:
    """One well-formed record of every kind, for a sealed package or for none."""

    def __init__(self, package: CheckPackage, checkout: Path) -> None:
        self.package = package
        self.checkout = checkout

    def make(self, kind: str, *, with_package: bool) -> BaseEvent:
        package_id = self.package.package_id if with_package else None
        if kind == PACKAGE_FROZEN:
            return package_frozen_event(BOUNDARY, self.package)
        if kind == CONSTRUCTION_FAILED:
            return construction_failed_event(
                BOUNDARY, seed_digest=self.package.seed_digest, input_digest="1" * 64, reason="r"
            )
        if kind == REFERENCE_CHECKED:
            payload = ReferenceCheckPayload(
                schema_version="ouroboros.reference_check.v2", excluded_cases=(), uncovered=()
            )
            return reference_checked_event(BOUNDARY, package_id=str(package_id), payload=payload)
        if kind == ADMISSION_COMPLETED:
            return admission_completed_event(
                BOUNDARY, admission_receipt(self.package, self.checkout)
            )
        if kind == ACTOR_STARTED:
            return actor_started_event(BOUNDARY, actor_id=RUN, package_id=package_id, runtime=None)
        if kind == SUPERSEDED:
            return superseded_event(
                BOUNDARY,
                superseded_by=SUCCESSOR,
                package_id=package_id,
                successor_package_id=None,
                reason="r",
            )
        if kind == BINDING_RECORDED:
            # A resumed run's bindings: the one binding record every started phase allows.
            return binding_recorded_event(
                BOUNDARY, package_id=str(package_id), payload=_bindings("resumed", self.package)
            )
        if kind == CANDIDATE_VERIFIED:
            # Every check timed out: a run, and a re-run of it, the product can write.
            timed_out = tuple(
                expected_execution(check).model_copy(
                    update={
                        "status": CheckStatus.INDETERMINATE,
                        "reason": "timeout",
                        "timed_out": True,
                        "return_code": None,
                        "signature_seen": False,
                        "tier": CheckTier.S,
                    }
                )
                for check in self.package.checks
            )
            return candidate_verified_event(
                BOUNDARY, verification_receipt(self.package, self.checkout, timed_out)
            )
        if kind == ACCEPTANCE_RECONCILED:
            return acceptance_reconciled_event(
                BOUNDARY,
                package_id=package_id,
                reconciliation=ReconciliationPayload.model_validate(_undecided(self.package)),
            )
        if kind == ACCEPTANCE_RESUMED:
            payload = ResumedPayload.model_validate(
                {**_undecided(self.package), "source": "memory"}
            )
            return acceptance_resumed_event(BOUNDARY, package_id=package_id, payload=payload)
        raise AssertionError(kind)

    def final_bindings(self) -> BaseEvent:
        return binding_recorded_event(
            BOUNDARY, package_id=self.package.package_id, payload=_bindings("final", self.package)
        )

    def rejected_admission(self) -> BaseEvent:
        return admission_completed_event(
            BOUNDARY, admission_receipt(self.package, self.checkout, PackageVerdict.REJECTED)
        )


def _timed(events: list[BaseEvent]) -> list[BaseEvent]:
    return [
        event.model_copy(update={"timestamp": T0 + timedelta(seconds=n)})
        for n, event in enumerate(events)
    ]


# Each state: the records that reach it, whether its records cite a package,
# and the records the lifecycle allows next (every other record is refused).
STATES: dict[str, tuple[tuple[str, ...], bool, frozenset[str]]] = {
    "none": ((), True, frozenset({PACKAGE_FROZEN, CONSTRUCTION_FAILED})),
    "frozen": (
        (PACKAGE_FROZEN,),
        True,
        frozenset({REFERENCE_CHECKED, ADMISSION_COMPLETED, SUPERSEDED}),
    ),
    "rejected": ((PACKAGE_FROZEN, "rejected_admission"), True, frozenset({SUPERSEDED})),
    "admitted": (
        (PACKAGE_FROZEN, ADMISSION_COMPLETED),
        True,
        frozenset({ACTOR_STARTED, SUPERSEDED}),
    ),
    "started": (
        (PACKAGE_FROZEN, ADMISSION_COMPLETED, ACTOR_STARTED, "final_bindings"),
        True,
        frozenset(
            {BINDING_RECORDED, CANDIDATE_VERIFIED, ACCEPTANCE_RECONCILED, ACCEPTANCE_RESUMED}
        ),
    ),
    "verified": (
        (PACKAGE_FROZEN, ADMISSION_COMPLETED, ACTOR_STARTED, "final_bindings", CANDIDATE_VERIFIED),
        True,
        frozenset(
            {BINDING_RECORDED, CANDIDATE_VERIFIED, ACCEPTANCE_RECONCILED, ACCEPTANCE_RESUMED}
        ),
    ),
    "decided": (
        (
            PACKAGE_FROZEN,
            ADMISSION_COMPLETED,
            ACTOR_STARTED,
            "final_bindings",
            CANDIDATE_VERIFIED,
            ACCEPTANCE_RECONCILED,
        ),
        True,
        frozenset({BINDING_RECORDED, ACCEPTANCE_RESUMED}),
    ),
    "resuming": (
        (
            PACKAGE_FROZEN,
            ADMISSION_COMPLETED,
            ACTOR_STARTED,
            "final_bindings",
            CANDIDATE_VERIFIED,
            ACCEPTANCE_RECONCILED,
            BINDING_RECORDED,
        ),
        True,
        frozenset({BINDING_RECORDED, CANDIDATE_VERIFIED, ACCEPTANCE_RESUMED}),
    ),
    "superseded": ((PACKAGE_FROZEN, SUPERSEDED), True, frozenset()),
    "no_package": ((CONSTRUCTION_FAILED,), False, frozenset({ACTOR_STARTED, SUPERSEDED})),
    "no_package_started": (
        (CONSTRUCTION_FAILED, ACTOR_STARTED),
        False,
        frozenset({ACCEPTANCE_RECONCILED}),
    ),
    "no_package_decided": (
        (CONSTRUCTION_FAILED, ACTOR_STARTED, ACCEPTANCE_RECONCILED),
        False,
        frozenset(),
    ),
}


@pytest.fixture
def records(seed, base_checkout) -> _Records:
    return _Records(seal_package(build_package(seed)), base_checkout)


def _path(records: _Records, state: str) -> list[BaseEvent]:
    steps, with_package, _allowed = STATES[state]
    events = []
    for step in steps:
        if step == "final_bindings":
            events.append(records.final_bindings())
        elif step == "rejected_admission":
            events.append(records.rejected_admission())
        else:
            events.append(records.make(step, with_package=with_package))
    return events


@pytest.mark.parametrize("state", sorted(STATES))
@pytest.mark.parametrize("kind", VERSION_RECORDS)
def test_every_phase_allows_exactly_the_records_the_lifecycle_names(
    records: _Records, state: str, kind: str
) -> None:
    _steps, with_package, allowed = STATES[state]
    journal = _timed([*_path(records, state), records.make(kind, with_package=with_package)])
    reached = version_state(journal[:-1])  # the path itself is always valid
    if kind in allowed:
        advance(reached, journal[-1])
        assert not any(
            "recorded" in problem and kind in problem for problem in verify_boundary_order(journal)
        )
        return
    with pytest.raises(BoundaryOrderError) as refused:
        advance(reached, journal[-1])
    # Replay flags exactly what the write refused.
    assert refused.value.message in verify_boundary_order(journal)


def test_the_transition_table_is_the_documented_lifecycle() -> None:
    assert set(TRANSITIONS) == {
        (Phase.NONE, PACKAGE_FROZEN),
        (Phase.NONE, CONSTRUCTION_FAILED),
        (Phase.FROZEN, REFERENCE_CHECKED),
        (Phase.FROZEN, ADMISSION_COMPLETED),
        (Phase.FROZEN, SUPERSEDED),
        (Phase.REJECTED, SUPERSEDED),
        (Phase.ADMITTED, SUPERSEDED),
        (Phase.NO_PACKAGE, SUPERSEDED),
        (Phase.ADMITTED, ACTOR_STARTED),
        (Phase.NO_PACKAGE, ACTOR_STARTED),
        (Phase.STARTED, BINDING_RECORDED),
        (Phase.VERIFIED, BINDING_RECORDED),
        (Phase.DECIDED, BINDING_RECORDED),
        (Phase.RESUMING, BINDING_RECORDED),
        (Phase.RESUMING, CANDIDATE_VERIFIED),
        (Phase.RESUMING, ACCEPTANCE_RESUMED),
        (Phase.STARTED, CANDIDATE_VERIFIED),
        (Phase.VERIFIED, CANDIDATE_VERIFIED),
        (Phase.STARTED, ACCEPTANCE_RECONCILED),
        (Phase.VERIFIED, ACCEPTANCE_RECONCILED),
        (Phase.STARTED, ACCEPTANCE_RESUMED),
        (Phase.VERIFIED, ACCEPTANCE_RESUMED),
        (Phase.DECIDED, ACCEPTANCE_RESUMED),
    }
    assert VersionState().phase is Phase.NONE


# --------------------------------------------------------------------------
# The four transitions round 4 reproduced, each refused before publication.


def _script_tier_a(records: _Records) -> None:
    # An admission record that gives a model-written script check tier A:
    # admission never writes one (a script is tier S), and the ledger
    # refuses to publish it.
    frozen = package_frozen_event(BOUNDARY, records.package)
    receipt = admission_receipt(records.package, records.checkout)
    tiers = dict.fromkeys(receipt.check_tiers or {}, "A")
    data = admission_completed_event(BOUNDARY, receipt).data
    admitted_exclusions(frozen_manifest(frozen.data), {**data, "check_tiers": tiers})


def _start_on_rejected_admission(records: _Records) -> None:
    journal = _timed(
        [
            records.make(PACKAGE_FROZEN, with_package=True),
            records.rejected_admission(),
            records.make(ACTOR_STARTED, with_package=True),
        ]
    )
    version_state(journal)


def _verification_before_any_worker(records: _Records) -> None:
    journal = _timed(
        [
            records.make(PACKAGE_FROZEN, with_package=True),
            records.make(ADMISSION_COMPLETED, with_package=True),
            records.make(CANDIDATE_VERIFIED, with_package=True),
        ]
    )
    version_state(journal)


def _planted_record(records: _Records) -> None:
    store = records.checkout.parent / "store"
    store.mkdir()
    (store / f"{records.package.package_id}.json").write_bytes(b"corrupt")
    write_package_record(records.package, store)


def _criterion_zero(records: _Records) -> None:
    oracle = _oracle(1, "oracle_1", "reproduction", (15, 0, 10), 10)
    normalize_reply({"oracles": [{**oracle, "criterion": 0}]})


@pytest.mark.parametrize(
    ("attempt", "refusal"),
    [
        (_script_tier_a, BoundaryOrderError),
        (_start_on_rejected_admission, BoundaryOrderError),
        (_verification_before_any_worker, BoundaryOrderError),
        (_planted_record, CheckPackageError),
        (_criterion_zero, ReplyError),
    ],
    ids=[
        "script_check_tier_a",
        "start_on_rejected_admission",
        "verification_before_any_worker",
        "planted_record",
        "criterion_zero",
    ],
)
def test_round_four_authority_transitions_are_refused_before_publication(
    records: _Records, attempt: Any, refusal: type[Exception]
) -> None:
    with pytest.raises(refusal) as refused:
        attempt(records)
    if isinstance(refused.value, ReplyError):
        assert refused.value.code is ReplyFailure.CRITERION_INVALID
