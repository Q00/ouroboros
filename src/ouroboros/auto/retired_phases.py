"""Phases that only the retired ``--complete-product`` path could enter.

The enum members survive so sessions persisted in them still deserialize, but
nothing transitions into them any more. A resume that lands on one cannot be
driven forward, so it says what happened instead of dispatching to a handler
that no longer exists — and it says it *before* the generic deadline gate, so
the explanation does not depend on whether the old session's deadline happens
to be spent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ouroboros.auto.state import AutoPhase

if TYPE_CHECKING:
    from ouroboros.auto.state import AutoPipelineState

RETIRED_PHASES = frozenset({AutoPhase.RALPH_HANDOFF, AutoPhase.EVALUATE, AutoPhase.UNSTUCK_LATERAL})

RETIRED_PHASE_BLOCKER = (
    "auto phase {phase} was retired with --complete-product; the run job now owns "
    "evaluate/ralph. Start a new session, or follow the run job's own chain with "
    "ouroboros_job_status."
)
RETIRED_COMPLETE_PRODUCT_RUN_BLOCKER = (
    "auto phase RUN successor ownership was retired with --complete-product; "
    "this legacy run job was dispatched without evaluate/ralph successors, so "
    "the session cannot safely report product completion. Inspect the run with "
    "ouroboros_job_status, then start a new session to run the current "
    "run/evaluate/ralph chain."
)
RETIRED_PHASE_MIGRATION_TOOL = "retired_phase_migration"


def mark_retired_phase(state: AutoPipelineState) -> bool:
    """Block or recognize a session parked by the retired-phase migration."""
    if (
        state.phase == AutoPhase.BLOCKED
        and state.last_tool_name == RETIRED_PHASE_MIGRATION_TOOL
        and state.last_error is not None
        and "was retired with --complete-product" in state.last_error
    ):
        return True
    if state.phase not in RETIRED_PHASES or state.is_terminal():
        return False
    state.mark_blocked(
        RETIRED_PHASE_BLOCKER.format(phase=state.phase.value),
        # This dedicated marker is intentionally absent from the recoverable
        # tool map. It also lets a later resume recognize and preserve the
        # migration blocker before the watchdog or normal resume path runs.
        tool_name=RETIRED_PHASE_MIGRATION_TOOL,
    )
    return True


def mark_retired_complete_product_run(state: AutoPipelineState) -> bool:
    """Block the pre-retirement ``RUN`` crash window without claiming success.

    Older complete-product sessions dispatched their run jobs with successor
    ownership disabled because Auto itself intended to continue into the now
    retired phases. If such a process stopped before leaving ``RUN``, neither
    the old job nor the current Auto pipeline can truthfully own evaluation and
    Ralph. Preserve that durable intent as explicit migration guidance.
    """
    if state.phase != AutoPhase.RUN or not state.complete_product:
        return False
    state.mark_blocked(
        RETIRED_COMPLETE_PRODUCT_RUN_BLOCKER,
        tool_name=RETIRED_PHASE_MIGRATION_TOOL,
    )
    return True
