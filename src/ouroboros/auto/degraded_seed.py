"""Whether a degraded (interview-deadline) Seed runs, and the code it stops with.

The interview deadline closes the interview on the evidence collected so far
(#1257) and stamps the resulting Seed ``metadata.degraded``. This is the
deadline branch of the interview's own closure, so two of the interview's
closure rules still apply to it before RUN:

* a required section the interview marked ``BLOCKED`` (a question only a human
  can answer) stops the session with
  :data:`DEGRADED_SEED_SAFETY_BLOCKERS_STOP_REASON_CODE`, exactly as it keeps
  the interview from closing normally;
* without the user's goal on record there is no goal to run, so the session
  stops as a typed partial product with
  :data:`DEGRADED_SEED_NOT_EXECUTED_STOP_REASON_CODE`.

Otherwise the Seed runs like any other auto Seed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ouroboros.auto.ledger import LedgerStatus

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ouroboros.auto.ledger import SeedDraftLedger
    from ouroboros.auto.state import AutoPipelineState
    from ouroboros.core.seed import Seed

DEGRADED_SEED_SAFETY_BLOCKERS_STOP_REASON_CODE = "degraded_seed_safety_blockers"
# A degraded Seed without the user's goal on record is emitted for the user but
# never executed, so the session stops here instead of reporting complete.
DEGRADED_SEED_NOT_EXECUTED_STOP_REASON_CODE = "degraded_seed_not_executed"


def degraded_seed_safety_blocker(ledger: SeedDraftLedger) -> tuple[str, str] | None:
    """Return ``(blocker, stop_reason_code)`` when a human must answer first.

    ``BLOCKED`` is the ledger's explicit signal that an answer only a human can
    give is missing. The interview never closes over one; the deadline closure
    must not turn it into a run either (#1257 §I6).
    """
    statuses = ledger.section_statuses()
    blocked = [name for name, status in statuses.items() if status is LedgerStatus.BLOCKED]
    if not blocked:
        return None
    return (
        "Degraded seed has required sections a human must answer before a run: "
        + ", ".join(blocked),
        DEGRADED_SEED_SAFETY_BLOCKERS_STOP_REASON_CODE,
    )


def degraded_seed_runs_as_stated(seed: Seed) -> bool:
    """Whether a degraded Seed runs on the user's request as stated.

    A degraded Seed comes from the interview deadline, so some slots were never
    answered. When it carries ``sourced_scope`` (the user's goal verbatim,
    stamped from ledger provenance), the worker is told only that record, with
    "Resolve the request as stated in the goal." as the objective when nothing
    binds: the Seed's smoke criterion and unresolved-slot constraints never
    reach it. Such a Seed runs like any auto Seed, and its unresolved slots stay
    in ``metadata.unresolved_slots`` and ``open_questions``. Without the record
    there is no sourced goal to run, so the Seed stays a partial product.
    """
    return bool(getattr(seed.metadata, "degraded", False)) and seed.sourced_scope is not None


def degraded_seed_not_executed_blocker(unresolved_slots: int) -> tuple[str, str]:
    """Return ``(blocker, stop_reason_code)`` for the partial-product stop.

    The degraded Seed is emitted for the user but never executed, so the
    session must not report complete.
    """
    return (
        "Degraded seed emitted as a partial product and not executed; "
        f"{unresolved_slots} unresolved slot(s) need an answer before a run",
        DEGRADED_SEED_NOT_EXECUTED_STOP_REASON_CODE,
    )


def is_degraded_seed_not_executed_stop(state: AutoPipelineState) -> bool:
    """Whether ``state`` stopped at the partial-product terminal."""
    return (
        state.phase.value == "blocked"
        and state.last_error_code == DEGRADED_SEED_NOT_EXECUTED_STOP_REASON_CODE
    )
