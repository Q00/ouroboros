"""Closed, privacy-safe cause vocabulary for a failed ``run`` (execute_seed).

A run's terminal event carries prose (``Parallel Execution Complete ...``)
and the job's ``result_meta`` carried only ``status="failed"``, so every run
failure reached telemetry as ``failure_reason_code=unknown``.  This module
derives one machine-readable cause from the durable evidence the executor
already writes to the event store — never from prose, paths, commands, or
output — so a fleet-wide "why do runs fail" question has an answer.

The vocabulary is closed on purpose.  Extend it here, together with the
telemetry contract in ``TELEMETRY.md``; never pass free text through it.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

from ouroboros.mcp.failure_taxonomy import (
    RUN_FAILURE_CAUSES,
    UNKNOWN_RUN_FAILURE_CAUSE,
    FailureReasonCode,
)
from ouroboros.orchestrator.verify_gate_outcome import _VERIFY_GATE_CAUSES

# Worker judgement classes come from orchestrator/failure_taxonomy.FailureClass
# (upper-case in events); anything outside this audited set folds to
# ``worker_failed`` — an AC judged not done with no retry budget or route
# left and no verify-gate cause to name.
_WORKER_FAILURE_CLASSES: Mapping[str, str] = {
    "EVIDENCE_MISSING": "worker_evidence_missing",
    "FABRICATION_SUSPECTED": "worker_fabrication_suspected",
    "BLOCKED": "worker_blocked",
}
# Every value this module can return must be a member of the closed set the
# telemetry boundary accepts.
assert {f"verify_{cause}" for cause in _VERIFY_GATE_CAUSES} <= RUN_FAILURE_CAUSES
assert set(_WORKER_FAILURE_CLASSES.values()) <= RUN_FAILURE_CAUSES

_REASON_CODE_BY_CAUSE: Mapping[str, FailureReasonCode] = {
    "verify_timeout": FailureReasonCode.TIMEOUT,
    "verify_environment_unverifiable": FailureReasonCode.CONFIG,
    "runtime_error": FailureReasonCode.TOOL,
    "cancelled": FailureReasonCode.CANCELLED,
    "unknown": FailureReasonCode.UNKNOWN,
}


def _data(event: Any) -> Mapping[str, Any]:
    data = getattr(event, "data", None)
    return data if isinstance(data, Mapping) else {}


def _event_type(event: Any) -> str:
    event_type = getattr(event, "type", None)
    return event_type if isinstance(event_type, str) else ""


def _most_common(values: Iterable[str]) -> str | None:
    counted = Counter(values)
    if not counted:
        return None
    # Deterministic tie-break: highest count, then lexical.
    return sorted(counted.items(), key=lambda item: (-item[1], item[0]))[0][0]


def derive_run_failure_cause(
    events: Iterable[Any],
    *,
    session_id: str,
    terminal_status: str | None,
) -> str:
    """Return one ``RUN_FAILURE_CAUSES`` member for a session's failed run.

    ``events`` is the session's durable event stream (any order).  Only events
    whose ``session_id`` matches are considered.  Precedence follows causal
    weight, most decisive first:

    1. ``execution.verify.failed`` at final settlement / coordinator
       revalidation — the verdict that actually failed the run.
    2. ``execution.ac.recovery_exhausted`` — the AC whose retry budget ran out,
       attributed to its last verify-gate cause, else its failure class.
    3. ``orchestrator.session.failed`` with an audited ``error_type`` — the
       orchestrator raised instead of judging (``runtime_error``).
    4. Every judged attempt ``blocked`` — an upstream dependency failed the
       run without anything itself failing (``dependency_blocked``).
    """
    if terminal_status == "cancelled":
        return "cancelled"

    verify_final: list[str] = []
    verify_last_by_ac: dict[int, str] = {}
    exhausted: list[Mapping[str, Any]] = []
    judged_outcomes: list[str] = []
    session_error_type: str | None = None

    for event in events:
        data = _data(event)
        # Execution-aggregate events name the session in ``data``; session-
        # aggregate lifecycle events name it as the aggregate id.
        if (
            data.get("session_id") != session_id
            and getattr(event, "aggregate_id", None) != session_id
        ):
            continue
        event_type = _event_type(event)
        if event_type == "execution.verify.failed":
            cause = data.get("verify_cause")
            if not isinstance(cause, str) or cause not in _VERIFY_GATE_CAUSES:
                continue
            if data.get("final_workspace_revalidation") is True:
                verify_final.append(cause)
            ac_index = data.get("ac_index")
            if isinstance(ac_index, int) and not isinstance(ac_index, bool):
                verify_last_by_ac[ac_index] = cause
        elif event_type == "execution.ac.recovery_exhausted":
            exhausted.append(data)
        elif event_type == "execution.ac.attempt_judged":
            outcome = data.get("outcome")
            if isinstance(outcome, str):
                judged_outcomes.append(outcome)
        elif event_type == "orchestrator.session.failed":
            error_type = data.get("error_type")
            if isinstance(error_type, str) and error_type:
                session_error_type = error_type

    final_cause = _most_common(verify_final)
    if final_cause is not None:
        return f"verify_{final_cause}"

    if exhausted:
        attributed: list[str] = []
        for data in exhausted:
            ac_index = data.get("root_ac_index")
            if isinstance(ac_index, int) and ac_index in verify_last_by_ac:
                attributed.append(f"verify_{verify_last_by_ac[ac_index]}")
                continue
            failure_class = data.get("last_failure_class")
            attributed.append(
                _WORKER_FAILURE_CLASSES.get(
                    failure_class if isinstance(failure_class, str) else "",
                    "worker_failed",
                )
            )
        # An exhausted AC with a concrete verify cause outranks an
        # unattributed worker failure on a sibling.
        verify_attributed = [cause for cause in attributed if cause.startswith("verify_")]
        chosen = _most_common(verify_attributed) or _most_common(attributed)
        if chosen is not None:
            return chosen

    if session_error_type is not None:
        return "runtime_error"

    if judged_outcomes and all(outcome == "blocked" for outcome in judged_outcomes):
        return "dependency_blocked"

    return UNKNOWN_RUN_FAILURE_CAUSE


def failure_reason_code_for_run_cause(cause: str) -> FailureReasonCode:
    """Fold a run cause into the coarse ``failure_reason_code`` vocabulary.

    Verify-gate and worker verdicts are quality failures of the produced work
    (``validation``); only the environmental branches map elsewhere.
    """
    if cause not in RUN_FAILURE_CAUSES:
        return FailureReasonCode.UNKNOWN
    return _REASON_CODE_BY_CAUSE.get(cause, FailureReasonCode.VALIDATION)


__all__ = [
    "RUN_FAILURE_CAUSES",
    "UNKNOWN_RUN_FAILURE_CAUSE",
    "derive_run_failure_cause",
    "failure_reason_code_for_run_cause",
]
