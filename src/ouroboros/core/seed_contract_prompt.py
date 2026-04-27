"""Prompt rendering for Seed semantic contracts."""

from __future__ import annotations

from ouroboros.core.seed_contract import OntologyLens, SeedContract


def render_constraints_section(contract: SeedContract) -> str:
    """Render hard boundaries from the Seed contract."""
    constraints_text = (
        "\n".join(f"- {constraint}" for constraint in contract.constraints)
        if contract.constraints
        else "None"
    )
    return f"""## Constraints
{constraints_text}"""


def render_brownfield_section(contract: SeedContract) -> str:
    """Render existing-codebase boundaries from the Seed contract."""
    context = contract.brownfield_context
    if context.project_type != "brownfield":
        return ""

    refs = "\n".join(
        f"- [{reference.role.upper()}] {reference.path}: {reference.summary}"
        for reference in context.context_references
    )
    patterns = "\n".join(f"- {pattern}" for pattern in context.existing_patterns)
    deps = ", ".join(context.existing_dependencies)

    return f"""## Existing Codebase Context (BROWNFIELD)
IMPORTANT: You are extending existing code, NOT creating a new project.

### Referenced Codebases
{refs or "None specified"}

### Existing Patterns to Follow
{patterns or "None specified"}

### Existing Dependencies to Reuse
{deps or "None specified"}"""


def render_ontology_lens_section(
    lens: OntologyLens,
    *,
    decision_context: str = "execution decisions",
) -> str:
    """Render the Seed ontology as a phase-specific conceptual lens."""
    lines = [
        "## Ontology / Conceptual Lens",
        (
            f"Use this ontology as the conceptual lens for {decision_context}. "
            "It defines the Seed's domain concepts and the coherence that must be "
            "preserved while satisfying the goal and acceptance criteria. "
            "It is not a mandatory output outline."
        ),
        "",
        f"Name: {lens.name}",
        f"Description: {lens.description}",
    ]

    if lens.has_concepts:
        lines.append("")
        lines.append("Concepts:")
        for concept in lens.concepts:
            required = "required concept" if concept.required else "optional concept"
            lines.append(
                f"- {concept.name} [{concept.field_type}]: {concept.description} ({required})"
            )

    lines.extend(
        [
            "",
            f"When {decision_context} are ambiguous:",
            "- Preserve this ontology's concepts and boundaries.",
            "- Do not introduce concepts that contradict the ontology.",
            "- Prefer choices that make the result closer to the Seed's intended outcome.",
            "- Do not force the final artifact to mirror these fields unless the task asks for that structure.",
            "- Treat missing optional concepts as acceptable unless the Seed goal, constraints, or ACs require them.",
            "- Required concepts must remain represented in the reasoning, artifact, or validation evidence.",
        ]
    )
    return "\n".join(lines)


def render_evaluation_principles_section(contract: SeedContract) -> str:
    """Render evaluation principles from the Seed contract."""
    principles_text = (
        "\n".join(
            f"- {principle.name}: {principle.description}"
            for principle in contract.evaluation_principles
        )
        if contract.evaluation_principles
        else "None"
    )
    return f"""## Evaluation Principles
{principles_text}"""


def render_acceptance_criteria_section(contract: SeedContract) -> str:
    """Render acceptance criteria from the Seed contract."""
    criteria_text = (
        "\n".join(
            f"{index + 1}. {criterion}"
            for index, criterion in enumerate(contract.acceptance_criteria)
        )
        if contract.acceptance_criteria
        else "None"
    )
    return f"""## Acceptance Criteria
{criteria_text}"""


def render_exit_conditions_section(contract: SeedContract) -> str:
    """Render exit conditions from the Seed contract."""
    conditions_text = (
        "\n".join(
            f"- {condition.name}: {condition.description} ({condition.evaluation_criteria})"
            for condition in contract.exit_conditions
        )
        if contract.exit_conditions
        else "None"
    )
    return f"""## Exit Conditions
{conditions_text}"""


def render_seed_contract_for_execution(contract: SeedContract) -> str:
    """Render the Seed contract into runtime-facing execution instructions."""
    sections = [
        "## Seed Contract",
        "The Seed is the immutable source of truth for this execution. Interpret every execution decision through this contract.",
        "",
        "## Goal",
        contract.goal,
        "",
        "## Task Type",
        contract.task_type,
        "",
        render_constraints_section(contract),
    ]

    brownfield = render_brownfield_section(contract)
    if brownfield:
        sections.extend(["", brownfield])

    sections.extend(
        [
            "",
            render_ontology_lens_section(contract.ontology_lens),
            "",
            render_evaluation_principles_section(contract),
            "",
            render_exit_conditions_section(contract),
        ]
    )
    return "\n".join(sections)


__all__ = [
    "render_brownfield_section",
    "render_acceptance_criteria_section",
    "render_constraints_section",
    "render_evaluation_principles_section",
    "render_exit_conditions_section",
    "render_ontology_lens_section",
    "render_seed_contract_for_execution",
]
