"""Successor authorization tests at the execute-seed MCP boundary."""

from __future__ import annotations

import pytest
import yaml

from ouroboros.mcp.tools.conductor_handler import RecordConductorDecisionHandler
from ouroboros.mcp.tools.execution_handlers import _prepare_conductor_successor_seed
from ouroboros.persistence.event_store import EventStore

_SEED = """goal: Preserve the approved behavior
constraints:
  - Keep compatibility
acceptance_criteria:
  - The verification remains green
ontology_schema:
  name: Successor
  description: Successor authorization
metadata:
  ambiguity_score: 0.1
"""

_DIRECTIVE = {
    "source_attention_event_id": "attention_1",
    "instruction": "Add repository evidence for the rejected claim.",
    "rejected_reasons": ["The cited file was not observed."],
    "deterministic": True,
}


@pytest.fixture
async def store() -> EventStore:
    event_store = EventStore("sqlite+aiosqlite:///:memory:")
    await event_store.initialize()
    try:
        yield event_store
    finally:
        await event_store.close()


async def _record_selection(store: EventStore) -> None:
    result = await RecordConductorDecisionHandler(store).handle(
        {
            "decision_id": "decision_1",
            "phase": "selected",
            "attention_event_id": "attention_1",
            "evidence_event_ids": ["event_1"],
            "verification_summary": "The failure was reproduced from durable evidence.",
            "selected_action": "start_corrective_successor",
            "selected_effect": "successor_only",
            "actor_mode": "auto",
            "engine_ownership_state": "closed",
            "root_job_id": "job_root",
            "predecessor_execution_id": "exec_predecessor",
            "action_arguments": {"model_tier": "large"},
            "conductor_directive": _DIRECTIVE,
        }
    )
    assert result.is_ok


@pytest.mark.asyncio
async def test_authorized_successor_embeds_exact_audited_directive(store: EventStore) -> None:
    await _record_selection(store)

    result = await _prepare_conductor_successor_seed(
        arguments={
            "conductor_decision_id": "decision_1",
            "predecessor_execution_id": "exec_predecessor",
            "conductor_directive": _DIRECTIVE,
        },
        seed_content=_SEED,
        event_store=store,
        tool_name="ouroboros_start_execute_seed",
        is_resume=False,
    )

    assert result.is_ok
    parsed = yaml.safe_load(result.value)
    assert parsed["conductor_decision_id"] == "decision_1"
    assert parsed["predecessor_execution_id"] == "exec_predecessor"
    assert parsed["conductor_directive"]["instruction"] == _DIRECTIVE["instruction"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "expected_error"),
    [
        (
            {
                "conductor_decision_id": "decision_1",
                "predecessor_execution_id": "exec_other",
                "conductor_directive": _DIRECTIVE,
            },
            "predecessor_execution_id",
        ),
        (
            {
                "conductor_decision_id": "decision_1",
                "predecessor_execution_id": "exec_predecessor",
                "conductor_directive": {
                    **_DIRECTIVE,
                    "instruction": "Use an unaudited replacement instruction.",
                },
            },
            "conductor_directive",
        ),
    ],
)
async def test_successor_rejects_mismatched_authorization(
    store: EventStore,
    arguments: dict[str, object],
    expected_error: str,
) -> None:
    await _record_selection(store)

    result = await _prepare_conductor_successor_seed(
        arguments=arguments,
        seed_content=_SEED,
        event_store=store,
        tool_name="ouroboros_start_execute_seed",
        is_resume=False,
    )

    assert result.is_err
    assert expected_error in result.error.message


@pytest.mark.asyncio
async def test_successor_directive_cannot_be_injected_on_resume(store: EventStore) -> None:
    result = await _prepare_conductor_successor_seed(
        arguments={"conductor_directive": _DIRECTIVE},
        seed_content=_SEED,
        event_store=store,
        tool_name="ouroboros_start_execute_seed",
        is_resume=True,
    )

    assert result.is_err
    assert "cannot be injected on resume" in result.error.message
