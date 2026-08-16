from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from ouroboros.events.base import BaseEvent
from ouroboros.mcp.tools.attention_relay import classify_relay_events

_BASE = datetime(2026, 7, 13, tzinfo=UTC)
Relay = dict[str, Any]


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

    relays = cast(list[Relay], classify_relay_events([routed, exhausted], job_id="job_1"))
    attention = [relay for relay in relays if relay["kind"] == "attention_required"]

    assert {relay["trigger"] for relay in attention} == {
        "ac_recovery_exhausted",
        "model_escalation_failed",
    }
    assert all(relay["engine_ownership"]["state"] == "closed" for relay in attention)
    assert all(relay["recommended_host_actions"][0]["kind"] == "host_verify" for relay in attention)


def test_route_progress_and_exhaustion_surface_to_the_human() -> None:
    escalated = _event(
        1,
        "execution.ac.route_observed",
        {
            "root_ac_index": 0,
            "observation": {
                "route_id": "cheap",
                "failure_class": "EVIDENCE_MISSING",
            },
            "decision": {
                "action": "escalate_route",
                "selected_route": {"route_id": "standard"},
                "reason": "classified_failure",
            },
            "human_handoff_required": False,
        },
    )
    exhausted = _event(
        2,
        "execution.ac.route_observed",
        {
            "root_ac_index": 0,
            "observation": {
                "route_id": "frontier",
                "failure_class": "EVIDENCE_MISSING",
            },
            "decision": {
                "action": "blocked",
                "selected_route": None,
                "attempted_route_ids": ["cheap", "standard", "frontier"],
                "reason": "routes_exhausted",
            },
            "human_handoff_required": True,
        },
    )

    relays = cast(list[Relay], classify_relay_events([escalated, exhausted], job_id="job_1"))

    progress = next(relay for relay in relays if relay.get("subtype") == "route_escalated")
    assert progress["evidence"]["from_route_id"] == "cheap"
    assert progress["evidence"]["to_route_id"] == "standard"
    attention = next(relay for relay in relays if relay.get("trigger") == "route_exhausted")
    assert attention["engine_ownership"]["state"] == "closed"
    assert attention["evidence"]["reason"] == "routes_exhausted"


def test_hard_block_is_not_mislabeled_as_route_exhaustion() -> None:
    blocked = _event(
        1,
        "execution.ac.route_observed",
        {
            "root_ac_index": 0,
            "observation": {
                "route_id": "cheap",
                "failure_class": "BLOCKED",
            },
            "decision": {
                "action": "blocked",
                "selected_route": None,
                "attempted_route_ids": ["cheap"],
                "remaining_route_ids": ["standard", "frontier"],
                "reason": "human_handoff_required",
            },
            "human_handoff_required": True,
        },
    )

    relays = cast(list[Relay], classify_relay_events([blocked], job_id="job_1"))

    attention = next(relay for relay in relays if relay.get("trigger") == "route_blocked")
    assert attention["evidence"]["reason"] == "human_handoff_required"


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

    without_audit = cast(
        Relay,
        classify_relay_events(
            [exhausted],
            available_tools={"ouroboros_start_execute_seed"},
        )[0],
    )
    with_both = cast(
        Relay,
        classify_relay_events(
            [exhausted],
            available_tools={
                "ouroboros_start_execute_seed",
                "ouroboros_record_conductor_decision",
            },
        )[0],
    )

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

    relays = cast(list[Relay], classify_relay_events([first, second], job_id="job_1"))
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
        ("auto.seed_preflight.blocked", "seed_preflight_blocked"),
        ("auto.session.blocked", "auto_session_blocked"),
        ("lineage.stagnated", "lineage_stagnated"),
        ("control.session.signal.delivery_uncertain", "session_signal_delivery_uncertain"),
    ],
)
def test_direct_attention_triggers(event_type: str, expected_trigger: str) -> None:
    event = _event(1, event_type, {"reason": "bounded reason"})

    relays = classify_relay_events([event], job_id="job_1")

    assert any(relay.get("trigger") == expected_trigger for relay in relays)


def test_auto_blocked_events_wake_attention_waiters() -> None:
    """Silent-block regression: a blocked auto session must be able to wake a
    ``wait_for="attention_or_ac_change"`` observer, which filters on
    ``RELAY_SOURCE_EVENT_TYPES`` membership before classification runs."""
    from ouroboros.mcp.tools.attention_relay import RELAY_SOURCE_EVENT_TYPES

    assert "auto.seed_qa.blocked" in RELAY_SOURCE_EVENT_TYPES
    assert "auto.seed_preflight.blocked" in RELAY_SOURCE_EVENT_TYPES
    assert "auto.session.blocked" in RELAY_SOURCE_EVENT_TYPES


