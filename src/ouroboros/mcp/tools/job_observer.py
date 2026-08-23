"""Structured handoff contract for observing background Ouroboros jobs."""

from __future__ import annotations

import base64
from collections.abc import Mapping
import json
import re
from typing import Any

JOB_OBSERVER_PROTOCOL = "ouroboros.job_observer.v1"
JOB_OBSERVER_INLINE_OPEN = "<!-- ouroboros-job-observer-v1 base64\n"
JOB_OBSERVER_INLINE_CLOSE = "\n-->"
_JOB_OBSERVER_INLINE_MAX_DECODED_BYTES = 8_192
_JOB_OBSERVER_INLINE_MAX_ENCODED_CHARS = 10_924
_JOB_OBSERVER_JOB_ID_PATTERN = re.compile(r"^job_[A-Za-z0-9][A-Za-z0-9_.:-]*$")
_JOB_OBSERVER_FOLLOW_RESULT_KEYS = frozenset(
    {
        "job_id",
        "ralph_job_id",
        "chained_evaluate_job_id",
        "chained_ralph_job_id",
    }
)


def build_job_observer_contract(
    *,
    job_id: str,
    cursor: object = 0,
    session_id: str | None = None,
    execution_id: str | None = None,
    follow_result_job_keys: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build the model-facing contract for one exclusive read-only observer.

    Host runtimes with independent child sessions can hand this object to one
    observer session. The main conversation owns start, explicit user-requested
    drill-down calls, and any host-specific child-message relay lifecycle, while
    the observer owns the wait cursor and terminal result retrieval. Sequential
    hosts use the declared fallback.
    """
    normalized_cursor = cursor if isinstance(cursor, int) and not isinstance(cursor, bool) else 0
    normalized_follow_result_job_keys = list(follow_result_job_keys)
    if len(normalized_follow_result_job_keys) != len(set(normalized_follow_result_job_keys)):
        raise ValueError("Observer follow-result job keys must be unique")
    if not set(normalized_follow_result_job_keys).issubset(_JOB_OBSERVER_FOLLOW_RESULT_KEYS):
        raise ValueError("Observer follow-result job key is not allowed by protocol v1")
    if normalized_cursor < 0:
        normalized_cursor = 0

    return {
        "protocol": JOB_OBSERVER_PROTOCOL,
        "role": "read_only_job_observer",
        "recommended_host_action": "spawn_observer_session",
        "ownership": "exclusive",
        "job_id": job_id,
        "session_id": session_id,
        "execution_id": execution_id,
        "cursor": normalized_cursor,
        "wait": {
            "tool": "ouroboros_job_wait",
            "arguments": {
                "job_id": job_id,
                "cursor": normalized_cursor,
                "timeout_seconds": 180,
                "view": "summary",
                "stream": "linked",
                "wait_for": "attention_or_ac_change",
            },
        },
        "result": {
            "tool": "ouroboros_job_result",
            "arguments": {"job_id": job_id},
        },
        "follow_result_job_keys": normalized_follow_result_job_keys,
        "main_session_policy": "start_and_on_demand_only",
        "host_lifecycle": {
            "spawn_required_for_live_relay": True,
            "codex_spawn_tool": "spawn_agent",
            "codex_task_name": "run_observer",
            "spawn_ack_required": True,
            "wait_is_not_spawn": True,
            "codex_parent_relay": {
                "wait_tool": "wait_agent",
                "wait_timeout_ms": 60000,
                "keep_turn_open_while_observer_active": True,
                "interruptible_by_user": True,
                "user_opt_out": "stop_relay_keep_durable_job_running",
                "observer_exit_fallback": "durable_catch_up_next_parent_turn",
            },
            "durable_job_survives_parent_turn": True,
            "fallback_keep_turn_open": False,
            "fallback_notification_timing": "next_parent_turn_or_explicit_status",
        },
        "relay": {
            "mode": "event_driven",
            "target": "parent_session",
            "events": [
                "phase_changed",
                "progress_advanced",
                "attention_required",
                "terminal",
            ],
            "suppress": ["unchanged", "heartbeat", "raw_tool_output"],
            "max_lines_per_event": 2,
            "attention_priority": "immediate",
        },
        "parent_session": {
            "availability": "available_after_handoff",
            "initial_handoff": [
                "show_job_and_session_handles",
                "show_dashboard_url_or_tui_command",
                "state_that_the_main_conversation_remains_available",
            ],
            "available_work": [
                "continue_user_conversation",
                "refine_requirements",
                "read_only_repository_inspection",
                "unrelated_work_in_an_isolated_worktree",
                "explicit_status_or_control_requests",
            ],
            "workspace_write_policy": "check_active_worker_conflicts_or_use_isolated_worktree",
            "dashboard_meta_key": "dashboard_url",
            "tui_command": "ouroboros tui open",
        },
        "instructions": [
            "For live proactive relays, create one real child with the host spawn primitive and require its live agent/session acknowledgement; a wait call is not a spawn.",
            "On Codex call spawn_agent exactly once with task_name run_observer and include this contract unchanged in the child message.",
            "After Codex receives the live child acknowledgement, the parent should keep its turn open with host_lifecycle.codex_parent_relay.wait_tool while the observer is active. A child send_message only reaches the parent's mailbox; it does not wake a parent turn that already ended.",
            "Use the Codex parent relay timeout, relay meaningful mailbox updates, and wait again until the observer reports terminal completion or the child exits. User input may interrupt the wait and must be handled before observation resumes.",
            "If the user asks to stop live observation, end the parent relay loop without cancelling the durable job, explain that catch-up remains available on the next turn or explicit status request, and do not resume waiting.",
            "If the observer child fails, is cancelled, or exits before a terminal job summary, stop the parent relay loop and fall back to durable catch-up instead of waiting indefinitely.",
            "If spawning is unavailable or fails, do not claim an observer exists. State that the durable worker continues independently and that the parent will catch up on the next turn or explicit status request; keep the turn open only when the user explicitly asked for live watching.",
            "Reload deferred Ouroboros tool schemas immediately before each tool call.",
            "Call wait.tool with wait.arguments; replace the local cursor from response meta.",
            "If the wait returns non-terminal or times out unchanged, repeat silently.",
            "For each relay.events change, send at most relay.max_lines_per_event concise lines to the parent session; never send suppressed events or raw tool output.",
            "Send attention_required immediately for blockers, pending user decisions, or failures that need intervention.",
            "After terminal status, call result.tool with result.arguments.",
            "For each non-empty follow_result_job_keys value in the result meta, observe that job from cursor 0 only when it differs from every already visited job ID.",
            "Return one compact terminal summary to the parent session.",
        ],
        "restrictions": [
            "read_only",
            "no_repository_edits",
            "no_execution_control",
            "no_worker_fanout",
            "no_duplicate_polling_owner",
        ],
        "fallback": {
            "host_action": "catch_up_on_next_parent_turn",
            "keep_main_turn_open": False,
            "durable_worker_continues": True,
            "live_proactive_relay": False,
            "stream": "linked",
            "wait_for": "attention_or_ac_change",
            "view": "summary",
        },
    }


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build one JSON object while rejecting keys hidden by last-write-wins."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON object key {key!r}")
        result[key] = value
    return result


def validate_job_observer_contract(
    value: object,
    *,
    expected_job_id: str | None = None,
    expected_session_id: str | None = None,
    expected_execution_id: str | None = None,
) -> dict[str, Any] | None:
    """Return a complete canonical observer contract or fail closed."""
    if not isinstance(value, dict):
        return None

    job_id = value.get("job_id")
    cursor = value.get("cursor")
    session_id = value.get("session_id")
    execution_id = value.get("execution_id")
    follow_result_job_keys = value.get("follow_result_job_keys")

    if not isinstance(job_id, str) or not _JOB_OBSERVER_JOB_ID_PATTERN.fullmatch(job_id):
        return None
    if expected_job_id is not None and job_id != expected_job_id:
        return None
    if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
        return None
    if session_id is not None and (not isinstance(session_id, str) or not session_id):
        return None
    if expected_session_id is not None and session_id != expected_session_id:
        return None
    if execution_id is not None and (not isinstance(execution_id, str) or not execution_id):
        return None
    if expected_execution_id is not None and execution_id != expected_execution_id:
        return None
    if not isinstance(follow_result_job_keys, list) or any(
        not isinstance(key, str) or key not in _JOB_OBSERVER_FOLLOW_RESULT_KEYS
        for key in follow_result_job_keys
    ):
        return None
    if len(follow_result_job_keys) != len(set(follow_result_job_keys)):
        return None
    expected = build_job_observer_contract(
        job_id=job_id,
        cursor=cursor,
        session_id=session_id,
        execution_id=execution_id,
        follow_result_job_keys=tuple(follow_result_job_keys),
    )
    return dict(value) if value == expected else None


def append_job_observer_inline_handoff(
    text: str,
    contract: Mapping[str, Any],
) -> str:
    """Append one bounded canonical observer contract for text-only hosts."""
    canonical = validate_job_observer_contract(dict(contract))
    if canonical is None:
        raise ValueError("Observer inline handoff requires a canonical protocol v1 contract")
    payload = json.dumps(
        {"job_observer": canonical},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > _JOB_OBSERVER_INLINE_MAX_DECODED_BYTES:
        raise ValueError("Observer inline handoff exceeds the bounded payload size")
    encoded = base64.b64encode(payload).decode("ascii")
    return f"{text.rstrip()}\n\n{JOB_OBSERVER_INLINE_OPEN}{encoded}{JOB_OBSERVER_INLINE_CLOSE}"


def extract_job_observer_inline_handoff(
    text: str,
    *,
    expected_job_id: str,
    expected_session_id: str | None = None,
    expected_execution_id: str | None = None,
) -> dict[str, Any] | None:
    """Recover and validate the canonical observer contract from text output."""
    if text.count(JOB_OBSERVER_INLINE_OPEN) != 1:
        return None
    open_idx = text.rfind(JOB_OBSERVER_INLINE_OPEN)
    close_idx = text.find(JOB_OBSERVER_INLINE_CLOSE, open_idx)
    if close_idx == -1:
        return None
    if text[close_idx + len(JOB_OBSERVER_INLINE_CLOSE) :].strip():
        return None
    encoded = text[open_idx + len(JOB_OBSERVER_INLINE_OPEN) : close_idx]
    if not encoded or len(encoded) > _JOB_OBSERVER_INLINE_MAX_ENCODED_CHARS:
        return None
    try:
        payload_bytes = base64.b64decode(encoded.encode("ascii"), validate=True)
        if len(payload_bytes) > _JOB_OBSERVER_INLINE_MAX_DECODED_BYTES:
            return None
        decoded = payload_bytes.decode("utf-8")
        payload = json.loads(decoded, object_pairs_hook=_reject_duplicate_json_keys)
    except (RecursionError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or set(payload) != {"job_observer"}:
        return None
    return validate_job_observer_contract(
        payload["job_observer"],
        expected_job_id=expected_job_id,
        expected_session_id=expected_session_id,
        expected_execution_id=expected_execution_id,
    )


__all__ = [
    "JOB_OBSERVER_INLINE_CLOSE",
    "JOB_OBSERVER_INLINE_OPEN",
    "JOB_OBSERVER_PROTOCOL",
    "append_job_observer_inline_handoff",
    "build_job_observer_contract",
    "extract_job_observer_inline_handoff",
    "validate_job_observer_contract",
]
