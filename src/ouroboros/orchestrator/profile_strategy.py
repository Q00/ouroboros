"""Profile-backed ExecutionStrategy (RFC v2 #830, PR 9 wiring).

The legacy `CodeStrategy / ResearchStrategy / AnalysisStrategy` triple
in `execution_strategy.py` reads its system-prompt fragment from
`src/ouroboros/agents/{name}.md` and hardcodes its tool list. RFC v2
moves both of those into the profile YAMLs so adding a new domain is
a YAML edit, not a Python + markdown edit.

This module ships a `ProfileBackedStrategy` that satisfies the existing
`ExecutionStrategy` Protocol but reads tools and system-prompt fragment
from a loaded `ExecutionProfile`. The system prompt fragment carries
the H3 `[POST]` block (`build_post_block`) so the evidence_schema
flows through `runner.build_system_prompt`; the `[PRE]` restate-and-
preconditions gate lives in `get_task_prompt_suffix` so it grounds in
the AC list `runner.build_task_prompt` renders immediately above it.

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

from ouroboros.orchestrator.phase_wrappers import build_post_block
from ouroboros.orchestrator.profile_loader import ExecutionProfile
from ouroboros.orchestrator.workflow_state import ActivityType

# Shared activity classification for tools that mean the same thing in
# every profile. Bash is intentionally absent — its semantics differ
# per profile (TESTING for code/analysis where Bash invokes test
# commands, EXPLORING for research where Bash backs grep/curl/etc.).
_SHARED_ACTIVITY_MAP: dict[str, ActivityType] = {
    "Read": ActivityType.EXPLORING,
    "Glob": ActivityType.EXPLORING,
    "Grep": ActivityType.EXPLORING,
    "Edit": ActivityType.BUILDING,
    "Write": ActivityType.BUILDING,
    "NotebookEdit": ActivityType.BUILDING,
    "MultiEdit": ActivityType.BUILDING,
}

# Per-profile overrides preserve the legacy execution_strategy behavior
# (Bash → TESTING for code/analysis, EXPLORING for research). Without
# this, opting into ProfileBackedStrategy for the research profile
# would flip Bash live phase reporting from EXPLORING to TESTING,
# regressing dashboard semantics (bot finding on #891 r2).
_PROFILE_ACTIVITY_OVERRIDES: dict[str, dict[str, ActivityType]] = {
    "code": {"Bash": ActivityType.TESTING},
    "analysis": {"Bash": ActivityType.TESTING},
    "research": {"Bash": ActivityType.EXPLORING},
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

        Combines the profile anchor (axis, min_unit, verifier_focus)
        with the H3 [POST] block sourced from `phase_wrappers` so the
        evidence_schema requirements and the "no DONE in prose" rule
        flow through `runner.build_system_prompt` unmodified. Without
        this composition, opting into ProfileBackedStrategy would
        bypass H2/H1 entirely.
        """
        header = (
            f"You are executing an acceptance criterion under the "
            f"{self.profile.profile!r} profile.\n"
            f"Decomposition axis: {self.profile.axis}.\n"
            f"Smallest acceptable unit: {self.profile.min_unit}.\n"
            f"The verifier will focus on: {self.profile.verifier_focus.strip()}"
        )
        # The POST block carries the evidence-field names and rejection
        # rules verbatim; the leaf executor must see them or it cannot
        # produce a record the H2 validator will accept.
        return f"{header}\n\n{build_post_block(self.profile)}"

    def get_task_prompt_suffix(self) -> str:
        """Compose the [PRE]-style restate / precondition gate.

        The Strategy boundary has no per-AC context — the AC list is
        rendered into the task prompt by `runner.build_task_prompt`
        immediately above this suffix. We therefore phrase the H3 [PRE]
        gate as "for each acceptance criterion above" so the executor
        runs the restatement + precondition pass against the AC list it
        just received.
        """
        return (
            "[PRE — harness-injected; restate before any action]\n"
            "For each acceptance criterion above, restate it in one "
            "sentence and list every precondition you are assuming "
            "(paths, commands, external services, access tokens). Do "
            "not begin execution if any precondition is unverified — "
            "surface the blocker instead.\n\n"
            "When you finish the work for an AC, emit a single fenced "
            "JSON evidence record per the active profile and stop. Do "
            "not declare DONE in prose — the harness adjudicates via "
            "the verifier loop."
        )

    def get_activity_map(self) -> dict[str, ActivityType]:
        # Compose the shared map with per-profile overrides so Bash
        # semantics match the legacy execution_strategy (TESTING for
        # code/analysis, EXPLORING for research). Unknown tools default
        # to EXPLORING — they get logged but don't break the dashboard.
        overrides = _PROFILE_ACTIVITY_OVERRIDES.get(self.profile.profile, {})
        merged: dict[str, ActivityType] = {**_SHARED_ACTIVITY_MAP, **overrides}
        return {
            tool: merged.get(tool, ActivityType.EXPLORING) for tool in self.profile.suggested_tools
        }


__all__ = [
    "ProfileBackedStrategy",
]
