"""Tests for interview-less Seed crystallization (grounded-lateral RFC D6)."""

from __future__ import annotations

from ouroboros.bigbang.session_seed import (
    CRITERIA_GAP_QUESTION,
    GOAL_GAP_QUESTION,
    SESSION_CONTEXT_AMBIGUITY_CEILING,
    build_session_context_seed,
)


def _settled_context() -> dict:
    return {
        "goal": "Ship a CLI that lints commit messages against team rules",
        "acceptance_criteria": [
            "`lint-commit HEAD` exits 0 on a conforming message",
            "`lint-commit HEAD` exits 1 and names the broken rule otherwise",
        ],
        "constraints": ["Python 3.12+, stdlib only"],
        "decisions": ["Rules live in pyproject.toml, not a custom file"],
    }


def test_settled_context_yields_verbatim_seed() -> None:
    outcome = build_session_context_seed(_settled_context())

    assert outcome.gap_questions == ()
    seed = outcome.seed
    assert seed is not None
    # Verbatim anchoring: every settled string enters byte-for-byte.
    assert seed.goal == "Ship a CLI that lints commit messages against team rules"
    # Bare strings are coerced to AcceptanceCriterionSpec (W2 contract);
    # the settled wording survives verbatim in description.
    assert tuple(ac.description for ac in seed.acceptance_criteria) == (
        "`lint-commit HEAD` exits 0 on a conforming message",
        "`lint-commit HEAD` exits 1 and names the broken rule otherwise",
    )
    # Settled decisions constrain the solution space.
    assert seed.constraints == (
        "Python 3.12+, stdlib only",
        "Rules live in pyproject.toml, not a custom file",
    )
    # Conservative ceiling, never a caller-claimed score (#210 stays closed).
    assert seed.metadata.ambiguity_score == SESSION_CONTEXT_AMBIGUITY_CEILING


def test_missing_goal_and_criteria_return_both_gap_questions() -> None:
    outcome = build_session_context_seed({"constraints": ["Python"]})

    assert outcome.seed is None
    assert outcome.gap_questions == (GOAL_GAP_QUESTION, CRITERIA_GAP_QUESTION)


def test_missing_criteria_alone_returns_one_targeted_question() -> None:
    outcome = build_session_context_seed({"goal": "Build a thing"})

    assert outcome.seed is None
    assert outcome.gap_questions == (CRITERIA_GAP_QUESTION,)


def test_determinism_same_input_same_contract() -> None:
    first = build_session_context_seed(_settled_context()).seed
    second = build_session_context_seed(_settled_context()).seed
    assert first is not None and second is not None
    # Identity fields (seed_id/timestamps) may differ; the contract must not.
    assert first.goal == second.goal
    assert first.constraints == second.constraints
    assert tuple(ac.description for ac in first.acceptance_criteria) == tuple(
        ac.description for ac in second.acceptance_criteria
    )


def test_blank_and_duplicate_entries_are_dropped_not_reworded() -> None:
    context = _settled_context()
    context["constraints"] = ["  Python 3.12+, stdlib only  ", "", None]
    context["decisions"] = ["Python 3.12+, stdlib only"]

    seed = build_session_context_seed(context).seed
    assert seed is not None
    assert seed.constraints == ("Python 3.12+, stdlib only",)


def test_unknown_project_type_falls_back_to_greenfield() -> None:
    context = _settled_context()
    context["project_type"] = "purple"

    seed = build_session_context_seed(context).seed
    assert seed is not None
    assert seed.brownfield_context.project_type == "greenfield"
