"""Prompt rendering for Seed semantic contracts."""

from __future__ import annotations

from ouroboros.core.conductor import ConductorDirective
from ouroboros.core.seed import Seed, SourcedScope
from ouroboros.core.seed_contract import GOAL_OBJECTIVE_CRITERION, OntologyLens, SeedContract

AUTO_RECURSION_GUARD = """## Auto Recursion Guard
Do not invoke `ooo auto`, `ouroboros_auto`, `ouroboros_start_auto`, or any MCP auto
tool while executing this Seed. This execution is already downstream of any auto
authoring step. Implement the concrete Seed requirements directly; if evidence
about a prior auto run is needed, inspect existing artifacts/logs only and never
start a nested auto session."""


def render_auto_recursion_guard() -> str:
    """Render the execution-session guard that prevents nested auto dispatch."""
    return AUTO_RECURSION_GUARD


def render_constraints_section(contract: SeedContract) -> str:
    """Render hard boundaries from the Seed contract."""
    constraints_text = (
        "\n".join(f"- {constraint}" for constraint in contract.constraints)
        if contract.constraints
        else "None"
    )
    return f"""## Constraints
{constraints_text}"""


def render_requester_answers_section(contract: SeedContract) -> str:
    """Render the requester's answers verbatim, as quoted questions and answers."""
    if not contract.requester_answers:
        return ""
    pairs = "\n\n".join(
        (f"Question:\n{_quoted(item.question)}\n" if item.question.strip() else "")
        + f"Answer:\n{_quoted(item.answer)}"
        for item in contract.requester_answers
    )
    return f"""## Requester answers (confirmed)
The person who made the request answered these questions. Each question and answer
is quoted exactly, line by line after "> ". Follow what the answers say; do not read
more into an answer than its words say.

{pairs}"""


def render_repo_facts_section(contract: SeedContract) -> str:
    """Render checked repository facts as the code's current behavior, with quoted evidence."""
    if not contract.repo_facts:
        return ""
    items = []
    for item in contract.repo_facts:
        text = f"Question:\n{_quoted(item.question)}\n" if item.question.strip() else ""
        text += f"Answer:\n{_quoted(item.answer)}"
        if item.evidence:
            text += "\nEvidence:\n" + "\n".join(f"- {span}" for span in item.evidence)
        items.append(text)
    pairs = "\n\n".join(items)
    return f"""## Current behavior of the code (checked)
Auto read the repository to answer these interview questions. Each answer describes
what the code does now and cites path:line spans whose quotes were checked against
the files. The issue reports a defect, so this behavior may be exactly what has to
change; use it to understand the code, and follow the goal, the constraints and the
acceptance criteria for what to change.

{pairs}"""


def _quoted(text: str) -> str:
    return "\n".join(f"> {line}".rstrip() for line in text.strip().splitlines())


def render_assumptions_section(contract: SeedContract) -> str:
    """Render unconfirmed assumptions, kept apart from the binding constraints."""
    if not contract.assumptions:
        return ""
    assumptions_text = "\n".join(f"- {assumption}" for assumption in contract.assumptions)
    return f"""## Assumptions (non-binding)
Nobody confirmed these. They are guesses on the requester's behalf,
not requirements. An assumption is a default only where the code itself settles nothing;
follow the issue and the code over any assumption, and override the assumption when they
say otherwise.
{assumptions_text}"""


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

    requester_answers = render_requester_answers_section(contract)
    if requester_answers:
        sections.extend(["", requester_answers])

    repo_facts = render_repo_facts_section(contract)
    if repo_facts:
        sections.extend(["", repo_facts])

    if contract.sourced_only:
        # Only content with a known source reaches the worker; the Seed's
        # model-written sections stay in the Seed (``Seed.sourced_scope``).
        return "\n".join(sections)

    assumptions = render_assumptions_section(contract)
    if assumptions:
        sections.extend(["", assumptions])

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


def render_sourced_objective(scope: SourcedScope) -> str:
    """The worker's task text for a Seed with ``sourced_scope``.

    The binding criteria verbatim, or, when none binds, the request itself
    (:data:`GOAL_OBJECTIVE_CRITERION`), never a model-written criterion.
    """
    criteria = scope.acceptance_criteria or (GOAL_OBJECTIVE_CRITERION,)
    if len(criteria) == 1:
        return criteria[0]
    return "\n".join(f"{index}. {criterion}" for index, criterion in enumerate(criteria, 1))


def _render_conductor_directive(seed: Seed) -> str:
    """Render audited successor-only context without rewriting the Seed contract."""
    raw_directive = (seed.model_extra or {}).get("conductor_directive")
    if raw_directive is None:
        return ""
    if not isinstance(raw_directive, dict):
        raise ValueError("Seed conductor_directive must be a structured object")
    directive = ConductorDirective.from_mapping(raw_directive)
    reasons = (
        "\n".join(f"- {reason}" for reason in directive.rejected_reasons)
        if directive.rejected_reasons
        else "None recorded."
    )
    return f"""## Active Conductor Successor Directive
This is bounded additive context for a successor execution. The Seed above remains
the source of truth. Do not weaken or silently replace its approved direction.

Instruction: {directive.instruction}
Rejected evidence reasons:
{reasons}

Preservation contract:
- goal: {str(directive.preserve_goal).lower()}
- acceptance criteria: {str(directive.preserve_acceptance_criteria).lower()}
- constraints: {str(directive.preserve_constraints).lower()}
- non-goals: {str(directive.preserve_non_goals).lower()}

Re-check the affected implementation and verification evidence, then report the
specific correction made for this directive."""


__all__ = [
    "AUTO_RECURSION_GUARD",
    "render_assumptions_section",
    "render_auto_recursion_guard",
    "render_brownfield_section",
    "render_acceptance_criteria_section",
    "render_constraints_section",
    "render_evaluation_principles_section",
    "render_exit_conditions_section",
    "render_ontology_lens_section",
    "render_repo_facts_section",
    "render_requester_answers_section",
    "render_seed_contract_for_execution",
    "render_sourced_objective",
]
