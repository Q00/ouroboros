"""MCP decision-audit tests for Active Conductor."""

from __future__ import annotations

from pathlib import Path

import pytest

from ouroboros.mcp.tools.conductor_handler import RecordConductorDecisionHandler
from ouroboros.persistence.event_store import EventStore


def _selected(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "decision_id": "decision_1",
        "phase": "selected",
        "attention_event_id": "attention_1",
        "evidence_event_ids": ["event_1"],
        "verification_summary": "The failure is reproduced from durable evidence.",
        "selected_action": "start_corrective_successor",
        "selected_effect": "successor_only",
        "actor_mode": "auto",
        "engine_ownership_state": "closed",
        "root_job_id": "job_root",
        "predecessor_execution_id": "exec_predecessor",
        "action_arguments": {"model_tier": "large"},
        "conductor_directive": {
            "source_attention_event_id": "attention_1",
            "instruction": "Support the rejected claims with repository evidence.",
            "rejected_reasons": ["The cited file was not observed."],
            "deterministic": True,
        },
    }
    values.update(overrides)
    return values


@pytest.fixture
async def store(tmp_path: Path):
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'conductor.db'}")
    await event_store.initialize()
    try:
        yield event_store
    finally:
        await event_store.close()


def test_definition_exposes_selected_and_terminal_phases(store: EventStore) -> None:
    definition = RecordConductorDecisionHandler(store).definition
    params = {parameter.name: parameter for parameter in definition.parameters}

    assert definition.name == "ouroboros_record_conductor_decision"
    assert params["phase"].enum == ("selected", "completed", "failed", "declined")
    assert params["selected_effect"].enum == (
        "read_only",
        "successor_only",
        "specification_change",
        "user_escalation",
    )


@pytest.mark.asyncio
async def test_selected_and_completed_events_are_idempotent(store: EventStore) -> None:
    handler = RecordConductorDecisionHandler(store)

    first = await handler.handle(_selected())
    replay = await handler.handle(_selected())
    completed = await handler.handle(
        {
            "decision_id": "decision_1",
            "phase": "completed",
            "result_receipt": "Successor execution accepted.",
            "successor_execution_id": "exec_successor",
        }
    )
    completed_replay = await handler.handle(
        {
            "decision_id": "decision_1",
            "phase": "completed",
            "result_receipt": "Successor execution accepted.",
            "successor_execution_id": "exec_successor",
        }
    )

    assert first.is_ok and first.value.meta["replayed"] is False
    assert replay.is_ok and replay.value.meta["replayed"] is True
    assert completed.is_ok and completed.value.meta["phase"] == "completed"
    assert completed_replay.is_ok and completed_replay.value.meta["replayed"] is True
    events = await store.replay("conductor_decision", "decision_1")
    assert [event.type for event in events] == [
        "conductor.decision.selected",
        "conductor.decision.completed",
    ]
    assert events[0].data["arguments_digest"]
    assert "action_arguments" not in events[0].data

    selected_after_terminal = await handler.handle(_selected())
    assert selected_after_terminal.is_ok
    assert selected_after_terminal.value.meta["phase"] == "selected"
    assert selected_after_terminal.value.meta["event_id"] == events[0].id


@pytest.mark.asyncio
async def test_mutation_requires_closed_ownership(store: EventStore) -> None:
    result = await RecordConductorDecisionHandler(store).handle(
        _selected(engine_ownership_state="active")
    )

    assert result.is_err
    assert "require engine_ownership_state=closed" in result.error.message


@pytest.mark.asyncio
async def test_auto_requires_deterministic_non_relaxing_directive(store: EventStore) -> None:
    result = await RecordConductorDecisionHandler(store).handle(
        _selected(
            conductor_directive={
                "source_attention_event_id": "attention_1",
                "instruction": "Try another approach.",
            }
        )
    )

    assert result.is_err
    assert "deterministic non-relaxing" in result.error.message


