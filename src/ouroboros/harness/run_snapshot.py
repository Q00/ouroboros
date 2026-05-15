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
    _validate_projection_bundle(
        run=run,
        stages=stage_tuple,
        steps=step_tuple,
        artifacts=artifact_tuple,
        verdict=verdict,
    )

    completed_step_ids = tuple(
        step.step_id for step in step_tuple if step.ended_at and step.ok is True
    )
    pending_step_ids = tuple(step.step_id for step in step_tuple if step.ended_at is None)
    failed_step_ids = tuple(step.step_id for step in step_tuple if step.ok is False)
    unknown_step_ids = tuple(
        step.step_id for step in step_tuple if step.ended_at is not None and step.ok is None
    )

    missing_linked_verdict = run.verdict_id is not None and verdict is None
    unlinked_supplied_verdict = verdict is not None and run.verdict_id is None
    status = _derive_status(
        run=run,
        verdict=verdict,
        missing_linked_verdict=missing_linked_verdict,
        unlinked_supplied_verdict=unlinked_supplied_verdict,
        pending_step_ids=pending_step_ids,
        failed_step_ids=failed_step_ids,
        unknown_step_ids=unknown_step_ids,
    )
    blockers = _resume_blockers(
        status,
        pending_step_ids,
        failed_step_ids,
        unknown_step_ids,
        missing_linked_verdict=missing_linked_verdict,
        unlinked_supplied_verdict=unlinked_supplied_verdict,
    )
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
        verdict_id=(
            verdict.verdict_id
            if verdict is not None and not unlinked_supplied_verdict
            else run.verdict_id
        ),
        source_event_ids=_snapshot_source_event_ids(
            steps=step_tuple,
            verdict=verdict if not unlinked_supplied_verdict else None,
        ),
        recorded_at=recorded_at or datetime.now(UTC),
        metadata=MappingProxyType(metadata),
    )


def _validate_projection_bundle(
    *,
    run: RunRecord,
    stages: tuple[StageRecord, ...],
    steps: tuple[StepRecord, ...],
    artifacts: tuple[ArtifactRecord, ...],
    verdict: VerdictRecord | None,
) -> None:
    stage_ids = {stage.stage_id for stage in stages}
    step_ids = {step.step_id for step in steps}
    supplied_stage_ids = tuple(stage.stage_id for stage in stages)
    for stage in stages:
        if stage.run_id != run.run_id:
            msg = f"StageRecord {stage.stage_id!r} belongs to run {stage.run_id!r}, not {run.run_id!r}"
            raise ValueError(msg)

    if run.stage_ids != supplied_stage_ids:
        declared_stage_ids = set(run.stage_ids)
        missing_stage_ids = sorted(declared_stage_ids - stage_ids)
        extra_stage_ids = sorted(stage_ids - declared_stage_ids)
        msg = _format_bundle_mismatch(
            "RunRecord.stage_ids",
            missing=missing_stage_ids,
            extra=extra_stage_ids,
        )
        raise ValueError(msg)

    for step in steps:
        if step.run_id != run.run_id:
            msg = f"StepRecord {step.step_id!r} belongs to run {step.run_id!r}, not {run.run_id!r}"
            raise ValueError(msg)
        if step.stage_id not in stage_ids:
            msg = f"StepRecord {step.step_id!r} references unknown stage {step.stage_id!r}"
            raise ValueError(msg)

    for stage in stages:
        supplied_step_ids = tuple(step.step_id for step in steps if step.stage_id == stage.stage_id)
        if stage.step_ids != supplied_step_ids:
            declared_step_ids = set(stage.step_ids)
            stage_step_ids = set(supplied_step_ids)
            missing_step_ids = sorted(declared_step_ids - stage_step_ids)
            extra_step_ids = sorted(stage_step_ids - declared_step_ids)
            msg = _format_bundle_mismatch(
                f"StageRecord {stage.stage_id!r}.step_ids",
                missing=missing_step_ids,
                extra=extra_step_ids,
            )
            raise ValueError(msg)

    for artifact in artifacts:
        if artifact.step_id not in step_ids:
            msg = f"ArtifactRecord {artifact.artifact_id!r} references unknown step {artifact.step_id!r}"
            raise ValueError(msg)

    for step in steps:
        supplied_artifact_ids = tuple(
            artifact.artifact_id for artifact in artifacts if artifact.step_id == step.step_id
        )
        if step.artifact_ids != supplied_artifact_ids:
            declared_artifact_ids = set(step.artifact_ids)
            step_artifact_ids = set(supplied_artifact_ids)
            missing_artifact_ids = sorted(declared_artifact_ids - step_artifact_ids)
            extra_artifact_ids = sorted(step_artifact_ids - declared_artifact_ids)
            msg = _format_bundle_mismatch(
                f"StepRecord {step.step_id!r}.artifact_ids",
                missing=missing_artifact_ids,
                extra=extra_artifact_ids,
            )
            raise ValueError(msg)

    if verdict is not None:
        if verdict.run_id != run.run_id:
            msg = f"VerdictRecord {verdict.verdict_id!r} belongs to run {verdict.run_id!r}, not {run.run_id!r}"
            raise ValueError(msg)
        if verdict.scope != "run":
            msg = "build_run_snapshot requires a run-scoped VerdictRecord"
            raise ValueError(msg)
        if run.verdict_id is not None and run.verdict_id != verdict.verdict_id:
            msg = "RunRecord.verdict_id must match the supplied VerdictRecord"
            raise ValueError(msg)
        missing_evidence_artifact_ids = sorted(
            set(verdict.evidence_artifact_ids) - {artifact.artifact_id for artifact in artifacts}
        )
        if missing_evidence_artifact_ids:
            msg = _format_bundle_mismatch(
                f"VerdictRecord {verdict.verdict_id!r}.evidence_artifact_ids",
                missing=missing_evidence_artifact_ids,
                extra=[],
            )
            raise ValueError(msg)


