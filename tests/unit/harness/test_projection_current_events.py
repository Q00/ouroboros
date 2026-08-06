from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ouroboros.events.base import BaseEvent
from ouroboros.harness.projection import StepKind, VerdictOutcome
from ouroboros.harness.projection_builder import build_projection


def _event(
    event_id: str,
    event_type: str,
    when: datetime,
    data: dict[str, object],
) -> BaseEvent:
    return BaseEvent(
        id=event_id,
        type=event_type,
        timestamp=when,
        aggregate_type="execution",
        aggregate_id="exec_current",
        data=data,
    )


def test_current_execution_events_project_steps_artifacts_and_ac_verdicts() -> None:
    started = datetime(2026, 8, 6, tzinfo=UTC)
    events = (
        _event(
            "evt_tool_start",
            "execution.tool.started",
            started,
            {
                "execution_id": "exec_current",
                "ac_id": "node_1",
                "tool_call_id": "item_1",
                "tool_name": "Bash",
                "tool_detail": "pytest -q",
            },
        ),
        _event(
            "evt_tool_complete",
            "execution.tool.completed",
            started + timedelta(seconds=1),
            {
                "execution_id": "exec_current",
                "ac_id": "node_1",
                "tool_call_id": "item_1",
                "tool_name": "Bash",
                "tool_result_text": "1 passed",
                "is_error": False,
            },
        ),
        _event(
            "evt_evidence",
            "execution.ac.typed_evidence.observed",
            started + timedelta(seconds=2),
            {
                "execution_id": "exec_current",
                "ac_id": "node_1",
                "typed_evidence_valid": True,
                "verifier_passed": True,
                "verifier_status": "PASS",
            },
        ),
        _event(
            "evt_acceptance",
            "execution.ac.acceptance_finalized",
            started + timedelta(seconds=3),
            {
                "execution_id": "exec_current",
                "root_ac_index": 0,
                "accepted": True,
                "disposition": "accepted",
                "outcome": "succeeded",
                "terminal_status": "completed",
            },
        ),
    )

    result = build_projection(events, seed_id="seed_current")

    assert len(result.steps) == 2
    tool_step = next(step for step in result.steps if step.kind is StepKind.SHELL_COMMAND)
    assert tool_step.ok is True
    assert tool_step.source_event_ids == ("evt_tool_start", "evt_tool_complete")
    evidence_step = next(step for step in result.steps if step.kind is StepKind.EVIDENCE_SUBMISSION)
    assert evidence_step.ok is True
    assert len(result.artifacts) == 1
    assert result.artifacts[0].step_id == evidence_step.step_id
    assert len(result.verdicts) == 1
    assert result.verdicts[0].scope == "ac"
    assert result.verdicts[0].ac_id == "ac_0"
    assert result.verdicts[0].outcome is VerdictOutcome.PASS


def test_legacy_and_current_tool_events_with_same_call_id_do_not_double_count() -> None:
    started = datetime(2026, 8, 6, tzinfo=UTC)
    events = (
        _event(
            "evt_legacy_start",
            "tool.call.started",
            started,
            {"call_id": "shared", "tool_name": "Bash"},
        ),
        _event(
            "evt_current_start",
            "execution.tool.started",
            started + timedelta(milliseconds=1),
            {"tool_call_id": "shared", "tool_name": "Bash"},
        ),
        _event(
            "evt_current_complete",
            "execution.tool.completed",
            started + timedelta(seconds=1),
            {"tool_call_id": "shared", "tool_name": "Bash", "is_error": False},
        ),
    )

    result = build_projection(events, seed_id="seed_current")

    assert len(result.steps) == 1
    assert result.steps[0].source_event_ids == (
        "evt_current_start",
        "evt_current_complete",
    )
