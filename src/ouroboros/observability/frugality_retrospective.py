"""Neutral execution-finalized frugality evidence reporting.

The v1 retrospective is deliberately evidence-only. It summarizes runtime token
measurements associated with retries and latest unsuccessful AC outcomes, but it
does not label either signal avoidable, non-contributory, or non-advancing spend.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ouroboros.orchestrator.events import (
    FRUGALITY_RETROSPECTIVE_EVENT_TYPE,
    create_frugality_retrospective_event,
)
from ouroboros.orchestrator.evidence.common import (
    event_data,
    event_id,
    event_type,
    finite_number,
    parse_retry_attempt,
    parse_root_ac_index,
    strict_bool,
)
from ouroboros.orchestrator.frugality_proof import (
    EVENT_AC_OUTCOME_FINALIZED,
    EVENT_DELIVER_VERDICT,
    EVENT_EFFORT_ROUTED,
    EVENT_MODEL_ROUTED,
    EVENT_SHADOW_REPLAY,
    EVENT_TOKEN_ATTRIBUTION,
)

if TYPE_CHECKING:
    from ouroboros.persistence.event_store import EventStore

RETROSPECTIVE_VERSION = "v1"
RETROSPECTIVE_TRIGGER = "execution_finalized"
HARD_FINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
RETRY_ASSOCIATED_SPEND = "retry_associated_spend"
UNACCEPTED_SPEND = "unaccepted_spend"
_PROOF_EVENT_TYPE = "execution.frugality_proof.evaluated"
_ATTEMPT_EVENT_TYPES = frozenset(
    {
        "execution.session.started",
        "execution.session.resumed",
        EVENT_EFFORT_ROUTED,
        EVENT_MODEL_ROUTED,
        EVENT_TOKEN_ATTRIBUTION,
        EVENT_DELIVER_VERDICT,
        EVENT_SHADOW_REPLAY,
    }
)


@dataclass(frozen=True, slots=True)
class _AttemptKey:
    unit_id: str
    retry_attempt: int


@dataclass(slots=True)
class _AttemptEvidence:
    root_ac_index: int | None = None
    token_events: list[tuple[str | None, object]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _OutcomeEvidence:
    root_ac_index: int
    retry_attempt: int
    success: bool
    outcome: str
    event_id: str | None


def _non_empty_string(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _unit_id(data: Mapping[str, object]) -> str | None:
    return _non_empty_string(data.get("ac_id")) or _non_empty_string(data.get("node_id"))


def _non_negative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _normalized_outcome(data: Mapping[str, object], *, success: bool) -> str | None:
    raw = data.get("outcome")
    if raw is None:
        return "succeeded" if success else "failed"
    return _non_empty_string(raw)


def _attempt_key(data: Mapping[str, object]) -> _AttemptKey | None:
    unit_id = _unit_id(data)
    retry_attempt = parse_retry_attempt(data)
    if unit_id is None or retry_attempt is None:
        return None
    return _AttemptKey(unit_id=unit_id, retry_attempt=retry_attempt)


def _evidence_ids(values: Iterable[str | None]) -> list[str]:
    return sorted({value for value in values if value})


def _proof_reference(events: Iterable[object]) -> str | None:
    for event in events:
        if event_type(event) == _PROOF_EVENT_TYPE:
            reference = event_id(event)
            if reference is not None:
                return reference
    return None


def build_frugality_retrospective(
    events: Iterable[object],
    *,
    execution_id: str,
    session_id: str,
    terminal_status: str,
) -> dict[str, Any] | None:
    """Build the v1 evidence payload for one hard-finalized execution."""
    if terminal_status not in HARD_FINAL_STATUSES:
        return None

    event_list = list(events)
    attempts: dict[_AttemptKey, _AttemptEvidence] = {}
    invalid_attempt_keys: set[_AttemptKey] = set()
    anonymous_invalid_attempts: set[str] = set()
    outcomes_by_root: dict[int, dict[int, list[_OutcomeEvidence]]] = {}
    outcome_attempts_seen: dict[int, set[int]] = {}
    invalid_outcome_attempts: set[tuple[int, int]] = set()
    invalid_outcome_roots: set[int] = set()

    for position, event in enumerate(event_list):
        current_type = event_type(event)
        data = event_data(event)
        current_id = event_id(event)
        invalid_identity = current_id or f"event-{position}"

        if current_type in _ATTEMPT_EVENT_TYPES:
            key = _attempt_key(data)
            if key is None:
                if current_type == EVENT_TOKEN_ATTRIBUTION or _unit_id(data) is not None:
                    anonymous_invalid_attempts.add(invalid_identity)
                continue
            attempt = attempts.setdefault(key, _AttemptEvidence())
            root_ac_index = parse_root_ac_index(data)
            if root_ac_index is not None:
                if attempt.root_ac_index is None:
                    attempt.root_ac_index = root_ac_index
                elif attempt.root_ac_index != root_ac_index:
                    invalid_attempt_keys.add(key)
            if current_type == EVENT_TOKEN_ATTRIBUTION:
                attempt.token_events.append((current_id, data.get("token_spend")))
            continue

        if current_type != EVENT_AC_OUTCOME_FINALIZED:
            continue

        root_ac_index = parse_root_ac_index(data)
        retry_attempt = parse_retry_attempt(data)
        success = strict_bool(data.get("success"))
        outcome = _normalized_outcome(data, success=success) if success is not None else None
        if root_ac_index is None:
            anonymous_invalid_attempts.add(invalid_identity)
            continue
        if retry_attempt is None:
            invalid_outcome_roots.add(root_ac_index)
            anonymous_invalid_attempts.add(invalid_identity)
            continue
        outcome_attempts_seen.setdefault(root_ac_index, set()).add(retry_attempt)
        if success is None or outcome is None:
            invalid_outcome_attempts.add((root_ac_index, retry_attempt))
            continue
        outcomes_by_root.setdefault(root_ac_index, {}).setdefault(retry_attempt, []).append(
            _OutcomeEvidence(
                root_ac_index=root_ac_index,
                retry_attempt=retry_attempt,
                success=success,
                outcome=outcome,
                event_id=current_id,
            )
        )

    measured: dict[_AttemptKey, tuple[float, str | None]] = {}
    unknown_attempt_keys: set[_AttemptKey] = set()
    for key, attempt in attempts.items():
        if key in invalid_attempt_keys:
            continue
        if not attempt.token_events:
            unknown_attempt_keys.add(key)
            continue
        if len(attempt.token_events) > 1:
            invalid_attempt_keys.add(key)
            continue
        token_event_id, raw_spend = attempt.token_events[0]
        if raw_spend is None:
            unknown_attempt_keys.add(key)
            continue
        spend = finite_number(raw_spend)
        if spend is None or spend < 0:
            invalid_attempt_keys.add(key)
            continue
        measured[key] = (spend, token_event_id)

    latest_outcomes: dict[int, _OutcomeEvidence] = {}
    for root_ac_index, attempts_seen in outcome_attempts_seen.items():
        latest_attempt = max(attempts_seen)
        records = outcomes_by_root.get(root_ac_index, {}).get(latest_attempt, [])
        if (
            root_ac_index in invalid_outcome_roots
            or (root_ac_index, latest_attempt) in invalid_outcome_attempts
            or len(records) != 1
        ):
            invalid_outcome_attempts.add((root_ac_index, latest_attempt))
            continue
        latest_outcomes[root_ac_index] = records[0]

    retry_keys: set[_AttemptKey] = set()
    retry_latest_attempts: list[int] = []
    for unit_id in {key.unit_id for key in attempts}:
        unit_attempts = [key for key in attempts if key.unit_id == unit_id]
        latest_attempt = max(key.retry_attempt for key in unit_attempts)
        contributing = {
            key for key in unit_attempts if key.retry_attempt < latest_attempt and key in measured
        }
        if contributing:
            retry_latest_attempts.append(latest_attempt)
            retry_keys.update(contributing)

    retry_token_spend = sum(measured[key][0] for key in retry_keys)
    retry_event_ids = _evidence_ids(measured[key][1] for key in retry_keys)

    unaccepted_roots = {
        root_ac_index: outcome
        for root_ac_index, outcome in latest_outcomes.items()
        if not outcome.success
    }
    unaccepted_keys = {
        key
        for key, attempt in attempts.items()
        if key in measured
        and attempt.root_ac_index is not None
        and attempt.root_ac_index in unaccepted_roots
    }
    unaccepted_token_spend = sum(measured[key][0] for key in unaccepted_keys)
    unaccepted_event_ids = _evidence_ids(
        [
            *(measured[key][1] for key in unaccepted_keys),
            *(outcome.event_id for outcome in unaccepted_roots.values()),
        ]
    )
    outcome_labels = {outcome.outcome for outcome in unaccepted_roots.values()}
    latest_outcome = (
        next(iter(outcome_labels))
        if len(outcome_labels) == 1
        else ("mixed" if outcome_labels else None)
    )

    signals: list[dict[str, Any]] = []
    if retry_keys:
        signals.append(
            {
                "name": RETRY_ASSOCIATED_SPEND,
                "token_spend": retry_token_spend,
                "attempt_count": len(retry_keys),
                "latest_attempt_index": max(retry_latest_attempts),
                "evidence_event_ids": retry_event_ids,
            }
        )
    if unaccepted_keys:
        signals.append(
            {
                "name": UNACCEPTED_SPEND,
                "token_spend": unaccepted_token_spend,
                "attempt_count": len(unaccepted_keys),
                "latest_outcome": latest_outcome,
                "evidence_event_ids": unaccepted_event_ids,
            }
        )

    invalid_count = (
        len(invalid_attempt_keys) + len(anonymous_invalid_attempts) + len(invalid_outcome_attempts)
    )
    payload: dict[str, Any] = {
        "execution_id": execution_id,
        "session_id": session_id,
        "retrospective_version": RETROSPECTIVE_VERSION,
        "trigger": RETROSPECTIVE_TRIGGER,
        "terminal_status": terminal_status,
        "evidence_only": True,
        "coverage": {
            "measured_attempts": len(measured),
            "unknown_attempts": len(unknown_attempt_keys),
            "invalid_attempts": invalid_count,
            "total_measured_tokens": sum(
                (spend for spend, _event_id in measured.values()),
                0.0,
            ),
        },
        "evidence_signals": signals,
    }
    proof_reference = _proof_reference(event_list)
    if proof_reference is not None:
        payload["proof_reference"] = proof_reference
    return payload


async def report_frugality_retrospective(
    event_store: EventStore,
    *,
    execution_id: str,
    session_id: str,
    terminal_status: str,
) -> bool:
    """Persist the report once for a hard-finalized execution.

    ``paused`` returns before touching the EventStore, so it neither emits nor
    consumes execution-scoped deduplication. The deterministic event id protects
    against a concurrent duplicate append after the replay-based check.
    """
    if terminal_status not in HARD_FINAL_STATUSES:
        return False
    events = await event_store.query_execution_related_events(execution_id, limit=None)
    if any(event_type(event) == FRUGALITY_RETROSPECTIVE_EVENT_TYPE for event in events):
        return False
    payload = build_frugality_retrospective(
        events,
        execution_id=execution_id,
        session_id=session_id,
        terminal_status=terminal_status,
    )
    if payload is None:
        return False
    await event_store.append(create_frugality_retrospective_event(execution_id, payload))
    return True


def project_frugality_retrospective(payload: Mapping[str, object]) -> dict[str, Any] | None:
    """Validate and normalize the v1 payload for web/TUI read models."""
    if (
        payload.get("retrospective_version") != RETROSPECTIVE_VERSION
        or payload.get("trigger") != RETROSPECTIVE_TRIGGER
        or payload.get("terminal_status") not in HARD_FINAL_STATUSES
        or strict_bool(payload.get("evidence_only")) is not True
    ):
        return None
    coverage = payload.get("coverage")
    signals = payload.get("evidence_signals")
    if not isinstance(coverage, Mapping) or not isinstance(signals, (list, tuple)):
        return None
    measured_attempts = _non_negative_int(coverage.get("measured_attempts"))
    unknown_attempts = _non_negative_int(coverage.get("unknown_attempts"))
    invalid_attempts = _non_negative_int(coverage.get("invalid_attempts"))
    total_measured_tokens = finite_number(coverage.get("total_measured_tokens"))
    if (
        measured_attempts is None
        or unknown_attempts is None
        or invalid_attempts is None
        or total_measured_tokens is None
        or total_measured_tokens < 0
    ):
        return None

    projected_signals: dict[str, dict[str, Any]] = {}
    for signal in signals:
        if not isinstance(signal, Mapping):
            return None
        name = signal.get("name")
        if name not in {RETRY_ASSOCIATED_SPEND, UNACCEPTED_SPEND}:
            return None
        normalized_name = str(name)
        if normalized_name in projected_signals:
            return None
        token_spend = finite_number(signal.get("token_spend"))
        attempt_count = _non_negative_int(signal.get("attempt_count"))
        if token_spend is None or token_spend < 0 or attempt_count is None:
            return None
        projected_signals[normalized_name] = {
            "token_spend": token_spend,
            "attempt_count": attempt_count,
        }

    retry = projected_signals.get(RETRY_ASSOCIATED_SPEND, {})
    unaccepted = projected_signals.get(UNACCEPTED_SPEND, {})
    return {
        "terminal_status": str(payload["terminal_status"]),
        "measured_attempts": measured_attempts,
        "unknown_attempts": unknown_attempts,
        "invalid_attempts": invalid_attempts,
        "total_measured_tokens": total_measured_tokens,
        "retry_associated_tokens": retry.get("token_spend", 0.0),
        "retry_associated_attempts": retry.get("attempt_count", 0),
        "unaccepted_tokens": unaccepted.get("token_spend", 0.0),
        "unaccepted_attempts": unaccepted.get("attempt_count", 0),
    }


__all__ = [
    "HARD_FINAL_STATUSES",
    "RETROSPECTIVE_TRIGGER",
    "RETROSPECTIVE_VERSION",
    "RETRY_ASSOCIATED_SPEND",
    "UNACCEPTED_SPEND",
    "build_frugality_retrospective",
    "project_frugality_retrospective",
    "report_frugality_retrospective",
]
