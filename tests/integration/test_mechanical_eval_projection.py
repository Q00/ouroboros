"""Mechanical-evaluation projection fixture integration test.

This test implements the "Mechanical-evaluation fixture" follow-up slot
named in :mod:`docs/agentos/projection-followups.md`: prove that a small
execution + evaluation history projects to a complete projection bundle
(:class:`RunRecord`, :class:`StageRecord`, :class:`StepRecord` collection,
:class:`ArtifactRecord` collection, and :class:`VerdictRecord`) with
``source_event_ids`` populated on every step and source-event evidence
links on the verdict.

Scope:
    * 100% offline. No LLM call. No network. No external credentials.
    * The fixture (``tests/fixtures/seeds/mechanical-eval-minimal.yaml``)
      describes the synthetic event slice the projection builder must
      consume. The test materializes that fixture into in-memory
      :class:`BaseEvent` rows and runs the read-only
      :class:`ProjectionBuilder` over them, never invoking a live
      runtime.
    * Artifact and verdict projection are required: the fixture asserts
      populated :class:`ArtifactRecord` and :class:`VerdictRecord`
      rows rather than accepting a missing populator.

Refs: #1131, #946.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from ouroboros.events.base import BaseEvent
from ouroboros.harness.projection import (
    PROJECTION_SCHEMA_VERSION,
    ArtifactRecord,
    RunRecord,
    StageKind,
    StageRecord,
    StepKind,
    StepRecord,
    VerdictOutcome,
    VerdictRecord,
)
from ouroboros.harness.projection_builder import (
    ProjectionBuildResult,
    build_projection,
)

FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent / "fixtures" / "seeds" / "mechanical-eval-minimal.yaml"
)


def _load_fixture() -> dict[str, Any]:
    if not FIXTURE_PATH.is_file():
        pytest.skip(f"fixture file missing: {FIXTURE_PATH}")
    with FIXTURE_PATH.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)
    if not isinstance(loaded, dict):
        pytest.fail(f"fixture {FIXTURE_PATH} did not parse to a mapping")
    return loaded


def _materialize_events(fixture: dict[str, Any]) -> list[BaseEvent]:
    """Turn the fixture into a deterministic ordered event slice.

    Timestamps are anchored at a fixed UTC instant so projection record
    IDs (which are derived from event identity) remain byte-stable
    across runs. Identifiers are taken straight from the fixture so
    test assertions can reference them by name.
    """
    anchor = datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)
    execution_id = str(fixture["execution_id"])
    events: list[BaseEvent] = []
    offset_ms = 0
    for step in fixture.get("steps", []):
        call_id = str(step["call_id"])
        tool_name = str(step["tool_name"])
        start_event_id = f"evt_start_{call_id}"
        ret_event_id = f"evt_ret_{call_id}"
        started_at = anchor + timedelta(milliseconds=offset_ms)
        offset_ms += int(step.get("duration_ms", 10))
        ended_at = anchor + timedelta(milliseconds=offset_ms)
        offset_ms += 1
        events.append(
            BaseEvent(
                id=start_event_id,
                type="tool.call.started",
                timestamp=started_at,
                aggregate_type="execution",
                aggregate_id=execution_id,
                data={
                    "call_id": call_id,
                    "tool_name": tool_name,
                    "args_preview": step.get("args_preview"),
                },
            )
        )
        events.append(
            BaseEvent(
                id=ret_event_id,
                type="tool.call.returned",
                timestamp=ended_at,
                aggregate_type="execution",
                aggregate_id=execution_id,
                data={
                    "call_id": call_id,
                    "tool_name": tool_name,
                    "is_error": bool(step.get("is_error", False)),
                    "duration_ms": int(step.get("duration_ms", 10)),
                    "result_preview": step.get("result_preview"),
                },
            )
        )

    for index, artifact in enumerate(fixture.get("artifacts", [])):
        events.append(
            BaseEvent(
                id=f"evt_artifact_{index}",
                type="harness.artifact.recorded",
                timestamp=anchor + timedelta(milliseconds=offset_ms),
                aggregate_type="execution",
                aggregate_id=execution_id,
                data={
                    "artifact_id": str(artifact["artifact_id"]),
                    "call_id": str(artifact["step_call_id"]),
                    "step_family": str(artifact.get("step_family", "tool")),
                    "kind": str(artifact.get("kind", "evidence")),
                    "path": artifact.get("path"),
                    "media_type": artifact.get("media_type"),
                    "summary": artifact.get("summary", ""),
                },
            )
        )
        offset_ms += 1

    for index, verdict in enumerate(fixture.get("verdicts", [])):
        events.append(
            BaseEvent(
                id=f"evt_verdict_{index}",
                type="harness.verdict.recorded",
                timestamp=anchor + timedelta(milliseconds=offset_ms),
                aggregate_type="execution",
                aggregate_id=execution_id,
                data={
                    "verdict_id": str(verdict["verdict_id"]),
                    "scope": str(verdict.get("scope", "run")),
                    "outcome": str(verdict.get("outcome", "pass")),
                    "rationale": verdict.get("rationale", ""),
                    "evidence_artifact_ids": list(verdict.get("evidence_artifact_ids", [])),
                },
            )
        )
        offset_ms += 1

    return events


def _build_result(fixture: dict[str, Any]) -> ProjectionBuildResult:
    events = _materialize_events(fixture)
    source_key = f"execution:{fixture['execution_id']}"
    return build_projection(
        events,
        seed_id=str(fixture["seed_id"]),
        goal=str(fixture.get("goal", "")),
        source_key=source_key,
    )


@pytest.fixture(scope="module")
def fixture() -> dict[str, Any]:
    return _load_fixture()


@pytest.fixture(scope="module")
def result(fixture: dict[str, Any]) -> ProjectionBuildResult:
    return _build_result(fixture)


class TestMechanicalEvaluationFixture:
    """Drive the fixture through the projection builder and assert records."""

    def test_run_record_is_populated(
        self,
        fixture: dict[str, Any],
        result: ProjectionBuildResult,
    ) -> None:
        run = result.run
        assert isinstance(run, RunRecord)
        assert run.schema_version == PROJECTION_SCHEMA_VERSION
        assert run.seed_id == fixture["seed_id"]
        assert run.goal == fixture["goal"]
        assert run.run_id.startswith("run_")
        assert run.started_at is not None
        assert run.ended_at is not None
        assert run.ended_at >= run.started_at
        # Run links to exactly one stage, which is the v1 builder
        # contract (richer stage detection is deferred).
        assert len(run.stage_ids) == fixture["expected"]["run"]["stage_count"]

    def test_stage_record_owns_every_step(
        self,
        fixture: dict[str, Any],
        result: ProjectionBuildResult,
    ) -> None:
        assert len(result.stages) == 1
        stage = result.stages[0]
        assert isinstance(stage, StageRecord)
        assert stage.kind is StageKind.EXECUTE
        assert stage.run_id == result.run.run_id
        assert stage.stage_id == result.run.stage_ids[0]
        expected_step_count = fixture["expected"]["stage"]["step_count"]
        assert len(stage.step_ids) == expected_step_count
        # Stage step ids must mirror the projected steps in execution
        # order so consumers can iterate without joining tables.
        assert stage.step_ids == tuple(step.step_id for step in result.steps)

    def test_step_records_link_source_events(
        self,
        fixture: dict[str, Any],
        result: ProjectionBuildResult,
    ) -> None:
        expected = fixture["expected"]["steps"]
        assert len(result.steps) == len(expected)
        artifact_ids = {artifact.artifact_id for artifact in result.artifacts}
        kind_lookup = {
            "tool_call": StepKind.TOOL_CALL,
            "shell_command": StepKind.SHELL_COMMAND,
            "model_call": StepKind.MODEL_CALL,
        }
        for step, want in zip(result.steps, expected, strict=True):
            assert isinstance(step, StepRecord)
            assert step.run_id == result.run.run_id
            assert step.stage_id == result.stages[0].stage_id
            assert step.kind is kind_lookup[want["kind"]]
            assert step.name == want["name"]
            assert step.ok is want["ok"]
            # Every step links source event IDs (per #946 AC #3).
            assert step.source_event_ids
            assert all(isinstance(eid, str) and eid.strip() for eid in step.source_event_ids)
            # Mechanical fixtures must never need the legacy escape hatch.
            assert step.legacy_inferred is False
            # Steps with attached artifacts surface them via artifact_ids.
            for artifact_id in step.artifact_ids:
                assert artifact_id in artifact_ids

    def test_artifact_records_attach_to_steps(
        self,
        fixture: dict[str, Any],
        result: ProjectionBuildResult,
    ) -> None:
        expected_count = fixture["expected"]["artifact_count"]
        assert len(result.artifacts) == expected_count
        step_ids = {step.step_id for step in result.steps}
        for artifact in result.artifacts:
            assert isinstance(artifact, ArtifactRecord)
            assert artifact.step_id in step_ids
            assert artifact.kind
            # Artifact metadata captures the source event id so audit
            # consumers can trace the artifact back to its evidence row.
            assert artifact.metadata.get("source_event_id")

    def test_verdict_record_links_evidence(
        self,
        fixture: dict[str, Any],
        result: ProjectionBuildResult,
    ) -> None:
        expected = fixture["expected"]["verdict"]
        assert len(result.verdicts) == fixture["expected"]["verdict_count"]
        verdict = result.verdicts[0]
        assert isinstance(verdict, VerdictRecord)
        assert verdict.run_id == result.run.run_id
        assert verdict.scope == expected["scope"]
        assert verdict.outcome is VerdictOutcome(expected["outcome"])
        # Verdict must link source events and existing artifact IDs.
        assert verdict.evidence_event_ids
        assert all(isinstance(eid, str) and eid.strip() for eid in verdict.evidence_event_ids)
        if result.artifacts:
            artifact_ids = {artifact.artifact_id for artifact in result.artifacts}
            assert set(verdict.evidence_artifact_ids).issubset(artifact_ids)
        # Run record points at the run-level verdict for safe lookup.
        assert result.run.verdict_id == verdict.verdict_id

    def test_projection_is_deterministic(
        self,
        fixture: dict[str, Any],
    ) -> None:
        """Replays of the same fixture must produce byte-stable IDs."""
        first = _build_result(fixture)
        second = _build_result(fixture)
        assert first.run.run_id == second.run.run_id
        assert tuple(stage.stage_id for stage in first.stages) == tuple(
            stage.stage_id for stage in second.stages
        )
        assert tuple(step.step_id for step in first.steps) == tuple(
            step.step_id for step in second.steps
        )
        assert tuple(art.artifact_id for art in first.artifacts) == tuple(
            art.artifact_id for art in second.artifacts
        )
        assert tuple(v.verdict_id for v in first.verdicts) == tuple(
            v.verdict_id for v in second.verdicts
        )
