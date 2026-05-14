"""Unit tests for the projection builder.

Covers the contract from issue #946 PR-1b:

* ``ProjectionBuilder`` produces a single ``RunRecord`` with a default
  ``EXECUTE`` stage and one ``StepRecord`` per paired tool/LLM call.
* Tool / LLM pairing is by ``call_id`` and uses the canonical recorder
  field names (``tool_name`` / ``model_id``).
* ``Bash`` tool calls are projected as ``StepKind.SHELL_COMMAND``;
  other tool calls as ``StepKind.TOOL_CALL``.
* Unpaired start events surface as dangling steps so callers can
  detect in-flight work.
* Every projected ``StepRecord`` links its source event ids.
* ``build_projection`` is the convenience one-shot wrapper.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ouroboros.events.base import BaseEvent
from ouroboros.harness.projection import StageKind, StepKind
from ouroboros.harness.projection_builder import (
    ProjectionBuilder,
    ProjectionBuildResult,
    build_projection,
)


def _tool_started(
    *,
    call_id: str,
    tool_name: str,
    when: datetime | None = None,
    event_id: str | None = None,
    args_preview: str | None = None,
) -> BaseEvent:
    return BaseEvent(
        id=event_id or f"evt_start_{call_id}",
        type="tool.call.started",
        timestamp=when or datetime.now(UTC),
        aggregate_type="execution",
        aggregate_id="exec_1",
        data={
            "call_id": call_id,
            "tool_name": tool_name,
            "args_preview": args_preview,
        },
    )


def _tool_returned(
    *,
    call_id: str,
    tool_name: str,
    when: datetime | None = None,
    event_id: str | None = None,
    is_error: bool = False,
    duration_ms: int = 12,
    result_preview: str | None = None,
) -> BaseEvent:
    return BaseEvent(
        id=event_id or f"evt_ret_{call_id}",
        type="tool.call.returned",
        timestamp=when or datetime.now(UTC),
        aggregate_type="execution",
        aggregate_id="exec_1",
        data={
            "call_id": call_id,
            "tool_name": tool_name,
            "is_error": is_error,
            "duration_ms": duration_ms,
            "result_preview": result_preview,
        },
    )


def _llm_requested(
    *,
    call_id: str,
    model_id: str,
    when: datetime | None = None,
    event_id: str | None = None,
    caller: str | None = None,
) -> BaseEvent:
    return BaseEvent(
        id=event_id or f"evt_llm_req_{call_id}",
        type="llm.call.requested",
        timestamp=when or datetime.now(UTC),
        aggregate_type="execution",
        aggregate_id="exec_1",
        data={"call_id": call_id, "model_id": model_id, "caller": caller},
    )


def _llm_returned(
    *,
    call_id: str,
    model_id: str,
    when: datetime | None = None,
    event_id: str | None = None,
    is_error: bool = False,
    duration_ms: int = 800,
) -> BaseEvent:
    return BaseEvent(
        id=event_id or f"evt_llm_ret_{call_id}",
        type="llm.call.returned",
        timestamp=when or datetime.now(UTC),
        aggregate_type="execution",
        aggregate_id="exec_1",
        data={
            "call_id": call_id,
            "model_id": model_id,
            "is_error": is_error,
            "duration_ms": duration_ms,
        },
    )


class TestProjectionBuilderConstruction:
    def test_seed_id_required(self) -> None:
        with pytest.raises(ValueError):
            ProjectionBuilder(seed_id="   ")

    def test_empty_run_has_single_default_stage(self) -> None:
        result = ProjectionBuilder(seed_id="seed_abc").build()
        assert isinstance(result, ProjectionBuildResult)
        assert result.run.seed_id == "seed_abc"
        assert result.run.stage_ids == (result.stages[0].stage_id,)
        assert result.stages[0].kind is StageKind.EXECUTE
        assert result.steps == ()

    def test_empty_build_timestamps_are_stable(self) -> None:
        builder = ProjectionBuilder(seed_id="seed_abc")
        first = builder.build()
        second = builder.build()

        assert first.run.started_at == second.run.started_at
        assert first.stages[0].started_at == second.stages[0].started_at


class TestToolProjection:
    def test_paired_tool_emits_one_step(self) -> None:
        start_time = datetime.now(UTC)
        events = [
            _tool_started(
                call_id="c1",
                tool_name="Edit",
                when=start_time,
                event_id="evt_start_c1",
                args_preview="path=src/foo.py",
            ),
            _tool_returned(
                call_id="c1",
                tool_name="Edit",
                when=start_time + timedelta(milliseconds=12),
                event_id="evt_ret_c1",
                result_preview="ok",
            ),
        ]
        result = build_projection(events, seed_id="seed_abc")
        assert len(result.steps) == 1
        step = result.steps[0]
        assert step.kind is StepKind.TOOL_CALL
        assert step.name == "Edit"
        assert step.ok is True
        assert step.source_event_ids == ("evt_start_c1", "evt_ret_c1")
        assert step.metadata["args_preview"] == "path=src/foo.py"
        assert step.metadata["result_preview"] == "ok"
        assert step.metadata["duration_ms"] == 12

    def test_bash_tool_is_shell_command(self) -> None:
        events = [
            _tool_started(call_id="c2", tool_name="Bash"),
            _tool_returned(call_id="c2", tool_name="Bash"),
        ]
        result = build_projection(events, seed_id="seed_abc")
        assert result.steps[0].kind is StepKind.SHELL_COMMAND

    def test_error_returned_sets_ok_false(self) -> None:
        events = [
            _tool_started(call_id="c3", tool_name="Bash"),
            _tool_returned(call_id="c3", tool_name="Bash", is_error=True),
        ]
        result = build_projection(events, seed_id="seed_abc")
        assert result.steps[0].ok is False

    def test_unpaired_start_emits_dangling_step(self) -> None:
        events = [_tool_started(call_id="c4", tool_name="Bash")]
        result = build_projection(events, seed_id="seed_abc")
        assert len(result.steps) == 1
        step = result.steps[0]
        assert step.ok is None
        assert step.ended_at is None
        assert step.source_event_ids == ("evt_start_c4",)

    def test_completion_only_pair_still_emitted(self) -> None:
        events = [_tool_returned(call_id="orphan", tool_name="Bash")]
        result = build_projection(events, seed_id="seed_abc")
        assert len(result.steps) == 1
        step = result.steps[0]
        assert step.source_event_ids == ("evt_ret_orphan",)


class TestLLMProjection:
    def test_paired_llm_emits_one_model_call_step(self) -> None:
        start_time = datetime.now(UTC)
        events = [
            _llm_requested(
                call_id="llm1",
                model_id="claude-sonnet-4.6",
                caller="executor:deliver",
                when=start_time,
                event_id="evt_llm_req",
            ),
            _llm_returned(
                call_id="llm1",
                model_id="claude-sonnet-4.6",
                duration_ms=750,
                when=start_time + timedelta(milliseconds=750),
                event_id="evt_llm_ret",
            ),
        ]
        result = build_projection(events, seed_id="seed_abc")
        assert len(result.steps) == 1
        step = result.steps[0]
        assert step.kind is StepKind.MODEL_CALL
        assert step.name == "claude-sonnet-4.6"
        assert step.source_event_ids == ("evt_llm_req", "evt_llm_ret")
        assert step.metadata["caller"] == "executor:deliver"
        assert step.metadata["duration_ms"] == 750


class TestRunRecordAggregation:
    def test_run_started_and_ended_span_event_range(self) -> None:
        t0 = datetime.now(UTC)
        events = [
            _tool_started(call_id="c1", tool_name="Bash", when=t0, event_id="evt_a"),
            _tool_returned(
                call_id="c1",
                tool_name="Bash",
                when=t0 + timedelta(milliseconds=10),
                event_id="evt_b",
            ),
            _llm_requested(
                call_id="llm1",
                model_id="claude-sonnet-4.6",
                when=t0 + timedelta(milliseconds=20),
                event_id="evt_c",
            ),
            _llm_returned(
                call_id="llm1",
                model_id="claude-sonnet-4.6",
                when=t0 + timedelta(milliseconds=120),
                event_id="evt_d",
            ),
        ]
        result = build_projection(events, seed_id="seed_abc")
        assert result.run.started_at == t0
        assert result.run.ended_at == t0 + timedelta(milliseconds=120)
        assert result.stages[0].started_at == t0
        assert result.stages[0].ended_at == t0 + timedelta(milliseconds=120)
        # All steps reference the stage and run id.
        assert all(s.stage_id == result.stages[0].stage_id for s in result.steps)
        assert all(s.run_id == result.run.run_id for s in result.steps)

    def test_step_ids_match_stage_step_ids(self) -> None:
        events = [
            _tool_started(call_id="c1", tool_name="Bash"),
            _tool_returned(call_id="c1", tool_name="Bash"),
            _tool_started(call_id="c2", tool_name="Edit"),
            _tool_returned(call_id="c2", tool_name="Edit"),
        ]
        result = build_projection(events, seed_id="seed_abc")
        assert tuple(s.step_id for s in result.steps) == result.stages[0].step_ids


class TestIncrementalIngestion:
    def test_add_event_is_chainable(self) -> None:
        builder = ProjectionBuilder(seed_id="seed_abc")
        events = [
            _tool_started(call_id="c1", tool_name="Bash"),
            _tool_returned(call_id="c1", tool_name="Bash"),
        ]
        builder.add_event(events[0]).add_event(events[1])
        result = builder.build()
        assert len(result.steps) == 1

    def test_add_events_is_chainable(self) -> None:
        builder = ProjectionBuilder(seed_id="seed_abc")
        events = [
            _tool_started(call_id="c1", tool_name="Bash"),
            _tool_returned(call_id="c1", tool_name="Bash"),
        ]
        result = builder.add_events(events).build()
        assert len(result.steps) == 1

    def test_replay_build_is_independent(self) -> None:
        builder = ProjectionBuilder(seed_id="seed_abc")
        events = [
            _tool_started(call_id="c1", tool_name="Bash"),
            _tool_returned(call_id="c1", tool_name="Bash"),
        ]
        builder.add_events(events)
        first = builder.build()
        second = builder.build()
        # Step content matches (run id stable across rebuilds).
        assert first.run.run_id == second.run.run_id
        assert first.steps[0].step_id == second.steps[0].step_id

    def test_one_shot_projection_ids_are_stable_across_replays(self) -> None:
        t0 = datetime.now(UTC)
        events = [
            _tool_started(call_id="c1", tool_name="Bash", when=t0, event_id="evt_a"),
            _tool_returned(
                call_id="c1",
                tool_name="Bash",
                when=t0 + timedelta(milliseconds=10),
                event_id="evt_b",
            ),
        ]

        first = build_projection(events, seed_id="seed_abc")
        second = build_projection(events, seed_id="seed_abc")

        assert first.run.run_id == second.run.run_id
        assert first.stages[0].stage_id == second.stages[0].stage_id
        assert first.steps[0].step_id == second.steps[0].step_id

    def test_projection_source_key_controls_run_identity(self) -> None:
        events = [_tool_started(call_id="c1", tool_name="Bash")]

        first = build_projection(events, seed_id="seed_abc", source_key="execution:exec_a")
        second = build_projection(events, seed_id="seed_abc", source_key="execution:exec_a")
        different = build_projection(events, seed_id="seed_abc", source_key="execution:exec_b")

        assert first.run.run_id == second.run.run_id
        assert first.stages[0].stage_id == second.stages[0].stage_id
        assert first.steps[0].step_id == second.steps[0].step_id
        assert first.run.run_id != different.run.run_id
        assert first.stages[0].stage_id != different.stages[0].stage_id
        assert first.steps[0].step_id != different.steps[0].step_id

    def test_incremental_build_after_empty_build_converges_with_one_shot(self) -> None:
        t0 = datetime.now(UTC)
        events = [
            _tool_started(
                call_id="late",
                tool_name="Bash",
                when=t0,
                event_id="evt_late_start",
            )
        ]
        builder = ProjectionBuilder(seed_id="seed_abc")

        empty = builder.build()
        builder.add_events(events)
        incremental = builder.build()
        one_shot = build_projection(events, seed_id="seed_abc")

        assert empty.run.run_id != incremental.run.run_id
        assert incremental.run.run_id == one_shot.run.run_id
        assert incremental.stages[0].stage_id == one_shot.stages[0].stage_id
        assert incremental.steps[0].step_id == one_shot.steps[0].step_id

    def test_in_flight_step_id_stays_stable_across_builds_and_completion(self) -> None:
        t0 = datetime.now(UTC)
        builder = ProjectionBuilder(seed_id="seed_abc")
        builder.add_event(
            _tool_started(
                call_id="stable",
                tool_name="Bash",
                when=t0,
                event_id="evt_start_stable",
            )
        )

        first = builder.build()
        second = builder.build()
        assert len(first.steps) == len(second.steps) == 1
        assert first.steps[0].step_id == second.steps[0].step_id
        assert first.steps[0].ended_at is None

        builder.add_event(
            _tool_returned(
                call_id="stable",
                tool_name="Bash",
                when=t0 + timedelta(milliseconds=10),
                event_id="evt_ret_stable",
            )
        )
        completed = builder.build()
        assert completed.steps[0].step_id == first.steps[0].step_id
        assert completed.steps[0].source_event_ids == (
            "evt_start_stable",
            "evt_ret_stable",
        )
        assert completed.steps[0].ended_at == t0 + timedelta(milliseconds=10)

    def test_steps_remain_in_execution_start_order_not_completion_order(self) -> None:
        t0 = datetime.now(UTC)
        events = [
            _tool_started(call_id="slow", tool_name="Edit", when=t0, event_id="evt_slow_start"),
            _tool_started(
                call_id="fast",
                tool_name="Bash",
                when=t0 + timedelta(milliseconds=1),
                event_id="evt_fast_start",
            ),
            _tool_returned(
                call_id="fast",
                tool_name="Bash",
                when=t0 + timedelta(milliseconds=2),
                event_id="evt_fast_ret",
            ),
            _tool_returned(
                call_id="slow",
                tool_name="Edit",
                when=t0 + timedelta(milliseconds=3),
                event_id="evt_slow_ret",
            ),
        ]

        result = build_projection(events, seed_id="seed_abc")

        assert [step.name for step in result.steps] == ["Edit", "Bash"]
        assert [step.source_event_ids for step in result.steps] == [
            ("evt_slow_start", "evt_slow_ret"),
            ("evt_fast_start", "evt_fast_ret"),
        ]
