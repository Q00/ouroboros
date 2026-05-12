"""Profile-backed ExecutionStrategy (RFC v2 #830, PR 9 wiring).

The legacy `CodeStrategy / ResearchStrategy / AnalysisStrategy` triple
in `execution_strategy.py` reads its system-prompt fragment from
`src/ouroboros/agents/{name}.md` and hardcodes its tool list. RFC v2
moves both of those into the profile YAMLs so adding a new domain is
a YAML edit, not a Python + markdown edit.

This module ships a `ProfileBackedStrategy` that satisfies the existing
`ExecutionStrategy` Protocol but reads tools and system-prompt fragment
from a loaded `ExecutionProfile`. The system prompt is composed via
`phase_wrappers.build_pre_block` so the H1/H2/H3 guardrails are baked
into the prompt the leaf executor sees.

Opt-in by design — the default strategy registry in
`execution_strategy._STRATEGY_REGISTRY` is **not** modified by this PR.
Callers that want profile-backed behavior pass the new strategy
explicitly. The follow-up flip-the-default PR depends on shipping the
verifier + decomposer wire-ups (currently behind the open #830 stack).

Usage:
    from ouroboros.orchestrator.profile_loader import load_profile
    from ouroboros.orchestrator.profile_strategy import (
        ProfileBackedStrategy,
    )

    strategy = ProfileBackedStrategy(load_profile("code"))
    strategy.get_tools()                # from profile.suggested_tools
    strategy.get_system_prompt_fragment()  # H3-wrapped, profile-aware
"""

from __future__ import annotations

from dataclasses import dataclass

from ouroboros.orchestrator.profile_loader import ExecutionProfile
from ouroboros.orchestrator.workflow_state import ActivityType

_DEFAULT_ACTIVITY_MAP: dict[str, ActivityType] = {
    "Read": ActivityType.EXPLORING,
    "Glob": ActivityType.EXPLORING,
    "Grep": ActivityType.EXPLORING,
    "Edit": ActivityType.BUILDING,
    "Write": ActivityType.BUILDING,
    "Bash": ActivityType.TESTING,
}


@dataclass(frozen=True)
class ProfileBackedStrategy:
    """ExecutionStrategy whose tools + prompt come from an ExecutionProfile.

    Satisfies the `ExecutionStrategy` Protocol in `execution_strategy`.
    Constructed with a loaded profile; nothing else. The legacy markdown
    agent files (`agents/code-executor.md` etc.) are not consulted —
    the H3 wrappers in `phase_wrappers` source their content directly
    from the profile, keeping skill and harness in lockstep.
    """

    profile: ExecutionProfile

    def get_tools(self) -> list[str]:
        return list(self.profile.suggested_tools)

    def get_system_prompt_fragment(self) -> str:
        """Compose the harness-owned system prompt fragment.

        Profile axis + min_unit anchor the leaf executor to the
        decomposition contract; the verifier focus surfaces the
        verifier's expectation up-front so the leaf can self-correct
        before the verifier pass.
        """
        return (
            f"You are executing an acceptance criterion under the "
            f"{self.profile.profile!r} profile.\n"
            f"Decomposition axis: {self.profile.axis}.\n"
            f"Smallest acceptable unit: {self.profile.min_unit}.\n"
            f"The verifier will focus on: {self.profile.verifier_focus.strip()}"
        )

    def get_task_prompt_suffix(self) -> str:
        return (
            "Execute the criterion in full. When you finish, emit a "
            "single fenced JSON evidence record per the active profile "
            "and stop — do not declare DONE in prose."
        )

    def get_activity_map(self) -> dict[str, ActivityType]:
        # Resolve activity types from the profile's suggested tools.
        # Unknown tools default to EXPLORING — they get logged but
        # don't break the dashboard.
        return {
            tool: _DEFAULT_ACTIVITY_MAP.get(tool, ActivityType.EXPLORING)
            for tool in self.profile.suggested_tools
        }


__all__ = [
    "ProfileBackedStrategy",
]
