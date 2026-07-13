"""Neutral execution frugality summary from EventStore evidence.

This is intentionally narrower than the deterministic frugality proof. It makes
no claim about avoidable waste, no USD-cost claim, and emits no guardrail. It
only summarizes runtime token-attribution events against strict retry/outcome
markers so final execution records can expose evidence useful for later review.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from ouroboros.orchestrator.frugality_proof import (
    EVENT_AC_OUTCOME_FINALIZED,
    EVENT_TOKEN_ATTRIBUTION,
    _event_data,
    _event_type,
    _finite_number,
    _strict_bool,
    strict_retry_attempt,
    strict_root_ac_index,
)

EVENT_FRUGALITY_PROOF_EVALUATED = "execution.frugality_proof.evaluated"
EVENT_FRUGALITY_RETROSPECTIVE_REPORTED = "execution.frugality_retrospective.reported"


@dataclass(frozen=True)
class FrugalityRetrospectiveSummary:
    """Evidence-only token-spend summary for one execution."""

    execution_id: str
    retry_associated_spend: float
    unaccepted_spend: float
    measured_attempts: int
    unknown_attempts: int
    invalid_attempts: int
    frugality_proof_event_id: str | None = None
    frugality_proof_status: str | None = None

    def to_event_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "execution_id": self.execution_id,
            "retry_associated_spend": self.retry_associated_spend,
            "unaccepted_spend": self.unaccepted_spend,
            "coverage": {
                "measured_attempts": self.measured_attempts,
                "unknown_attempts": self.unknown_attempts,
                "invalid_attempts": self.invalid_attempts,
            },
        }
        if self.frugality_proof_event_id or self.frugality_proof_status:
            data["frugality_proof"] = {
                "event_id": self.frugality_proof_event_id,
                "status": self.frugality_proof_status,
            }
        return data


def _run_anchor(data: Mapping[str, Any]) -> str | None:
    run = data.get("seed_run_id") or data.get("execution_id")
    return str(run) if run is not None else None


def _event_id(event: object) -> str | None:
    if isinstance(event, Mapping):
        value = event.get("id") or event.get("event_id")
    else:
        value = getattr(event, "id", None) or getattr(event, "event_id", None)
    return str(value) if value else None


def summarize_frugality_retrospective(
    events: Iterable[object],
    *,
    execution_id: str,
) -> FrugalityRetrospectiveSummary:
    """Summarize retry-associated and unaccepted token spend.

    Coverage counts are attempt-observation counts:
    * measured: a valid token attribution has one unambiguous outcome marker for
      the same root AC and retry attempt.
    * unknown: token spend is valid, but no matching outcome marker exists.
    * invalid: token spend identity/spend or the matching outcome marker is
      malformed or ambiguous.
    """

    outcome_by_attempt: dict[tuple[str | None, int, int], list[bool | None]] = defaultdict(list)
    invalid_outcomes: set[tuple[str | None, int, int]] = set()
    token_spend_by_attempt: dict[tuple[str | None, str, int], list[float]] = defaultdict(list)
    token_root_by_attempt: dict[tuple[str | None, str, int], int] = {}
    invalid_token_attempts = 0
    latest_proof_event_id: str | None = None
    latest_proof_status: str | None = None

    for event in events:
        event_type = _event_type(event)
        data = _event_data(event)
        if event_type == EVENT_AC_OUTCOME_FINALIZED:
            run_key = _run_anchor(data)
            root_index = strict_root_ac_index(data)
            attempt = strict_retry_attempt(data)
            success = _strict_bool(data.get("success"))
            if root_index is None or attempt is None:
                continue
            key = (run_key, root_index, attempt)
            if success is None:
                invalid_outcomes.add(key)
            outcome_by_attempt[key].append(success)
        elif event_type == EVENT_TOKEN_ATTRIBUTION:
            run_key = _run_anchor(data)
            root_index = strict_root_ac_index(data)
            attempt = strict_retry_attempt(data)
            ac_id = data.get("ac_id")
            spend = _finite_number(data.get("token_spend"))
            if (
                root_index is None
                or attempt is None
                or not isinstance(ac_id, str)
                or not ac_id
                or spend is None
                or spend < 0
            ):
                invalid_token_attempts += 1
                continue
            key = (run_key, ac_id, attempt)
            token_spend_by_attempt[key].append(spend)
            token_root_by_attempt[key] = root_index
        elif event_type == EVENT_FRUGALITY_PROOF_EVALUATED:
            latest_proof_event_id = _event_id(event)
            status = data.get("status")
            latest_proof_status = status if isinstance(status, str) and status else None

    retry_associated_spend = 0.0
    unaccepted_spend = 0.0
    measured_attempts = 0
    unknown_attempts = 0
    invalid_attempts = invalid_token_attempts

    for token_key, spends in token_spend_by_attempt.items():
        run_key, _ac_id, attempt = token_key
        root_index = token_root_by_attempt[token_key]
        if len(spends) != 1:
            invalid_attempts += len(spends)
            continue

        spend = spends[0]
        if attempt > 0:
            retry_associated_spend += spend

        outcome_key = (run_key, root_index, attempt)
        outcomes = outcome_by_attempt.get(outcome_key, [])
        if not outcomes:
            unknown_attempts += 1
            continue
        if outcome_key in invalid_outcomes or len(outcomes) != 1 or outcomes[0] is None:
            invalid_attempts += 1
            continue

        measured_attempts += 1
        if outcomes[0] is False:
            unaccepted_spend += spend

    return FrugalityRetrospectiveSummary(
        execution_id=execution_id,
        retry_associated_spend=retry_associated_spend,
        unaccepted_spend=unaccepted_spend,
        measured_attempts=measured_attempts,
        unknown_attempts=unknown_attempts,
        invalid_attempts=invalid_attempts,
        frugality_proof_event_id=latest_proof_event_id,
        frugality_proof_status=latest_proof_status,
    )


__all__ = [
    "EVENT_FRUGALITY_RETROSPECTIVE_REPORTED",
    "FrugalityRetrospectiveSummary",
    "summarize_frugality_retrospective",
]
