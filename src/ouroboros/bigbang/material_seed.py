"""Attach validated opaque local material references after requirement extraction."""

from typing import TYPE_CHECKING

from ouroboros.core.seed import Seed
from ouroboros.interview_adapters.material_evidence import evidence_refs

if TYPE_CHECKING:
    from ouroboros.bigbang.interview import InterviewState


def attach_material_references(seed: Seed, state: "InterviewState") -> Seed:
    refs = evidence_refs(state.inline_material, state.interview_id)
    return (
        Seed.from_dict(
            {
                **seed.to_dict(),
                "material_evidence_refs": [ref.model_dump(mode="json") for ref in refs],
            }
        )
        if refs
        else seed
    )
