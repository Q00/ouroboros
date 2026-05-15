"""RunSnapshot builder for safe-resume projection views.

This is a narrow #946 follow-up over the public projection records. It derives a
single immutable snapshot from already-projected Run/Stage/Step/Artifact/Verdict
records and intentionally performs no EventStore writes or runtime dispatch.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from types import MappingProxyType

from ouroboros.harness.projection import (
    ArtifactRecord,
    RunRecord,
    RunSnapshotRecord,
    RunSnapshotStatus,
    StageRecord,
    StepRecord,
    VerdictOutcome,
    VerdictRecord,
)


def build_run_snapshot(
    *,
    run: RunRecord,
    stages: Iterable[StageRecord] = (),
    steps: Iterable[StepRecord] = (),
    artifacts: Iterable[ArtifactRecord] = (),
    verdict: VerdictRecord | None = None,
    source_event_ids: Iterable[str] = (),
    recorded_at: datetime | None = None,
) -> RunSnapshotRecord:
    """Build a safe-resume snapshot from projection records.

    ``safe_resume`` is conservative: only non-terminal runs with at least one
    pending step and no failed steps are marked resumable. Terminal verdicts,
    failed steps, missing pending work, and explicit human-escalation verdicts
    produce blocker codes instead of guessing a resume action.
    """

    stage_tuple = tuple(stages)
    step_tuple = tuple(steps)
    artifact_tuple = tuple(artifacts)

    completed_step_ids = tuple(
        step.step_id for step in step_tuple if step.ended_at and step.ok is True
    )
    pending_step_ids = tuple(step.step_id for step in step_tuple if step.ended_at is None)
    failed_step_ids = tuple(step.step_id for step in step_tuple if step.ok is False)
    unknown_step_ids = tuple(
        step.step_id for step in step_tuple if step.ended_at is not None and step.ok is None
    )

    status = _derive_status(
        run=run,
        verdict=verdict,
        pending_step_ids=pending_step_ids,
        failed_step_ids=failed_step_ids,
        unknown_step_ids=unknown_step_ids,
    )
    blockers = _resume_blockers(status, pending_step_ids, failed_step_ids, unknown_step_ids)
    safe_resume = status is RunSnapshotStatus.RUNNING and bool(pending_step_ids) and not blockers

    metadata = {
        "stage_count": len(stage_tuple),
        "step_count": len(step_tuple),
        "artifact_count": len(artifact_tuple),
    }
    return RunSnapshotRecord(
        run_id=run.run_id,
        status=status,
        safe_resume=safe_resume,
        resume_blockers=blockers,
        stage_ids=tuple(stage.stage_id for stage in stage_tuple),
        completed_step_ids=completed_step_ids,
        pending_step_ids=pending_step_ids,
        failed_step_ids=failed_step_ids,
        unknown_step_ids=unknown_step_ids,
        artifact_ids=tuple(artifact.artifact_id for artifact in artifact_tuple),
        verdict_id=verdict.verdict_id if verdict is not None else run.verdict_id,
        source_event_ids=tuple(source_event_ids),
        recorded_at=recorded_at or datetime.now(UTC),
        metadata=MappingProxyType(metadata),
    )


def _derive_status(
    *,
    run: RunRecord,
    verdict: VerdictRecord | None,
    pending_step_ids: tuple[str, ...],
    failed_step_ids: tuple[str, ...],
    unknown_step_ids: tuple[str, ...],
) -> RunSnapshotStatus:
    if verdict is not None:
        if verdict.outcome is VerdictOutcome.PASS:
            return RunSnapshotStatus.COMPLETED
        if verdict.outcome is VerdictOutcome.FAIL:
            return RunSnapshotStatus.FAILED
        if verdict.outcome is VerdictOutcome.CANCELLED:
            return RunSnapshotStatus.CANCELLED
        if verdict.outcome is VerdictOutcome.ESCALATE_HUMAN:
            return RunSnapshotStatus.WAITING
        return RunSnapshotStatus.UNKNOWN
    if failed_step_ids:
        return RunSnapshotStatus.FAILED
    if pending_step_ids:
        return RunSnapshotStatus.RUNNING
    if unknown_step_ids:
        return RunSnapshotStatus.UNKNOWN
    if run.ended_at is not None:
        return RunSnapshotStatus.COMPLETED
    return RunSnapshotStatus.RUNNING


def _resume_blockers(
    status: RunSnapshotStatus,
    pending_step_ids: tuple[str, ...],
    failed_step_ids: tuple[str, ...],
    unknown_step_ids: tuple[str, ...],
) -> tuple[str, ...]:
    blockers: list[str] = []
    if status in {
        RunSnapshotStatus.COMPLETED,
        RunSnapshotStatus.FAILED,
        RunSnapshotStatus.CANCELLED,
    }:
        blockers.append(f"terminal_status:{status.value}")
    if status is RunSnapshotStatus.WAITING:
        blockers.append("human_input_required")
    if status is RunSnapshotStatus.UNKNOWN:
        blockers.append("status_unknown")
    if failed_step_ids:
        blockers.append("failed_steps_present")
    if unknown_step_ids:
        blockers.append("unknown_steps_present")
    if status is RunSnapshotStatus.RUNNING and not pending_step_ids:
        blockers.append("no_pending_steps")
    return tuple(blockers)


__all__ = ["build_run_snapshot"]
