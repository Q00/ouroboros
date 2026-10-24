"""Execution contract derived from an immutable Seed.

The Seed remains the frozen source of truth.  This module provides a small
interpretation layer that turns the Seed into semantic parts the runtime can
render consistently during execution without teaching every caller how to read
Seed internals.
"""

from __future__ import annotations

from dataclasses import dataclass

from ouroboros.core.seed import (
    BrownfieldContext,
    EvaluationPrinciple,
    ExitCondition,
    RepoFact,
    RequesterAnswer,
    Seed,
    ac_texts,
)

GOAL_OBJECTIVE_CRITERION = "Resolve the request as stated in the goal."
"""The worker's objective when no acceptance criterion has a confirmed source."""


@dataclass(frozen=True, slots=True)
class OntologyConcept:
    """A concept exposed by the Seed ontology."""

    name: str
    field_type: str
    description: str
    required: bool


@dataclass(frozen=True, slots=True)
class OntologyLens:
    """The Seed ontology interpreted as a conceptual lens."""

    name: str
    description: str
    concepts: tuple[OntologyConcept, ...]

    @property
    def has_concepts(self) -> bool:
        """Return True when the lens names concrete concepts."""
        return bool(self.concepts)


@dataclass(frozen=True, slots=True)
class SeedContract:
    """Runtime-ready semantic contract derived from a Seed."""

    goal: str
    task_type: str
    acceptance_criteria: tuple[str, ...]
    constraints: tuple[str, ...]
    ontology_lens: OntologyLens
    evaluation_principles: tuple[EvaluationPrinciple, ...]
    exit_conditions: tuple[ExitCondition, ...]
    brownfield_context: BrownfieldContext
    assumptions: tuple[str, ...] = ()
    requester_answers: tuple[RequesterAnswer, ...] = ()
    repo_facts: tuple[RepoFact, ...] = ()
    # True when the contract holds only content with a known source
    # (``Seed.sourced_scope``); the renderer then omits every model-written section.
    sourced_only: bool = False

    @classmethod
    def from_seed(cls, seed: Seed) -> SeedContract:
        """Interpret a Seed as an immutable execution contract.

        A Seed with ``sourced_scope`` yields a contract of its sourced content
        only: the user's goal verbatim, the binding constraints and criteria
        (or, with no binding criterion, :data:`GOAL_OBJECTIVE_CRITERION`), the
        requester answers and the repo facts. Every other field stays in the
        Seed and is left out of the contract.
        """
        scope = seed.sourced_scope
        if scope is not None:
            return cls(
                goal=scope.goal,
                task_type=seed.task_type,
                acceptance_criteria=scope.acceptance_criteria or (GOAL_OBJECTIVE_CRITERION,),
                constraints=scope.constraints,
                ontology_lens=OntologyLens(name="", description="", concepts=()),
                evaluation_principles=(),
                exit_conditions=(),
                brownfield_context=BrownfieldContext(),
                requester_answers=seed.requester_answers,
                repo_facts=seed.repo_facts,
                sourced_only=True,
            )
        return cls(
            goal=seed.goal,
            task_type=seed.task_type,
            acceptance_criteria=ac_texts(seed.acceptance_criteria),
            constraints=seed.constraints,
            ontology_lens=OntologyLens(
                name=seed.ontology_schema.name,
                description=seed.ontology_schema.description,
                concepts=tuple(
                    OntologyConcept(
                        name=field.name,
                        field_type=field.field_type,
                        description=field.description,
                        required=field.required,
                    )
                    for field in seed.ontology_schema.fields
                ),
            ),
            evaluation_principles=seed.evaluation_principles,
            exit_conditions=seed.exit_conditions,
            brownfield_context=seed.brownfield_context,
            assumptions=seed.assumptions,
            requester_answers=seed.requester_answers,
            repo_facts=seed.repo_facts,
        )


__all__ = [
    "GOAL_OBJECTIVE_CRITERION",
    "OntologyConcept",
    "OntologyLens",
    "SeedContract",
]
