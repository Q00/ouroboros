"""Tests for the structured background-job observer handoff."""

from ouroboros.mcp.tools.job_observer import (
    JOB_OBSERVER_PROTOCOL,
    build_job_observer_contract,
)


def test_job_observer_contract_assigns_exclusive_read_only_ownership() -> None:
    contract = build_job_observer_contract(
        job_id="job_123",
        cursor=7,
        session_id="orch_123",
        execution_id="exec_123",
        follow_result_job_keys=("chained_evaluate_job_id",),
    )

    assert contract == {
        "protocol": JOB_OBSERVER_PROTOCOL,
        "role": "read_only_job_observer",
        "recommended_host_action": "spawn_observer_session",
        "ownership": "exclusive",
        "job_id": "job_123",
        "session_id": "orch_123",
        "execution_id": "exec_123",
        "cursor": 7,
        "wait": {
            "tool": "ouroboros_job_wait",
            "arguments": {
                "job_id": "job_123",
                "cursor": 7,
                "timeout_seconds": 180,
                "view": "summary",
                "stream": "linked",
                "wait_for": "attention_or_ac_change",
            },
        },
        "result": {
            "tool": "ouroboros_job_result",
            "arguments": {"job_id": "job_123"},
        },
        "follow_result_job_keys": ["chained_evaluate_job_id"],
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


def test_job_observer_contract_normalizes_non_integer_cursor() -> None:
    contract = build_job_observer_contract(job_id="job_123", cursor="pending")

    assert contract["cursor"] == 0
    assert contract["wait"]["arguments"]["cursor"] == 0
