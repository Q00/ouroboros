"""The check package decision a run recorded, as formal evaluation reads it.

Formal evaluation (``ouroboros_evaluate``) must not decide again what the
controller already decided by running the frozen package. It reads the one
decision the journal admitted for the run: the run's journal is replayed under
the product's rule (``ledger.recovery_projection``), and the decision on the
version the worker was bound to is returned only when it names exactly the
Seed's criteria, in Seed order. Anything else yields no decision, which is no
evidence, never a pass.
"""

from __future__ import annotations

from ouroboros.boundary.events import CriterionDecisionRecord, parse_boundary_version
from ouroboros.boundary.ledger import (
    BoundaryLedger,
    RecoveryBound,
    RecoveryNoPackage,
    recovery_projection,
    version_state,
)
from ouroboros.boundary.package import seed_criterion_keys
from ouroboros.core.seed import Seed
from ouroboros.persistence.event_store import EventStore


async def recorded_criterion_decisions(
    store: EventStore, execution_id: str, seed: Seed
) -> tuple[CriterionDecisionRecord, ...]:
    """The run's recorded per-criterion decisions, in Seed order; empty when there are none.

    Empty when the check package was off, when the journal is not one the
    product could write, when no decision was recorded yet, or when the
    decision does not name the Seed's criteria in order.
    """
    ledger = BoundaryLedger(store)
    versions = await ledger.run_versions(execution_id)
    projection = recovery_projection(execution_id, await ledger.events(execution_id), versions)
    if not isinstance(projection, RecoveryBound | RecoveryNoPackage):
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
