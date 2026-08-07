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
                "ac_id": "exec_current_node_1",
                "root_ac_index": 0,
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
                "ac_id": "exec_current_node_1",
                "root_ac_index": 0,
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
                "ac_id": "exec_current_node_1",
                "root_ac_index": 0,
                "typed_evidence_valid": True,
                "verifier_ran": False,
                "verifier_passed": False,
                "verifier_status": None,
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
    assert tool_step.ac_id == evidence_step.ac_id == "ac_0"
    assert len(result.artifacts) == 1
    assert result.artifacts[0].step_id == evidence_step.step_id
    assert len(result.verdicts) == 1
    assert result.verdicts[0].scope == "ac"
    assert result.verdicts[0].ac_id == "ac_0"
    assert result.verdicts[0].ac_id == tool_step.ac_id
    assert result.verdicts[0].outcome is VerdictOutcome.PASS
    assert "args_preview" not in tool_step.metadata
    assert "result_preview" not in tool_step.metadata


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


def test_current_acceptance_preserves_terminal_outcome_semantics() -> None:
    started = datetime(2026, 8, 6, tzinfo=UTC)
    cases = (
        ("cancelled", "cancelled", VerdictOutcome.CANCELLED),
        ("blocked", "failed", VerdictOutcome.FAIL),
        ("invalid", "failed", VerdictOutcome.FAIL),
        ("failed", "failed", VerdictOutcome.FAIL),
    )

    for index, (disposition, terminal_status, expected) in enumerate(cases):
        event = _event(
            f"evt_{disposition}",
            "execution.ac.acceptance_finalized",
            started + timedelta(seconds=index),
            {
                "execution_id": "exec_current",
                "root_ac_index": index,
                "accepted": False,
                "disposition": disposition,
                "outcome": disposition,
                "terminal_status": terminal_status,
            },
        )

        result = build_projection((event,), seed_id="seed_current")

        assert len(result.verdicts) == 1
        assert result.verdicts[0].outcome is expected


def test_current_tool_call_ids_are_scoped_by_canonical_ac_identity() -> None:
    started = datetime(2026, 8, 6, tzinfo=UTC)
    events: list[BaseEvent] = []
    for root_ac_index in range(2):
        events.extend(
            (
                _event(
                    f"evt_start_{root_ac_index}",
                    "execution.tool.started",
                    started + timedelta(milliseconds=root_ac_index),
                    {
                        "execution_id": "exec_current",
                        "ac_id": f"exec_current_runtime_{root_ac_index}",
                        "root_ac_index": root_ac_index,
                        "tool_call_id": "item_0",
                        "tool_name": "Bash",
                    },
                ),
                _event(
                    f"evt_complete_{root_ac_index}",
                    "execution.tool.completed",
                    started + timedelta(seconds=root_ac_index + 1),
                    {
                        "execution_id": "exec_current",
                        "ac_id": f"exec_current_runtime_{root_ac_index}",
                        "root_ac_index": root_ac_index,
                        "tool_call_id": "item_0",
                        "tool_name": "Bash",
                        "is_error": False,
                    },
                ),
            )
        )

    result = build_projection(tuple(events), seed_id="seed_current")

    assert len(result.steps) == 2
    assert {step.ac_id for step in result.steps} == {"ac_0", "ac_1"}
    assert len({step.step_id for step in result.steps}) == 2
    assert {step.source_event_ids for step in result.steps} == {
        ("evt_start_0", "evt_complete_0"),
        ("evt_start_1", "evt_complete_1"),
    }


def test_current_tool_and_evidence_steps_preserve_retry_attempt_history() -> None:
    started = datetime(2026, 8, 6, tzinfo=UTC)
    events: list[BaseEvent] = []
    for retry_attempt in range(2):
        attempt_metadata = {
            "ac_id": "exec_current_runtime_0",
            "root_ac_index": 0,
            "retry_attempt": retry_attempt,
            "session_attempt_id": f"exec_current_runtime_0_attempt_{retry_attempt + 1}",
        }
        events.extend(
            (
                _event(
                    f"evt_start_attempt_{retry_attempt}",
                    "execution.tool.started",
                    started + timedelta(seconds=retry_attempt * 3),
                    {
                        **attempt_metadata,
                        "tool_call_id": "item_0",
                        "tool_name": "Bash",
                    },
                ),
                _event(
                    f"evt_complete_attempt_{retry_attempt}",
                    "execution.tool.completed",
                    started + timedelta(seconds=retry_attempt * 3 + 1),
                    {
                        **attempt_metadata,
                        "tool_call_id": "item_0",
                        "tool_name": "Bash",
                        "is_error": retry_attempt == 0,
                    },
                ),
                _event(
                    f"evt_evidence_attempt_{retry_attempt}",
                    "execution.ac.typed_evidence.observed",
                    started + timedelta(seconds=retry_attempt * 3 + 2),
                    {
                        **attempt_metadata,
                        "typed_evidence_valid": retry_attempt == 1,
                        "verifier_ran": False,
                        "verifier_passed": False,
                    },
                ),
            )
        )

    result = build_projection(tuple(events), seed_id="seed_current")

    assert len(result.steps) == 4
    assert {step.ac_id for step in result.steps} == {"ac_0"}
    assert len({step.step_id for step in result.steps}) == 4
    tool_steps = [step for step in result.steps if step.kind is StepKind.SHELL_COMMAND]
    assert {step.source_event_ids for step in tool_steps} == {
        ("evt_start_attempt_0", "evt_complete_attempt_0"),
        ("evt_start_attempt_1", "evt_complete_attempt_1"),
    }
    assert {step.source_event_ids: step.ok for step in tool_steps} == {
        ("evt_start_attempt_0", "evt_complete_attempt_0"): False,
        ("evt_start_attempt_1", "evt_complete_attempt_1"): True,
    }
    evidence_steps = [step for step in result.steps if step.kind is StepKind.EVIDENCE_SUBMISSION]
    assert {step.source_event_ids: step.ok for step in evidence_steps} == {
        ("evt_evidence_attempt_0",): False,
        ("evt_evidence_attempt_1",): True,
    }
    assert len(result.artifacts) == 2
    evidence_step_ids = {step.step_id for step in evidence_steps}
    assert {artifact.step_id for artifact in result.artifacts} == evidence_step_ids
    assert all(step.artifact_ids for step in evidence_steps)


