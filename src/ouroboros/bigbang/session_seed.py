"""Interview-less Seed crystallization from host-settled session context.

Grounded-lateral RFC D6: when a session has already settled the key decisions
(after a decision-mode lateral run, or plain conversational convergence), the
host offers to crystallize that material into a Seed without routing through
the interview. A Seed is valuable even if it is never run — it is a reviewable
spec artifact, an acceptance-criteria checklist, and a publishable contract.

Determinism contract (RFC D6, owner-approved):

1. *Settled inputs are anchored verbatim.* The host-supplied goal,
   constraints, decisions, and acceptance criteria enter the Seed
   byte-for-byte — no LLM touches this path at all, which makes it the
   strongest form of the anchoring rule: composition here IS deterministic.
2. *Acceptance is deterministic.* The gate is structural completeness — a
   goal and at least one acceptance criterion — and a submission that fails
   it gets back the specific gap questions (never more than the two gaps
   that exist), not a blocked/force binary.
3. *The recorded ambiguity score is a conservative ceiling.* There is no
   trusted LLM score on this path and a caller-supplied number would be
   exactly the gate-override #210 removed, so the metadata records the 0.2
   pass-boundary ceiling unconditionally: honest ("no better than the
   threshold"), deterministic, and ungameable.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ouroboros.core.seed import (
    BrownfieldContext,
    EvaluationPrinciple,
    ExitCondition,
    OntologySchema,
    Seed,
    SeedMetadata,
)

SESSION_CONTEXT_AMBIGUITY_CEILING = 0.2
SESSION_CONTEXT_INTERVIEW_ID = "session-context"

GOAL_GAP_QUESTION = (
    "What single outcome should this work produce? One sentence, concrete "
    "enough that a reviewer could say yes/no to 'is this achieved?'."
)
CRITERIA_GAP_QUESTION = (
    "What observable checks prove it is done? List 1-5 acceptance criteria, "
    "each one verifiable (a command, a visible behavior, or a measurable state)."
)


@dataclass(frozen=True, slots=True)
class SessionSeedOutcome:
    """Either a crystallized Seed or the exact gap questions blocking one."""

    seed: Seed | None
    gap_questions: tuple[str, ...]


def _string_tuple(raw: Any) -> tuple[str, ...]:
    """Normalize a host-supplied list into verbatim, non-blank strings.

    Strip-only: whitespace trimming is the entire transformation. Anything
    else would violate the verbatim-anchor rule.
    """
    if isinstance(raw, str):
        value = raw.strip()
        return (value,) if value else ()
    if isinstance(raw, (list, tuple)):
        return tuple(
            item for item in (str(entry).strip() for entry in raw if entry is not None) if item
        )
    return ()


def build_session_context_seed(session_context: Mapping[str, Any]) -> SessionSeedOutcome:
    """Crystallize host-settled context into a Seed, or return gap questions.

    Recognized keys: ``goal`` (string), ``acceptance_criteria`` (list),
    ``constraints`` (list), ``decisions`` (list — settled decisions constrain
    the solution space, so they land in constraints, verbatim and
    deduplicated), ``project_type`` (``greenfield``/``brownfield``).
    Unknown keys are ignored.
    """
    goal = str(session_context.get("goal") or "").strip()
    criteria = _string_tuple(session_context.get("acceptance_criteria"))
    constraints = _string_tuple(session_context.get("constraints"))
    decisions = _string_tuple(session_context.get("decisions"))

    gap_questions: list[str] = []
    if not goal:
        gap_questions.append(GOAL_GAP_QUESTION)
    if not criteria:
        gap_questions.append(CRITERIA_GAP_QUESTION)
    if gap_questions:
        return SessionSeedOutcome(seed=None, gap_questions=tuple(gap_questions))

    project_type = str(session_context.get("project_type") or "greenfield").strip()
    if project_type not in ("greenfield", "brownfield"):
        project_type = "greenfield"

    seed = Seed(
        goal=goal,
        constraints=tuple(dict.fromkeys((*constraints, *decisions))),
        acceptance_criteria=criteria,
        ontology_schema=OntologySchema(
            name="SessionSettledContract",
            description=(
                "Requirements the session already settled, crystallized "
                "without an interview. Every entry is verbatim host-supplied "
                "material — no LLM composed any part of this Seed."
            ),
        ),
        evaluation_principles=(
            EvaluationPrinciple(
                name="settled_requirements",
                description="Evaluate only the session-settled requirements.",
                weight=1.0,
            ),
        ),
        exit_conditions=(
            ExitCondition(
                name="settled_requirements_met",
                description="All session-settled requirements are satisfied.",
                evaluation_criteria="Every listed acceptance criterion passes.",
            ),
        ),
        brownfield_context=BrownfieldContext(project_type=project_type),
        metadata=SeedMetadata(
            ambiguity_score=SESSION_CONTEXT_AMBIGUITY_CEILING,
            interview_id=SESSION_CONTEXT_INTERVIEW_ID,
        ),
    )
    return SessionSeedOutcome(seed=seed, gap_questions=())


__all__ = [
    "CRITERIA_GAP_QUESTION",
    "GOAL_GAP_QUESTION",
    "SESSION_CONTEXT_AMBIGUITY_CEILING",
    "SESSION_CONTEXT_INTERVIEW_ID",
    "SessionSeedOutcome",
    "build_session_context_seed",
]
