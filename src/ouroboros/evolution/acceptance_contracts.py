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
    semantic key; missing reflected entries cannot silently delete a contract.
    """
    refined = tuple(refined_descriptions)
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
