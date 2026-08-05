"""Tests for the satisficing delta — ACPatch composition and backstop.

Covers RFC §5.2: patch parse; composition order (keep/revise in place, add
appended); backstop forces keep on passed+unchallenged+unregressed; challenged
or failed or regressed ACs may revise; kept-but-failed AC not settled; legacy
full-list fallback diff (identical→keep, changed→revise, longer→add,
shorter→full-rewrite semantics); malformed patches dropped, every parent index
present exactly once.
"""

from __future__ import annotations

import json

import pytest

from ouroboros.bigbang.seed_generator import SeedGenerator
from ouroboros.core.lineage import ACResult, EvaluationSummary, OntologyLineage
from ouroboros.core.seed import (
    AcceptanceCriterionSpec,
    InvestmentSpec,
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
)
from ouroboros.evolution.reflect import (
    ACPatch,
    ReflectEngine,
    ReflectOutput,
    _apply_satisficing_backstop,
    _derive_legacy_patches,
    _parse_ac_patches,
)
from ouroboros.evolution.regression import ACRegression, RegressionReport
from ouroboros.evolution.wonder import GroundedQuestion, WonderOutput

PARENT_ACS = ("AC zero", "AC one", "AC two")


def _seed(
    acs: tuple[str | AcceptanceCriterionSpec, ...] = PARENT_ACS,
    **extra_fields: object,
) -> Seed:
    return Seed(
        metadata=SeedMetadata(ambiguity_score=0.1),
        goal="Build a thing",
        constraints=("c1",),
        acceptance_criteria=acs,
        ontology_schema=OntologySchema(
            name="o",
            description="d",
            fields=(OntologyField(name="f", field_type="entity", description="a field"),),
        ),
        **extra_fields,
    )


def _seed_with_ontology(fields: tuple[OntologyField, ...]) -> Seed:
    return Seed(
        metadata=SeedMetadata(ambiguity_score=0.1),
        goal="Build a thing",
        constraints=("c1",),
        acceptance_criteria=PARENT_ACS,
        ontology_schema=OntologySchema(
            name="o",
            description="d",
            fields=fields,
        ),
    )


def _summary(passed: dict[int, bool]) -> EvaluationSummary:
    return EvaluationSummary(
        final_approved=all(passed.values()),
        highest_stage_passed=2,
        score=0.8,
        ac_results=tuple(
            ACResult(ac_index=i, ac_content=PARENT_ACS[i], passed=p) for i, p in passed.items()
        ),
    )


def _wonder(challenge_indices: tuple[int, ...] = ()) -> WonderOutput:
    grounded = ()
    if challenge_indices:
        grounded = (
            GroundedQuestion(question="challenge", kind="challenge", ac_indices=challenge_indices),
        )
    return WonderOutput(questions=("q",), grounded_questions=grounded)


def _regression(indices: tuple[int, ...] = ()) -> RegressionReport:
    regs = tuple(
        ACRegression(
            ac_index=i,
            ac_text=PARENT_ACS[i],
            passed_in_generation=1,
            failed_in_generation=2,
        )
        for i in indices
    )
    return RegressionReport(regressions=regs)


def _compose(
    data: dict,
    parent: tuple[str, ...] = PARENT_ACS,
    passed: dict[int, bool] | None = None,
    challenge: tuple[int, ...] = (),
    regressed: tuple[int, ...] = (),
    active: tuple[int, ...] | None = None,
) -> tuple[tuple[str, ...], tuple[ACPatch, ...], tuple[int, ...]]:
    if passed is None:
        passed = {0: True, 1: True, 2: True}
    return ReflectEngine._compose_acs(
        data,
        parent,
        _summary(passed),
        _wonder(challenge),
        _regression(regressed),
        active,
    )


def _parse_explicit(
    parent: Seed,
    patches: list[dict[str, object]],
    *,
    refined_acs: tuple[str, ...] | None = None,
) -> ReflectOutput | None:
    data: dict[str, object] = {
        "refined_goal": parent.goal,
        "refined_constraints": list(parent.constraints),
        "ac_patches": patches,
        "ontology_mutations": [],
        "reasoning": "r",
    }
    if refined_acs is not None:
        data["refined_acs"] = list(refined_acs)
    evaluation = EvaluationSummary(
        final_approved=False,
        highest_stage_passed=2,
        score=0.5,
        ac_results=tuple(
            ACResult(ac_index=index, ac_content=criterion.description, passed=False)
            for index, criterion in enumerate(parent.acceptance_criteria)
        ),
    )
    return ReflectEngine(llm_adapter=_FakeAdapter(""), model="test")._parse_response(
        json.dumps(data),
        parent,
        evaluation,
        _wonder(),
        _regression(),
    )


