"""Tests for post-execution QA quality bar derivation."""

from __future__ import annotations

from ouroboros.core.seed import OntologyField, OntologySchema, Seed, SeedMetadata
from ouroboros.mcp.tools.execution_handlers import ExecuteSeedHandler


def test_derive_quality_bar_uses_full_seed_contract() -> None:
    """Post-run QA should judge ACs through the same Seed ontology lens."""
    seed = Seed(
        goal="Write a concise research memo",
        constraints=("Do not produce source code",),
        acceptance_criteria=("Memo answers the research question",),
        ontology_schema=OntologySchema(
            name="ResearchMemo",
            description="Research memo concepts",
            fields=(
                OntologyField(
                    name="claim",
                    field_type="string",
                    description="Central answer to evaluate",
                ),
            ),
        ),
        metadata=SeedMetadata(ambiguity_score=0.1),
    )

    quality_bar = ExecuteSeedHandler._derive_quality_bar(seed)

    assert "preserving the Seed contract" in quality_bar
    assert "## Seed Contract" in quality_bar
    assert "conceptual lens for evaluation judgments" in quality_bar
    assert "- claim [string]: Central answer to evaluate (required concept)" in quality_bar
    assert "Do not produce source code" in quality_bar
    assert "- Memo answers the research question" in quality_bar
