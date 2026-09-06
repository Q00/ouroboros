"""Privacy-safe failure reasons and fixed recovery guidance for MCP jobs.

This module deliberately classifies only terminal status and machine-readable
metadata.  It never inspects exception text, result text, prompts, paths, or
identifiers.  The returned values are closed vocabularies suitable for both
user-facing job results and anonymous telemetry.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum


class FailureReasonCode(StrEnum):
    """Stable, privacy-safe reason categories for failed workflows."""

    CONFIG = "config"
    AUTH = "auth"
    TIMEOUT = "timeout"
    MODEL = "model"
    TOOL = "tool"
    VALIDATION = "validation"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


# Closed run-level failure causes for ``workflow_outcome.failure_cause``
# (command=run). Produced by orchestrator/run_failure_cause.py from durable
# executor evidence; anything outside this set folds to ``unknown`` at the
# telemetry boundary. ``verify_*`` mirrors the verify gate's cause vocabulary
# (orchestrator/verify_gate_outcome.py) — edit both together.
RUN_FAILURE_CAUSES = frozenset(
    {
        "verify_invalid_contract",
        "verify_artifacts_missing",
        "verify_artifacts_missing_found_elsewhere",
        "verify_environment_unverifiable",
        "verify_timeout",
        "verify_exit_nonzero",
        "verify_output_assertion_unmatched",
        "verify_workspace_mutated",
        "worker_evidence_missing",
        "worker_fabrication_suspected",
        "worker_blocked",
        "worker_failed",
        "dependency_blocked",
        "runtime_error",
        "cancelled",
        "unknown",
    }
)
UNKNOWN_RUN_FAILURE_CAUSE = "unknown"


class RecoveryAction(StrEnum):
    """The single recommended user action after a failed workflow."""

    RETRY = "retry"
    SETUP = "setup"
    LOGIN = "login"
    UPDATE = "update"
    INSPECT_LOGS = "inspect_logs"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class FailureResolution:
    """A closed reason/action pair plus fixed user-visible next-step text."""

    reason_code: FailureReasonCode
    recovery_action: RecoveryAction
    next_step: str


_FAILURE_STATUSES = frozenset({"failed", "cancelled", "interrupted", "timed_out", "timeout"})
_SAFE_REASON_VALUES = frozenset(item.value for item in FailureReasonCode)
_SAFE_MACHINE_REASON_KEYS = (
    "failure_reason_code",
    "reason_code",
    "error_code",
    "status",
    "evaluation_status",
)
_SAFE_MACHINE_REASON_KEYS += ("action", "stop_reason")

# These values are producer-owned vocabularies, not arbitrary error text.
# Keep the mapping deliberately conservative: a generic ``failed`` action does
# not identify whether the provider, a tool, or the workflow itself failed.
_STRUCTURED_REASON_VALUES: dict[str, FailureReasonCode] = {
    "iteration_timeout": FailureReasonCode.TIMEOUT,
    "wall_clock_exhausted": FailureReasonCode.TIMEOUT,
    # A generation budget is a quality/convergence guard, not a wall-clock
    # deadline. Keep timeout reserved for the two actual time-budget signals.
    "max_generations reached": FailureReasonCode.VALIDATION,
    "qa_failed": FailureReasonCode.VALIDATION,
    "oscillation_detected": FailureReasonCode.VALIDATION,
    "grade_regressing": FailureReasonCode.VALIDATION,
    "stagnated": FailureReasonCode.VALIDATION,
    "interrupted": FailureReasonCode.CANCELLED,
}

_ACTION_BY_REASON: dict[FailureReasonCode, tuple[RecoveryAction, str]] = {
    FailureReasonCode.CONFIG: (
        RecoveryAction.SETUP,
        "Run setup, then retry the workflow.",
    ),
    FailureReasonCode.AUTH: (
        RecoveryAction.LOGIN,
        "Sign in to the required provider, then retry the workflow.",
    ),
    FailureReasonCode.TIMEOUT: (
        RecoveryAction.RETRY,
        "Retry the workflow.",
    ),
    FailureReasonCode.MODEL: (
        RecoveryAction.RETRY,
        "Retry the workflow with the current model configuration.",
    ),
    FailureReasonCode.TOOL: (
        RecoveryAction.INSPECT_LOGS,
        "Inspect the server logs before retrying the workflow.",
    ),
    FailureReasonCode.VALIDATION: (
        RecoveryAction.INSPECT_LOGS,
        "Inspect the validation output before retrying the workflow.",
    ),
    FailureReasonCode.CANCELLED: (
        RecoveryAction.RETRY,
        "Retry the workflow when you are ready.",
    ),
    FailureReasonCode.UNKNOWN: (
        RecoveryAction.INSPECT_LOGS,
        "Inspect the server logs before retrying the workflow.",
    ),
}


def _explicit_reason(metadata: Mapping[str, object]) -> FailureReasonCode | None:
    """Accept only exact values from known machine-readable metadata keys."""
    for key in _SAFE_MACHINE_REASON_KEYS:
        value = metadata.get(key)
        if isinstance(value, str) and value in _SAFE_REASON_VALUES:
            return FailureReasonCode(value)
        if not isinstance(value, str):
            continue
        structured_reason = _STRUCTURED_REASON_VALUES.get(value)
        if structured_reason is not None:
            return structured_reason
        if value in {"timed_out", "timeout"}:
            return FailureReasonCode.TIMEOUT
        if value in {"config_error", "configuration_error"}:
            return FailureReasonCode.CONFIG
        if value in {"auth_error", "authentication_error", "unauthorized"}:
            return FailureReasonCode.AUTH
        if value in {"model_error", "provider_error"}:
            return FailureReasonCode.MODEL
        if value in {"tool_error"}:
            return FailureReasonCode.TOOL
        if value in {"validation_error", "invalid"}:
            return FailureReasonCode.VALIDATION
    return None


def classify_failure(
    terminal_status: str,
    metadata: Mapping[str, object] | None = None,
) -> FailureResolution | None:
    """Classify one failed terminal outcome without reading raw error text.

    ``None`` is returned for successful/non-terminal statuses.  For a failed
    status, the classifier always returns a resolution, including the safe
    ``unknown`` fallback, so every terminal failure can be measured.
    """
    status = terminal_status.strip().lower() if isinstance(terminal_status, str) else ""
    if status not in _FAILURE_STATUSES:
        return None

    meta = metadata if isinstance(metadata, Mapping) else {}
    reason: FailureReasonCode | None = None

    if (
        status in {"cancelled", "interrupted"}
        or meta.get("interrupted_from_shutdown") is True
        or any(
            meta.get(key) is True
            for key in (
                "interrupted_from_dead_owner",
                "interrupted_from_stranded_job_task",
                "cancelled_by_user",
            )
        )
    ):
        reason = FailureReasonCode.CANCELLED
    elif meta.get("failed_from_progress_accounting_stall") is True or any(
        meta.get(key) is True for key in ("timed_out", "wait_timed_out")
    ):
        reason = FailureReasonCode.TIMEOUT
    else:
        reason = _explicit_reason(meta)

    if reason is None:
        reason = FailureReasonCode.UNKNOWN
    action, next_step = _ACTION_BY_REASON[reason]
    return FailureResolution(reason, action, next_step)


__all__ = [
    "FailureReasonCode",
    "FailureResolution",
    "RecoveryAction",
    "classify_failure",
]
