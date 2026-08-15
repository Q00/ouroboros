"""Classify what an Auto session actually produced, separately from its status.

``status`` answers "what did the orchestration do?"; this answers "what exists,
and was it checked?". Keeping them apart is what stops a renderer from implying
completion when a BLOCKED run still left useful files behind — or, in the other
direction, from calling a Seed that never ran a verified product.
"""

from __future__ import annotations

from ouroboros.auto.handoff_contract import RUN_HANDOFF_STARTED_STATUS
from ouroboros.auto.state import AutoPhase

COMPLETE_VERIFIED = "complete_verified"
COMPLETE_UNVERIFIED = "complete_unverified"
PARTIAL_ARTIFACT_GENERATED = "partial_artifact_generated"
ARTIFACT_IN_PROGRESS = "artifact_in_progress"
NOT_GENERATED = "not_generated"


def artifact_state_for_result(
    *,
    status: str,
    phase: AutoPhase,
    seed_path: str | None,
    execution_id: str | None,
    job_id: str | None,
    run_session_id: str | None,
    partial_product: bool,
    run_handoff_status: str | None = None,
    product_verified: bool = False,
) -> str:
    """Classify the generated-artifact outcome separately from orchestration."""
    has_run = bool(execution_id or job_id or run_session_id)
    has_generated_artifact = bool(seed_path) or has_run
    if status in {AutoPhase.BLOCKED.value, AutoPhase.FAILED.value}:
        return PARTIAL_ARTIFACT_GENERATED if has_generated_artifact else status
    if status == AutoPhase.COMPLETE.value:
        if partial_product:
            return COMPLETE_UNVERIFIED
        if product_verified:
            return COMPLETE_VERIFIED
        # Nothing ran, or the run was only handed off: there is no product whose
        # verification could have happened. A skip-run session completes with a
        # Seed, and calling that "verified" is how a renderer ends up printing
        # "Product status: verified" for work that was never executed.
        if not has_run or run_handoff_status == RUN_HANDOFF_STARTED_STATUS:
            return COMPLETE_UNVERIFIED
        return COMPLETE_VERIFIED
    if phase in {AutoPhase.BLOCKED, AutoPhase.FAILED}:
        return PARTIAL_ARTIFACT_GENERATED if has_generated_artifact else phase.value
    return ARTIFACT_IN_PROGRESS if has_generated_artifact else NOT_GENERATED
