"""Closed, privacy-safe cause vocabulary for a failed ``run`` (execute_seed).

A run's terminal event carries prose (``Parallel Execution Complete ...``)
and the job's ``result_meta`` carried only ``status="failed"``, so every run
failure reached telemetry as ``failure_reason_code=unknown``.  This module
derives one machine-readable cause from the durable evidence the executor
already writes to the event store — never from prose, paths, commands, or
output — so a fleet-wide "why do runs fail" question has an answer.

The vocabulary is closed on purpose.  Extend it here, together with the
telemetry contract in ``TELEMETRY.md``; never pass free text through it.
Beside the cause, :func:`derive_run_failure` returns the Seed artifact paths
the verify-gate failures behind it recorded missing, for the local run result
only; they never enter the vocabulary.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from ouroboros.core.seed import expected_artifact_path_error
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
    # Pre-launch rejections: the environment (workspace/config) or the
    # handler's own preparation failed, not the produced work.
    "launch_workspace_unavailable": FailureReasonCode.CONFIG,
    "launch_config_error": FailureReasonCode.CONFIG,
    "launch_prepare_failed": FailureReasonCode.TOOL,
}


@dataclass(frozen=True)
class RunFailure:
    """A failed run's closed cause, and the artifacts the verify gate found missing for it.

    ``cause`` is a ``RUN_FAILURE_CAUSES`` member and the only part telemetry
    may carry. ``missing_artifacts`` are the workspace-relative
    ``expected_artifacts`` paths that the verify-gate failures the cause was
    derived from recorded as missing, in first-seen order, so the run result
    can say which file is absent (#3072).
    """

    cause: str
    missing_artifacts: tuple[str, ...] = ()


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


def _missing_artifacts(data: Mapping[str, Any]) -> tuple[str, ...]:
    """The portable workspace-relative paths a verify-gate failure recorded missing."""
    value = data.get("missing_artifacts")
    if not isinstance(value, list | tuple):
        return ()
    return tuple(
        item
        for item in value
        if isinstance(item, str) and expected_artifact_path_error(item) is None
    )


def _attributed(cause: str, sources: Iterable[tuple[str, tuple[str, ...]]]) -> RunFailure:
    """``cause`` with the missing artifacts of every source attributed to it, deduplicated."""
    missing = (path for source, paths in sources if source == cause for path in paths)
    return RunFailure(cause, tuple(dict.fromkeys(missing)))


def derive_run_failure_cause(
    events: Iterable[Any],
    *,
    session_id: str,
    terminal_status: str | None,
) -> str:
    """Return one ``RUN_FAILURE_CAUSES`` member for a session's failed run.

    The cause of :func:`derive_run_failure`.
    """
    return derive_run_failure(events, session_id=session_id, terminal_status=terminal_status).cause


def derive_run_failure(
    events: Iterable[Any],
    *,
    session_id: str,
    terminal_status: str | None,
) -> RunFailure:
    """Return a session's failed-run cause and the artifacts it names.

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
    5. A criterion whose latest judgment failed on its verify gate with no
       recovery record — one judged once from a shared attempt (#2737), which
       gets no retry of its own — attributed to the verify-gate cause that
       judgment recorded (``verify_cause``).
    """
    if terminal_status == "cancelled":
        return RunFailure("cancelled")

    verify_final: list[tuple[str, tuple[str, ...]]] = []
    verify_last_by_ac: dict[int, tuple[tuple[Any, str], str, tuple[str, ...]]] = {}
    exhausted: list[Mapping[str, Any]] = []
    judged_outcomes: list[str] = []
    # Per root AC: the latest judgment's ordering key, outcome, and verify-gate cause.
    last_judged_by_ac: dict[int, tuple[Any, str, str | None]] = {}
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
            missing = _missing_artifacts(data)
            if data.get("final_workspace_revalidation") is True:
                verify_final.append((cause, missing))
            ac_index = data.get("ac_index")
            if isinstance(ac_index, int) and not isinstance(ac_index, bool):
                timestamp = getattr(event, "timestamp", None)
                event_id = getattr(event, "id", None)
                ordering_key = (
                    timestamp,
                    event_id if isinstance(event_id, str) else "",
                )
                previous = verify_last_by_ac.get(ac_index)
                if (
                    previous is None
                    or timestamp is None
                    or previous[0][0] is None
                    or previous[0] <= ordering_key
                ):
                    verify_last_by_ac[ac_index] = (ordering_key, cause, missing)
        elif event_type == "execution.ac.recovery_exhausted":
            exhausted.append(data)
        elif event_type == "execution.ac.attempt_judged":
            outcome = data.get("outcome")
            if isinstance(outcome, str):
                judged_outcomes.append(outcome)
                ac_index = data.get("root_ac_index", data.get("ac_index"))
                timestamp = getattr(event, "timestamp", None)
                event_id = getattr(event, "id", None)
                if isinstance(ac_index, int) and not isinstance(ac_index, bool):
                    previous = last_judged_by_ac.get(ac_index)
                    # The durable store orders equal timestamps by event id
                    # (event_store.query_events: timestamp, id), so "latest"
                    # must key on both. Timestamp alone leaves same-instant
                    # judgements to iterable order.
                    ordering_key = (
                        timestamp,
                        event_id if isinstance(event_id, str) else "",
                    )
                    if (
                        previous is None
                        or timestamp is None
                        or previous[0][0] is None
                        or previous[0] <= ordering_key
                    ):
                        judged_cause = data.get("verify_cause")
                        last_judged_by_ac[ac_index] = (
                            ordering_key,
                            outcome,
                            judged_cause
                            if isinstance(judged_cause, str) and judged_cause in _VERIFY_GATE_CAUSES
                            else None,
                        )
        elif event_type == "orchestrator.session.failed":
            error_type = data.get("error_type")
            if isinstance(error_type, str) and error_type:
                session_error_type = error_type

    final_cause = _most_common(cause for cause, _ in verify_final)
    if final_cause is not None:
        return _attributed(
            f"verify_{final_cause}",
            ((f"verify_{cause}", missing) for cause, missing in verify_final),
        )

    if exhausted:
        # Dependency-cascade victims can also emit recovery_exhausted, but
        # their BLOCKED class describes the consequence rather than the cause.
        # Ignore those victims whenever a sibling exhausted after a non-blocked
        # judgement. If every exhausted AC is blocked, preserve the existing
        # dependency_blocked diagnosis below.
        has_non_blocked_exhaustion = any(
            last_judged_by_ac.get(data.get("root_ac_index"), (None, None))[1] != "blocked"
            for data in exhausted
        )
        causal_exhausted = [
            data
            for data in exhausted
            if not (
                has_non_blocked_exhaustion
                and last_judged_by_ac.get(data.get("root_ac_index"), (None, None))[1] == "blocked"
            )
        ]
        if causal_exhausted and not has_non_blocked_exhaustion:
            return RunFailure("dependency_blocked")
        attributed: list[tuple[str, tuple[str, ...]]] = []
        for data in causal_exhausted:
            ac_index = data.get("root_ac_index")
            if isinstance(ac_index, int) and ac_index in verify_last_by_ac:
                _, cause, missing = verify_last_by_ac[ac_index]
                attributed.append((f"verify_{cause}", missing))
                continue
            failure_class = data.get("last_failure_class")
            attributed.append(
                (
                    _WORKER_FAILURE_CLASSES.get(
                        failure_class if isinstance(failure_class, str) else "",
                        "worker_failed",
                    ),
                    (),
                )
            )
        # An exhausted AC with a concrete verify cause outranks an
        # unattributed worker failure on a sibling.
        causes = [cause for cause, _ in attributed]
        verify_attributed = [cause for cause in causes if cause.startswith("verify_")]
        chosen = _most_common(verify_attributed) or _most_common(causes)
        if chosen is not None:
            return _attributed(chosen, attributed)

    if session_error_type is not None:
        return RunFailure("runtime_error")

    if judged_outcomes and all(outcome == "blocked" for outcome in judged_outcomes):
        return RunFailure("dependency_blocked")

    # No recovery record: a criterion judged once from a shared attempt fails
    # without a retry of its own. The verify-gate cause its judgment recorded is
    # why the run failed; the gate's rejection event names what was missing.
    judged_failed = [
        (
            f"verify_{cause}",
            verify_last_by_ac[ac_index][2]
            if ac_index in verify_last_by_ac and verify_last_by_ac[ac_index][1] == cause
            else (),
        )
        for ac_index, (_, outcome, cause) in last_judged_by_ac.items()
        if outcome == "failed" and cause is not None
    ]
    chosen = _most_common(cause for cause, _ in judged_failed)
    if chosen is not None:
        return _attributed(chosen, judged_failed)

    return RunFailure(UNKNOWN_RUN_FAILURE_CAUSE)


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
    "RunFailure",
    "derive_run_failure",
    "derive_run_failure_cause",
    "failure_reason_code_for_run_cause",
]
