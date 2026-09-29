"""The run's recorded check package decision as Stage 1 evidence, per criterion.

The controller decided each covered criterion by running the frozen check
package after the worker stopped (``boundary.acceptance.reconciled``). Formal
evaluation reads that decision instead of deciding again: a package ``pass``
is an executed pass for its criterion, a package ``fail`` an executed failure,
and every other outcome (indeterminate, unverified, uncovered, an undecided
decision, or a criterion the existing verifier governs) is no evidence. The
mapping reads the recorded fields only; it never re-derives acceptance.
"""

from __future__ import annotations

from ouroboros.boundary.decision import recorded_criterion_decisions
from ouroboros.boundary.events import CriterionDecisionRecord
from ouroboros.core.seed import Seed, ac_text
from ouroboros.evaluation.models import CheckResult, CheckType
from ouroboros.persistence.event_store import EventStore

_EXECUTED = {"pass": True, "fail": False}


def package_evidence(record: CriterionDecisionRecord) -> tuple[CheckResult, ...]:
    """The Stage 1 check one recorded criterion decision supports, if any."""
    passed = _EXECUTED.get(record.package_status)
    if passed is None or record.governed_by != "check_package":
        return ()
    return (
        CheckResult(
            check_type=CheckType.CHECK_PACKAGE,
            passed=passed,
            message=f"check package {record.package_status} ({record.reason})",
            details={"criterion_key": record.criterion_key, "tier": record.tier},
            executed=True,
        ),
    )


async def recorded_checks_by_position(
    store: EventStore, session_id: str, seed: Seed
) -> tuple[tuple[CheckResult, ...], ...]:
    """Per Seed criterion position, the evidence the run's recorded decision supports.

    ``session_id`` is the evaluated execution's id, or a session whose start
    names it. The result is aligned with ``seed.acceptance_criteria`` (empty
    when there is no decision); criteria are identified by position, never by
    their text, so two criteria with the same description stay apart.
    """
    execution_id = await store.resolve_execution_id_for_session(session_id) or session_id
    decisions = await recorded_criterion_decisions(store, execution_id, seed)
    return tuple(package_evidence(record) for record in decisions)


def evidence_for_criteria(
    evaluated: tuple[str, ...],
    seed: Seed,
    by_position: tuple[tuple[CheckResult, ...], ...],
) -> tuple[tuple[CheckResult, ...], ...]:
    """The evidence for each evaluated criterion, by position; none when positions are unknown.

    Evidence is applied only when the evaluated criteria are exactly the
    Seed's, in Seed order, so each position names one Seed criterion.
    """
    seed_texts = tuple(ac_text(criterion).strip() for criterion in seed.acceptance_criteria)
    if not by_position or evaluated != seed_texts:
        return tuple(() for _ in evaluated)
    return by_position
