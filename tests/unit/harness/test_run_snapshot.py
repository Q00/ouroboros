from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pydantic import ValidationError
import pytest

from ouroboros.harness.projection import (
    ArtifactRecord,
    RunRecord,
    RunSnapshotRecord,
    RunSnapshotStatus,
    StageKind,
    StageRecord,
    StepKind,
    StepRecord,
    VerdictOutcome,
    VerdictRecord,
)
from ouroboros.harness.run_snapshot import build_run_snapshot


def _run(*, ended: bool = False, verdict_id: str | None = None) -> RunRecord:
    start = datetime(2026, 5, 15, tzinfo=UTC)
    return RunRecord(
        run_id="run_1",
        seed_id="seed_1",
        started_at=start,
        ended_at=start + timedelta(seconds=10) if ended else None,
        stage_ids=("stage_1",),
        verdict_id=verdict_id,
    )


def _stage() -> StageRecord:
    return StageRecord(stage_id="stage_1", run_id="run_1", kind=StageKind.EXECUTE)


def _step(step_id: str, *, ended: bool, ok: bool | None) -> StepRecord:
    start = datetime(2026, 5, 15, tzinfo=UTC)
    return StepRecord(
        step_id=step_id,
        run_id="run_1",
        stage_id="stage_1",
        kind=StepKind.TOOL_CALL,
        started_at=start,
        ended_at=start + timedelta(seconds=1) if ended else None,
        ok=ok,
        source_event_ids=(f"evt_{step_id}",),
    )


def test_running_snapshot_with_pending_work_is_safe_to_resume() -> None:
    completed = _step("step_done", ended=True, ok=True)
    pending = _step("step_pending", ended=False, ok=None)
    artifact = ArtifactRecord(artifact_id="artifact_1", step_id="step_done", kind="log")

    snapshot = build_run_snapshot(
        run=_run(),
        stages=[_stage()],
        steps=[completed, pending],
        artifacts=[artifact],
        source_event_ids=("evt_step_done", "evt_step_pending"),
        recorded_at=datetime(2026, 5, 15, tzinfo=UTC),
    )

    assert snapshot.status is RunSnapshotStatus.RUNNING
    assert snapshot.safe_resume is True
    assert snapshot.resume_blockers == ()
    assert snapshot.stage_ids == ("stage_1",)
    assert snapshot.completed_step_ids == ("step_done",)
    assert snapshot.pending_step_ids == ("step_pending",)
    assert snapshot.artifact_ids == ("artifact_1",)
    assert snapshot.source_event_ids == ("evt_step_done", "evt_step_pending")
    assert snapshot.metadata == {"stage_count": 1, "step_count": 2, "artifact_count": 1}


def test_failed_step_blocks_resume() -> None:
    snapshot = build_run_snapshot(
        run=_run(),
        stages=[_stage()],
        steps=[_step("step_failed", ended=True, ok=False)],
    )

    assert snapshot.status is RunSnapshotStatus.FAILED
    assert snapshot.safe_resume is False
    assert snapshot.failed_step_ids == ("step_failed",)
    assert "failed_steps_present" in snapshot.resume_blockers


def test_human_escalation_verdict_is_waiting_but_not_safe_resume() -> None:
    verdict = VerdictRecord(
        verdict_id="verdict_1",
        run_id="run_1",
        scope="run",
        outcome=VerdictOutcome.ESCALATE_HUMAN,
        evidence_event_ids=("evt_verdict",),
    )

    snapshot = build_run_snapshot(run=_run(verdict_id="verdict_1"), verdict=verdict)

    assert snapshot.status is RunSnapshotStatus.WAITING
    assert snapshot.safe_resume is False
    assert snapshot.verdict_id == "verdict_1"
    assert snapshot.resume_blockers == ("human_input_required",)


