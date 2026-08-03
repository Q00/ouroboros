"""Preserve structured acceptance authority across evolution generations."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from ouroboros.core.seed import AcceptanceCriterionSpec, Seed


class _AcceptancePatch(Protocol):
    @property
    def op(self) -> str: ...

    @property
    def index(self) -> int | None: ...

    @property
    def content(self) -> str | None: ...


def evolve_acceptance_contracts(
    parent_criteria: tuple[AcceptanceCriterionSpec, ...],
    refined_descriptions: Sequence[str],
    refined_patches: Sequence[_AcceptancePatch] = (),
) -> tuple[AcceptanceCriterionSpec, ...]:
    """Apply reflected prose without reducing structured ACs back to strings.

    Reflect intentionally reasons over human-readable descriptions.  Existing
    mechanical verification and investment fields remain authoritative and are
    carried forward by position.  A revised description receives a fresh
    semantic key.  Ambiguous deletion and reordering are rejected before they
    can rebind structured authority to a different criterion.
    """
    refined = tuple(_validated_description(description) for description in refined_descriptions)
    identity_is_explicit = _validate_explicit_patch_identity(
        parent_criteria,
        refined,
        refined_patches,
    )
    _reject_ambiguous_structured_rewrite(
        parent_criteria,
        refined,
        identity_is_explicit=identity_is_explicit,
    )
    evolved: list[AcceptanceCriterionSpec] = []
    for index, parent in enumerate(parent_criteria):
        if index >= len(refined) or refined[index] == parent.description:
            evolved.append(parent)
            continue
        evolved.append(
            AcceptanceCriterionSpec.model_validate(
                {
                    **parent.model_dump(mode="python"),
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


def _validated_description(description: Any) -> str:
    if not isinstance(description, str) or not description.strip():
        raise ValueError("refined acceptance criteria must be non-empty strings")
    stripped = description.strip()
    if any(ord(character) < 32 or ord(character) == 127 for character in stripped):
        raise ValueError("refined acceptance criteria cannot contain control characters")
    return stripped


def _validate_explicit_patch_identity(
    parent_criteria: tuple[AcceptanceCriterionSpec, ...],
    refined_descriptions: tuple[str, ...],
    refined_patches: Sequence[_AcceptancePatch],
) -> bool:
    if not refined_patches:
        return False
    if len(refined_patches) != len(refined_descriptions):
        raise ValueError("ac_patches must identify every refined acceptance criterion")

    parent_count = len(parent_criteria)
    for position, (description, patch) in enumerate(
        zip(refined_descriptions, refined_patches, strict=True)
    ):
        op = getattr(patch, "op", None)
        index = getattr(patch, "index", None)
        content = getattr(patch, "content", None)
        if position < parent_count:
            parent = parent_criteria[position]
            if index != position or op not in ("keep", "revise"):
                raise ValueError("ac_patches do not preserve acceptance criterion identity")
            if op == "keep" and description != parent.description:
                raise ValueError("keep patch changed an acceptance criterion")
            if op == "revise" and _validated_description(content) != description:
                raise ValueError("revise patch content does not match refined_acs")
            continue
        if op != "add" or index is not None or _validated_description(content) != description:
            raise ValueError("add patch does not match the appended refined_acs entry")
    return True


def _reject_ambiguous_structured_rewrite(
    parent_criteria: tuple[AcceptanceCriterionSpec, ...],
    refined_descriptions: tuple[str, ...],
    *,
    identity_is_explicit: bool,
) -> None:
    """Reject legacy prose rewrites that cannot preserve structured identity."""
    has_structured_authority = any(
        criterion.has_success_contract or criterion.investment is not None
        for criterion in parent_criteria
    )
    if not has_structured_authority:
        return

    if identity_is_explicit:
        return

    if len(refined_descriptions) < len(parent_criteria):
        raise ValueError(
            "shorter refined_acs cannot preserve structured acceptance contracts; "
            "explicit stable AC identity is required"
        )

    parent_descriptions = tuple(criterion.description for criterion in parent_criteria)
    changed_count = 0
    for index, description in enumerate(refined_descriptions[: len(parent_criteria)]):
        if description == parent_descriptions[index]:
            continue
        changed_count += 1
        if description in parent_descriptions:
            raise ValueError(
                "refined_acs reorders structured acceptance contracts; "
                "explicit stable AC identity is required"
            )
    if changed_count > 1:
        raise ValueError(
            "multiple structured acceptance contract revisions require explicit stable AC identity"
        )


def evolve_seed_contract_fields(
    parent_seed: Seed,
    refined_descriptions: Sequence[str],
    refined_patches: Sequence[_AcceptancePatch] = (),
) -> dict[str, Any]:
    """Return successor ACs plus thawed plugin-owned Seed fields."""
    serialized_parent = parent_seed.to_dict()
    extra_fields = {key: serialized_parent[key] for key in (parent_seed.model_extra or {})}
    return {
        "acceptance_criteria": evolve_acceptance_contracts(
            parent_seed.acceptance_criteria,
            refined_descriptions,
            refined_patches,
        ),
        **extra_fields,
    }


__all__ = ["evolve_acceptance_contracts", "evolve_seed_contract_fields"]
