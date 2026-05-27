"""Deterministic Seed synthesis from an auto Seed Draft Ledger."""

from __future__ import annotations

import re
from uuid import uuid4

from ouroboros.auto.grading import deterministic_floor
from ouroboros.auto.ledger import LedgerStatus, SeedDraftLedger
from ouroboros.core.seed import (
    EvaluationPrinciple,
    ExitCondition,
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
)

_INACTIVE_STATUSES: frozenset[LedgerStatus] = frozenset(
    {
        LedgerStatus.WEAK,
        LedgerStatus.CONFLICTING,
        LedgerStatus.BLOCKED,
        LedgerStatus.MISSING,
    }
)


def synthesize_seed_from_ledger(
    ledger: SeedDraftLedger,
    *,
    interview_id: str | None = None,
) -> Seed:
    """Build a valid Seed directly from a complete auto ledger.

    This is the fail-soft path for ``ooo auto`` when the authoring backend is
    unavailable after the deterministic ledger has enough evidence to proceed.
    The ledger remains the source of truth; this function performs no model or
    external IO.
    """
    if not ledger.is_seed_ready():
        msg = f"cannot synthesize Seed from incomplete ledger: {', '.join(ledger.open_gaps())}"
        raise ValueError(msg)

    goal = _latest_value(ledger, "goal")
    constraints = _lines_from_section(ledger, "constraints")
    non_goals = _lines_from_section(ledger, "non_goals")
    if non_goals:
        constraints = (*constraints, *[f"Non-goal: {item}" for item in non_goals])

    acceptance_criteria = _lines_from_section(ledger, "acceptance_criteria")
    verification_plan = _joined_section(ledger, "verification_plan")
    failure_modes = _joined_section(ledger, "failure_modes")

    return Seed(
        goal=goal,
        constraints=constraints,
        acceptance_criteria=acceptance_criteria,
        ontology_schema=OntologySchema(
            name=_ontology_name(goal),
            description="Auto-synthesized ontology from the completed Seed Draft Ledger.",
            fields=(
                OntologyField(
                    name="actors",
                    field_type="string",
                    description=_joined_section(ledger, "actors"),
                ),
                OntologyField(
                    name="inputs",
                    field_type="string",
                    description=_joined_section(ledger, "inputs"),
                ),
                OntologyField(
                    name="outputs",
                    field_type="string",
                    description=_joined_section(ledger, "outputs"),
                ),
                OntologyField(
                    name="runtime_context",
                    field_type="string",
                    description=_joined_section(ledger, "runtime_context"),
                ),
            ),
        ),
        evaluation_principles=(
            EvaluationPrinciple(
                name="ledger_completeness",
                description="All required Seed Draft Ledger sections are resolved before execution.",
                weight=0.5,
            ),
            EvaluationPrinciple(
                name="observable_verification",
                description=verification_plan,
                weight=0.5,
            ),
        ),
        exit_conditions=(
            ExitCondition(
                name="acceptance_verified",
                description="All acceptance criteria pass under the verification plan.",
                evaluation_criteria=verification_plan,
            ),
            ExitCondition(
                name="failure_modes_absent",
                description=f"Known failure modes are absent: {failure_modes}",
                evaluation_criteria="No listed failure mode is observed in verification evidence.",
            ),
        ),
        metadata=SeedMetadata(
            seed_id=f"seed_{uuid4().hex[:12]}",
            ambiguity_score=max(0.05, deterministic_floor(ledger)),
            interview_id=interview_id,
        ),
    )


def _latest_value(ledger: SeedDraftLedger, section_name: str) -> str:
    values = _active_values(ledger, section_name)
    if not values:
        msg = f"ledger section has no active value: {section_name}"
        raise ValueError(msg)
    return values[-1]


def _joined_section(ledger: SeedDraftLedger, section_name: str) -> str:
    return "; ".join(_active_values(ledger, section_name))


def _lines_from_section(ledger: SeedDraftLedger, section_name: str) -> tuple[str, ...]:
    lines: list[str] = []
    for value in _active_values(ledger, section_name):
        parts = re.split(r"(?:\r?\n\s*[-*]\s+|\s*;\s+)", value)
        for part in parts:
            cleaned = part.strip(" \t\r\n-*:;.")
            if cleaned:
                lines.append(cleaned)
    return tuple(dict.fromkeys(lines))


def _active_values(ledger: SeedDraftLedger, section_name: str) -> tuple[str, ...]:
    section = ledger.sections.get(section_name)
    if section is None:
        return ()
    return tuple(
        entry.value.strip()
        for entry in section.entries
        if entry.status not in _INACTIVE_STATUSES and entry.value.strip()
    )


def _ontology_name(goal: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", goal.title())
    name = "".join(words[:4]) or "AutoTask"
    if not name[0].isalpha():
        name = f"Auto{name}"
    return name[:64]
