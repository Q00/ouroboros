"""Pure Active Conductor classification over durable linked-job events."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence, Set

from ouroboros.events.base import BaseEvent

ATTENTION_SOURCE_EVENT_TYPES = frozenset(
    {
        "execution.ac.recovery_exhausted",
        "execution.ac.deliver_verdict",
        "execution.frugality_proof.evaluated",
        "auto.seed_qa.blocked",
        "lineage.stagnated",
        "control.session.signal.rejected",
        "control.session.signal.delivery_uncertain",
    }
)

PROACTIVE_SOURCE_EVENT_TYPES = frozenset(
    {
        "execution.run.configuration_resolved",
        "execution.plan.created",
        "execution.ac.phase_changed",
        "execution.ac.discovery.updated",
        "execution.decomposition.level_started",
        "execution.decomposition.level_completed",
        "execution.ac.model_routed",
        "execution.ac.alt_harness_redispatched",
        "execution.ac.attempt_judged",
        "execution.ac.outcome_finalized",
        "execution.ac.acceptance_finalized",
        "control.session.signal.queued",
        "control.session.signal.applied",
        "control.session.signal.completed",
    }
)

RELAY_SOURCE_EVENT_TYPES = ATTENTION_SOURCE_EVENT_TYPES | PROACTIVE_SOURCE_EVENT_TYPES

_MAX_RELAY_EVENTS = 20
_MAX_EVIDENCE_EVENT_IDS = 8
_MAX_TEXT = 320
_MAX_LIST = 8


def _text(value: object, *, limit: int = _MAX_TEXT) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized[:limit] if normalized else None


def _strings(value: object, *, limit: int = _MAX_LIST) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    for item in value:
        normalized = _text(item)
        if normalized and normalized not in result:
            result.append(normalized)
        if len(result) >= limit:
            break
    return result


def _scope(event: BaseEvent, *, job_id: str | None) -> dict[str, object]:
    data = event.data
    return {
        "job_id": job_id,
        "execution_id": _text(data.get("execution_id"), limit=96),
        "session_id": _text(data.get("session_id"), limit=96),
        "lineage_id": _text(data.get("lineage_id"), limit=96),
        "semantic_ac_key": _text(data.get("semantic_ac_key"), limit=96),
        "root_ac_index": (
            data.get("root_ac_index")
            if isinstance(data.get("root_ac_index"), int)
            and not isinstance(data.get("root_ac_index"), bool)
            else None
        ),
    }


def _host_verify_action(event_ids: Sequence[str]) -> dict[str, object]:
    return {
        "kind": "host_verify",
        "action": "spawn_read_only_verifier",
        "arguments": {
            "evidence_event_ids": list(event_ids[:_MAX_EVIDENCE_EVENT_IDS]),
            "workspace_policy": "read_only",
        },
        "effect": "read_only",
        "rationale": "Confirm the durable evidence against current repository state.",
    }


def _user_action(action: str, *, effect: str, rationale: str) -> dict[str, object]:
    return {
        "kind": "user_escalation",
        "action": action,
        "arguments": {},
        "effect": effect,
        "rationale": rationale,
    }


def _successor_action(tool: str, *, lineage: bool = False) -> dict[str, object]:
    required_host_inputs = [
        "conductor_decision_id",
        "predecessor_execution_id",
        "conductor_directive",
    ]
    if lineage:
        required_host_inputs.insert(0, "lineage_id")
    else:
        required_host_inputs.insert(0, "seed_content")
    return {
        "kind": "mcp_tool",
        "tool": tool,
        "arguments": ({"max_generations": 1} if lineage else {}),
        "required_host_inputs": required_host_inputs,
        "decision_audit": {
            "tool": "ouroboros_record_conductor_decision",
            "record_before": "selected",
            "record_after": ["completed", "failed", "declined"],
        },
        "effect": "successor_only",
        "rationale": (
            "Start one bounded successor generation with a deterministic non-relaxing directive."
            if lineage
            else "Start a new bounded execution; do not mutate or redispatch the closed predecessor."
        ),
    }


def _attention(
    event: BaseEvent,
    *,
    trigger: str,
    job_id: str | None,
    ownership_state: str,
    evidence: Mapping[str, object],
    evidence_event_ids: Sequence[str],
    actions: Sequence[Mapping[str, object]] | None = None,
    available_tools: Set[str] = frozenset(),
    successor_tool: str | None = "ouroboros_start_execute_seed",
    lineage_successor: bool = False,
) -> dict[str, object]:
    ids = list(dict.fromkeys(evidence_event_ids))[:_MAX_EVIDENCE_EVENT_IDS]
    menu = (
        list(actions)
        if actions is not None
        else (
            [_host_verify_action(ids)]
            + (
                [_successor_action(successor_tool, lineage=lineage_successor)]
                if ownership_state == "closed"
                and successor_tool is not None
                and successor_tool in available_tools
                and "ouroboros_record_conductor_decision" in available_tools
                else []
            )
            + [
                _user_action(
                    "review_successor_options",
                    effect="read_only",
                    rationale="Keep the approved contract unchanged until a successor action is authorized.",
                )
            ]
        )
    )
    return {
        "id": f"attention_{trigger}_{event.id}",
        "kind": "attention_required",
        "subtype": "conductor_attention",
        "trigger": trigger,
        "source_event_id": event.id,
        "scope": _scope(event, job_id=job_id),
        "engine_ownership": {
            "state": ownership_state,
            "evidence_event_ids": ids,
        },
        "evidence": dict(evidence),
        "recommended_host_actions": menu,
    }


def _progress(
    event: BaseEvent,
    *,
    kind: str,
    subtype: str,
    job_id: str | None,
    evidence: Mapping[str, object],
) -> dict[str, object]:
    return {
        "id": f"relay_{subtype}_{event.id}",
        "kind": kind,
        "subtype": subtype,
        "source_event_id": event.id,
        "scope": _scope(event, job_id=job_id),
        "evidence": dict(evidence),
    }


def _event_order(events: Iterable[BaseEvent]) -> list[BaseEvent]:
    return sorted(events, key=lambda event: (event.timestamp, event.id))


def _latest_configuration_before(
    history: Sequence[BaseEvent],
    target: BaseEvent,
) -> BaseEvent | None:
    latest: BaseEvent | None = None
    for event in history:
        if (event.timestamp, event.id) > (target.timestamp, target.id):
            break
        if event.type == "execution.run.configuration_resolved":
            latest = event
    return latest


def _proactive_relays(
    history: Sequence[BaseEvent],
    new_event_ids: set[str],
    *,
    job_id: str | None,
) -> list[dict[str, object]]:
    relays: list[dict[str, object]] = []
    last_route_by_ac: dict[str, tuple[object, ...]] = {}
    for event in history:
        data = event.data
        if event.type == "execution.ac.model_routed":
            route_key = str(data.get("semantic_ac_key") or data.get("ac_id") or event.aggregate_id)
            signature = (
                data.get("model_tier"),
                data.get("model"),
                data.get("model_mode"),
                data.get("runtime_backend"),
                data.get("retry_attempt"),
            )
            changed = last_route_by_ac.get(route_key) != signature
            last_route_by_ac[route_key] = signature
            if event.id not in new_event_ids or not changed:
                continue
            relays.append(
                _progress(
                    event,
                    kind="progress_advanced",
                    subtype="ac_routing",
                    job_id=job_id,
                    evidence={
                        "model_tier": data.get("model_tier"),
                        "model": _text(data.get("model"), limit=120),
                        "model_mode": data.get("model_mode"),
                        "runtime_backend": data.get("runtime_backend"),
                        "retry_attempt": data.get("retry_attempt"),
                        "model_escalated": data.get("model_escalated") is True,
                    },
                )
            )
            continue
        if event.id not in new_event_ids:
            continue
        if event.type == "execution.run.configuration_resolved":
            relays.append(
                _progress(
                    event,
                    kind="progress_advanced",
                    subtype="run_configuration",
                    job_id=job_id,
                    evidence={
                        key: data.get(key)
                        for key in (
                            "efficiency_mode",
                            "frugality_assurance",
                            "primary_runtime_backend",
                            "primary_harness_label",
                            "model_routing_enabled",
                            "starting_model_tier",
                            "starting_model",
                            "progressive_escalation_enabled",
                            "alternate_harness_enabled",
                        )
                    },
                )
            )
        elif event.type == "execution.plan.created":
            levels = data.get("levels") if isinstance(data.get("levels"), list) else []
            first_indices = data.get("first_ac_indices")
            relays.append(
                _progress(
                    event,
                    kind="progress_advanced",
                    subtype="execution_plan",
                    job_id=job_id,
                    evidence={
                        "total_acs": data.get("total_acs"),
                        "total_levels": data.get("total_levels"),
                        "parallelizable": data.get("parallelizable"),
                        "first_level": data.get("first_level"),
                        "first_ac_indices": (
                            list(first_indices)[:_MAX_LIST]
                            if isinstance(first_indices, list)
                            else []
                        ),
                        "first_ac_summaries": (
                            _strings(levels[0].get("ac_summaries"))
                            if levels and isinstance(levels[0], dict)
                            else []
                        ),
                    },
                )
            )
        elif event.type == "execution.ac.phase_changed":
            relays.append(
                _progress(
                    event,
                    kind="phase_changed",
                    subtype="ac_phase",
                    job_id=job_id,
                    evidence={"phase": data.get("phase"), "source": data.get("source")},
                )
            )
        elif event.type == "execution.ac.discovery.updated":
            relays.append(
                _progress(
                    event,
                    kind="progress_advanced",
                    subtype="discovery_summary",
                    job_id=job_id,
                    evidence={
                        "targets": _strings(data.get("targets"), limit=5),
                        "purpose": _text(data.get("purpose"), limit=240),
                        "source": data.get("source"),
                    },
                )
            )
        elif event.type in {
            "execution.decomposition.level_started",
            "execution.decomposition.level_completed",
        }:
            relays.append(
                _progress(
                    event,
                    kind="progress_advanced",
                    subtype=(
                        "level_started"
                        if event.type.endswith("level_started")
                        else "level_completed"
                    ),
                    job_id=job_id,
                    evidence={
                        key: data.get(key)
                        for key in (
                            "level",
                            "total_levels",
                            "child_indices",
                            "successful",
                            "failed",
                            "blocked",
                            "outcome",
                        )
                    },
                )
            )
        elif event.type == "execution.ac.alt_harness_redispatched":
            relays.append(
                _progress(
                    event,
                    kind="progress_advanced",
                    subtype="harness_changed",
                    job_id=job_id,
                    evidence={
                        "from_backend": data.get("from_backend"),
                        "to_backend": data.get("to_backend"),
                        "failure_class": data.get("failure_class"),
                        "reason": _text(data.get("reason")),
                    },
                )
            )
        elif (
            event.type
            in {
                "execution.ac.attempt_judged",
                "execution.ac.outcome_finalized",
            }
            and data.get("success") is True
        ):
            relays.append(
                _progress(
                    event,
                    kind="progress_advanced",
                    subtype="ac_attempt_judged",
                    job_id=job_id,
                    evidence={
                        "root_ac_index": data.get("root_ac_index"),
                        "retry_attempt": data.get("retry_attempt"),
                        "attempt_number": data.get("attempt_number"),
                        "outcome": data.get("outcome"),
                    },
                )
            )
        elif event.type == "execution.ac.acceptance_finalized":
            relays.append(
                _progress(
                    event,
                    kind="progress_advanced",
                    subtype=("ac_accepted" if data.get("accepted") is True else "ac_rejected"),
                    job_id=job_id,
                    evidence={
                        "root_ac_index": data.get("root_ac_index"),
                        "final_retry_attempt": data.get("final_retry_attempt"),
                        "accepted": data.get("accepted"),
                        "disposition": data.get("disposition"),
                        "terminal_status": data.get("terminal_status"),
                    },
                )
            )
        elif event.type.startswith("control.session.signal.") and event.type not in {
            "control.session.signal.rejected",
            "control.session.signal.delivery_uncertain",
        }:
            relays.append(
                _progress(
                    event,
                    kind="progress_advanced",
                    subtype="synapse_delivery",
                    job_id=job_id,
                    evidence={
                        "state": event.type.rsplit(".", 1)[-1],
                        "requested_mode": data.get("requested_mode"),
                        "effective_mode": data.get("effective_mode"),
                        "application_proven": event.type
                        in {"control.session.signal.applied", "control.session.signal.completed"},
                        "summary": _text(data.get("summary")),
                        "reply": _text(data.get("reply")),
                    },
                )
            )
    return relays


def classify_relay_events(
    history_events: Sequence[BaseEvent],
    *,
    new_event_ids: set[str] | None = None,
    job_id: str | None = None,
    available_tools: Set[str] | None = None,
) -> list[dict[str, object]]:
    """Classify bounded proactive and attention relay envelopes.

    ``history_events`` provides context for streaks and closure checks.
    ``new_event_ids`` limits emission to the current cursor page; omitting it
    performs the terminal full-history scan.
    """
    history = _event_order(history_events)
    registered = available_tools or frozenset()
    new_ids = new_event_ids if new_event_ids is not None else {event.id for event in history}
    relays = _proactive_relays(history, new_ids, job_id=job_id)

    recovery_by_key: dict[str, BaseEvent] = {}
    for event in history:
        if event.type == "execution.ac.recovery_exhausted":
            key = _text(event.data.get("semantic_ac_key"), limit=96)
            if key:
                recovery_by_key[key] = event
            if event.id not in new_ids:
                continue
            evidence = {
                key_name: event.data.get(key_name)
                for key_name in (
                    "retry_termination_reason",
                    "alternate_redispatch_status",
                    "last_failure_class",
                    "retry_attempt",
                    "configured_retry_attempts",
                )
            }
            relays.append(
                _attention(
                    event,
                    trigger="ac_recovery_exhausted",
                    job_id=job_id,
                    ownership_state="closed",
                    evidence=evidence,
                    evidence_event_ids=[event.id],
                    available_tools=registered,
                )
            )

            semantic_key = _text(event.data.get("semantic_ac_key"), limit=96)
            latest_route = next(
                (
                    candidate
                    for candidate in reversed(history)
                    if candidate.type == "execution.ac.model_routed"
                    and candidate.data.get("semantic_ac_key") == semantic_key
                    and (candidate.timestamp, candidate.id) <= (event.timestamp, event.id)
                ),
                None,
            )
            if latest_route is not None and latest_route.data.get("model_escalated") is True:
                relays.append(
                    _attention(
                        event,
                        trigger="model_escalation_failed",
                        job_id=job_id,
                        ownership_state="closed",
                        evidence={
                            "model_tier": latest_route.data.get("model_tier"),
                            "model": _text(latest_route.data.get("model"), limit=120),
                            "retry_attempt": latest_route.data.get("retry_attempt"),
                            "last_failure_class": event.data.get("last_failure_class"),
                        },
                        evidence_event_ids=[latest_route.id, event.id],
                        available_tools=registered,
                    )
                )

    rejected_by_group: dict[tuple[str, str], list[BaseEvent]] = defaultdict(list)
    for event in history:
        if event.type != "execution.ac.deliver_verdict":
            continue
        semantic_key = _text(event.data.get("semantic_ac_key"), limit=96)
        if not semantic_key:
            continue
        judgment_scope = (
            _text(event.data.get("lineage_id"), limit=96)
            or _text(event.data.get("root_job_id"), limit=96)
            or job_id
            or "unknown"
        )
        group = (judgment_scope, semantic_key)
        if event.data.get("traceguard_verdict") != "rejected":
            rejected_by_group[group].clear()
            continue
        rejected_by_group[group].append(event)
        streak = rejected_by_group[group]
        if len(streak) < 2 or event.id not in new_ids:
            continue
        closed_event = recovery_by_key.get(semantic_key)
        evidence_ids = [item.id for item in streak[-_MAX_EVIDENCE_EVENT_IDS:]]
        if closed_event is not None:
            evidence_ids.append(closed_event.id)
        reasons: list[str] = []
        for rejected in streak[-2:]:
            for reason in _strings(rejected.data.get("rejected_reasons"), limit=5):
                if reason not in reasons:
                    reasons.append(reason)
        actions: list[Mapping[str, object]] = [_host_verify_action(evidence_ids)]
        if closed_event is None:
            actions.append(
                _user_action(
                    "defer_until_engine_recovery_closes",
                    effect="read_only",
                    rationale="The engine still owns retry and routing recovery for this AC.",
                )
            )
        else:
            if {
                "ouroboros_record_conductor_decision",
                "ouroboros_start_execute_seed",
            }.issubset(registered):
                actions.append(_successor_action("ouroboros_start_execute_seed"))
            actions.append(
                _user_action(
                    "review_corrective_successor",
                    effect="successor_only",
                    rationale="A corrective successor may be considered without weakening the AC.",
                )
            )
        relays.append(
            _attention(
                event,
                trigger="deliver_verdict_rejected_streak",
                job_id=job_id,
                ownership_state="closed" if closed_event is not None else "active",
                evidence={
                    "judgment_scope_id": judgment_scope,
                    "rejected_count": len(streak),
                    "rejected_reasons": reasons[:_MAX_LIST],
                },
                evidence_event_ids=evidence_ids,
                actions=actions,
            )
        )

    for event in history:
        if event.id not in new_ids:
            continue
        data = event.data
        if event.type == "execution.frugality_proof.evaluated":
            configuration = _latest_configuration_before(history, event)
            assurance = configuration.data.get("frugality_assurance") if configuration else None
            if assurance == "off":
                continue
            status = data.get("status")
            trigger = {
                "fail_grounding_regression": "frugality_grounding_regression",
                "fail_no_frugality": "frugality_no_savings",
            }.get(status)
            if trigger is None:
                continue
            relays.append(
                _attention(
                    event,
                    trigger=trigger,
                    job_id=job_id,
                    ownership_state="closed",
                    evidence={
                        "status": status,
                        "reason": _text(data.get("reason")),
                        "token_reduction_pct": data.get("token_reduction_pct"),
                        "grounding_regressions": data.get("grounding_regressions"),
                        "frugality_assurance": assurance,
                    },
                    evidence_event_ids=[event.id],
                    available_tools=registered,
                )
            )
        elif event.type == "auto.seed_qa.blocked":
            relays.append(
                _attention(
                    event,
                    trigger="seed_qa_blocked",
                    job_id=job_id,
                    ownership_state="closed",
                    evidence={
                        "attempts": data.get("attempts"),
                        "verdict": _text(data.get("verdict"), limit=80),
                        "score": data.get("score"),
                        "differences": _strings(data.get("differences"), limit=5),
                        "suggestions": _strings(data.get("suggestions"), limit=5),
                        "reason": data.get("reason"),
                    },
                    evidence_event_ids=[event.id],
                    available_tools=registered,
                )
            )
        elif event.type == "lineage.stagnated":
            relays.append(
                _attention(
                    event,
                    trigger="lineage_stagnated",
                    job_id=job_id,
                    ownership_state="closed",
                    evidence={
                        "reason": _text(data.get("reason")),
                        "generation": data.get("generation"),
                        "best_score": data.get("best_score"),
                    },
                    evidence_event_ids=[event.id],
                    available_tools=registered,
                    successor_tool="ouroboros_start_ralph",
                    lineage_successor=True,
                )
            )
        elif event.type in {
            "control.session.signal.rejected",
            "control.session.signal.delivery_uncertain",
        }:
            state = event.type.rsplit(".", 1)[-1]
            relays.append(
                _attention(
                    event,
                    trigger=f"session_signal_{state}",
                    job_id=job_id,
                    ownership_state="closed",
                    evidence={
                        "state": state,
                        "requested_mode": data.get("requested_mode"),
                        "effective_mode": data.get("effective_mode"),
                        "rejection_code": data.get("rejection_code"),
                        "detail": _text(data.get("detail")),
                    },
                    evidence_event_ids=[event.id],
                    actions=[
                        _host_verify_action([event.id]),
                        _user_action(
                            "review_synapse_delivery",
                            effect="read_only",
                            rationale="Inspect the exact target and choose a new explicit delivery action.",
                        ),
                    ],
                )
            )

    deduped: list[dict[str, object]] = []
    seen: set[str] = set()
    for relay in relays:
        relay_id = str(relay["id"])
        if relay_id in seen:
            continue
        seen.add(relay_id)
        deduped.append(relay)
        if len(deduped) >= _MAX_RELAY_EVENTS:
            break
    return deduped


__all__ = [
    "ATTENTION_SOURCE_EVENT_TYPES",
    "PROACTIVE_SOURCE_EVENT_TYPES",
    "RELAY_SOURCE_EVENT_TYPES",
    "classify_relay_events",
]