def test_auto_session_blocked_relays_blocker_evidence() -> None:
    event = _event(
        1,
        "auto.session.blocked",
        {
            "status": "blocked",
            "stop_reason_code": "seed_preflight_unexecutable",
            "tool_name": "seed_preflight",
            "blocker": "Seed preflight found 3 unexecutable contract claim(s); …",
            "resume_capability": "none",
        },
    )

    relays = cast(list[Relay], classify_relay_events([event], job_id="job_1"))
    relay = next(item for item in relays if item.get("trigger") == "auto_session_blocked")

    assert relay["evidence"]["stop_reason_code"] == "seed_preflight_unexecutable"
    assert relay["evidence"]["blocker"].startswith("Seed preflight found")
    assert relay["evidence"]["resume_capability"] == "none"


def test_seed_preflight_blocked_relays_open_questions() -> None:
    event = _event(
        1,
        "auto.seed_preflight.blocked",
        {
            "seed_id": "seed_2be2907edc07",
            "codes": ["unbound_env_var", "context_reference_unresolved"],
            "open_questions": [
                "What concrete value must $VAULT_PATH hold when the verify command runs?",
                "What is the real, absolute path for the referenced context 'Obsidian Vault'?",
            ],
        },
    )

    relays = cast(list[Relay], classify_relay_events([event], job_id="job_1"))
    relay = next(item for item in relays if item.get("trigger") == "seed_preflight_blocked")

    assert relay["evidence"]["codes"] == [
        "unbound_env_var",
        "context_reference_unresolved",
    ]
    assert relay["evidence"]["open_questions"][0].startswith("What concrete value")


def test_seed_qa_advisory_reports_without_claiming_ownership() -> None:
    """An advisory verdict is information, not an ownership claim.

    The engine ran the Seed despite an unresolved verdict, but whether it keeps
    going is decided by the gates that follow — so this must never be durable
    evidence that ownership is `active`, which a later block would falsify.
    """
    blocked = _event(1, "auto.seed_qa.blocked", {"reason": "repair_budget_exhausted"})
    advisory = _event(2, "auto.seed_qa.advisory_override", {"reason": "repair_budget_exhausted"})

    relays = classify_relay_events([blocked, advisory], job_id="job_1")
    by_key = {cast(str, r.get("trigger") or r.get("subtype")): r for r in relays}

    assert cast(dict, by_key["seed_qa_blocked"]["engine_ownership"])["state"] == "closed"
    reported = by_key["seed_qa_advisory"]
    assert reported["kind"] == "progress_advanced"
    assert "engine_ownership" not in reported
    assert "recommended_host_actions" not in reported
    assert reported["evidence"]["reason"] == "repair_budget_exhausted"


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

    relays = cast(
        list[Relay],
        classify_relay_events([plan, route_one, route_same], job_id="job_1"),
    )

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


def test_legacy_level_producers_use_session_aggregate_scope() -> None:
    """Production level events carry the session only as aggregate_id."""
    level_started = BaseEvent(
        id="legacy_level_started",
        type="execution.decomposition.level_started",
        aggregate_type="execution",
        aggregate_id="orch_1",
        timestamp=_BASE + timedelta(seconds=1),
        data={"level": 0, "total_levels": 1, "child_indices": [0]},
    )
    level_completed = BaseEvent(
        id="legacy_level_completed",
        type="execution.decomposition.level_completed",
        aggregate_type="execution",
        aggregate_id="orch_1",
        timestamp=_BASE + timedelta(seconds=2),
        data={"level": 0, "successful": 1, "failed": 0, "outcome": "succeeded"},
    )

    relays = cast(
        list[Relay],
        classify_relay_events(
            [level_started, level_completed],
            job_id="job_1",
            session_id="orch_1",
        ),
    )

    assert [relay["subtype"] for relay in relays] == ["level_started", "level_completed"]
    assert all(relay["scope"]["session_id"] == "orch_1" for relay in relays)


def test_explicit_foreign_session_wins_over_legacy_level_aggregate_inference() -> None:
    """A migrated payload must not be re-attributed from its aggregate shape."""
    foreign_level = BaseEvent(
        id="foreign_level",
        type="execution.decomposition.level_started",
        aggregate_type="execution",
        aggregate_id="orch_1",
        timestamp=_BASE + timedelta(seconds=1),
        data={
            "orchestrator_session_id": "orch_2",
            "level": 0,
            "total_levels": 1,
            "child_indices": [0],
        },
    )

    assert (
        classify_relay_events(
            [foreign_level],
            job_id="job_1",
            session_id="orch_1",
        )
        == []
    )


