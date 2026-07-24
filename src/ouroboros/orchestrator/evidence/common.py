"""Common evidence value normalization helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math

_MAX_LEAF_RESULT_CHARS = 1200

_ATTEMPT_JUDGED_EVENT = "execution.ac.attempt_judged"
_HISTORICAL_ATTEMPT_JUDGED_EVENT = "execution.ac.outcome_finalized"
_ATTEMPT_OUTCOMES = frozenset(
    {"succeeded", "satisfied_externally", "failed", "blocked", "invalid", "cancelled"}
)
_SUCCESSFUL_ATTEMPT_OUTCOMES = frozenset({"succeeded", "satisfied_externally"})


@dataclass(frozen=True, slots=True)
class AttemptJudgmentContract:
    """Normalized, semantically validated outer AC attempt telemetry."""

    root_ac_index: int
    retry_attempt: int
    attempt_number: int
    success: bool
    outcome: str
    is_decomposed: bool


def validate_attempt_judgment_payload(
    data: Mapping[str, object],
    *,
    event_type: str | None = None,
    aggregate_id: str | None = None,
    expected_execution_id: str | None = None,
    expected_session_id: str | None = None,
) -> AttemptJudgmentContract:
    """Validate and normalize the shared attempt-judgment contract.

    New ``execution.ac.attempt_judged`` events are strict: they must carry the
    one-based ``attempt_number`` and the boolean ``is_decomposed`` marker. The
    historical ``execution.ac.outcome_finalized`` alias remains readable with
    those two fields omitted, but any supplied value is still validated. Both
    forms must agree on the retry number, success flag, and canonical outcome.
    """
    if event_type not in {None, _ATTEMPT_JUDGED_EVENT, _HISTORICAL_ATTEMPT_JUDGED_EVENT}:
        raise ValueError(f"unsupported attempt judgment event type: {event_type!r}")
    is_current = event_type in {None, _ATTEMPT_JUDGED_EVENT}

    raw_root = data.get("root_ac_index")
    root = (
        raw_root
        if isinstance(raw_root, int) and not isinstance(raw_root, bool) and raw_root >= 0
        else None
    )
    if root is None and not is_current:
        root = parse_root_ac_index(data)
    if root is None:
        raise ValueError("attempt judgment requires a non-negative root_ac_index")
    for alias in ("parent_ac_index", "ac_index"):
        if alias not in data:
            continue
        alias_root = data.get(alias)
        if (
            isinstance(alias_root, bool)
            or not isinstance(alias_root, int)
            or alias_root < 0
            or alias_root != root
        ):
            raise ValueError("attempt judgment root aliases are inconsistent")
    retry = parse_retry_attempt(data)
    if retry is None:
        raise ValueError("attempt judgment requires a non-negative retry_attempt")

    raw_attempt_number = data.get("attempt_number")
    if raw_attempt_number is None:
        if is_current:
            raise ValueError("attempt_judged requires attempt_number")
        attempt_number = retry + 1
    elif (
        isinstance(raw_attempt_number, bool)
        or not isinstance(raw_attempt_number, int)
        or raw_attempt_number < 1
        or raw_attempt_number != retry + 1
    ):
        raise ValueError("attempt_number must equal retry_attempt + 1")
    else:
        attempt_number = raw_attempt_number

    raw_decomposed = data.get("is_decomposed")
    if raw_decomposed is None:
        if is_current:
            raise ValueError("attempt_judged requires is_decomposed")
        is_decomposed = False
    elif not isinstance(raw_decomposed, bool):
        raise ValueError("is_decomposed must be a boolean")
    else:
        is_decomposed = raw_decomposed

    success = data.get("success")
    if not isinstance(success, bool):
        raise ValueError("attempt judgment requires a boolean success")
    outcome = data.get("outcome")
    if not isinstance(outcome, str) or outcome not in _ATTEMPT_OUTCOMES:
        raise ValueError("attempt judgment requires a canonical outcome")
    if success != (outcome in _SUCCESSFUL_ATTEMPT_OUTCOMES):
        raise ValueError("success and outcome are contradictory")

    execution_id = data.get("execution_id")
    session_id = data.get("session_id")
    if is_current:
        if (
            not isinstance(execution_id, str)
            or not execution_id
            or execution_id != execution_id.strip()
            or not isinstance(session_id, str)
            or not session_id
            or session_id != session_id.strip()
        ):
            raise ValueError("attempt_judged requires execution_id and session_id")
    elif expected_execution_id is not None or aggregate_id is not None:
        if not isinstance(execution_id, str) or not execution_id:
            raise ValueError("attempt judgment requires execution_id")
    if expected_execution_id is not None and execution_id != expected_execution_id:
        raise ValueError("attempt judgment execution identity does not match")
    if aggregate_id is not None and execution_id != aggregate_id:
        raise ValueError("attempt judgment aggregate identity does not match")
    if expected_session_id is not None and session_id != expected_session_id:
        raise ValueError("attempt judgment session identity does not match")

    return AttemptJudgmentContract(
        root_ac_index=root,
        retry_attempt=retry,
        attempt_number=attempt_number,
        success=success,
        outcome=outcome,
        is_decomposed=is_decomposed,
    )


def finite_number(value: object) -> float | None:
    """Return a finite numeric value, rejecting booleans and malformed inputs."""
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def strict_bool(value: object) -> bool | None:
    """Return a real boolean without applying truthiness coercion."""
    return value if isinstance(value, bool) else None


def event_type(event: object) -> str | None:
    """Read an event type from mapping-style or object-style events."""
    if isinstance(event, Mapping):
        value = event.get("type") or event.get("event_type")
    else:
        value = getattr(event, "type", None) or getattr(event, "event_type", None)
    return value if isinstance(value, str) else None


def event_data(event: object) -> Mapping[str, object]:
    """Read an event payload from mapping-style or object-style events."""
    if isinstance(event, Mapping):
        data = event.get("data") or event.get("payload") or {}
    else:
        data = getattr(event, "data", None) or getattr(event, "payload", None) or {}
    return data if isinstance(data, Mapping) else {}


def event_id(event: object) -> str | None:
    """Read a non-empty event identifier from mapping- or object-style events."""
    value = event.get("id") if isinstance(event, Mapping) else getattr(event, "id", None)
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def execution_run_anchor(data: Mapping[str, object]) -> str | None:
    """Return the persisted run anchor shared by execution evidence events."""
    run = data.get("seed_run_id") or data.get("execution_id")
    return str(run) if run is not None else None


def parse_retry_attempt(data: Mapping[str, object]) -> int | None:
    """Parse the required zero-based retry identity without defaulting missing data."""
    if "retry_attempt" not in data:
        return None
    value = data.get("retry_attempt")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def parse_root_ac_index(data: Mapping[str, object]) -> int | None:
    """Return the first valid zero-based root AC index from the evidence aliases."""
    for key in ("root_ac_index", "parent_ac_index", "ac_index"):
        value = data.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def _flatten_evidence_values(value: object) -> tuple[str, ...]:
    """Return concrete string claims from a typed evidence field."""
    if value is None:
        return ()
    if isinstance(value, str):
        stripped = value.strip()
        return (stripped,) if stripped else ()
    if isinstance(value, (int, float, bool)):
        return (str(value),)
    if isinstance(value, dict):
        flattened: list[str] = []
        for item in value.values():
            flattened.extend(_flatten_evidence_values(item))
        return tuple(flattened)
    if isinstance(value, (list, tuple, set)):
        flattened_sequence: list[str] = []
        for item in value:
            flattened_sequence.extend(_flatten_evidence_values(item))
        return tuple(flattened_sequence)
    return (str(value),)


def _normalized_evidence_text(text: str) -> str:
    """Normalize transcript/claim text for conservative containment checks."""
    return " ".join(text.lower().split())


def _normalize_command(command: str) -> str:
    """Normalize Bash commands for stable audit output."""
    return " ".join(command.split())


def _normalize_exact_command(command: str) -> str:
    """Normalize command whitespace while preserving case-sensitive exactness."""
    return " ".join(command.split())


def _truncate_text(text: str, limit: int = _MAX_LEAF_RESULT_CHARS) -> str:
    """Truncate long evidence blocks while preserving their beginning."""
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[:limit].rstrip() + "\n[TRUNCATED]"