def _snapshot_source_event_ids(
    *, steps: tuple[StepRecord, ...], verdict: VerdictRecord | None
) -> tuple[str, ...]:
    ordered_ids: list[str] = []
    seen: set[str] = set()
    for event_id in (
        *(event_id for step in steps for event_id in step.source_event_ids),
        *((verdict.evidence_event_ids) if verdict is not None else ()),
    ):
        if event_id not in seen:
            ordered_ids.append(event_id)
            seen.add(event_id)
    return tuple(ordered_ids)


def _format_bundle_mismatch(owner: str, *, missing: list[str], extra: list[str]) -> str:
    details: list[str] = []
    if missing:
        details.append(f"missing {missing!r}")
    if extra:
        details.append(f"unexpected {extra!r}")
    detail = "; ".join(details) or "mismatched projection records"
    return f"{owner} does not match supplied projection bundle: {detail}"


def _derive_status(
    *,
    run: RunRecord,
    verdict: VerdictRecord | None,
    missing_linked_verdict: bool,
    unlinked_supplied_verdict: bool,
    pending_step_ids: tuple[str, ...],
    failed_step_ids: tuple[str, ...],
    unknown_step_ids: tuple[str, ...],
) -> RunSnapshotStatus:
    if unlinked_supplied_verdict:
        return RunSnapshotStatus.UNKNOWN
    if verdict is not None:
        if _verdict_conflicts_with_steps(
            verdict=verdict,
            pending_step_ids=pending_step_ids,
            failed_step_ids=failed_step_ids,
            unknown_step_ids=unknown_step_ids,
        ):
            return RunSnapshotStatus.UNKNOWN
        if verdict.outcome is VerdictOutcome.PASS:
            return RunSnapshotStatus.COMPLETED
        if verdict.outcome is VerdictOutcome.FAIL:
            return RunSnapshotStatus.FAILED
        if verdict.outcome is VerdictOutcome.CANCELLED:
            return RunSnapshotStatus.CANCELLED
        if verdict.outcome is VerdictOutcome.ESCALATE_HUMAN:
            return RunSnapshotStatus.WAITING
        return RunSnapshotStatus.UNKNOWN
    if missing_linked_verdict:
        return RunSnapshotStatus.UNKNOWN
    if failed_step_ids:
        return RunSnapshotStatus.FAILED
    if run.ended_at is not None:
        if pending_step_ids or unknown_step_ids:
            return RunSnapshotStatus.UNKNOWN
        return RunSnapshotStatus.COMPLETED
    if pending_step_ids:
        return RunSnapshotStatus.RUNNING
    if unknown_step_ids:
        return RunSnapshotStatus.UNKNOWN
    return RunSnapshotStatus.RUNNING


def _verdict_conflicts_with_steps(
    *,
    verdict: VerdictRecord,
    pending_step_ids: tuple[str, ...],
    failed_step_ids: tuple[str, ...],
    unknown_step_ids: tuple[str, ...],
) -> bool:
    if verdict.outcome in {
        VerdictOutcome.PASS,
        VerdictOutcome.FAIL,
        VerdictOutcome.CANCELLED,
    } and (pending_step_ids or unknown_step_ids):
        return True
    return verdict.outcome is VerdictOutcome.PASS and bool(failed_step_ids)


def _resume_blockers(
    status: RunSnapshotStatus,
    pending_step_ids: tuple[str, ...],
    failed_step_ids: tuple[str, ...],
    unknown_step_ids: tuple[str, ...],
    *,
    missing_linked_verdict: bool,
    unlinked_supplied_verdict: bool,
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
    if status is RunSnapshotStatus.UNKNOWN and pending_step_ids:
        blockers.append("pending_steps_present")
    if missing_linked_verdict:
        blockers.append("linked_verdict_missing")
    if unlinked_supplied_verdict:
        blockers.append("unlinked_verdict_present")
    if failed_step_ids:
        blockers.append("failed_steps_present")
    if unknown_step_ids:
        blockers.append("unknown_steps_present")
    if status is RunSnapshotStatus.RUNNING and not pending_step_ids:
        blockers.append("no_pending_steps")
    return tuple(blockers)


__all__ = ["build_run_snapshot"]