def test_legacy_frugality_proof_uses_single_session_execution_scope() -> None:
    """Production proof events omit session_id but share the execution aggregate."""
    configuration = BaseEvent(
        id="legacy_config",
        type="execution.run.configuration_resolved",
        aggregate_type="execution",
        aggregate_id="exec_1",
        timestamp=_BASE,
        data={
            "execution_id": "exec_1",
            "session_id": "orch_1",
            "frugality_assurance": "observe",
        },
    )
    proof = BaseEvent(
        id="legacy_proof",
        type="execution.frugality_proof.evaluated",
        aggregate_type="execution",
        aggregate_id="exec_1",
        timestamp=_BASE + timedelta(seconds=1),
        data={
            "execution_id": "exec_1",
            "status": "fail_no_frugality",
            "reason": "no measurable savings",
        },
    )

    relays = cast(
        list[Relay],
        classify_relay_events(
            [configuration, proof],
            job_id="job_1",
            session_id="orch_1",
        ),
    )

    attention = next(relay for relay in relays if relay["kind"] == "attention_required")
    assert attention["trigger"] == "frugality_no_savings"
    assert attention["scope"]["session_id"] == "orch_1"


def test_legacy_frugality_proof_fails_closed_for_ambiguous_sessions() -> None:
    proof = BaseEvent(
        id="ambiguous_proof",
        type="execution.frugality_proof.evaluated",
        aggregate_type="execution",
        aggregate_id="exec_1",
        timestamp=_BASE + timedelta(seconds=2),
        data={
            "execution_id": "exec_1",
            "status": "fail_no_frugality",
            "reason": "no measurable savings",
        },
    )
    history = [
        _event(1, "execution.run.configuration_resolved", {"frugality_assurance": "observe"}),
        _event(
            2,
            "execution.run.configuration_resolved",
            {"session_id": "orch_2", "frugality_assurance": "observe"},
        ),
        proof,
    ]

    relays = classify_relay_events(history, job_id="job_1", session_id="orch_1")
    assert not any(relay.get("trigger") == "frugality_no_savings" for relay in relays)


def test_foreign_execution_attention_evidence_is_not_relayed() -> None:
    foreign_routed = _event(
        1,
        "execution.ac.model_routed",
        {
            "session_id": "orch_2",
            "semantic_ac_key": "ac_foreign",
            "model_tier": "frontier",
            "model": "gpt-5.5",
            "model_escalated": True,
        },
    )
    foreign_exhausted = _event(
        2,
        "execution.ac.recovery_exhausted",
        {
            "session_id": "orch_2",
            "semantic_ac_key": "ac_foreign",
            "last_failure_class": "verify_command_failed",
        },
    )
    foreign_attempt = _event(
        3,
        "execution.ac.attempt_judged",
        {
            "session_id": "orch_2",
            "semantic_ac_key": "ac_foreign",
            "root_ac_index": 0,
            "attempt_number": 1,
            "outcome": "failed",
        },
    )

    relays = classify_relay_events(
        [foreign_routed, foreign_exhausted, foreign_attempt],
        job_id="job_1",
        session_id="orch_1",
    )

    assert relays == []


def test_execution_event_without_session_identity_fails_closed_for_linked_job() -> None:
    unscoped = _event(
        1,
        "execution.ac.recovery_exhausted",
        {
            "session_id": None,
            "semantic_ac_key": "ac_unscoped",
            "last_failure_class": "verify_command_failed",
        },
    )

    assert (
        classify_relay_events(
            [unscoped],
            job_id="job_1",
            session_id="orch_1",
        )
        == []
    )


def test_foreign_control_signal_attention_is_not_relayed() -> None:
    foreign = _event(
        1,
        "control.session.signal.rejected",
        {
            "orchestrator_session_id": "orch_2",
            "rejection_code": "expired",
            "detail": "signal expired before delivery",
        },
    )
    unscoped = _event(
        2,
        "control.session.signal.rejected",
        {
            "session_id": None,
            "rejection_code": "invalid_target",
            "detail": "target session missing",
        },
    )

    assert (
        classify_relay_events(
            [foreign, unscoped],
            job_id="job_1",
            session_id="orch_1",
        )
        == []
    )


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

    relay = cast(Relay, classify_relay_events([completed], job_id="job_1")[0])

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