class TestPatchParse:
    def test_parses_keep_revise_add(self) -> None:
        patches = _parse_ac_patches(
            [
                {"op": "keep", "index": 0},
                {"op": "revise", "index": 1, "content": "new"},
                {"op": "add", "content": "extra"},
            ]
        )
        assert [p.op for p in patches] == ["keep", "revise", "add"]
        assert patches[1].content == "new"
        assert patches[2].index is None

    def test_remove_coerced_to_keep(self) -> None:
        patches = _parse_ac_patches([{"op": "remove", "index": 1}])
        assert patches[0].op == "keep"
        assert patches[0].index == 1

    def test_unknown_op_coerced_to_keep(self) -> None:
        patches = _parse_ac_patches([{"op": "frobnicate", "index": 0}])
        assert patches[0].op == "keep"

    def test_non_dict_items_skipped(self) -> None:
        patches = _parse_ac_patches(["nonsense", 42, {"op": "keep", "index": 0}])
        assert len(patches) == 1

    def test_bool_index_rejected(self) -> None:
        # True is an int subclass; must not be treated as index 1.
        patches = _parse_ac_patches([{"op": "keep", "index": True}])
        assert patches[0].index is None

    @pytest.mark.parametrize("raw_patches", [None, {"op": "keep"}, "not-a-list"])
    def test_wrong_shaped_ac_patches_rejected(self, raw_patches: object) -> None:
        with pytest.raises(TypeError, match="Expected ac_patches to be a list"):
            _parse_ac_patches(raw_patches)


class TestComposition:
    def test_keep_revise_in_place_add_appended(self) -> None:
        data = {
            "ac_patches": [
                {"op": "keep", "index": 0},
                {"op": "revise", "index": 1, "content": "revised one"},
                {"op": "keep", "index": 2},
                {"op": "add", "content": "brand new"},
            ]
        }
        # AC 1 challenged so revise is allowed
        refined, patches, settled = _compose(data, challenge=(1,))
        assert refined == ("AC zero", "revised one", "AC two", "brand new")

    def test_every_parent_index_present_exactly_once(self) -> None:
        # LLM omits index 2 entirely — implicit keep must fill it.
        data = {"ac_patches": [{"op": "keep", "index": 0}, {"op": "keep", "index": 1}]}
        refined, patches, _ = _compose(data)
        keep_revise_indices = [
            p.index for p in patches if p.op in ("keep", "revise") and p.index is not None
        ]
        assert sorted(keep_revise_indices) == [0, 1, 2]
        assert len(refined) == 3

    def test_add_content_missing_dropped(self) -> None:
        data = {"ac_patches": [{"op": "add"}]}  # no content
        refined, patches, _ = _compose(data)
        # only the 3 implicit keeps remain
        assert len(refined) == 3
        assert all(p.op == "keep" for p in patches)

    def test_add_index_is_normalized_before_seed_materialization(self) -> None:
        refined, patches, _ = _compose(
            {"ac_patches": [{"op": "add", "index": 99, "content": "brand new"}]}
        )

        addition = patches[-1]
        assert refined[-1] == "brand new"
        assert addition.op == "add"
        assert addition.index is None

    @pytest.mark.parametrize(
        "patches",
        [
            [
                {"op": "revise", "index": 0, "content": "AC one"},
                {"op": "revise", "index": 1, "content": "AC zero"},
            ],
            [{"op": "add", "content": "AC one"}],
        ],
        ids=["semantic-swap", "add-parent-identity"],
    )
    def test_fresh_parent_identity_reuse_is_rejected_for_reflect_retry(
        self,
        patches: list[dict[str, object]],
    ) -> None:
        with pytest.raises(ValueError, match="reuses another parent identity"):
            _compose({"ac_patches": patches})

    def test_focused_evolution_drops_new_nodes(self) -> None:
        refined, patches, _ = _compose(
            {
                "ac_patches": [
                    {"op": "revise", "index": 1, "content": "target fixed"},
                    {"op": "add", "content": "scope expansion"},
                ]
            },
            passed={0: True, 1: False, 2: False},
            active=(1,),
        )

        assert refined == ("AC zero", "target fixed", "AC two")
        assert all(patch.op != "add" for patch in patches)

    def test_focused_evolution_forces_non_target_failed_node_to_keep(self) -> None:
        refined, _, _ = _compose(
            {
                "ac_patches": [
                    {"op": "revise", "index": 1, "content": "target fixed"},
                    {"op": "revise", "index": 2, "content": "not targeted"},
                ]
            },
            passed={0: True, 1: False, 2: False},
            active=(1,),
        )

        assert refined == ("AC zero", "target fixed", "AC two")