def test_current_retry_index_scopes_calls_when_session_attempt_id_is_missing() -> None:
    started = datetime(2026, 8, 6, tzinfo=UTC)
    events: list[BaseEvent] = []
    for retry_attempt in range(2):
        for event_type, suffix in (
            ("execution.tool.started", "start"),
            ("execution.tool.completed", "complete"),
        ):
            events.append(
                _event(
                    f"evt_{suffix}_{retry_attempt}",
                    event_type,
                    started + timedelta(seconds=retry_attempt, milliseconds=len(events)),
                    {
                        "ac_id": "runtime_ac0",
                        "root_ac_index": 0,
                        "retry_attempt": retry_attempt,
                        "tool_call_id": "item_0",
                        "tool_name": "Bash",
                        "is_error": False,
                    },
                )
            )

    result = build_projection(tuple(events), seed_id="seed_current")

    assert len(result.steps) == 2
    assert len({step.step_id for step in result.steps}) == 2
    assert {step.source_event_ids for step in result.steps} == {
        ("evt_start_0", "evt_complete_0"),
        ("evt_start_1", "evt_complete_1"),
    }


def test_current_idless_tool_events_use_ac_scoped_fifo_fallback() -> None:
    started = datetime(2026, 8, 6, tzinfo=UTC)
    events = (
        _event(
            "evt_start_ac0",
            "execution.tool.started",
            started,
            {
                "ac_id": "runtime_ac0",
                "root_ac_index": 0,
                "tool_name": "Bash",
            },
        ),
        _event(
            "evt_start_ac1",
            "execution.tool.started",
            started + timedelta(milliseconds=1),
            {
                "ac_id": "runtime_ac1",
                "root_ac_index": 1,
                "tool_name": "Bash",
            },
        ),
        _event(
            "evt_complete_ac1",
            "execution.tool.completed",
            started + timedelta(seconds=1),
            {
                "ac_id": "runtime_ac1",
                "root_ac_index": 1,
                "tool_name": "Bash",
                "tool_result": {"is_error": False},
            },
        ),
        _event(
            "evt_complete_ac0",
            "execution.tool.completed",
            started + timedelta(seconds=2),
            {
                "ac_id": "runtime_ac0",
                "root_ac_index": 0,
                "tool_name": "Bash",
                "tool_result": {"is_error": False},
            },
        ),
    )

    result = build_projection(events, seed_id="seed_current")

    assert len(result.steps) == 2
    assert {step.source_event_ids for step in result.steps} == {
        ("evt_start_ac0", "evt_complete_ac0"),
        ("evt_start_ac1", "evt_complete_ac1"),
    }
    assert all(step.ok is True for step in result.steps)


def test_current_nested_tool_results_preserve_fail_closed_outcomes() -> None:
    started = datetime(2026, 8, 6, tzinfo=UTC)
    cases: tuple[tuple[dict[str, object], bool | None], ...] = (
        ({"tool_result": {"is_error": False}}, True),
        ({"tool_result": {"is_error": True}}, False),
        ({"tool_result": {"is_error": "invalid"}}, False),
        ({"is_error": False, "tool_result": {"is_error": True}}, False),
        ({"tool_result": "malformed"}, False),
        ({}, None),
    )

    for index, (outcome_data, expected) in enumerate(cases):
        event = _event(
            f"evt_result_{index}",
            "execution.tool.completed",
            started + timedelta(seconds=index),
            {
                "ac_id": "runtime_ac0",
                "root_ac_index": 0,
                "tool_call_id": f"item_{index}",
                "tool_name": "Bash",
                **outcome_data,
            },
        )

        result = build_projection((event,), seed_id="seed_current")

        assert len(result.steps) == 1
        assert result.steps[0].ok is expected


def test_typed_evidence_uses_verifier_result_only_when_verifier_ran() -> None:
    started = datetime(2026, 8, 6, tzinfo=UTC)
    cases = (
        (False, False, True, True),
        (False, True, False, False),
        (True, False, True, False),
        (True, True, False, True),
    )

    for index, (verifier_ran, verifier_passed, evidence_valid, expected) in enumerate(cases):
        event = _event(
            f"evt_evidence_{index}",
            "execution.ac.typed_evidence.observed",
            started + timedelta(seconds=index),
            {
                "ac_id": "runtime_ac0",
                "root_ac_index": 0,
                "typed_evidence_valid": evidence_valid,
                "verifier_ran": verifier_ran,
                "verifier_passed": verifier_passed,
            },
        )

        result = build_projection((event,), seed_id="seed_current")

        assert len(result.steps) == 1
        assert result.steps[0].ok is expected
