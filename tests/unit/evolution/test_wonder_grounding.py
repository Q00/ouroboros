"""Tests for grounded elenchus — Wonder question → AC grounding.

Covers RFC §5.1: new-shape parse (challenge + gap, 1-based→0-based); legacy
strings → deterministic regex fallback; out-of-range refs dropped (all-dropped
challenge → gap); ``questions`` always populated from grounded questions.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

from ouroboros.core.seed import (
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
)
from ouroboros.evolution.wonder import (
    GroundedQuestion,
    WonderEngine,
    ground_question_text,
    ground_questions,
)

_ONTOLOGY = OntologySchema(
    name="login",
    description="Login system ontology",
    fields=(OntologyField(name="user", field_type="entity", description="A user"),),
)


def _seed(num_acs: int = 3) -> Seed:
    return Seed(
        metadata=SeedMetadata(ambiguity_score=0.1),
        goal="Build a login system",
        constraints=("Must use OAuth",),
        acceptance_criteria=tuple(f"AC number {i}" for i in range(1, num_acs + 1)),
        ontology_schema=_ONTOLOGY,
    )


def _engine() -> WonderEngine:
    return WonderEngine(llm_adapter=AsyncMock(), model="test")


class TestNewShapeParse:
    def test_challenge_refs_convert_one_based_to_zero_based(self) -> None:
        content = json.dumps(
            {
                "questions": [
                    {"question": "Why does AC 2 assume a single provider?", "ac_refs": [2]},
                    {"question": "What handles token refresh?", "kind": "gap"},
                ],
                "should_continue": True,
            }
        )
        out = _engine()._parse_response(content, _seed(3))

        assert len(out.grounded_questions) == 2
        challenge, gap = out.grounded_questions
        assert challenge.kind == "challenge"
        assert challenge.ac_indices == (1,)  # 1-based 2 → 0-based 1
        assert gap.kind == "gap"
        assert gap.ac_indices == ()

    def test_multiple_refs_deduped_and_sorted_in_order(self) -> None:
        content = json.dumps(
            {"questions": [{"question": "AC 1 and AC 3 conflict", "ac_refs": [3, 1, 3]}]}
        )
        out = _engine()._parse_response(content, _seed(3))

        gq = out.grounded_questions[0]
        assert gq.kind == "challenge"
        assert gq.ac_indices == (2, 0)  # order preserved, deduped

    def test_questions_field_populated_from_grounded(self) -> None:
        content = json.dumps(
            {
                "questions": [
                    {"question": "Challenge one", "ac_refs": [1]},
                    {"question": "Gap two", "kind": "gap"},
                ]
            }
        )
        out = _engine()._parse_response(content, _seed(2))

        assert out.questions == ("Challenge one", "Gap two")


class TestLegacyStringFallback:
    def test_plain_string_with_ac_ref_grounds_to_challenge(self) -> None:
        content = json.dumps({"questions": ["Why did AC 2 regress in this build?"]})
        out = _engine()._parse_response(content, _seed(3))

        gq = out.grounded_questions[0]
        assert gq.kind == "challenge"
        assert gq.ac_indices == (1,)

    def test_plain_string_without_ac_ref_is_gap(self) -> None:
        content = json.dumps({"questions": ["What about rate limiting entirely?"]})
        out = _engine()._parse_response(content, _seed(3))

        gq = out.grounded_questions[0]
        assert gq.kind == "gap"
        assert gq.ac_indices == ()

    def test_regex_is_case_insensitive_and_hash_tolerant(self) -> None:
        assert ground_question_text("ac#2 is wrong", 3).ac_indices == (1,)
        assert ground_question_text("AC 3 broke", 3).ac_indices == (2,)
        assert ground_question_text("Ac1 fails", 3).ac_indices == (0,)

    def test_ground_questions_helper_batches(self) -> None:
        grounded = ground_questions(["AC 1 broke", "generic gap"], 3)
        assert grounded[0].kind == "challenge"
        assert grounded[1].kind == "gap"


class TestOutOfRangeDropped:
    def test_out_of_range_ref_dropped_and_all_dropped_becomes_gap(self) -> None:
        # AC 9 does not exist in a 3-AC seed → dropped → no valid refs → gap.
        content = json.dumps({"questions": [{"question": "AC 9?", "ac_refs": [9]}]})
        out = _engine()._parse_response(content, _seed(3))

        gq = out.grounded_questions[0]
        assert gq.kind == "gap"
        assert gq.ac_indices == ()

    def test_partial_out_of_range_keeps_valid_refs(self) -> None:
        content = json.dumps({"questions": [{"question": "AC 1 and AC 9", "ac_refs": [1, 9]}]})
        out = _engine()._parse_response(content, _seed(3))

        gq = out.grounded_questions[0]
        assert gq.kind == "challenge"
        assert gq.ac_indices == (0,)

    def test_legacy_string_out_of_range_ref_dropped(self) -> None:
        content = json.dumps({"questions": ["AC 42 is suspicious"]})
        out = _engine()._parse_response(content, _seed(3))

        assert out.grounded_questions[0].kind == "gap"

    def test_no_seed_means_no_range_check(self) -> None:
        # Without a seed we cannot know the AC count; refs are kept as-is.
        out = ground_question_text("AC 5 is odd", None)
        assert out.kind == "challenge"
        assert out.ac_indices == (4,)


class TestFallbackPathsPopulateGrounded:
    def test_parse_error_fallback_is_gap(self) -> None:
        out = _engine()._parse_response("not json", _seed(2))
        assert len(out.grounded_questions) == 1
        assert out.grounded_questions[0].kind == "gap"
        assert out.grounded_questions[0].question == out.questions[0]

    def test_degraded_output_populates_grounded_gaps(self) -> None:
        from ouroboros.core.lineage import EvaluationSummary

        seed = _seed(2)
        summary = EvaluationSummary(
            final_approved=False,
            highest_stage_passed=2,
            score=0.4,
            drift_score=0.6,
            failure_reason="1/2 ACs failed",
        )
        out = _engine()._degraded_output(summary, seed.ontology_schema, seed)

        assert len(out.grounded_questions) == len(out.questions)
        assert all(gq.kind == "gap" for gq in out.grounded_questions)


class TestGroundedQuestionModel:
    def test_frozen_and_defaults(self) -> None:
        gq = GroundedQuestion(question="q")
        assert gq.kind == "gap"
        assert gq.ac_indices == ()
