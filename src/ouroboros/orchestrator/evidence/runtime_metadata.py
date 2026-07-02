"""Runtime metadata constants shared by evidence-aware execution."""

from __future__ import annotations

_REUSABLE_RUNTIME_EVENT_TYPES = frozenset(
    {
        "execution.session.recovered",
        "execution.session.started",
        "execution.session.resumed",
    }
)
_NON_REUSABLE_RUNTIME_EVENT_TYPES = frozenset(
    {
        "execution.session.completed",
        "execution.session.failed",
    }
)
_AC_RUNTIME_OWNERSHIP_METADATA_KEYS = frozenset(
    {
        "ac_id",
        "ac_index",
        "attempt_number",
        "depth",
        "display_path",
        "execution_id",
        "identity_model",
        "legacy_node_id",
        "legacy_node_aliases",
        "legacy_parent_node_id",
        "legacy_parent_node_aliases",
        "legacy_session_scope_id",
        "legacy_session_scope_ids",
        "legacy_session_state_path",
        "legacy_session_state_paths",
        "node_kind",
        "node_id",
        "ordinal",
        "parent_ac_index",
        "parent_node_id",
        "path",
        "retry_attempt",
        "root_ac_index",
        "root_ac_number",
        "scope",
        "schema_version",
        "session_attempt_id",
        "session_role",
        "session_scope_id",
        "session_state_path",
        "sub_ac_index",
    }
)
_AC_RUNTIME_SCOPE_METADATA_KEYS = frozenset(
    {
        "ac_id",
        "ac_index",
        "execution_id",
        "identity_model",
        "node_kind",
        "node_id",
        "parent_ac_index",
        "parent_node_id",
        "path",
        "root_ac_index",
        "scope",
        "schema_version",
        "session_role",
        "session_scope_id",
        "session_state_path",
        "sub_ac_index",
    }
)
_AC_RUNTIME_RESUME_METADATA_KEYS = frozenset({"runtime_event_type", "server_session_id"})

# Stall detection constants
STALL_TIMEOUT_SECONDS: float = 900.0  # 15 minutes of silence → stall for realistic test suites
HEARTBEAT_INTERVAL_SECONDS: float = 30.0  # Heartbeat emission interval
MAX_STALL_RETRIES: int = 2  # Max retries after stall (3 total attempts)
_STALL_SENTINEL = "__STALL_DETECTED__"  # Sentinel error for stall results

_SIBLING_HEADLINE_CHARS = 80
_SiblingACRef = tuple[int | None, str]
