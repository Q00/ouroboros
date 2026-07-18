"""Task 3: conductor decision responses echo verification_summary/selected_action
and surface the Task 1/2 trust-verdict / lateral-escalation state for the
execution the decision follows.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ouroboros.events.base import BaseEvent
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
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'conductor_trust.db'}")
    await event_store.initialize()
    try:
        yield event_store
    finally:
        await event_store.close()


@pytest.mark.asyncio
async def test_selected_response_echoes_verification_summary_and_action(
    store: EventStore,
) -> None:
    handler = RecordConductorDecisionHandler(store)

    result = await handler.handle(_selected())

    assert result.is_ok
    tool_result = result.value
    assert "Selected action: start_corrective_successor" in tool_result.text_content
    assert (
        "Verification summary: The failure is reproduced from durable evidence."
        in tool_result.text_content
    )
    assert tool_result.meta["selected_action"] == "start_corrective_successor"
    assert (
        tool_result.meta["verification_summary"]
        == "The failure is reproduced from durable evidence."
    )


@pytest.mark.asyncio
async def test_terminal_response_also_echoes_the_original_selection(store: EventStore) -> None:
    handler = RecordConductorDecisionHandler(store)
    await handler.handle(_selected())

    completed = await handler.handle(
        {
            "decision_id": "decision_1",
            "phase": "completed",
            "result_receipt": "Successor execution accepted.",
            "successor_execution_id": "exec_successor",
        }
    )

    assert completed.is_ok
    assert "Selected action: start_corrective_successor" in completed.value.text_content
    assert completed.value.meta["selected_action"] == "start_corrective_successor"


@pytest.mark.asyncio
async def test_trust_and_escalation_state_surfaced_when_present(store: EventStore) -> None:
    """The decision follows execution ``exec_predecessor``, which has one
    untrustworthy decomposition and one parked AC — both must surface."""
    await store.append(
        BaseEvent(
            type="execution.ac.decomposition_attested",
            aggregate_type="execution",
            aggregate_id="exec_predecessor",
            data={
                "node_id": "ac_1",
                "verdict": "untrustworthy",
                "trustworthy": False,
                "reason": "parent verify gate failed after decomposition",
            },
        )
    )
    await store.append(
        BaseEvent(
            type="execution.ac.parked_for_operator",
            aggregate_type="execution",
            aggregate_id="exec_predecessor",
            data={
                "node_id": "ac_2",
                "root_ac_index": 1,
                "personas_tried": ["hacker", "researcher", "simplifier", "architect", "contrarian"],
                "consecutive_terminal_failures": 9,
                "backoff_seconds": 300.0,
                "reason": "all lateral-thinking personas exhausted",
            },
        )
    )

    handler = RecordConductorDecisionHandler(store)
    result = await handler.handle(_selected())

    assert result.is_ok
    assert "1 untrustworthy decomposition" in result.value.text_content
    assert "1 AC parked for operator" in result.value.text_content
    assert "1 untrustworthy decomposition" in result.value.meta["trust_escalation_summary"]


@pytest.mark.asyncio
async def test_resolved_parked_ac_is_excluded_from_the_summary(store: EventStore) -> None:
    """Fix 8 (BLOCKING, PR #1648 review): an AC parked then later resolved
    (succeeded) must NOT keep being cited as "parked for operator" in this
    audit summary — without subtracting the resolution, the receipt would
    misreport a completed AC as still needing operator attention forever."""
    await store.append(
        BaseEvent(
            type="execution.ac.parked_for_operator",
            aggregate_type="execution",
            aggregate_id="exec_predecessor",
            data={
                "node_id": "ac_2",
                "root_ac_index": 1,
                "personas_tried": ["hacker"],
                "consecutive_terminal_failures": 3,
                "backoff_seconds": 300.0,
                "reason": "all lateral-thinking personas exhausted",
            },
        )
    )
    await store.append(
        BaseEvent(
            type="execution.ac.parked_resolved",
            aggregate_type="execution",
            aggregate_id="exec_predecessor",
            data={"node_id": "ac_2", "root_ac_index": 1},
        )
    )

    handler = RecordConductorDecisionHandler(store)
    result = await handler.handle(_selected())

    assert result.is_ok
    assert "parked for operator" not in result.value.text_content
    assert "trust_escalation_summary" not in result.value.meta


@pytest.mark.asyncio
async def test_node_untrustworthy_then_trustworthy_again_is_not_reported(
    store: EventStore,
) -> None:
    """Fix 8 (round 3, BLOCKING): a node that was untrustworthy and LATER
    became trustworthy again (a fresh decomposition round re-attested) must
    NOT keep being reported as untrustworthy forever -- the summary must
    reflect the LATEST attestation, folded in chronological order, not "was
    it EVER untrustworthy across the whole history."""
    from datetime import UTC, datetime

    await store.append(
        BaseEvent(
            type="execution.ac.decomposition_attested",
            aggregate_type="execution",
            aggregate_id="exec_predecessor",
            timestamp=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
            data={
                "node_id": "ac_1",
                "verdict": "untrustworthy",
                "trustworthy": False,
                "reason": "parent verify gate failed after decomposition",
            },
        )
    )
    await store.append(
        BaseEvent(
            type="execution.ac.decomposition_attested",
            aggregate_type="execution",
            aggregate_id="exec_predecessor",
            timestamp=datetime(2026, 1, 1, 0, 5, 0, tzinfo=UTC),
            data={
                "node_id": "ac_1",
                "verdict": "trustworthy",
                "trustworthy": True,
                "reason": "every sibling and the parent gate passed on retry",
            },
        )
    )

    handler = RecordConductorDecisionHandler(store)
    result = await handler.handle(_selected())

    assert result.is_ok
    assert "untrustworthy decomposition" not in result.value.text_content
    assert "trust_escalation_summary" not in result.value.meta


@pytest.mark.asyncio
async def test_park_resolve_park_cycle_still_reports_parked(store: EventStore) -> None:
    """Fix 8 (round 3, BLOCKING): a node parked, then resolved, then parked
    AGAIN (a second escalation cycle) must be reported as CURRENTLY parked --
    the LATEST event for that node id, not "was it EVER resolved across the
    whole history" (which used to permanently subtract it out via set
    subtraction, even after a later re-park)."""
    from datetime import UTC, datetime

    await store.append(
        BaseEvent(
            type="execution.ac.parked_for_operator",
            aggregate_type="execution",
            aggregate_id="exec_predecessor",
            timestamp=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
            data={
                "node_id": "ac_2",
                "root_ac_index": 1,
                "personas_tried": ["hacker"],
                "consecutive_terminal_failures": 3,
                "backoff_seconds": 300.0,
                "reason": "all lateral-thinking personas exhausted",
            },
        )
    )
    await store.append(
        BaseEvent(
            type="execution.ac.parked_resolved",
            aggregate_type="execution",
            aggregate_id="exec_predecessor",
            timestamp=datetime(2026, 1, 1, 0, 5, 0, tzinfo=UTC),
            data={"node_id": "ac_2", "root_ac_index": 1},
        )
    )
    await store.append(
        BaseEvent(
            type="execution.ac.parked_for_operator",
            aggregate_type="execution",
            aggregate_id="exec_predecessor",
            timestamp=datetime(2026, 1, 1, 0, 10, 0, tzinfo=UTC),
            data={
                "node_id": "ac_2",
                "root_ac_index": 1,
                "personas_tried": ["hacker", "researcher", "simplifier"],
                "consecutive_terminal_failures": 6,
                "backoff_seconds": 300.0,
                "reason": "all lateral-thinking personas exhausted",
            },
        )
    )

    handler = RecordConductorDecisionHandler(store)
    result = await handler.handle(_selected())

    assert result.is_ok
    assert "1 AC parked for operator" in result.value.text_content
    assert "1 AC parked for operator" in result.value.meta["trust_escalation_summary"]


@pytest.mark.asyncio
async def test_no_trust_escalation_line_when_nothing_reported(store: EventStore) -> None:
    handler = RecordConductorDecisionHandler(store)

    result = await handler.handle(_selected())

    assert result.is_ok
    assert "Trust/escalation" not in result.value.text_content
    assert "trust_escalation_summary" not in result.value.meta


@pytest.mark.asyncio
async def test_no_predecessor_execution_id_skips_trust_escalation_query(store: EventStore) -> None:
    """A read_only decision with no predecessor execution has nothing 'in
    scope' to surface — must not raise, must not appear in the response."""
    handler = RecordConductorDecisionHandler(store)

    result = await handler.handle(
        _selected(
            decision_id="decision_read_only",
            selected_effect="read_only",
            predecessor_execution_id=None,
            root_job_id=None,
        )
    )

    assert result.is_ok
    assert "Trust/escalation" not in result.value.text_content