class TestSatisficingBackstop:
    def test_non_authoritative_pass_is_revisable_and_never_settled(self) -> None:
        summary = EvaluationSummary(
            final_approved=True,
            highest_stage_passed=2,
            ac_results=(
                ACResult(
                    ac_index=0,
                    ac_content=PARENT_ACS[0],
                    passed=True,
                    ac_verdict_state="not_evaluated",
                ),
            ),
        )

        refined, patches, settled = ReflectEngine._compose_acs(
            {"ac_patches": [{"op": "revise", "index": 0, "content": "verified AC zero"}]},
            PARENT_ACS,
            summary,
            _wonder(),
            _regression(),
            (0,),
        )

        assert refined[0] == "verified AC zero"
        assert patches[0].op == "revise"
        assert 0 not in settled

    def test_forces_keep_on_protected(self) -> None:
        # AC 0 passed, unchallenged, unregressed → protected. LLM tries to revise.
        patches = [ACPatch(op="revise", index=0, content="sneaky rewrite")]
        refined, final, settled = _apply_satisficing_backstop(
            PARENT_ACS, patches, protected={0}, passed_indices={0, 1, 2}
        )
        assert refined[0] == "AC zero"  # verbatim, not the rewrite
        assert final[0].op == "keep"
        assert 0 in settled

    def test_challenged_ac_may_revise(self) -> None:
        # AC 1 passed but challenged → NOT protected → revise honored.
        refined, _, settled = _compose(
            {"ac_patches": [{"op": "revise", "index": 1, "content": "challenged fix"}]},
            challenge=(1,),
        )
        assert refined[1] == "challenged fix"
        assert 1 not in settled  # revised, not kept

    def test_challenged_ac_is_not_settled_when_kept(self) -> None:
        _, _, settled = _compose(
            {"ac_patches": [{"op": "keep", "index": 1}]},
            challenge=(1,),
        )
        assert 1 not in settled

    def test_failed_ac_may_revise(self) -> None:
        refined, _, settled = _compose(
            {"ac_patches": [{"op": "revise", "index": 2, "content": "fix failed"}]},
            passed={0: True, 1: True, 2: False},
        )
        assert refined[2] == "fix failed"
        assert 2 not in settled

    def test_regressed_ac_may_revise(self) -> None:
        # AC 1 passed this eval but regression report flags it → not protected.
        refined, _, settled = _compose(
            {"ac_patches": [{"op": "revise", "index": 1, "content": "regression fix"}]},
            regressed=(1,),
        )
        assert refined[1] == "regression fix"
        assert 1 not in settled

    def test_regressed_ac_never_settled_even_if_kept(self) -> None:
        # Passed this eval + kept, but regressed → excluded from settling.
        _, _, settled = _compose(
            {"ac_patches": [{"op": "keep", "index": 1}]},
            regressed=(1,),
        )
        assert 1 not in settled

    def test_missing_evaluation_mints_no_protection_or_settlement(self) -> None:
        refined, _, settled = ReflectEngine._compose_acs(
            {
                "ac_patches": [
                    {"op": "revise", "index": 0, "content": "ontology revision"},
                    {"op": "keep", "index": 1},
                    {"op": "keep", "index": 2},
                ]
            },
            PARENT_ACS,
            None,
            _wonder(),
            _regression(),
        )

        assert refined[0] == "ontology revision"
        assert settled == ()


class TestSettledIndices:
    def test_kept_passing_acs_are_settled(self) -> None:
        _, _, settled = _compose(
            {"ac_patches": [{"op": "keep", "index": 0}, {"op": "keep", "index": 1}]}
        )
        # index 2 implicitly kept; all passed → all settled
        assert set(settled) == {0, 1, 2}

    def test_kept_but_failed_ac_not_settled(self) -> None:
        _, _, settled = _compose(
            {"ac_patches": [{"op": "keep", "index": 2}]},
            passed={0: True, 1: True, 2: False},
        )
        assert 2 not in settled
        assert set(settled) == {0, 1}


