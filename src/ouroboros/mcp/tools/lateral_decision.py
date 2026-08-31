"""Decision-mode contracts and prompt blocks for lateral_think (RFC D2/D3).

Split out of the grandfathered ``evaluation_handlers.py`` / ``subagent.py``
modules (#1797 ratchet): everything specific to the decision-advisory mode
lives here — the synthesis contract every host receives, the per-persona task
blocks, and the deep-tier evidence contract.
"""

from __future__ import annotations

from typing import Any

from ouroboros.mcp.types import MCPToolParameter, ToolInputType

LATERAL_MODES = ("unstuck", "decision")

# Schema fragments for LateralThinkHandler.definition (kept here so the
# grandfathered evaluation_handlers.py does not grow — #1797 ratchet).
LATERAL_MODE_PARAMETERS: tuple[MCPToolParameter, ...] = (
    MCPToolParameter(
        name="mode",
        type=ToolInputType.STRING,
        description=(
            "'unstuck' (default): break through stagnation. "
            "'decision': personas advise on a consequential choice; "
            "problem_context describes the options, current_approach "
            "the currently favored one (or 'undecided'). The fan-out "
            "synthesis contract then requires ONE recommendation, its "
            "grounds, the strongest dissent, and flip conditions."
        ),
        required=False,
        enum=LATERAL_MODES,
    ),
    MCPToolParameter(
        name="research",
        type=ToolInputType.BOOLEAN,
        description=(
            "Deep tier opt-in: personas ground claims with web "
            "evidence when their runtime exposes web tools, and emit "
            "a machine-readable evidence block. Slower; never the "
            "default. Runtimes without web tools degrade to "
            "opinion-only instead of failing."
        ),
        required=False,
    ),
)

# Decision-mode synthesis contract (grounded-lateral RFC D2), attached to the
# dispatch metadata so every host synthesizes fan-out results the same way. A
# decision advisory that ends as a pile of perspectives leaves an unsure user
# MORE unsure — the debate must converge. Citation honesty mirrors the
# deep-tier evidence contract below: only sources a persona actually fetched
# may be cited, and unverifiable citations are dropped or marked unverified,
# never presented as authority.
DECISION_SYNTHESIS_CONTRACT: dict[str, Any] = {
    "converge_to": [
        "recommendation: exactly ONE recommended option, stated first",
        "grounds: why, citing persona evidence (verified sources only)",
        "dissent: the strongest surviving argument against the recommendation",
        "flip_conditions: concrete conditions under which the recommendation flips",
    ],
    "citation_rule": (
        "Cite only sources personas actually fetched; drop or mark 'unverified' "
        "anything you cannot trace to a fetched URL. Never invent citations."
    ),
    "follow_up": (
        "After the user accepts a recommendation, offer to persist it via "
        "`ouroboros_record_conductor_decision`, and when the session has "
        "settled goal + constraints + success criteria, offer "
        "`ouroboros_generate_seed` directly — no interview needed."
    ),
}

DECISION_INLINE_CONTRACT_TEXT = (
    "\n## Decision output contract\n"
    "Converge — do not stop at perspectives. State exactly one "
    "recommendation first, then its grounds, the strongest "
    "dissent, and the concrete flip conditions under which the "
    "recommendation changes.\n"
)


def stamp_decision_meta(meta: dict[str, Any], mode: str) -> dict[str, Any]:
    """Stamp ``mode`` (and, for decision mode, the synthesis contract) onto meta."""
    meta["mode"] = mode
    if mode == "decision":
        meta["synthesis_contract"] = DECISION_SYNTHESIS_CONTRACT
    return meta


def build_lateral_task_block(persona: str, mode: str) -> str:
    """Per-persona subagent task block for the given lateral mode."""
    if mode == "decision":
        return (
            "## Task for you (subagent)\n"
            f"You are thinking as the **{persona}** persona, advising "
            "on a decision the user must make. The problem context above "
            "describes the choice; the current approach is the option "
            "currently favored (if any). Produce:\n"
            "1. Your position: which option this persona picks, in one line.\n"
            "2. Your strongest argument for it (2-4 bullets).\n"
            "3. The strongest argument AGAINST your own position — steelman "
            "the other side, do not strawman it.\n"
            "4. Flip condition: the concrete condition under which you would "
            "switch to another option.\n\n"
            "Keep it tight. Your output will be weighed against other "
            "personas advising in parallel. Be distinctive — lean hard into "
            "your persona."
        )
    return (
        "## Task for you (subagent)\n"
        f"You are thinking as the **{persona}** persona. Apply the "
        "instructions above to this specific problem. Produce:\n"
        "1. A concrete alternative plan (3-5 bullet steps).\n"
        "2. The single biggest assumption you challenge.\n"
        "3. A one-line verdict: would this plan work? why/why not?\n\n"
        "Keep it tight. Your output will be compared with 4 other personas "
        "thinking in parallel. Be distinctive — lean hard into your persona."
    )


def build_lateral_research_block(research: bool) -> str:
    """Deep-tier evidence contract (RFC D3/D4); empty when research is off."""
    if not research:
        return ""
    return (
        "\n\n## Evidence (deep tier)\n"
        "If your runtime exposes web tools (WebSearch/WebFetch or "
        "equivalent), ground every load-bearing claim in a source you "
        "actually fetched, and end your reply with exactly one fenced "
        "json block of the shape "
        '{"external_sources": ["<url>", ...], "claims": '
        '[{"claim": "<one sentence>", "source": "<url>"}, ...]}. '
        "List only URLs you fetched in this task — never invent or "
        "recall a URL from memory. If you have no web tools, skip this "
        "section entirely and reason from the given context; do not "
        "fabricate sources."
    )


__all__ = [
    "DECISION_INLINE_CONTRACT_TEXT",
    "DECISION_SYNTHESIS_CONTRACT",
    "LATERAL_MODES",
    "build_lateral_research_block",
    "build_lateral_task_block",
    "stamp_decision_meta",
]
