from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ouroboros.events.base import BaseEvent
from ouroboros.mcp.tools.attention_relay import classify_relay_events

_BASE = datetime(2026, 7, 13, tzinfo=UTC)


def _event(index: int, event_type: str, data: dict[str, object]) -> BaseEvent:
    return BaseEvent(
        id=f"event_{index:02d}",
        type=event_type,
        aggregate_type="execution",
        aggregate_id="exec_1",
        timestamp=_BASE + timedelta(seconds=index),
        data={"execution_id": "exec_1", "session_id": "orch_1", **data},
    )


def test_recovery_exhaustion_and_model_escalation_are_closed_attention() -> None:
    routed = _event(
        1,
        "execution.ac.model_routed",
        {
            "semantic_ac_key": "ac_0123456789abcdef",
            "model_tier": "frontier",
            "model": "gpt-5.5",
            "model_escalated": True,
            "retry_attempt": 2,
        },
    )
    exhausted = _event(
        2,
        "execution.ac.recovery_exhausted",
        {
            "semantic_ac_key": "ac_0123456789abcdef",
            "retry_termination_reason": "budget_exhausted",
            "alternate_redispatch_status": "failed",
            "last_failure_class": "verify_command_failed",
            "retry_attempt": 2,
            "configured_retry_attempts": 2,
        },
    )

    relays = classify_relay_events([routed, exhausted], job_id="job_1")
    attention = [relay for relay in relays if relay["kind"] == "attention_required"]

    assert {relay["trigger"] for relay in attention} == {
        "ac_recovery_exhausted",
        "model_escalation_failed",
    }
    assert all(relay["engine_ownership"]["state"] == "closed" for relay in attention)
    assert all(relay["recommended_host_actions"][0]["kind"] == "host_verify" for relay in attention)


def test_mutating_action_menu_requires_both_successor_and_audit_tools() -> None:
    exhausted = _event(
        1,
        "execution.ac.recovery_exhausted",
        {
            "semantic_ac_key": "ac_0123456789abcdef",
            "retry_termination_reason": "budget_exhausted",
            "alternate_redispatch_status": "failed",
            "last_failure_class": "verify_command_failed",
            "retry_attempt": 2,
            "configured_retry_attempts": 2,
        },
    )

    without_audit = classify_relay_events(
        [exhausted],
        available_tools={"ouroboros_start_execute_seed"},
    )[0]
    with_both = classify_relay_events(
        [exhausted],
        available_tools={
            "ouroboros_start_execute_seed",
            "ouroboros_record_conductor_decision",
        },
    )[0]

    assert not any(
        action["kind"] == "mcp_tool" for action in without_audit["recommended_host_actions"]
    )
    mcp_actions = [
        action for action in with_both["recommended_host_actions"] if action["kind"] == "mcp_tool"
    ]
    assert [action["tool"] for action in mcp_actions] == ["ouroboros_start_execute_seed"]
    assert mcp_actions[0]["decision_audit"]["tool"] == ("ouroboros_record_conductor_decision")


def test_rejected_streak_is_read_only_until_recovery_closes() -> None:
    first = _event(
        1,
        "execution.ac.deliver_verdict",
        {
            "semantic_ac_key": "ac_0123456789abcdef",
            "traceguard_verdict": "rejected",
            "rejected_reasons": ["missing repository evidence"],
        },
    )
    second = _event(
        2,
        "execution.ac.deliver_verdict",
        {
            "semantic_ac_key": "ac_0123456789abcdef",
            "traceguard_verdict": "rejected",
            "rejected_reasons": ["claim does not match test output"],
        },
    )

    relays = classify_relay_events([first, second], job_id="job_1")
    relay = next(
        item for item in relays if item.get("trigger") == "deliver_verdict_rejected_streak"
    )

    assert relay["engine_ownership"]["state"] == "active"
    assert relay["recommended_host_actions"][1]["action"] == ("defer_until_engine_recovery_closes")
    assert relay["evidence"]["rejected_reasons"] == [
        "missing repository evidence",
        "claim does not match test output",
    ]