class TestLegacyFallbackDiff:
    def test_identical_keep_changed_revise_longer_add(self) -> None:
        # No ac_patches key → legacy diff of refined_acs vs parent.
        data = {"refined_acs": ["AC zero", "CHANGED one", "AC two", "NEW three"]}
        # AC 1 challenged so its revise survives the backstop
        refined, patches, settled = _compose(data, challenge=(1,))
        assert refined == ("AC zero", "CHANGED one", "AC two", "NEW three")
        ops = [p.op for p in patches]
        assert ops == ["keep", "revise", "keep", "add"]
        assert set(settled) == {0, 2}  # kept + passed; index 1 revised

    def test_shorter_list_is_rejected_as_identity_ambiguous(self) -> None:
        data = {"refined_acs": ["only one"]}
        with pytest.raises(ValueError, match="cannot delete or reorder"):
            _compose(data)

    def test_reordered_list_is_rejected_as_identity_ambiguous(self) -> None:
        data = {"refined_acs": ["AC one", "AC zero", "AC two"]}
        with pytest.raises(ValueError, match="cannot delete or reorder"):
            _compose(data)

    def test_derive_legacy_patches_shorter_returns_none(self) -> None:
        assert _derive_legacy_patches(("a",), ("a", "b", "c")) is None

    def test_derive_legacy_patches_equal_lengths(self) -> None:
        patches = _derive_legacy_patches(("a", "X"), ("a", "b"))
        assert patches is not None
        assert patches[0].op == "keep"
        assert patches[1].op == "revise"
        assert patches[1].content == "X"

    @pytest.mark.parametrize("raw_patches", [None, {"0": {"op": "keep"}}, "not-a-list"])
    def test_present_wrong_shaped_ac_patches_do_not_use_legacy_fallback(
        self, raw_patches: object
    ) -> None:
        data = {
            "ac_patches": raw_patches,
            "refined_acs": ["AC zero", "CHANGED one", "AC two", "NEW three"],
        }
        with pytest.raises(TypeError, match="Expected ac_patches to be a list"):
            _compose(data, challenge=(1,))


class TestMalformedPatches:
    def test_out_of_range_and_duplicate_dropped(self) -> None:
        patches = [
            ACPatch(op="keep", index=0),
            ACPatch(op="keep", index=0),  # duplicate
            ACPatch(op="revise", index=99, content="x"),  # out of range
            ACPatch(op="revise", index=None, content="y"),  # no index
        ]
        refined, final, _ = _apply_satisficing_backstop(
            PARENT_ACS, patches, protected=set(), passed_indices=set()
        )
        # every parent index present exactly once
        assert len(refined) == 3
        indices = sorted(p.index for p in final if p.index is not None)
        assert indices == [0, 1, 2]

    def test_revise_without_content_dropped_then_implicit_keep(self) -> None:
        patches = [ACPatch(op="revise", index=0, content=None)]
        refined, final, _ = _apply_satisficing_backstop(
            PARENT_ACS, patches, protected=set(), passed_indices=set()
        )
        assert refined[0] == "AC zero"  # implicit keep filled it
        assert final[0].op == "keep"


