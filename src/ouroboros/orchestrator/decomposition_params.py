"""Profile-aware decomposition parameters + prompt builder (RFC v2 H4, #830).

H4 reframes decomposition from "free prose system prompt" to "structured
parameters consumed by a uniform decomposer". Adding a new domain becomes
a YAML edit, not a prompt-engineering pass.

`parallel_executor._try_decompose_ac` consumes these parameters when
the live runner supplies an `ExecutionProfile`, while preserving the
legacy generic prompt when no profile is available. This keeps the
profile-aware path additive and auditable before any default flip.
"""

from __future__ import annotations

from dataclasses import dataclass

from ouroboros.orchestrator.profile_loader import ExecutionProfile

DEFAULT_MIN_BRANCHING: int = 2
DEFAULT_MAX_BRANCHING: int = 5


@dataclass(frozen=True)
class DecompositionParams:
    """Structured inputs the uniform decomposer consumes.

    Attributes:
        profile_name: Profile identifier (e.g. 'code'); preserved for logs.
        axis: Decomposition axis (e.g. 'testable_unit', 'subtopic').
        min_unit: Smallest dispatchable unit description.
        cut_signal: Heuristic that a sub-AC is small enough to stop.
        min_branching: Minimum sub-AC count when splitting (>= 1).
        max_branching: Maximum sub-AC count when splitting.
    """

    profile_name: str
    axis: str
    min_unit: str
    cut_signal: str
    min_branching: int = DEFAULT_MIN_BRANCHING
    max_branching: int = DEFAULT_MAX_BRANCHING

    def __post_init__(self) -> None:
        if self.min_branching < 1:
            msg = f"min_branching must be >= 1, got {self.min_branching}"
            raise ValueError(msg)
        if self.max_branching < self.min_branching:
            msg = (
                f"max_branching ({self.max_branching}) must be >= "
                f"min_branching ({self.min_branching})"
            )
            raise ValueError(msg)


def params_from_profile(
    profile: ExecutionProfile,
    *,
    min_branching: int = DEFAULT_MIN_BRANCHING,
    max_branching: int | None = None,
) -> DecompositionParams:
    """Project an ExecutionProfile into the decomposer's parameter shape."""
    return DecompositionParams(
        profile_name=profile.profile,
        axis=profile.axis,
        min_unit=profile.min_unit,
        cut_signal=profile.cut_signal,
        min_branching=min_branching,
        max_branching=profile.max_branching if max_branching is None else max_branching,
    )


def build_decomposition_system_prompt(params: DecompositionParams) -> str:
    """Build the profile-aware system prompt for the decomposer.

    Replaces the current
    `"You are a task decomposition expert. Analyze tasks and break them
    down if needed."` system prompt with one that names the axis and
    min_unit explicitly, so the splitter's output respects the profile.
    """
    return (
        "You are a task decomposition expert for the "
        f"{params.profile_name!r} domain.\n"
        f"Split along the axis: {params.axis}.\n"
        f"Smallest acceptable unit: {params.min_unit}.\n"
        "Each sub-AC must be independently executable along this axis "
        "and never overlap a sibling."
    )


def build_decomposition_user_prompt(
    params: DecompositionParams,
    *,
    ac_label: str,
    ac_content: str,
    seed_goal: str,
) -> str:
    """Build the user-side decomposer prompt.

    Mirrors the body shape currently used in
    `parallel_executor._try_decompose_ac` so PR 9's wiring is a drop-in
    replacement: the LLM still answers with ATOMIC or a JSON array.
    """
    cut_signal_line = (
        f"A sub-AC is small enough when: {params.cut_signal}.\n" if params.cut_signal else ""
    )
    return (
        "Analyze this acceptance criterion and determine if it should be "
        "decomposed.\n\n"
        "## Goal Context\n"
        f"{seed_goal}\n\n"
        f"## Acceptance Criterion ({ac_label})\n"
        f"{ac_content}\n\n"
        "## Instructions\n"
        f"Split along the axis: {params.axis}.\n"
        f"Smallest acceptable unit: {params.min_unit}.\n"
        f"{cut_signal_line}"
        f"If this AC is complex along the {params.axis} axis, decompose "
        f"it into {params.min_branching}-{params.max_branching} sub-ACs.\n"
        "If the AC is already at or below the minimum unit, respond with: "
        "ATOMIC\n\n"
        "If decomposing, respond with ONLY a JSON array of sub-AC "
        'descriptions: ["Sub-AC 1: ...", "Sub-AC 2: ...", ...]\n\n'
        "Each sub-AC must be:\n"
        f"- Independently executable along the {params.axis} axis\n"
        f"- Not smaller than: {params.min_unit}\n"
        "- Non-overlapping with siblings\n\n"
        'Respond with either "ATOMIC" or the JSON array only, nothing else.'
    )


__all__ = [
    "DEFAULT_MAX_BRANCHING",
    "DEFAULT_MIN_BRANCHING",
    "DecompositionParams",
    "build_decomposition_system_prompt",
    "build_decomposition_user_prompt",
    "params_from_profile",
]
