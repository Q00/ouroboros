"""The check package decision a run recorded, as formal evaluation reads it.

Formal evaluation (``ouroboros_evaluate``) must not decide again what the
controller already decided by running the frozen package. It reads the one
decision the journal admitted for the run: the run's journal is replayed under
the product's rule (``ledger.recovery_projection``), and the decision on the
version the worker was bound to is returned only when its frozen package was
built for this exact Seed (the recorded Seed digest) and the decision names
the Seed's criteria in Seed order. Anything else yields no decision, which is
no evidence, never a pass.
"""

from __future__ import annotations

from ouroboros.boundary.events import (
    BOUNDARY_AGGREGATE_TYPE,
    PACKAGE_FROZEN,
    CriterionDecisionRecord,
    parse_boundary_version,
)
from ouroboros.boundary.ledger import (
    BoundaryLedger,
    RecoveryBound,
    recovery_projection,
    version_state,
)
from ouroboros.boundary.package import seed_criterion_keys, seed_digest
from ouroboros.core.seed import Seed
from ouroboros.persistence.event_store import EventStore


async def recorded_criterion_decisions(
    store: EventStore, execution_id: str, seed: Seed
) -> tuple[CriterionDecisionRecord, ...]:
    """The run's recorded per-criterion decisions, in Seed order; empty when there are none.

    Empty when the check package was off or no package was admitted, when
    the journal is not one the product could write, when no decision was
    recorded yet, when the package was frozen for another Seed, or when the
    decision does not name the Seed's criteria in order.
    """
    ledger = BoundaryLedger(store)
    versions = await ledger.run_versions(execution_id)
    projection = recovery_projection(execution_id, await ledger.events(execution_id), versions)
    if not isinstance(projection, RecoveryBound) or projection.seed_digest != seed_digest(seed):
        return ()
    parsed = parse_boundary_version(projection.boundary_id)
    if parsed is None:
        return ()
    decision = version_state(versions[parsed[1]]).decision
    if decision is None:
        return ()
    if tuple(item.criterion_key for item in decision.criteria) != seed_criterion_keys(seed):
        return ()
    return decision.criteria


_PAGE = 500


async def recorded_execution_for_seed(store: EventStore, seed: Seed) -> str | None:
    """The latest run whose check package was frozen for exactly this Seed, if any.

    A generation evaluated after a resume has no in-memory record of its run;
    the journal names it: a frozen package cites its Seed digest.
    """
    digest = seed_digest(seed)
    latest: tuple[object, str] | None = None
    offset = 0
    while True:
        page = await store.query_events(
            aggregate_type=BOUNDARY_AGGREGATE_TYPE,
            event_type=PACKAGE_FROZEN,
            limit=_PAGE,
            offset=offset,
        )
        for event in page:
            run = parse_boundary_version(event.aggregate_id)
            if run is None or event.data.get("seed_digest") != digest:
                continue
            if latest is None or event.timestamp >= latest[0]:  # type: ignore[operator]
                latest = (event.timestamp, run[0])
        if len(page) < _PAGE:
            break
        offset += _PAGE
    return None if latest is None else latest[1]