def test_terminal_verdicts_block_resume() -> None:
    verdict = VerdictRecord(
        verdict_id="verdict_pass",
        run_id="run_1",
        scope="run",
        outcome=VerdictOutcome.PASS,
    )

    snapshot = build_run_snapshot(run=_run(ended=True, verdict_id="verdict_pass"), verdict=verdict)

    assert snapshot.status is RunSnapshotStatus.COMPLETED
    assert snapshot.safe_resume is False
    assert snapshot.resume_blockers == ("terminal_status:completed",)


def test_snapshot_record_enforces_safe_resume_invariant() -> None:
    with pytest.raises(ValidationError, match="only valid"):
        RunSnapshotRecord(run_id="run_1", status=RunSnapshotStatus.COMPLETED, safe_resume=True)

    with pytest.raises(ValidationError, match="resume_blockers"):
        RunSnapshotRecord(
            run_id="run_1",
            status=RunSnapshotStatus.RUNNING,
            safe_resume=False,
        )


def test_snapshot_metadata_is_read_only() -> None:
    snapshot = build_run_snapshot(run=_run(), steps=[_step("step_pending", ended=False, ok=None)])
    with pytest.raises(TypeError):
        snapshot.metadata["new"] = "value"  # type: ignore[index]


def test_rejects_foreign_projection_records() -> None:
    foreign_stage = StageRecord(
        stage_id="stage_foreign", run_id="run_other", kind=StageKind.EXECUTE
    )
    with pytest.raises(ValueError, match="belongs to run"):
        build_run_snapshot(run=_run(), stages=[foreign_stage])

    foreign_step = StepRecord(
        step_id="step_foreign",
        run_id="run_other",
        stage_id="stage_1",
        kind=StepKind.TOOL_CALL,
        legacy_inferred=True,
    )
    with pytest.raises(ValueError, match="belongs to run"):
        build_run_snapshot(run=_run(), stages=[_stage()], steps=[foreign_step])


def test_rejects_artifacts_not_owned_by_snapshot_steps() -> None:
    artifact = ArtifactRecord(artifact_id="artifact_orphan", step_id="step_missing", kind="log")

    with pytest.raises(ValueError, match="unknown step"):
        build_run_snapshot(run=_run(), artifacts=[artifact])


def test_rejects_foreign_or_ac_scoped_verdicts() -> None:
    foreign = VerdictRecord(
        verdict_id="verdict_foreign",
        run_id="run_other",
        scope="run",
        outcome=VerdictOutcome.PASS,
    )
    with pytest.raises(ValueError, match="belongs to run"):
        build_run_snapshot(run=_run(), verdict=foreign)

    ac_verdict = VerdictRecord(
        verdict_id="verdict_ac",
        run_id="run_1",
        scope="ac",
        ac_id="ac_1",
        outcome=VerdictOutcome.PASS,
    )
    with pytest.raises(ValueError, match="run-scoped"):
        build_run_snapshot(run=_run(), verdict=ac_verdict)


def test_ended_run_with_stale_pending_step_is_not_safe_resume() -> None:
    snapshot = build_run_snapshot(
        run=_run(ended=True),
        stages=[_stage()],
        steps=[_step("step_stale_pending", ended=False, ok=None)],
    )

    assert snapshot.status is RunSnapshotStatus.COMPLETED
    assert snapshot.safe_resume is False
    assert snapshot.pending_step_ids == ("step_stale_pending",)
    assert snapshot.resume_blockers == ("terminal_status:completed",)


def test_missing_linked_run_verdict_blocks_resume_conservatively() -> None:
    snapshot = build_run_snapshot(
        run=_run(verdict_id="verdict_missing"),
        stages=[_stage()],
        steps=[_step("step_pending", ended=False, ok=None)],
    )

    assert snapshot.status is RunSnapshotStatus.UNKNOWN
    assert snapshot.safe_resume is False
    assert snapshot.verdict_id == "verdict_missing"
    assert snapshot.resume_blockers == ("status_unknown", "linked_verdict_missing")
