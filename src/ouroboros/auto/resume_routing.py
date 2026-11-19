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
# Seed generation owns reading back (``seed_loader``) and writing
# (``seed_saver``) the Seed. The other names are the tools of the retired Seed
# review stage (#3064) and its pre-run checks. A session they blocked resumes at
# Seed generation: a persisted Seed is written and goes straight to the run, and
# a missing one is generated first.
_SEED_TOOLS = frozenset(
    {
        "seed_generator",
        "seed_loader",
        "seed_saver",
        "grade_gate",
        "seed_repairer",
        "seed_reviewer",
        "seed_qa",
        "seed_preflight",
    }
)


def recoverable_phase_for_tool(tool_name: str | None) -> AutoPhase | None:
    if tool_name in _INTERVIEW_TOOLS:
        return AutoPhase.INTERVIEW
    if tool_name in _SEED_TOOLS:
        return AutoPhase.SEED_GENERATION
    if tool_name == "run_starter":
        return AutoPhase.RUN
    return None
