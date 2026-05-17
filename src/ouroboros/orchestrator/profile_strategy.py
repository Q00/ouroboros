"""Profile-backed ExecutionStrategy (RFC v2 #830, PR 9 wiring).

The legacy `CodeStrategy / ResearchStrategy / AnalysisStrategy` triple
in `execution_strategy.py` reads its system-prompt fragment from
`src/ouroboros/agents/{name}.md` and hardcodes its tool list. RFC v2
moves both of those into the profile YAMLs so adding a new domain is
a YAML edit, not a Python + markdown edit.

Once the #830 stack lands and the default strategy is flipped to the
profile-backed variant, the legacy `agents/{name}.md` files can be
removed. They are intentionally NOT annotated as deprecated in their
own text — that text is the live system prompt for the legacy code
path; any "deprecated" banner inside the markdown would become part
of the prompt the model receives and skew runtime behavior on the
unflipped legacy path (bot finding on #891 r8). Treat the legacy
markdown files as inert prompt content; the deprecation lives here.

This module ships a `ProfileBackedStrategy` that satisfies the existing
`ExecutionStrategy` Protocol but reads tools and system-prompt fragment
from a loaded `ExecutionProfile`. The system prompt fragment composes a
custom multi-AC POST section inline (it intentionally does NOT call
`phase_wrappers.build_post_block`, whose "emit one block, then stop"
contract is single-dispatch only); the `[PRE]` restate-and-
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

# Per-profile executor guidance preserves the domain-specific behavior
# the legacy strategies carried in `src/ouroboros/agents/{name}.md`.
# Without this, opting into ProfileBackedStrategy would lose the
# instructions research callers rely on (cite sources, save outputs as
# markdown), analysis callers rely on (structured tradeoff analysis),
# and code callers rely on ("clean, well-tested code"). The legacy
# markdown files are kept for the deprecated `CodeStrategy` etc.; this
# table is the canonical source for the profile-backed path
# (bot finding on #891 r4).
#
# Each template contains a `{tools}` placeholder that gets filled in
# with `profile.suggested_tools` at render time, so the prompt and the
# tool envelope cannot drift when the YAML adds or removes a tool
# (bot finding on #891 r6).
_PROFILE_GUIDANCE: dict[str, str] = {
    "code": (
        "## Domain guidelines (code profile)\n"
        "- Use the available tools ({tools}) to accomplish each AC.\n"
        "- Write clean, well-tested code that follows project conventions.\n"
        "- Surface blockers clearly instead of working around unverified "
        "preconditions."
    ),
    "research": (
        "## Domain guidelines (research profile)\n"
        "- Use the available tools ({tools}) to gather information from "
        "available sources thoroughly and cross-reference multiple "
        "sources for accuracy.\n"
        "- Synthesize findings into clear, structured markdown documents "
        "saved under docs/ or output/.\n"
        "- Cite sources and provide references where applicable.\n"
        "- Surface blockers clearly instead of fabricating coverage."
    ),
    "analysis": (
        "## Domain guidelines (analysis profile)\n"
        "- Use the available tools ({tools}) to read and understand the "
        "subject matter thoroughly before concluding.\n"
        "- Apply structured analytical frameworks; consider multiple "
        "perspectives and explicit tradeoffs.\n"
        "- Document the analytical process and present findings with "
        "supporting evidence in markdown.\n"
        "- Save analysis outputs as .md files."
    ),
}
_DEFAULT_GUIDANCE_TEMPLATE: str = (
    "## Domain guidelines\n"
    "- Use the available tools ({tools}) to execute each AC thoroughly "
    "and surface blockers explicitly."
)


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
        with a multi-AC-aware evidence directive that names every
        required field and rejection rule from `profile.evidence_schema`
        so the leaf can produce records the H2 validator will accept.

        We deliberately do NOT reuse `phase_wrappers.build_post_block`
        here: that helper says "emit one JSON block, then stop", which
        is correct for a single-dispatch leaf but contradicts the
        per-AC iteration required by the runner's monolithic multi-AC
        path. The stop instruction has system-prompt precedence over
        the task suffix's "continue through every AC" cue, so the run
        would terminate after the first criterion (bot finding on
        #891 r3).
        """
        schema = self.profile.evidence_schema
        required = (
            ", ".join(schema.required)
            if schema.required
            else "(profile declares no required evidence fields)"
        )
        rejected = (
            "; ".join(schema.rejected_if)
            if schema.rejected_if
            else "(profile declares no automatic rejection rules)"
        )
        anchor = (
            f"You are executing acceptance criteria under the "
            f"{self.profile.profile!r} profile.\n"
            f"Decomposition axis: {self.profile.axis}.\n"
            f"Smallest acceptable unit: {self.profile.min_unit}.\n"
            f"The verifier will focus on: {self.profile.verifier_focus.strip()}"
        )
        # Domain guidance preserves the behavior the legacy strategies
        # carried — e.g. research's "cite sources, save as markdown",
        # analysis's "structured tradeoff", code's "clean, well-tested".
        # Tools list is interpolated from profile.suggested_tools so
        # the prompt and the tool envelope cannot drift when the YAML
        # adds or removes a tool. Profiles without a registered
        # guidance block fall back to a minimal generic template.
        tools_csv = (
            ", ".join(self.profile.suggested_tools) if self.profile.suggested_tools else "(none)"
        )
        guidance_template = _PROFILE_GUIDANCE.get(self.profile.profile, _DEFAULT_GUIDANCE_TEMPLATE)
        guidance = guidance_template.format(tools=tools_csv)
        # H2's extract_evidence() consumes exactly ONE fenced JSON
        # object per dispatch. Earlier rounds asked the executor for
        # "one record per AC"; the parser would silently drop every
        # record after the first. Now we ask for ONE consolidated
        # record at the end of the dispatch, with cumulative per-AC
        # evidence populating the required fields.
        #
        # NOTE on blocked ACs: the current harness does not have a
        # structured BLOCKED channel. extract_evidence() / rejected_if
        # / classify() collectively map any missing-or-empty required
        # field to EVIDENCE_MISSING regardless of intent. Encoding a
        # blocker marker in the record (e.g. an empty list) would
        # trigger rejected_if and still surface as EVIDENCE_MISSING,
        # not BLOCKED (bot finding on #891 r7). Routing genuinely
        # BLOCKED ACs through H7 therefore requires a follow-up that
        # adds a `blocked` field (or similar) to EvidenceRecord /
        # ExecutionProfile.evidence_schema and teaches `classify()` to
        # honor it. Until that lands, this prompt does NOT instruct
        # the executor to fabricate blocker markers — it just asks for
        # accurate evidence; H7 will currently degrade legitimate
        # blockers to RETRY, which is suboptimal but safe.
        contract = (
            "[POST — harness-injected; consolidated evidence contract]\n"
            "When you have completed every acceptance criterion above, "
            "emit exactly ONE fenced JSON evidence record at the end "
            "of your response. The record must cover every AC you "
            f"addressed — populate its required fields ({required}) "
            "with the cumulative evidence across all ACs.\n"
            f"Automatic rejection rules: {rejected}.\n"
            "For commands_run, cite only commands that directly validate or "
            "produce the claimed deliverable, such as test, build, lint, generation, "
            "or docs verification commands. Do not include exploratory discovery "
            "commands such as rg, grep, sed, cat, ls, find, or pwd unless that "
            "command is itself the validation required by the current AC.\n"
            "Do not declare DONE in prose — the harness adjudicates via "
            "an external verifier pass. Emit the single evidence record, "
            "then stop."
        )
        return f"{anchor}\n\n{guidance}\n\n{contract}"

    def get_task_prompt_suffix(self) -> str:
        """Compose the [PRE]-style restate / precondition gate.

        The Strategy boundary has no per-AC context — the AC list is
        rendered into the task prompt by `runner.build_task_prompt`
        immediately above this suffix. We therefore phrase the H3 [PRE]
        gate as "for each acceptance criterion above" so the executor
        runs the restatement + precondition pass against the AC list it
        just received.
        """
        # See the system-prompt POST block in get_system_prompt_fragment
        # for why blocked-AC routing through a record marker is not
        # encoded here: the current harness has no structured BLOCKED
        # channel, so any marker would still surface as EVIDENCE_MISSING
        # via classify(). Document the restatement+precondition gate
        # only; blocker handling is a follow-up that lands with the H2
        # schema extension.
        return (
            "[PRE — harness-injected; restate before any action]\n"
            "For each acceptance criterion above, restate it in one "
            "sentence and list every precondition you are assuming "
            "(paths, commands, external services, access tokens). "
            "Verify the preconditions before invoking any tool; if one "
            "cannot be verified, halt that AC and capture the blocker "
            "as part of the evidence record's prose fields so the "
            "operator can act on it.\n\n"
            "When work on all reachable ACs is complete, emit the "
            "single consolidated evidence record described in the "
            "system prompt."
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
