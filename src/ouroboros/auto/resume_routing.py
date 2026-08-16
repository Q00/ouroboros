"""Stable mappings from failed tools to resumable auto phases."""

from ouroboros.auto.state import AutoPhase

_INTERVIEW_TOOLS = frozenset(
    {
        "interview.start",
        "interview.resume",
        "interview.answer",
        "auto_answerer",
        "domain_profile_registry",
        "interview_driver",
    }
)
_REVIEW_TOOLS = frozenset(
    {
        "seed_saver",
        "grade_gate",
        "seed_loader",
        "seed_repairer",
        "seed_qa",
        "seed_reviewer",
    }
)


def recoverable_phase_for_tool(tool_name: str | None) -> AutoPhase | None:
    if tool_name in _INTERVIEW_TOOLS:
        return AutoPhase.INTERVIEW
    if tool_name == "seed_generator":
        return AutoPhase.SEED_GENERATION
    if tool_name in _REVIEW_TOOLS:
        return AutoPhase.REVIEW
    if tool_name == "run_starter":
        return AutoPhase.RUN
    return None