class TestReflectEndToEnd:
    async def test_ontology_only_reflect_uses_no_synthetic_evaluation_authority(self) -> None:
        response = json.dumps(
            {
                "refined_goal": "Build a thing with explicit ownership",
                "refined_constraints": ["c1"],
                "ac_patches": [
                    {"op": "revise", "index": 0, "content": "AC zero clarified"},
                    {"op": "keep", "index": 1},
                    {"op": "keep", "index": 2},
                ],
                "ontology_mutations": [
                    {
                        "action": "add",
                        "field_name": "owner",
                        "field_type": "entity",
                        "reason": "answer the Wonder question",
                    }
                ],
                "reasoning": "ontology-only exploration",
            }
        )
        adapter = _FakeAdapter(response)

        result = await ReflectEngine(llm_adapter=adapter, model="test").reflect(
            current_seed=_seed(),
            execution_output="",
            evaluation_summary=None,
            wonder_output=_wonder(),
            lineage=OntologyLineage(lineage_id="l", goal="Build a thing"),
        )

        assert result.is_ok
        assert result.value.refined_acs[0] == "AC zero clarified"
        assert result.value.settled_ac_indices == ()
        assert "Not evaluated (ontology-only exploration)" in adapter.messages[1].content
        assert "No AC has PASS, protection, or settling authority" in adapter.messages[1].content

    async def test_whitespace_only_refined_constraints_rejected_before_seed_materialization(
        self,
    ) -> None:
        response = json.dumps(
            {
                "refined_goal": "Build a thing better",
                "refined_constraints": ["c1", "   "],
                "ac_patches": [
                    {"op": "keep", "index": 0},
                    {"op": "keep", "index": 1},
                    {"op": "keep", "index": 2},
                ],
                "ontology_mutations": [],
                "reasoning": "r",
            }
        )

        result = await ReflectEngine(llm_adapter=_FakeAdapter(response), model="test").reflect(
            current_seed=_seed(),
            execution_output="out",
            evaluation_summary=_summary({0: True, 1: True, 2: True}),
            wonder_output=_wonder(),
            lineage=OntologyLineage(lineage_id="l", goal="Build a thing"),
        )

        assert result.is_err
        assert "failed to parse" in result.error.message.lower()

    async def test_whitespace_only_ac_patch_content_rejected_before_seed_materialization(
        self,
    ) -> None:
        response = json.dumps(
            {
                "refined_goal": "Build a thing better",
                "refined_constraints": ["c1"],
                "ac_patches": [
                    {"op": "keep", "index": 0},
                    {"op": "revise", "index": 1, "content": "   "},
                    {"op": "keep", "index": 2},
                ],
                "ontology_mutations": [],
                "reasoning": "r",
            }
        )

        result = await ReflectEngine(llm_adapter=_FakeAdapter(response), model="test").reflect(
            current_seed=_seed(),
            execution_output="out",
            evaluation_summary=_summary({0: True, 1: True, 2: True}),
            wonder_output=_wonder(challenge_indices=(1,)),
            lineage=OntologyLineage(lineage_id="l", goal="Build a thing"),
        )

        assert result.is_err
        assert "failed to parse" in result.error.message.lower()

    async def test_whitespace_only_legacy_refined_acs_rejected_before_seed_materialization(
        self,
    ) -> None:
        response = json.dumps(
            {
                "refined_goal": "Build a thing better",
                "refined_constraints": ["c1"],
                "refined_acs": ["AC zero", "   ", "AC two"],
                "ontology_mutations": [],
                "reasoning": "r",
            }
        )

        result = await ReflectEngine(llm_adapter=_FakeAdapter(response), model="test").reflect(
            current_seed=_seed(),
            execution_output="out",
            evaluation_summary=_summary({0: True, 1: True, 2: True}),
            wonder_output=_wonder(challenge_indices=(1,)),
            lineage=OntologyLineage(lineage_id="l", goal="Build a thing"),
        )

        assert result.is_err
        assert "failed to parse" in result.error.message.lower()

    @pytest.mark.parametrize(
        "mutation",
        [
            {"action": "modify", "field_name": "f", "field_type": "   "},
            {"action": "modify", "field_name": "f", "description": "   "},
        ],
    )
    async def test_whitespace_only_ontology_values_rejected_before_seed_materialization(
        self, mutation: dict[str, str]
    ) -> None:
        parent = _seed_with_ontology(
            (OntologyField(name="f", field_type="entity", description="a field"),)
        )
        response = json.dumps(
            {
                "refined_goal": "Build a thing better",
                "refined_constraints": ["c1"],
                "ac_patches": [
                    {"op": "keep", "index": 0},
                    {"op": "keep", "index": 1},
                    {"op": "keep", "index": 2},
                ],
                "ontology_mutations": [mutation],
                "reasoning": "r",
            }
        )

        reflect_result = await ReflectEngine(
            llm_adapter=_FakeAdapter(response), model="test"
        ).reflect(
            current_seed=parent,
            execution_output="out",
            evaluation_summary=_summary({0: True, 1: True, 2: True}),
            wonder_output=_wonder(),
            lineage=OntologyLineage(lineage_id="l", goal="Build a thing"),
        )

        assert reflect_result.is_err

    async def test_reflect_composes_from_patches(self) -> None:
        response = json.dumps(
            {
                "refined_goal": "Build a thing better",
                "refined_constraints": ["c1"],
                "ac_patches": [
                    {"op": "keep", "index": 0},
                    {"op": "revise", "index": 1, "content": "fixed AC one"},
                    {"op": "keep", "index": 2},
                ],
                "ontology_mutations": [],
                "reasoning": "r",
            }
        )
        adapter = _FakeAdapter(response)
        engine = ReflectEngine(llm_adapter=adapter, model="test")

        from ouroboros.core.lineage import OntologyLineage

        result = await engine.reflect(
            current_seed=_seed(),
            execution_output="out",
            evaluation_summary=_summary({0: True, 1: True, 2: True}),
            wonder_output=_wonder(challenge_indices=(1,)),
            lineage=OntologyLineage(lineage_id="l", goal="Build a thing"),
        )

        assert result.is_ok
        out = result.value
        assert out.refined_acs == ("AC zero", "fixed AC one", "AC two")
        assert out.ac_patch_identity_explicit is True
        assert 0 in out.settled_ac_indices
        assert 2 in out.settled_ac_indices
        assert 1 not in out.settled_ac_indices

    @pytest.mark.parametrize(
        "ac_patches",
        [
            [
                {"op": "keep", "index": 0},
                {"op": "keep", "index": 2},
            ],
            [
                {"op": "keep", "index": 0},
                {"op": "revise", "index": 0, "content": "conflict"},
                {"op": "keep", "index": 1},
                {"op": "keep", "index": 2},
            ],
            [
                {"op": "keep", "index": 0},
                {"op": "keep", "index": 1},
                {"op": "keep", "index": 2},
                {"op": "keep", "index": 3},
            ],
            [
                {"op": "keep", "index": 0},
                {"op": "keep", "index": 1},
                {"op": "remove", "index": 2},
            ],
            [
                {"op": "keep", "index": 0, "content": "replacement"},
                {"op": "keep", "index": 1},
                {"op": "keep", "index": 2},
            ],
            [
                {"op": "keep", "index": 0},
                {"op": "revise", "index": 1},
                {"op": "keep", "index": 2},
            ],
            [
                {"op": "keep", "index": 0},
                {"op": "revise", "index": 1, "content": 42},
                {"op": "keep", "index": 2},
            ],
            [
                {"op": "keep", "index": 0},
                {"op": "keep", "index": 1},
                {"op": "keep", "index": 2},
                {"op": "add"},
            ],
            [
                {"op": "keep", "index": 0},
                {"op": "keep", "index": 1},
                {"op": "keep", "index": 2},
                {"op": "add", "content": 42},
            ],
            [
                {"op": "keep", "index": 0},
                {"op": "keep", "index": 1},
                {"op": "keep", "index": 2},
                {"op": "add", "index": 0, "content": "new criterion"},
            ],
        ],
        ids=(
            "missing",
            "duplicate",
            "out-of-range",
            "unsupported-op",
            "keep-content",
            "revise-missing-content",
            "revise-non-string-content",
            "add-missing-content",
            "add-non-string-content",
            "add-parent-index",
        ),
    )
    async def test_reflect_rejects_malformed_explicit_patch_identity(
        self,
        ac_patches: list[dict[str, object]],
    ) -> None:
        response = json.dumps(
            {
                "refined_goal": "Build a thing better",
                "refined_constraints": ["c1"],
                "ac_patches": ac_patches,
                "ontology_mutations": [],
                "reasoning": "r",
            }
        )

        result = await ReflectEngine(
            llm_adapter=_FakeAdapter(response),
            model="test",
        ).reflect(
            current_seed=_seed(),
            execution_output="out",
            evaluation_summary=_summary({0: True, 1: True, 2: True}),
            wonder_output=_wonder(),
            lineage=OntologyLineage(lineage_id="l", goal="Build a thing"),
        )

        assert result.is_err
        assert "failed to parse" in result.error.message.lower()

    async def test_reflect_marks_legacy_full_list_patch_identity_as_inferred(self) -> None:
        response = json.dumps(
            {
                "refined_goal": "Build a thing better",
                "refined_constraints": ["c1"],
                "refined_acs": ["AC zero", "fixed AC one", "AC two"],
                "ontology_mutations": [],
                "reasoning": "r",
            }
        )

        result = await ReflectEngine(
            llm_adapter=_FakeAdapter(response),
            model="test",
        ).reflect(
            current_seed=_seed(),
            execution_output="out",
            evaluation_summary=_summary({0: True, 1: True, 2: True}),
            wonder_output=_wonder(challenge_indices=(1,)),
            lineage=OntologyLineage(lineage_id="l", goal="Build a thing"),
        )

        assert result.is_ok
        assert result.value.ac_patches
        assert result.value.ac_patch_identity_explicit is False

    async def test_valid_mutations_materialize_downstream_seed(self) -> None:
        parent = _seed_with_ontology(
            (
                OntologyField(name="f", field_type="entity", description="a field"),
                OntologyField(name="obsolete", field_type="string", description="obsolete field"),
            )
        )
        response = json.dumps(
            {
                "refined_goal": "Build a thing with materialized ontology",
                "refined_constraints": ["c1"],
                "ac_patches": [
                    {"op": "keep", "index": 0},
                    {"op": "keep", "index": 1},
                    {"op": "keep", "index": 2},
                ],
                "ontology_mutations": [
                    {
                        "action": "add",
                        "field_name": "new_signal",
                        "field_type": "string",
                        "reason": "Captures the new signal",
                    },
                    {
                        "action": "modify",
                        "field_name": "f",
                        "field_type": "object",
                    },
                    {
                        "action": "remove",
                        "field_name": "obsolete",
                    },
                ],
                "reasoning": "r",
            }
        )
        result = await ReflectEngine(llm_adapter=_FakeAdapter(response), model="test").reflect(
            current_seed=parent,
            execution_output="out",
            evaluation_summary=_summary({0: True, 1: True, 2: True}),
            wonder_output=_wonder(),
            lineage=OntologyLineage(lineage_id="l", goal="Build a thing"),
        )

        assert result.is_ok
        seed_result = SeedGenerator(llm_adapter=_FakeAdapter("")).generate_from_reflect(
            parent, result.value
        )

        assert seed_result.is_ok
        fields = {field.name: field for field in seed_result.value.ontology_schema.fields}
        assert fields["new_signal"].description == "Captures the new signal"
        assert fields["f"].field_type == "object"
        assert fields["f"].description == "a field"
        assert "obsolete" not in fields

    def test_seed_generation_preserves_structured_ac_contracts(self) -> None:
        parent = _seed(
            (
                AcceptanceCriterionSpec(
                    description="AC zero",
                    verify_command="pytest -q tests/test_zero.py",
                    expected_artifacts=("dist/zero.json",),
                    output_assertion="1 passed",
                    investment=InvestmentSpec(
                        difficulty="high",
                        stakes="medium",
                        confidence="high",
                    ),
                ),
                "AC one",
                "AC two",
            ),
            plugin_contract={"items": ["one", {"nested": ["two"]}]},
        )
        original = parent.acceptance_criteria[0]
        reflected = ReflectOutput(
            refined_goal=parent.goal,
            refined_constraints=parent.constraints,
            refined_acs=("AC zero revised", "AC one", "AC two"),
            ac_patches=(
                ACPatch(op="revise", index=0, content="AC zero revised"),
                ACPatch(op="keep", index=1),
                ACPatch(op="keep", index=2),
            ),
        )

        result = SeedGenerator(llm_adapter=_FakeAdapter("")).generate_from_reflect(
            parent,
            reflected,
        )

        assert result.is_ok
        revised = result.value.acceptance_criteria[0]
        assert revised.description == "AC zero revised"
        assert revised.verify_command == original.verify_command
        assert revised.expected_artifacts == original.expected_artifacts
        assert revised.output_assertion == original.output_assertion
        assert revised.investment == original.investment
        assert revised.semantic_ac_key != original.semantic_ac_key
        assert result.value.to_dict()["plugin_contract"] == {"items": ["one", {"nested": ["two"]}]}

    @pytest.mark.parametrize(
        ("refined_acs", "expected_reason"),
        [
            (("AC one",), "shorter"),
            (("AC one", "AC zero"), "reorders"),
        ],
        ids=("deletion", "reordering"),
    )
    def test_seed_generation_rejects_ambiguous_structured_contract_rewrites(
        self,
        refined_acs: tuple[str, ...],
        expected_reason: str,
    ) -> None:
        parent = _seed(
            (
                AcceptanceCriterionSpec(
                    description="AC zero",
                    verify_command="python verify_zero.py",
                    expected_artifacts=("dist/zero.json",),
                    output_assertion="zero passed",
                    investment=InvestmentSpec(difficulty="high", stakes="medium"),
                ),
                AcceptanceCriterionSpec(
                    description="AC one",
                    verify_command="python verify_one.py",
                    expected_artifacts=("dist/one.json",),
                    output_assertion="one passed",
                    investment=InvestmentSpec(difficulty="medium", stakes="high"),
                ),
            )
        )
        reflected = ReflectOutput(
            refined_goal=parent.goal,
            refined_constraints=parent.constraints,
            refined_acs=refined_acs,
        )

        result = SeedGenerator(llm_adapter=_FakeAdapter("")).generate_from_reflect(
            parent,
            reflected,
        )

        assert result.is_err
        assert expected_reason in str(result.error)
        assert "structured acceptance contracts" in str(result.error)

    def test_seed_generation_rejects_edited_structured_contract_reordering(self) -> None:
        parent = _seed(
            (
                AcceptanceCriterionSpec(
                    description="Approve the invoice",
                    verify_command="python verify_invoice.py",
                    expected_artifacts=("invoice.json",),
                ),
                AcceptanceCriterionSpec(
                    description="Delete the account",
                    verify_command="python verify_delete.py",
                    expected_artifacts=("deleted.json",),
                ),
            )
        )
        reflected = ReflectOutput(
            refined_goal=parent.goal,
            refined_constraints=parent.constraints,
            refined_acs=(
                "Delete the account permanently",
                "Approve the invoice safely",
            ),
        )

        result = SeedGenerator(llm_adapter=_FakeAdapter("")).generate_from_reflect(
            parent,
            reflected,
        )

        assert result.is_err
        assert "explicit stable AC identity" in str(result.error)

    def test_seed_generation_uses_explicit_patch_identity_for_multiple_revisions(self) -> None:
        parent = _seed(
            (
                AcceptanceCriterionSpec(
                    description="Approve the invoice",
                    verify_command="python verify_invoice.py",
                    expected_artifacts=("invoice.json",),
                ),
                AcceptanceCriterionSpec(
                    description="Delete the account",
                    verify_command="python verify_delete.py",
                    expected_artifacts=("deleted.json",),
                ),
            )
        )
        reflected = _parse_explicit(
            parent,
            [
                {"op": "revise", "index": 0, "content": "Approve the invoice safely"},
                {"op": "revise", "index": 1, "content": "Delete the account permanently"},
            ],
        )
        assert reflected is not None

        result = SeedGenerator(llm_adapter=_FakeAdapter("")).generate_from_reflect(
            parent,
            reflected,
        )

        assert result.is_ok
        criteria = result.value.acceptance_criteria
        assert criteria[0].verify_command == "python verify_invoice.py"
        assert criteria[1].verify_command == "python verify_delete.py"

    def test_seed_generation_rejects_legacy_positional_patches_for_edited_reorder(
        self,
    ) -> None:
        parent = _seed(
            (
                AcceptanceCriterionSpec(
                    description="Approve the invoice",
                    verify_command="python verify_invoice.py",
                ),
                AcceptanceCriterionSpec(
                    description="Delete the account",
                    verify_command="python verify_delete.py",
                ),
            )
        )
        reflected = ReflectOutput(
            refined_goal=parent.goal,
            refined_constraints=parent.constraints,
            refined_acs=("Delete the account permanently", "Approve the invoice safely"),
            ac_patches=(
                ACPatch(op="revise", index=0, content="Delete the account permanently"),
                ACPatch(op="revise", index=1, content="Approve the invoice safely"),
            ),
        )

        result = SeedGenerator(llm_adapter=_FakeAdapter("")).generate_from_reflect(
            parent,
            reflected,
        )

        assert result.is_err
        assert "explicit stable AC identity" in str(result.error)

    def test_seed_generation_rejects_inconsistent_explicit_patch_for_plain_ac(self) -> None:
        parent = _seed(("original",))
        reflected = _parse_explicit(
            parent,
            [{"op": "keep", "index": 0}],
            refined_acs=("changed despite keep",),
        )
        assert reflected is None

    def test_serialized_or_copied_boolean_cannot_forge_explicit_patch_provenance(
        self,
    ) -> None:
        direct = ReflectOutput(
            refined_goal="Build a thing",
            refined_acs=("changed",),
            ac_patches=(ACPatch(op="revise", index=0, content="changed"),),
        )
        serialized = direct.model_dump(mode="json")
        serialized["ac_patch_identity_explicit"] = True

        assert ReflectOutput.model_validate(serialized).ac_patch_identity_explicit is False
        assert (
            direct.model_copy(
                update={"ac_patch_identity_explicit": True}
            ).ac_patch_identity_explicit
            is False
        )

    def test_explicit_patch_provenance_is_not_serialized(self) -> None:
        parsed = _parse_explicit(
            _seed(("original",)),
            [{"op": "revise", "index": 0, "content": "changed"}],
        )
        assert parsed is not None
        assert parsed.ac_patch_identity_explicit is True
        assert (
            ReflectOutput.model_validate(parsed.model_dump(mode="json")).ac_patch_identity_explicit
            is False
        )

    def test_model_copy_content_update_invalidates_explicit_patch_provenance(self) -> None:
        parent = _seed(
            (
                AcceptanceCriterionSpec(
                    description="first",
                    verify_command="python verify_first.py",
                ),
                AcceptanceCriterionSpec(
                    description="second",
                    verify_command="python verify_second.py",
                ),
            )
        )
        parsed = _parse_explicit(
            parent,
            [
                {"op": "revise", "index": 0, "content": "first revised"},
                {"op": "revise", "index": 1, "content": "second revised"},
            ],
        )
        assert parsed is not None
        forged = parsed.model_copy(
            update={
                "refined_acs": ("second", "first"),
                "ac_patches": (
                    ACPatch(op="revise", index=0, content="second"),
                    ACPatch(op="revise", index=1, content="first"),
                ),
            }
        )

        assert forged.ac_patch_identity_explicit is False
        result = SeedGenerator(llm_adapter=_FakeAdapter("")).generate_from_reflect(
            parent,
            forged,
        )
        assert result.is_err
        assert "explicit stable AC identity" in str(result.error)

    def test_ac_result_rejects_boolean_index(self) -> None:
        with pytest.raises(ValueError):
            ACResult(ac_index=True, ac_content="criterion", passed=True)

    @pytest.mark.parametrize(
        ("refined_acs", "expected_reason"),
        [
            (("   ",), "non-empty string"),
            (("criterion\x00suffix",), "control characters"),
            ((42,), "contain strings"),
        ],
        ids=("whitespace", "control-character", "non-string"),
    )
    def test_reflect_output_rejects_invalid_acceptance_criteria(
        self,
        refined_acs: tuple[object, ...],
        expected_reason: str,
    ) -> None:
        with pytest.raises((TypeError, ValueError), match=expected_reason):
            ReflectOutput(
                refined_goal="Build a thing",
                refined_acs=refined_acs,  # type: ignore[arg-type]
            )


class _FakeAdapter:
    def __init__(self, content: str) -> None:
        self.content = content
        self._max_turns = 1
        self.messages = ()

    async def complete(self, messages, config):  # type: ignore[no-untyped-def]
        from ouroboros.core.types import Result
        from ouroboros.providers.base import CompletionResponse, UsageInfo

        self.messages = messages
        return Result.ok(
            CompletionResponse(
                content=self.content,
                model=config.model,
                usage=UsageInfo(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            )
        )