@pytest.mark.asyncio
async def test_in_flight_persona_escalation_is_surfaced(store: EventStore) -> None:
    """Round-4 follow-up: an AC actively cycling personas (progressed events,
    not yet parked) must be cited as 'in persona escalation' so the conductor
    receipt reflects escalation progress before full parking."""
    await store.append(
        BaseEvent(
            type="execution.ac.lateral_escalation_progressed",
            aggregate_type="execution",
            aggregate_id="exec_predecessor",
            data={
                "node_id": "ac_3",
                "root_ac_index": 2,
                "personas_tried": ["hacker"],
                "consecutive_terminal_failures": 3,
                "parked": False,
                "persona": "hacker",
            },
        )
    )

    handler = RecordConductorDecisionHandler(store)
    result = await handler.handle(_selected())

    assert result.is_ok
    assert "1 AC in persona escalation" in result.value.text_content
    assert "1 AC in persona escalation" in result.value.meta["trust_escalation_summary"]


@pytest.mark.asyncio
async def test_parked_ac_is_not_double_counted_as_escalating(store: EventStore) -> None:
    """A node whose latest state is parked must be cited as parked only —
    never both parked AND 'in persona escalation'."""
    from datetime import UTC, datetime

    await store.append(
        BaseEvent(
            type="execution.ac.lateral_escalation_progressed",
            aggregate_type="execution",
            aggregate_id="exec_predecessor",
            timestamp=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
            data={
                "node_id": "ac_3",
                "root_ac_index": 2,
                "personas_tried": ["hacker"],
                "consecutive_terminal_failures": 3,
                "parked": False,
                "persona": "hacker",
            },
        )
    )
    await store.append(
        BaseEvent(
            type="execution.ac.parked_for_operator",
            aggregate_type="execution",
            aggregate_id="exec_predecessor",
            timestamp=datetime(2026, 1, 1, 0, 5, 0, tzinfo=UTC),
            data={
                "node_id": "ac_3",
                "root_ac_index": 2,
                "personas_tried": ["hacker", "researcher", "simplifier"],
                "consecutive_terminal_failures": 6,
                "backoff_seconds": 300.0,
                "reason": "all lateral-thinking personas exhausted",
            },
        )
    )

    handler = RecordConductorDecisionHandler(store)
    result = await handler.handle(_selected())

    assert result.is_ok
    assert "1 AC parked for operator" in result.value.text_content
    assert "in persona escalation" not in result.value.text_content
