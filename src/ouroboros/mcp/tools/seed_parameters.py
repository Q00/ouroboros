"""Seed authoring tool parameter descriptions."""

CLIENT_GATES_DESCRIPTION = (
    "Caller-reported client-side interview gate acknowledgements. "
    "These names are not server-verified proof that a gate ran or "
    "that a person approved it. Evaluated only for the interview "
    "session_id path, not the interview-less session_context path. "
    "Expected values include seed_ready_acceptance_guard and "
    "restate_goal_approved."
)
