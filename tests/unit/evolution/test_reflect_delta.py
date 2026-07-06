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

from ouroboros.core.lineage import ACResult, EvaluationSummary
from ouroboros.core.seed import OntologyField, OntologySchema, Seed, SeedMetadata
from ouroboros.evolution.reflect import (
    ACPatch,
    ReflectEngine,
    _apply_satisficing_backstop,
    _derive_legacy_patches,
    _parse_ac_patches,
)
from ouroboros.evolution.regression import ACRegression, RegressionReport
from ouroboros.evolution.wonder import GroundedQuestion, WonderOutput

PARENT_ACS = ("AC zero", "AC one", "AC two")


def _seed(acs: tuple[str, ...] = PARENT_ACS) -> Seed:
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
) -> tuple[tuple[str, ...], tuple[ACPatch, ...], tuple[int, ...]]:
    if passed is None:
        passed = {0: True, 1: True, 2: True}
    return ReflectEngine._compose_acs(
        data, parent, _summary(passed), _wonder(challenge), _regression(regressed)
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
        keep_revise_indices = [p.index for p in patches if p.op in ("keep", "revise")]
        assert sorted(keep_revise_indices) == [0, 1, 2]
        assert len(refined) == 3

    def test_add_content_missing_dropped(self) -> None:
        data = {"ac_patches": [{"op": "add"}]}  # no content
        refined, patches, _ = _compose(data)
        # only the 3 implicit keeps remain
        assert len(refined) == 3
        assert all(p.op == "keep" for p in patches)


class TestSatisficingBackstop:
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

    def test_shorter_list_full_rewrite_semantics(self) -> None:
        data = {"refined_acs": ["only one"]}
        refined, patches, settled = _compose(data)
        assert refined == ("only one",)
        assert patches == ()
        assert settled == ()

    def test_derive_legacy_patches_shorter_returns_none(self) -> None:
        assert _derive_legacy_patches(("a",), ("a", "b", "c")) is None

    def test_derive_legacy_patches_equal_lengths(self) -> None:
        patches = _derive_legacy_patches(("a", "X"), ("a", "b"))
        assert patches is not None
        assert patches[0].op == "keep"
        assert patches[1].op == "revise"
        assert patches[1].content == "X"


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
        assert 0 in out.settled_ac_indices
        assert 2 in out.settled_ac_indices
        assert 1 not in out.settled_ac_indices


class _FakeAdapter:
    def __init__(self, content: str) -> None:
        self.content = content
        self._max_turns = 1

    async def complete(self, messages, config):  # type: ignore[no-untyped-def]
        from ouroboros.core.types import Result
        from ouroboros.providers.base import CompletionResponse, UsageInfo

        return Result.ok(
            CompletionResponse(
                content=self.content,
                model=config.model,
                usage=UsageInfo(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            )
        )