@pytest.mark.asyncio
async def test_specification_change_requires_approval(store: EventStore) -> None:
    result = await RecordConductorDecisionHandler(store).handle(
        _selected(
            selected_effect="specification_change",
            actor_mode="run",
            conductor_directive={
                "source_attention_event_id": "attention_1",
                "instruction": "Replace the acceptance criterion.",
                "preserve_acceptance_criteria": False,
                "deterministic": True,
                "user_approval_event_id": "approval_1",
            },
        )
    )

    assert result.is_err
    assert "require user approval" in result.error.message


@pytest.mark.asyncio
async def test_relaxing_directive_requires_spec_effect_and_matching_approval(
    store: EventStore,
) -> None:
    relaxing = {
        "source_attention_event_id": "attention_1",
        "instruction": "Replace the acceptance criterion after approval.",
        "preserve_acceptance_criteria": False,
        "deterministic": True,
        "user_approval_event_id": "approval_directive",
    }
    wrong_effect = await RecordConductorDecisionHandler(store).handle(
        _selected(
            actor_mode="run",
            conductor_directive=relaxing,
        )
    )
    mismatched_approval = await RecordConductorDecisionHandler(store).handle(
        _selected(
            decision_id="decision_approval_mismatch",
            selected_effect="specification_change",
            actor_mode="run",
            user_approval_event_id="approval_selection",
            conductor_directive=relaxing,
        )
    )

    assert wrong_effect.is_err
    assert "selected_effect=specification_change" in wrong_effect.error.message
    assert mismatched_approval.is_err
    assert "approval must match" in mismatched_approval.error.message


@pytest.mark.asyncio
async def test_completed_mutating_decision_requires_successor_receipt(store: EventStore) -> None:
    handler = RecordConductorDecisionHandler(store)
    assert (await handler.handle(_selected())).is_ok

    result = await handler.handle(
        {
            "decision_id": "decision_1",
            "phase": "completed",
            "result_receipt": "Action reported completion without a successor ID.",
        }
    )

    assert result.is_err
    assert "require successor_execution_id" in result.error.message


@pytest.mark.asyncio
async def test_terminal_requires_selected_and_conflicting_terminal_fails(store: EventStore) -> None:
    handler = RecordConductorDecisionHandler(store)
    before_selected = await handler.handle(
        {
            "decision_id": "decision_missing",
            "phase": "declined",
            "result_receipt": "No safe action was available.",
        }
    )
    await handler.handle(_selected())
    await handler.handle(
        {
            "decision_id": "decision_1",
            "phase": "failed",
            "result_receipt": "Successor dispatch failed.",
        }
    )
    conflicting = await handler.handle(
        {
            "decision_id": "decision_1",
            "phase": "declined",
            "result_receipt": "User declined.",
        }
    )

    assert before_selected.is_err
    assert conflicting.is_err
    assert "different terminal outcome" in conflicting.error.message


@pytest.mark.asyncio
async def test_successor_budget_is_one_per_attention_and_two_per_root(store: EventStore) -> None:
    handler = RecordConductorDecisionHandler(store)
    assert (await handler.handle(_selected())).is_ok
    same_attention = await handler.handle(
        _selected(decision_id="decision_2", root_job_id="job_other")
    )
    assert same_attention.is_err
    assert "attention event" in same_attention.error.message

    assert (
        await handler.handle(
            _selected(
                decision_id="decision_3",
                attention_event_id="attention_2",
                root_job_id="job_root",
                conductor_directive={
                    "source_attention_event_id": "attention_2",
                    "instruction": "Correct the second verified failure.",
                    "deterministic": True,
                },
            )
        )
    ).is_ok
    root_exhausted = await handler.handle(
        _selected(
            decision_id="decision_4",
            attention_event_id="attention_3",
            root_job_id="job_root",
            conductor_directive={
                "source_attention_event_id": "attention_3",
                "instruction": "Correct the third verified failure.",
                "deterministic": True,
            },
        )
    )
    assert root_exhausted.is_err
    assert "root job" in root_exhausted.error.message
