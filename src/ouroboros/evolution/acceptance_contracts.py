"""Preserve structured acceptance authority across evolution generations."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ouroboros.core.seed import AcceptanceCriterionSpec, Seed


def evolve_acceptance_contracts(
    parent_criteria: tuple[AcceptanceCriterionSpec, ...],
    refined_descriptions: Sequence[str],
) -> tuple[AcceptanceCriterionSpec, ...]:
    """Apply reflected prose without reducing structured ACs back to strings.

    Reflect intentionally reasons over human-readable descriptions.  Existing
    mechanical verification and investment fields remain authoritative and are
    carried forward by position.  A revised description receives a fresh
    semantic key.  Ambiguous deletion and reordering are rejected before they
    can rebind structured authority to a different criterion.
    """
    refined = tuple(refined_descriptions)
    _reject_ambiguous_structured_rewrite(parent_criteria, refined)
    evolved: list[AcceptanceCriterionSpec] = []
    for index, parent in enumerate(parent_criteria):
        if index >= len(refined) or refined[index] == parent.description:
            evolved.append(parent)
            continue
        evolved.append(
            parent.model_copy(
                update={
                    "description": refined[index],
                    "semantic_ac_key": None,
                }
            )
        )
    evolved.extend(
        AcceptanceCriterionSpec(description=description)
        for description in refined[len(parent_criteria) :]
    )
    return tuple(evolved)


def _reject_ambiguous_structured_rewrite(
    parent_criteria: tuple[AcceptanceCriterionSpec, ...],
    refined_descriptions: tuple[str, ...],
) -> None:
    """Reject legacy prose rewrites that cannot preserve structured identity."""
    has_structured_authority = any(
        criterion.has_success_contract or criterion.investment is not None
        for criterion in parent_criteria
    )
    if not has_structured_authority:
        return

    if len(refined_descriptions) < len(parent_criteria):
        raise ValueError(
            "shorter refined_acs cannot preserve structured acceptance contracts; "
            "explicit stable AC identity is required"
        )

    parent_descriptions = tuple(criterion.description for criterion in parent_criteria)
    for index, description in enumerate(refined_descriptions[: len(parent_criteria)]):
        if description == parent_descriptions[index]:
            continue
        if description in parent_descriptions:
            raise ValueError(
                "refined_acs reorders structured acceptance contracts; "
                "explicit stable AC identity is required"
            )


def evolve_seed_contract_fields(
    parent_seed: Seed,
    refined_descriptions: Sequence[str],
) -> dict[str, Any]:
    """Return successor ACs plus thawed plugin-owned Seed fields."""
    serialized_parent = parent_seed.to_dict()
    extra_fields = {key: serialized_parent[key] for key in (parent_seed.model_extra or {})}
    return {
        "acceptance_criteria": evolve_acceptance_contracts(
            parent_seed.acceptance_criteria,
            refined_descriptions,
        ),
        **extra_fields,
    }


__all__ = ["evolve_acceptance_contracts", "evolve_seed_contract_fields"]
