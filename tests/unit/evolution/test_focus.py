"""Focused evolution selects a shrinking, evidence-backed AC working set."""

import pytest

from ouroboros.core.lineage import ACResult, EvaluationSummary
from ouroboros.core.seed import (
    AcceptanceCriterionSpec,
    InvestmentSpec,
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
)
from ouroboros.events.lineage import lineage_created, lineage_generation_completed
from ouroboros.evolution.focus import select_evolution_focus
from ouroboros.evolution.projector import LineageProjector
from ouroboros.evolution.regression import RegressionReport
from ouroboros.evolution.wonder import GroundedQuestion, WonderOutput


def _seed(*acs: str | AcceptanceCriterionSpec) -> Seed:
    return Seed(
        metadata=SeedMetadata(ambiguity_score=0.1),
        goal="Build the product",
        constraints=("keep scope",),
        acceptance_criteria=acs,
        ontology_schema=OntologySchema(
            name="product",
            description="product",
            fields=(OntologyField(name="item", field_type="entity", description="item"),),
        ),
    )


def _evaluation(seed: Seed, *passed: bool) -> EvaluationSummary:
    return EvaluationSummary(
        final_approved=all(passed),
        highest_stage_passed=2,
        score=sum(passed) / len(passed),
        ac_results=tuple(
            ACResult(
                ac_index=index,
                ac_content=seed.acceptance_criteria[index].description,
                semantic_ac_key=seed.acceptance_criteria[index].semantic_ac_key,
                passed=value,
            )
            for index, value in enumerate(passed)
        ),
    )


def test_only_failed_node_stays_active() -> None:
    seed = _seed("passed 0", "failed 1", "passed 2")

    focus = select_evolution_focus(
        seed,
        seed,
        _evaluation(seed, True, False, True),
        regression_report=RegressionReport(),
    )

    assert focus.active_ac_indices == (1,)
    assert focus.frozen_ac_indices == (0, 2)
    assert set(focus.externally_satisfied_acs()) == {0, 2}


def test_not_evaluated_pass_is_active_not_frozen() -> None:
    seed = _seed("unknown 0", "failed 1")
    evaluation = EvaluationSummary(
        final_approved=False,
        highest_stage_passed=1,
        ac_results=(
            ACResult(
                ac_index=0,
                ac_content="unknown 0",
                passed=True,
                ac_verdict_state="not_evaluated",
            ),
            ACResult(ac_index=1, ac_content="failed 1", passed=False),
        ),
    )

    focus = select_evolution_focus(seed, seed, evaluation)

    assert focus.active_ac_indices == (0, 1)
    assert focus.frozen_ac_indices == ()


def test_unknown_node_stays_active_while_authoritative_pass_freezes() -> None:
    seed = _seed("passed 0", "unknown 1")
    evaluation = EvaluationSummary(
        final_approved=False,
        highest_stage_passed=1,
        ac_results=(
            ACResult(ac_index=0, ac_content="passed 0", passed=True),
            ACResult(
                ac_index=1,
                ac_content="unknown 1",
                passed=True,
                ac_verdict_state="not_evaluated",
            ),
        ),
    )

    focus = select_evolution_focus(seed, seed, evaluation)

    assert focus.active_ac_indices == (1,)
    assert focus.frozen_ac_indices == (0,)


def test_challenge_reopens_a_previously_passing_node() -> None:
    seed = _seed("passed but challenged", "failed")
    wonder = WonderOutput(
        questions=("Challenge AC 1",),
        grounded_questions=(
            GroundedQuestion(
                question="Challenge AC 1",
                kind="challenge",
                ac_indices=(0,),
            ),
        ),
    )

    focus = select_evolution_focus(
        seed,
        seed,
        _evaluation(seed, True, False),
        wonder=wonder,
        regression_report=RegressionReport(),
    )

    assert focus.active_ac_indices == (0, 1)
    assert focus.frozen_ac_indices == ()


