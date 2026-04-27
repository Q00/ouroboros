"""Tests for Seed contract prompt rendering."""

from __future__ import annotations

from ouroboros.core.seed import OntologyField, OntologySchema, Seed, SeedMetadata
from ouroboros.core.seed_contract import SeedContract
from ouroboros.core.seed_contract_prompt import (
    render_ontology_lens_section,
    render_seed_contract_for_execution,
)


def _seed() -> Seed:
    return Seed(
        goal="Build a task manager",
        constraints=("No external database",),
        acceptance_criteria=("Tasks can be created",),
        ontology_schema=OntologySchema(
            name="TaskManager",
            description="Task management ontology",
            fields=(
                OntologyField(
                    name="tasks",
                    field_type="array",
                    description="List of task objects",
                ),
            ),
        ),
        metadata=SeedMetadata(ambiguity_score=0.1),
    )


def test_seed_contract_from_seed_interprets_ontology_lens() -> None:
    """SeedContract preserves ontology concepts without mutating Seed."""
    contract = SeedContract.from_seed(_seed())

    assert contract.goal == "Build a task manager"
    assert contract.task_type == "code"
    assert contract.acceptance_criteria == ("Tasks can be created",)
    assert contract.ontology_lens.name == "TaskManager"
    assert contract.ontology_lens.description == "Task management ontology"
    assert len(contract.ontology_lens.concepts) == 1
    assert contract.ontology_lens.concepts[0].name == "tasks"
    assert contract.ontology_lens.concepts[0].field_type == "array"


def test_render_ontology_lens_section_frames_ontology_as_lens() -> None:
    """Ontology rendering states how execution agents should use concepts."""
    contract = SeedContract.from_seed(_seed())

    section = render_ontology_lens_section(contract.ontology_lens)

    assert "## Ontology / Conceptual Lens" in section
    assert "conceptual lens for execution decisions" in section
    assert "It is not a mandatory output outline." in section
    assert "- tasks [array]: List of task objects (required concept)" in section
    assert "Do not introduce concepts that contradict the ontology." in section
    assert "Do not force the final artifact to mirror these fields" in section
    assert "Required concepts must remain represented" in section


def test_render_seed_contract_for_execution_includes_core_sections() -> None:
    """Full contract renderer includes goal, constraints, ontology, and evaluation."""
    contract = SeedContract.from_seed(_seed())

    rendered = render_seed_contract_for_execution(contract)

    assert "## Seed Contract" in rendered
    assert "## Goal" in rendered
    assert "Build a task manager" in rendered
    assert "## Task Type" in rendered
    assert "## Acceptance Criteria" not in rendered
    assert "## Constraints" in rendered
    assert "- No external database" in rendered
    assert "## Ontology / Conceptual Lens" in rendered
    assert "## Exit Conditions" in rendered