def test_frugality_attention_respects_persisted_assurance() -> None:
    proof = _event(
        2,
        "execution.frugality_proof.evaluated",
        {"status": "fail_no_frugality", "reason": "no measurable savings"},
    )
    off = _event(
        1,
        "execution.run.configuration_resolved",
        {"frugality_assurance": "off"},
    )
    observe = _event(
        1,
        "execution.run.configuration_resolved",
        {"frugality_assurance": "observe"},
    )

    assert not any(
        relay.get("trigger") == "frugality_no_savings"
        for relay in classify_relay_events([off, proof], job_id="job_1")
    )
    assert any(
        relay.get("trigger") == "frugality_no_savings"
        for relay in classify_relay_events([observe, proof], job_id="job_1")
    )


@pytest.mark.parametrize(
    ("event_type", "expected_trigger"),
    [
        ("auto.seed_qa.blocked", "seed_qa_blocked"),
        ("lineage.stagnated", "lineage_stagnated"),
        ("control.session.signal.delivery_uncertain", "session_signal_delivery_uncertain"),
    ],
)
def test_direct_attention_triggers(event_type: str, expected_trigger: str) -> None:
    event = _event(1, event_type, {"reason": "bounded reason"})

    relays = classify_relay_events([event], job_id="job_1")

    assert any(relay.get("trigger") == expected_trigger for relay in relays)


def test_proactive_relay_has_no_action_menu_and_deduplicates_unchanged_route() -> None:
    plan = _event(
        1,
        "execution.plan.created",
        {
            "total_acs": 2,
            "total_levels": 1,
            "parallelizable": True,
            "first_level": 1,
            "first_ac_indices": [0, 1],
            "levels": [{"ac_summaries": ["API", "CLI"]}],
        },
    )
    route_one = _event(
        2,
        "execution.ac.model_routed",
        {
            "semantic_ac_key": "ac_0123456789abcdef",
            "model_tier": "standard",
            "model": "gpt-5",
            "model_mode": "enforced",
            "runtime_backend": "codex_cli",
            "retry_attempt": 0,
        },
    )
    route_same = route_one.model_copy(
        update={"id": "event_03", "timestamp": _BASE + timedelta(seconds=3)}
    )

    relays = classify_relay_events([plan, route_one, route_same], job_id="job_1")

    proactive = [relay for relay in relays if relay["kind"] != "attention_required"]
    assert all("recommended_host_actions" not in relay for relay in proactive)
    assert sum(relay["subtype"] == "ac_routing" for relay in proactive) == 1
    plan_relay = next(relay for relay in proactive if relay["subtype"] == "execution_plan")
    assert plan_relay["evidence"]["first_ac_summaries"] == ["API", "CLI"]


def test_acceptance_relay_is_scoped_to_the_linked_session() -> None:
    acceptance_a = _event(
        1,
        "execution.ac.acceptance_finalized",
        {
            "root_ac_index": 0,
            "final_retry_attempt": 0,
            "accepted": True,
            "disposition": "accepted",
            "terminal_status": "completed",
        },
    )
    acceptance_b = _event(
        2,
        "execution.ac.acceptance_finalized",
        {
            "session_id": "orch_2",
            "root_ac_index": 1,
            "final_retry_attempt": 0,
            "accepted": True,
            "disposition": "accepted",
            "terminal_status": "completed",
        },
    )

    relays = classify_relay_events(
        [acceptance_a, acceptance_b],
        job_id="job_1",
        session_id="orch_1",
    )

    accepted = [relay for relay in relays if relay.get("subtype") == "ac_accepted"]
    assert [relay["source_event_id"] for relay in accepted] == [acceptance_a.id]


def test_synapse_completed_relay_carries_only_bounded_reply_summary() -> None:
    completed = _event(
        1,
        "control.session.signal.completed",
        {
            "requested_mode": "inform",
            "effective_mode": "inform",
            "summary": "Inform signal processing completed",
            "reply": "AC 1 is waiting on one assertion.",
        },
    )

    relay = classify_relay_events([completed], job_id="job_1")[0]

    assert relay["kind"] == "progress_advanced"
    assert relay["subtype"] == "synapse_delivery"
    assert relay["evidence"]["application_proven"] is True
    assert relay["evidence"]["reply"] == "AC 1 is waiting on one assertion."


def test_malformed_evidence_fails_closed() -> None:
    malformed = _event(
        1,
        "execution.ac.deliver_verdict",
        {"traceguard_verdict": "rejected", "rejected_reasons": "not-a-list"},
    )

    relays = classify_relay_events([malformed], job_id="job_1")

    assert not any(relay.get("trigger") == "deliver_verdict_rejected_streak" for relay in relays)