def test_new_candidate_node_is_active_not_silently_frozen() -> None:
    parent = _seed("passed", "failed")
    candidate = _seed("passed", "failed revised", "new")

    focus = select_evolution_focus(
        parent,
        candidate,
        _evaluation(parent, True, False),
        regression_report=RegressionReport(),
    )

    assert focus.active_ac_indices == (1, 2)
    assert focus.frozen_ac_indices == (0,)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("description", "passed with revised prose"),
        ("semantic_ac_key", "ac_0000000000000000"),
        ("verify_command", "python -m pytest new.py"),
        ("expected_artifacts", ("new-report.json",)),
        ("output_assertion", "2 passed"),
        (
            "investment",
            InvestmentSpec(
                difficulty="medium",
                stakes="low",
                provenance="declared",
                confidence="low",
            ),
        ),
        (
            "investment",
            InvestmentSpec(
                difficulty="low",
                stakes="medium",
                provenance="declared",
                confidence="low",
            ),
        ),
        (
            "investment",
            InvestmentSpec(
                difficulty="low",
                stakes="low",
                provenance="measured",
                confidence="low",
            ),
        ),
        (
            "investment",
            InvestmentSpec(
                difficulty="low",
                stakes="low",
                provenance="declared",
                confidence="high",
            ),
        ),
    ],
    ids=(
        "description",
        "semantic-key",
        "verify-command",
        "expected-artifacts",
        "output-assertion",
        "investment-difficulty",
        "investment-stakes",
        "investment-provenance",
        "investment-confidence",
    ),
)
def test_any_structured_contract_change_reopens_prior_pass(
    field: str,
    value: object,
) -> None:
    parent = _seed(
        AcceptanceCriterionSpec(
            description="passed",
            verify_command="python -m pytest old.py",
            expected_artifacts=("old-report.json",),
            output_assertion="1 passed",
            investment=InvestmentSpec(
                difficulty="low",
                stakes="low",
                provenance="declared",
                confidence="low",
            ),
        ),
        "failed",
    )
    revised = parent.acceptance_criteria[0].model_copy(update={field: value})
    candidate = _seed(revised, parent.acceptance_criteria[1])

    focus = select_evolution_focus(
        parent,
        candidate,
        _evaluation(parent, True, False),
        regression_report=RegressionReport(),
    )

    assert focus.active_ac_indices == (0, 1)
    assert focus.frozen_ac_indices == ()


def test_missing_per_ac_evidence_keeps_full_graph_open() -> None:
    seed = _seed("one", "two")
    evaluation = EvaluationSummary(
        final_approved=False,
        highest_stage_passed=1,
        score=0.0,
    )

    focus = select_evolution_focus(seed, seed, evaluation)

    assert focus.active_ac_indices == (0, 1)
    assert focus.frozen_ac_indices == ()


@pytest.mark.parametrize(
    "results",
    [
        (ACResult(ac_index=0, ac_content="one", passed=True),),
        (
            ACResult(ac_index=0, ac_content="one", passed=True),
            ACResult(ac_index=0, ac_content="one", passed=False),
        ),
        (
            ACResult(ac_index=0, ac_content="one", passed=True),
            ACResult(ac_index=2, ac_content="outside", passed=False),
        ),
        (
            ACResult(ac_index=0, ac_content="not one", passed=True),
            ACResult(ac_index=1, ac_content="two", passed=False),
        ),
    ],
    ids=("missing", "duplicate", "out-of-range", "content-mismatch"),
)
def test_incomplete_or_invalid_verdict_coverage_keeps_full_graph_open(
    results: tuple[ACResult, ...],
) -> None:
    seed = _seed("one", "two")
    evaluation = EvaluationSummary(
        final_approved=False,
        highest_stage_passed=1,
        score=0.0,
        ac_results=results,
    )

    focus = select_evolution_focus(seed, seed, evaluation)

    assert focus.active_ac_indices == (0, 1)
    assert focus.frozen_ac_indices == ()


def test_focus_is_persisted_in_lineage_trace() -> None:
    seed = _seed("passed", "failed")
    events = [
        lineage_created("lin_focus_trace", seed.goal),
        lineage_generation_completed(
            "lin_focus_trace",
            1,
            seed.metadata.seed_id,
            seed.ontology_schema.model_dump(mode="json"),
            _evaluation(seed, True, False).model_dump(mode="json"),
            active_ac_indices=[1],
            frozen_ac_indices=[0],
        ),
    ]

    lineage = LineageProjector().project(events)

    assert lineage is not None
    assert lineage.generations[0].active_ac_indices == (1,)
    assert lineage.generations[0].frozen_ac_indices == (0,)
