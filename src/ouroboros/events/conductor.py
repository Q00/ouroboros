"""Durable Active Conductor decision audit events."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ouroboros.core.conductor import (
    CONDUCTOR_SCHEMA_VERSION,
    MAX_ACTION_BYTES,
    MAX_DECISION_ID_BYTES,
    MAX_EVENT_ID_BYTES,
    MAX_RECEIPT_BYTES,
    MAX_VERIFICATION_SUMMARY_BYTES,
    ConductorActorMode,
    ConductorDecisionPhase,
    ConductorDirective,
    ConductorEffect,
    EngineOwnershipState,
    bounded_conductor_optional_text,
    bounded_conductor_text,
    stable_payload_digest,
)
from ouroboros.events.base import BaseEvent

MAX_EVIDENCE_EVENT_IDS = 10


def _event_ids(values: Sequence[str]) -> list[str]:
    if len(values) > MAX_EVIDENCE_EVENT_IDS:
        raise ValueError(f"evidence_event_ids exceeds {MAX_EVIDENCE_EVENT_IDS} items")
    return list(
        dict.fromkeys(
            bounded_conductor_text(
                f"evidence_event_ids[{index}]",
                value,
                max_bytes=MAX_EVENT_ID_BYTES,
                reject_secrets=False,
            )
            for index, value in enumerate(values)
        )
    )


def create_conductor_decision_selected_event(
    *,
    decision_id: str,
    attention_event_id: str,
    evidence_event_ids: Sequence[str],
    verification_summary: str,
    selected_action: str,
    selected_effect: ConductorEffect,
    actor_mode: ConductorActorMode,
    engine_ownership_state: EngineOwnershipState,
    action_arguments: object | None,
    root_job_id: str | None,
    predecessor_execution_id: str | None,
    conductor_directive: ConductorDirective | None,
    user_approval_event_id: str | None,
) -> BaseEvent:
    normalized_decision_id = bounded_conductor_text(
        "decision_id",
        decision_id,
        max_bytes=MAX_DECISION_ID_BYTES,
        reject_secrets=False,
    )
    normalized_attention = bounded_conductor_text(
        "attention_event_id",
        attention_event_id,
        max_bytes=MAX_EVENT_ID_BYTES,
        reject_secrets=False,
    )
    normalized_action = bounded_conductor_text(
        "selected_action",
        selected_action,
        max_bytes=MAX_ACTION_BYTES,
        reject_secrets=False,
    )
    normalized_root_job = bounded_conductor_optional_text(
        "root_job_id",
        root_job_id,
        max_bytes=MAX_EVENT_ID_BYTES,
        reject_secrets=False,
    )
    normalized_approval = bounded_conductor_optional_text(
        "user_approval_event_id",
        user_approval_event_id,
        max_bytes=MAX_EVENT_ID_BYTES,
        reject_secrets=False,
    )
    normalized_predecessor = bounded_conductor_optional_text(
        "predecessor_execution_id",
        predecessor_execution_id,
        max_bytes=MAX_EVENT_ID_BYTES,
        reject_secrets=False,
    )
    data: dict[str, Any] = {
        "schema_version": CONDUCTOR_SCHEMA_VERSION,
        "decision_id": normalized_decision_id,
        "phase": ConductorDecisionPhase.SELECTED.value,
        "attention_event_id": normalized_attention,
        "evidence_event_ids": _event_ids(evidence_event_ids),
        "verification_summary": bounded_conductor_text(
            "verification_summary",
            verification_summary,
            max_bytes=MAX_VERIFICATION_SUMMARY_BYTES,
        ),
        "selected_action": normalized_action,
        "selected_effect": selected_effect.value,
        "actor_mode": actor_mode.value,
        "engine_ownership_state": engine_ownership_state.value,
        "arguments_digest": stable_payload_digest(action_arguments or {}),
        "arguments_keys": (
            sorted(str(key)[:80] for key in action_arguments)[:20]
            if isinstance(action_arguments, dict)
            else []
        ),
        "mutating": selected_effect.mutates,
    }
    if normalized_root_job is not None:
        data["root_job_id"] = normalized_root_job
    if normalized_predecessor is not None:
        data["predecessor_execution_id"] = normalized_predecessor
    if normalized_approval is not None:
        data["user_approval_event_id"] = normalized_approval
    if conductor_directive is not None:
        data["conductor_directive"] = conductor_directive.to_event_data()
        data["conductor_directive_digest"] = conductor_directive.digest
    data["selection_digest"] = stable_payload_digest(data)
    return BaseEvent(
        type="conductor.decision.selected",
        aggregate_type="conductor_decision",
        aggregate_id=normalized_decision_id,
        data=data,
    )


def create_conductor_decision_terminal_event(
    *,
    decision_id: str,
    phase: ConductorDecisionPhase,
    result_receipt: str | None = None,
    successor_execution_id: str | None = None,
) -> BaseEvent:
    if not phase.is_terminal:
        raise ValueError("terminal conductor event requires completed, failed, or declined phase")
    normalized_decision_id = bounded_conductor_text(
        "decision_id",
        decision_id,
        max_bytes=MAX_DECISION_ID_BYTES,
        reject_secrets=False,
    )
    normalized_receipt = bounded_conductor_optional_text(
        "result_receipt",
        result_receipt,
        max_bytes=MAX_RECEIPT_BYTES,
    )
    if normalized_receipt is None:
        raise ValueError(f"conductor decision {phase.value} requires result_receipt")
    normalized_successor = bounded_conductor_optional_text(
        "successor_execution_id",
        successor_execution_id,
        max_bytes=MAX_EVENT_ID_BYTES,
        reject_secrets=False,
    )
    data: dict[str, Any] = {
        "schema_version": CONDUCTOR_SCHEMA_VERSION,
        "decision_id": normalized_decision_id,
        "phase": phase.value,
        "result_receipt": normalized_receipt,
    }
    if normalized_successor is not None:
        data["successor_execution_id"] = normalized_successor
    data["outcome_digest"] = stable_payload_digest(data)
    return BaseEvent(
        type=f"conductor.decision.{phase.value}",
        aggregate_type="conductor_decision",
        aggregate_id=normalized_decision_id,
        data=data,
    )


def create_conductor_directive_attached_event(
    *,
    decision_id: str,
    target_type: str,
    target_id: str,
    predecessor_execution_id: str,
    directive: ConductorDirective,
) -> BaseEvent:
    """Record that a selected directive was attached before successor dispatch."""
    normalized_decision_id = bounded_conductor_text(
        "decision_id",
        decision_id,
        max_bytes=MAX_DECISION_ID_BYTES,
        reject_secrets=False,
    )
    normalized_target_type = bounded_conductor_text(
        "target_type",
        target_type,
        max_bytes=80,
        reject_secrets=False,
    )
    normalized_target_id = bounded_conductor_text(
        "target_id",
        target_id,
        max_bytes=MAX_EVENT_ID_BYTES,
        reject_secrets=False,
    )
    normalized_predecessor = bounded_conductor_text(
        "predecessor_execution_id",
        predecessor_execution_id,
        max_bytes=MAX_EVENT_ID_BYTES,
        reject_secrets=False,
    )
    return BaseEvent(
        type="conductor.directive.attached",
        aggregate_type=normalized_target_type,
        aggregate_id=normalized_target_id,
        data={
            "schema_version": CONDUCTOR_SCHEMA_VERSION,
            "decision_id": normalized_decision_id,
            "target_type": normalized_target_type,
            "target_id": normalized_target_id,
            "predecessor_execution_id": normalized_predecessor,
            "source_attention_event_id": directive.source_attention_event_id,
            "conductor_directive": directive.to_event_data(),
            "conductor_directive_digest": directive.digest,
        },
    )


__all__ = [
    "MAX_EVIDENCE_EVENT_IDS",
    "create_conductor_decision_selected_event",
    "create_conductor_decision_terminal_event",
    "create_conductor_directive_attached_event",
]
