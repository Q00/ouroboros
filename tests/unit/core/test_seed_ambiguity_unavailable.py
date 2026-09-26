"""``SeedMetadata.ambiguity_score`` may be recorded as unavailable (``None``).

The default stays 0.15 so existing Seeds and Seed digests are unchanged; an
explicit ``None`` round-trips as null and is never replaced by a number.
"""

from __future__ import annotations

import yaml

from ouroboros.auto.grading import GradeGate
from ouroboros.core.seed import (
    OntologySchema,
    Seed,
    SeedMetadata,
    format_ambiguity_score,
)


def _seed(score: float | None) -> Seed:
    return Seed(
        goal="Fix the add function",
        constraints=("Python 3.12",),
        acceptance_criteria=("add(2, 3) returns 5",),
        ontology_schema=OntologySchema(name="calc", description="calculator"),
        metadata=SeedMetadata(seed_id="seed_fixed", ambiguity_score=score),
    )


def test_default_score_is_unchanged() -> None:
    assert SeedMetadata().ambiguity_score == 0.15


def test_unavailable_score_round_trips_as_null() -> None:
    seed = _seed(None)
    data = seed.to_dict()

    assert data["metadata"]["ambiguity_score"] is None
    reloaded = Seed.from_dict(yaml.safe_load(yaml.safe_dump(data)))
    assert reloaded.metadata.ambiguity_score is None
    assert reloaded == seed


def test_display_says_unavailable_instead_of_a_number() -> None:
    assert format_ambiguity_score(None) == "unavailable"
    assert format_ambiguity_score(0.1234) == "0.12"


def test_grading_records_unavailable_score_without_blocking_or_synthesizing() -> None:
    result = GradeGate().grade_seed(_seed(None))

    codes = [finding.code for finding in result.findings]
    assert "ambiguity_score_unavailable" in codes
    assert all(blocker.code != "high_ambiguity_score" for blocker in result.blockers)


def test_grading_still_blocks_a_high_numeric_score() -> None:
    result = GradeGate().grade_seed(_seed(0.45))

    assert any(blocker.code == "high_ambiguity_score" for blocker in result.blockers)
