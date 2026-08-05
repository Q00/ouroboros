"""Focused-evolution frugality is paired, measured, and quality-vetoed."""

from types import SimpleNamespace

import pytest

from ouroboros.core.lineage import ACResult, EvaluationSummary
from ouroboros.core.seed import OntologyField, OntologySchema, Seed, SeedMetadata
from ouroboros.events.base import BaseEvent
from ouroboros.evolution.focus import EvolutionFocus
from ouroboros.evolution.frugality import (
    OBSERVATION_EVENT,
    PROOF_EVENT,
    EvolutionFrugalityArm,
    EvolutionFrugalityObservation,
    EvolutionFrugalityStatus,
    ProjectBaseline,
    build_observation,
    comparison_key,
    evaluate_observations,
    evaluate_pair,
    query_execution_evidence_events,
    record_generation,
)
from ouroboros.evolution.provider_usage import ProviderUsageSummary
from ouroboros.persistence.event_store import EventStore


def _observation(
    arm: EvolutionFrugalityArm,
    *,
    receipt: str,
    workspace: str,
    tokens: float | None,
    frozen: tuple[int, ...] = (),
    approved: bool = True,
    passed: tuple[int, ...] = (0, 1),
    regressed: tuple[int, ...] = (),
    grounding: int | None = 0,
    evaluation_score: float | None = 0.9,
    evaluation_drift_score: float | None = 0.1,
    evaluation_reward_hacking_risk: float | None = 0.1,
    highest_stage_passed: int | None = 2,
    ac_scores: tuple[float | None, ...] = (0.9, 0.9),
) -> EvolutionFrugalityObservation:
    expected_attempts = 10 if arm is EvolutionFrugalityArm.FULL_GRAPH else 6
    active = (1,) if frozen else (0, 1)
    attempt_counts = (expected_attempts,) if len(active) == 1 else (4, 6)
    execution_assignments = tuple(
        (
            index,
            ("sha256:" + "d" * 64,) * attempt_counts[offset],
        )
        for offset, index in enumerate(active)
    )
    return EvolutionFrugalityObservation(
        receipt_id=receipt,
        comparison_key="sha256:paired",
        lineage_id=receipt,
        generation_number=2,
        execution_id=f"{receipt}:2",
        arm=arm,
        workspace_path=workspace,
        baseline_commit="abc123",
        baseline_clean=True,
        active_ac_indices=active,
        frozen_ac_indices=frozen,
        executed_ac_indices=active,
        total_node_count=2,
        evaluated_seed_fingerprint="sha256:" + "b" * 64,
        token_spend=tokens,
        execution_token_spend=tokens,
        token_event_count=expected_attempts if tokens is not None else 0,
        expected_attempt_count=expected_attempts,
        attempt_identity_digest="sha256:" + "a" * 64,
        execution_configuration_fingerprint="sha256:" + "d" * 64,
        execution_configuration_assignments=execution_assignments,
        token_evidence_complete=tokens is not None,
        provider_token_spend=0,
        provider_call_count=0,
        provider_identity_digest="sha256:" + "c" * 64,
        provider_configuration_fingerprint="sha256:" + "e" * 64,
        provider_configuration_assignments=(),
        provider_evidence_complete=True,
        generation_token_evidence_complete=tokens is not None,
        wall_time_seconds=10 if arm is EvolutionFrugalityArm.FULL_GRAPH else 6,
        runtime_calls=expected_attempts,
        final_approved=approved,
        evaluation_score=evaluation_score,
        evaluation_drift_score=evaluation_drift_score,
        evaluation_reward_hacking_risk=evaluation_reward_hacking_risk,
        highest_stage_passed=highest_stage_passed,
        passed_ac_indices=passed,
        failed_ac_indices=tuple(index for index in (0, 1) if index not in passed),
        ac_score_assignments=tuple(enumerate(ac_scores)),
        regressed_ac_indices=regressed,
        grounding_observations=expected_attempts if grounding is not None else 0,
        grounding_regressions=grounding,
        grounding_evidence_complete=grounding is not None,
        ac_evidence_complete=True,
    )


def _provider_usage() -> ProviderUsageSummary:
    return ProviderUsageSummary(
        call_count=0,
        token_spend=0,
        identity_digest="sha256:" + "c" * 64,
        configuration_fingerprint="sha256:" + "e" * 64,
        configuration_assignments=(),
        complete=True,
    )


def _pair(*, control_tokens: float = 100, treatment_tokens: float = 70):
    control = _observation(
        EvolutionFrugalityArm.FULL_GRAPH,
        receipt="control",
        workspace="/tmp/control",
        tokens=control_tokens,
    )
    treatment = _observation(
        EvolutionFrugalityArm.FOCUSED,
        receipt="treatment",
        workspace="/tmp/treatment",
        tokens=treatment_tokens,
        frozen=(0,),
    )
    return control, treatment


def _seed(seed_id: str = "seed_one") -> Seed:
    return Seed(
        metadata=SeedMetadata(seed_id=seed_id, ambiguity_score=0.1),
        goal="Build the product",
        constraints=("preserve quality",),
        acceptance_criteria=("first", "second"),
        ontology_schema=OntologySchema(
            name="product",
            description="product",
            fields=(OntologyField(name="item", field_type="entity", description="item"),),
        ),
    )


def _evaluation(*passed: bool, seed: Seed | None = None) -> EvaluationSummary:
    evaluated_seed = seed or _seed()
    return EvaluationSummary(
        final_approved=all(passed),
        highest_stage_passed=2,
        score=sum(passed) / len(passed),
        ac_results=tuple(
            ACResult(
                ac_index=index,
                ac_content=evaluated_seed.acceptance_criteria[index].description,
                semantic_ac_key=evaluated_seed.acceptance_criteria[index].semantic_ac_key,
                passed=value,
            )
            for index, value in enumerate(passed)
        ),
    )


def test_pair_passes_only_with_measured_savings_and_preserved_quality() -> None:
    control, treatment = _pair()

    proof = evaluate_pair(control, treatment)

    assert proof.status is EvolutionFrugalityStatus.PASS
    assert proof.token_reduction_pct == 30.0
    assert proof.wall_time_reduction_pct == 40.0
    assert proof.call_reduction_pct == 40.0
    assert proof.quality_vetoes == ()


def test_legacy_v1_receipts_cannot_be_reused_as_current_passes() -> None:
    control, treatment = _pair()
    proof = evaluate_pair(control, treatment)

    with pytest.raises(ValueError):
        EvolutionFrugalityObservation.model_validate(
            {**control.model_dump(mode="json"), "schema_version": 1}
        )
    with pytest.raises(ValueError):
        type(proof).model_validate({**proof.model_dump(mode="json"), "schema_version": 1})


def test_complete_receipt_rejects_assignment_count_mismatch() -> None:
    control, _ = _pair()
    data = control.model_dump(mode="json")
    data["execution_configuration_assignments"][0][1].pop()

    with pytest.raises(ValueError, match="every expected attempt"):
        EvolutionFrugalityObservation.model_validate(data)


@pytest.mark.parametrize("role", ["", "   ", "unknown", " unknown ", "UNKNOWN", " Unknown "])
def test_complete_receipt_rejects_invalid_persisted_provider_role(role: str) -> None:
    control, _ = _pair()
    data = control.model_dump(mode="json")
    data.update(
        {
            "execution_token_spend": 98,
            "provider_token_spend": 2,
            "provider_call_count": 1,
            "provider_configuration_assignments": [[role, ["sha256:" + "e" * 64]]],
            "runtime_calls": control.expected_attempt_count + 1,
        }
    )

    with pytest.raises(ValueError, match="every provider call"):
        EvolutionFrugalityObservation.model_validate(data)


def test_complete_receipt_rejects_zero_spend_for_nonempty_provider_surface() -> None:
    control, _ = _pair()
    data = control.model_dump(mode="json")
    data.update(
        {
            "provider_call_count": 1,
            "provider_configuration_assignments": [["reflect", ["sha256:" + "e" * 64]]],
            "runtime_calls": control.expected_attempt_count + 1,
        }
    )

    with pytest.raises(ValueError, match="every provider call"):
        EvolutionFrugalityObservation.model_validate(data)


def test_pair_defensively_rejects_zero_spend_for_nonempty_provider_surface() -> None:
    control, treatment = _pair()
    invalid = {
        "provider_call_count": 1,
        "provider_configuration_assignments": (("reflect", ("sha256:" + "e" * 64,)),),
    }
    control = control.model_copy(
        update={**invalid, "runtime_calls": control.expected_attempt_count + 1}
    )
    treatment = treatment.model_copy(
        update={**invalid, "runtime_calls": treatment.expected_attempt_count + 1}
    )

    proof = evaluate_pair(control, treatment)

    assert proof.status is EvolutionFrugalityStatus.INSUFFICIENT_DATA
    assert "provider-call token attribution" in proof.reason


def test_pair_fails_when_savings_are_below_threshold() -> None:
    control, treatment = _pair(treatment_tokens=95)

    proof = evaluate_pair(control, treatment)

    assert proof.status is EvolutionFrugalityStatus.FAIL_NO_SAVINGS
    assert proof.token_reduction_pct == 5.0


def test_pair_rejects_different_final_semantic_seed_contracts() -> None:
    control, treatment = _pair()
    treatment = treatment.model_copy(update={"evaluated_seed_fingerprint": "sha256:" + "d" * 64})

    proof = evaluate_pair(control, treatment)

    assert proof.status is EvolutionFrugalityStatus.INSUFFICIENT_DATA
    assert "final semantic Seed contracts differ" in proof.reason


def test_pair_rejects_primary_configuration_swapped_between_root_acs() -> None:
    control, treatment = _pair()
    control = control.model_copy(
        update={
            "active_ac_indices": (0, 1, 2),
            "executed_ac_indices": (0, 1, 2),
            "total_node_count": 3,
            "passed_ac_indices": (0, 1, 2),
            "ac_score_assignments": ((0, 0.9), (1, 0.9), (2, 0.9)),
            "execution_configuration_assignments": (
                (0, ("sha256:" + "a" * 64,) * 4),
                (1, ("sha256:" + "a" * 64,) * 3),
                (2, ("sha256:" + "b" * 64,) * 3),
            ),
        }
    )
    treatment = treatment.model_copy(
        update={
            "active_ac_indices": (1, 2),
            "frozen_ac_indices": (0,),
            "executed_ac_indices": (1, 2),
            "total_node_count": 3,
            "passed_ac_indices": (0, 1, 2),
            "ac_score_assignments": ((0, 0.9), (1, 0.9), (2, 0.9)),
            "execution_configuration_assignments": (
                (1, ("sha256:" + "b" * 64,) * 3),
                (2, ("sha256:" + "a" * 64,) * 3),
            ),
        }
    )

    proof = evaluate_pair(control, treatment)

    assert proof.status is EvolutionFrugalityStatus.INSUFFICIENT_DATA
    assert "active root AC" in proof.reason


def test_full_graph_control_must_execute_every_seed_root() -> None:
    control, treatment = _pair()
    control_data = control.model_dump(mode="json")
    control_data.update(
        {
            "active_ac_indices": (1,),
            "executed_ac_indices": (1,),
            "token_event_count": 6,
            "expected_attempt_count": 6,
            "execution_configuration_assignments": ((1, ("sha256:" + "d" * 64,) * 6),),
            "runtime_calls": 6,
            "grounding_observations": 6,
        }
    )
    incomplete_control = EvolutionFrugalityObservation.model_validate(control_data)

    proof = evaluate_pair(incomplete_control, treatment)

    assert proof.status is EvolutionFrugalityStatus.INSUFFICIENT_DATA
    assert "complete Seed graph" in proof.reason


def test_pair_rejects_configuration_multiplicity_swap_within_one_root() -> None:
    control, treatment = _pair()
    expensive = "sha256:" + "a" * 64
    cheap = "sha256:" + "b" * 64
    control = control.model_copy(
        update={
            "execution_configuration_assignments": (
                (0, (expensive,)),
                (1, (expensive,) * 8 + (cheap,)),
            )
        }
    )
    treatment = treatment.model_copy(
        update={"execution_configuration_assignments": ((1, (expensive,) + (cheap,) * 5),)}
    )

    proof = evaluate_pair(control, treatment)

    assert proof.status is EvolutionFrugalityStatus.INSUFFICIENT_DATA
    assert "active root AC" in proof.reason


def test_pair_rejects_auxiliary_configuration_swapped_between_roles() -> None:
    control, treatment = _pair()
    control = control.model_copy(
        update={
            "execution_token_spend": 99,
            "provider_token_spend": 1,
            "provider_call_count": 2,
            "runtime_calls": control.expected_attempt_count + 2,
            "provider_configuration_assignments": (
                ("semantic_evaluation", ("sha256:" + "a" * 64,)),
                ("wonder", ("sha256:" + "b" * 64,)),
            ),
        }
    )
    treatment = treatment.model_copy(
        update={
            "execution_token_spend": 68,
            "provider_token_spend": 2,
            "provider_call_count": 2,
            "runtime_calls": treatment.expected_attempt_count + 2,
            "provider_configuration_assignments": (
                ("semantic_evaluation", ("sha256:" + "b" * 64,)),
                ("wonder", ("sha256:" + "a" * 64,)),
            ),
        }
    )

    proof = evaluate_pair(control, treatment)

    assert proof.status is EvolutionFrugalityStatus.INSUFFICIENT_DATA
    assert "differ by role" in proof.reason


@pytest.mark.parametrize(
    "authority",
    [
        "semantic_cwd",
        "child_environment",
        "worker_executable",
        "adapter_implementation",
    ],
)
def test_pair_returns_insufficient_data_for_runtime_authority_drift(authority: str) -> None:
    control, treatment = _pair()
    control_digest, treatment_digest = ("a", "b") if authority == "semantic_cwd" else ("c", "d")
    control = control.model_copy(
        update={
            "execution_token_spend": 99,
            "provider_token_spend": 1,
            "provider_call_count": 1,
            "runtime_calls": control.expected_attempt_count + 1,
            "provider_configuration_fingerprint": "sha256:" + "1" * 64,
            "provider_configuration_assignments": (
                ("executor_auxiliary", ("sha256:" + control_digest * 64,)),
            ),
        }
    )
    treatment = treatment.model_copy(
        update={
            "execution_token_spend": 69,
            "provider_token_spend": 1,
            "provider_call_count": 1,
            "runtime_calls": treatment.expected_attempt_count + 1,
            "provider_configuration_fingerprint": "sha256:" + "2" * 64,
            "provider_configuration_assignments": (
                ("executor_auxiliary", ("sha256:" + treatment_digest * 64,)),
            ),
        }
    )

    proof = evaluate_pair(control, treatment)

    assert proof.status is EvolutionFrugalityStatus.INSUFFICIENT_DATA
    assert "differ by role" in proof.reason


def test_pair_rejects_configuration_multiplicity_swap_within_one_role() -> None:
    control, treatment = _pair()
    expensive = "sha256:" + "a" * 64
    cheap = "sha256:" + "b" * 64
    control = control.model_copy(
        update={
            "execution_token_spend": 90,
            "provider_token_spend": 10,
            "provider_call_count": 10,
            "runtime_calls": control.expected_attempt_count + 10,
            "provider_configuration_assignments": (("reflect", (expensive,) * 9 + (cheap,)),),
        }
    )
    treatment = treatment.model_copy(
        update={
            "execution_token_spend": 60,
            "provider_token_spend": 10,
            "provider_call_count": 10,
            "runtime_calls": treatment.expected_attempt_count + 10,
            "provider_configuration_assignments": (("reflect", (expensive,) + (cheap,) * 9),),
        }
    )

    proof = evaluate_pair(control, treatment)

    assert proof.status is EvolutionFrugalityStatus.INSUFFICIENT_DATA
    assert "differ by role" in proof.reason


def test_pair_rejects_ambiguous_call_removal_within_shared_role() -> None:
    control, treatment = _pair()
    first = "sha256:" + "a" * 64
    middle = "sha256:" + "b" * 64
    control = control.model_copy(
        update={
            "execution_token_spend": 97,
            "provider_token_spend": 3,
            "provider_call_count": 3,
            "runtime_calls": control.expected_attempt_count + 3,
            "provider_configuration_assignments": (("reflect", (first, middle, first)),),
        }
    )
    treatment = treatment.model_copy(
        update={
            "execution_token_spend": 68,
            "provider_token_spend": 2,
            "provider_call_count": 2,
            "runtime_calls": treatment.expected_attempt_count + 2,
            "provider_configuration_assignments": (("reflect", (middle, first)),),
        }
    )

    proof = evaluate_pair(control, treatment)

    assert proof.status is EvolutionFrugalityStatus.INSUFFICIENT_DATA
    assert "differ by role" in proof.reason


def test_pair_allows_eliminating_an_entire_control_only_role() -> None:
    control, treatment = _pair()
    control = control.model_copy(
        update={
            "execution_token_spend": 90,
            "provider_token_spend": 10,
            "provider_call_count": 2,
            "runtime_calls": control.expected_attempt_count + 2,
            "provider_configuration_assignments": (
                ("executor_shadow_replay", (("sha256:" + "a" * 64),) * 2),
            ),
        }
    )

    proof = evaluate_pair(control, treatment)

    assert proof.status is EvolutionFrugalityStatus.PASS


def test_pair_prices_all_generation_provider_calls() -> None:
    control, treatment = _pair()
    control = control.model_copy(
        update={
            "execution_token_spend": 80,
            "provider_token_spend": 20,
            "provider_call_count": 1,
            "provider_configuration_assignments": (("reflect", ("sha256:" + "e" * 64,)),),
            "runtime_calls": control.expected_attempt_count + 1,
        }
    )
    treatment = treatment.model_copy(
        update={
            "token_spend": 105,
            "execution_token_spend": 40,
            "provider_token_spend": 65,
            "provider_call_count": 1,
            "provider_configuration_assignments": (("reflect", ("sha256:" + "e" * 64,)),),
            "runtime_calls": treatment.expected_attempt_count + 1,
        }
    )

    proof = evaluate_pair(control, treatment)

    assert proof.status is EvolutionFrugalityStatus.FAIL_NO_SAVINGS
    assert proof.token_reduction_pct == -5.0


def test_unrounded_reduction_must_reach_threshold() -> None:
    control, treatment = _pair(treatment_tokens=90.00004)

    proof = evaluate_pair(control, treatment)

    assert proof.status is EvolutionFrugalityStatus.FAIL_NO_SAVINGS
    assert proof.token_reduction_pct is not None
    assert proof.token_reduction_pct < 10.0


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"approved": False}, "final approval regressed"),
        ({"passed": (1,), "regressed": (0,)}, "previously passing ACs regressed"),
        ({"grounding": 1}, "grounding regressed"),
        ({"evaluation_score": 0.8}, "evaluation score regressed"),
        ({"evaluation_drift_score": 0.2}, "evaluation drift regressed"),
        (
            {"evaluation_reward_hacking_risk": 0.2},
            "reward-hacking risk regressed",
        ),
        ({"highest_stage_passed": 1}, "highest evaluation stage regressed"),
        ({"ac_scores": (0.9, 0.8)}, "AC 1 score regressed"),
    ],
)
def test_quality_regression_vetoes_resource_savings(
    changes: dict[str, object], expected: str
) -> None:
    control, _ = _pair()
    treatment = _observation(
        EvolutionFrugalityArm.FOCUSED,
        receipt="treatment",
        workspace="/tmp/treatment",
        tokens=10,
        frozen=(0,),
        **changes,
    )

    proof = evaluate_pair(control, treatment)

    assert proof.status is EvolutionFrugalityStatus.FAIL_QUALITY_REGRESSION
    assert any(expected in veto for veto in proof.quality_vetoes)
    assert proof.token_reduction_pct is None


def test_asymmetric_quality_metric_evidence_is_insufficient() -> None:
    control, _ = _pair()
    treatment = _observation(
        EvolutionFrugalityArm.FOCUSED,
        receipt="treatment",
        workspace="/tmp/treatment",
        tokens=10,
        frozen=(0,),
        evaluation_drift_score=None,
    )

    proof = evaluate_pair(control, treatment)

    assert proof.status is EvolutionFrugalityStatus.INSUFFICIENT_DATA
    assert "evaluation drift evidence is not comparable" in proof.reason


@pytest.mark.parametrize(
    "treatment",
    [
        _observation(
            EvolutionFrugalityArm.FOCUSED,
            receipt="dirty",
            workspace="/tmp/treatment",
            tokens=70,
            frozen=(0,),
        ).model_copy(update={"baseline_clean": False}),
        _observation(
            EvolutionFrugalityArm.FOCUSED,
            receipt="same-worktree",
            workspace="/tmp/control",
            tokens=70,
            frozen=(0,),
        ),
        _observation(
            EvolutionFrugalityArm.FOCUSED,
            receipt="no-grounding",
            workspace="/tmp/treatment",
            tokens=70,
            frozen=(0,),
            grounding=None,
        ),
        _observation(
            EvolutionFrugalityArm.FOCUSED,
            receipt="no-tokens",
            workspace="/tmp/treatment",
            tokens=None,
            frozen=(0,),
        ),
    ],
)
def test_missing_isolation_or_measurement_is_insufficient(
    treatment: EvolutionFrugalityObservation,
) -> None:
    control, _ = _pair()

    proof = evaluate_pair(control, treatment)

    assert proof.status is EvolutionFrugalityStatus.INSUFFICIENT_DATA


def test_node_count_reduction_alone_does_not_prove_frugality() -> None:
    control, treatment = _pair()
    treatment = treatment.model_copy(update={"token_spend": None, "token_event_count": 0})

    proof = evaluate_pair(control, treatment)

    assert treatment.frozen_ac_indices == (0,)
    assert proof.status is EvolutionFrugalityStatus.INSUFFICIENT_DATA
    assert "token attribution" in proof.reason


def test_current_observation_pairs_with_opposite_arm() -> None:
    control, treatment = _pair()

    proof = evaluate_observations(treatment, [treatment, control])

    assert proof.status is EvolutionFrugalityStatus.PASS
    assert proof.control_receipt_id == "control"


def test_comparison_key_ignores_run_identity_but_tracks_starting_semantics() -> None:
    evaluation = _evaluation(True, False)
    baseline = ProjectBaseline(
        workspace_path="/tmp/worktree",
        git_commit="abc123",
        clean=True,
    )

    first = comparison_key(_seed("seed_one"), evaluation, baseline)
    second = comparison_key(_seed("seed_two"), evaluation, baseline)
    changed = comparison_key(_seed("seed_two"), _evaluation(False, False), baseline)

    assert first == second
    assert first != changed


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("goal", "Build a different product"),
        ("task_type", "research"),
        (
            "brownfield_context",
            {
                "project_type": "brownfield",
                "existing_patterns": ["repository pattern"],
            },
        ),
        ("constraints", ["different constraint"]),
        (
            "acceptance_criteria",
            [
                {
                    "description": "first",
                    "verify_command": "python -m pytest",
                    "expected_artifacts": ["report.json"],
                },
                "second",
            ],
        ),
        (
            "ontology_schema",
            {
                "name": "product",
                "description": "different ontology",
                "fields": [{"name": "item", "type": "entity", "description": "item"}],
            },
        ),
        (
            "evaluation_principles",
            [{"name": "quality", "description": "Preserve quality", "weight": 0.8}],
        ),
        (
            "exit_conditions",
            [
                {
                    "name": "done",
                    "description": "All work is done",
                    "criteria": "Every AC passes",
                }
            ],
        ),
        ("plugin_contract", {"mode": "strict", "limits": [1, 2]}),
    ],
)
def test_comparison_key_tracks_complete_seed_semantics(field: str, value: object) -> None:
    seed = _seed()
    seed_data = seed.to_dict()
    seed_data[field] = value
    changed = Seed.from_dict(seed_data)
    evaluation = _evaluation(True, False, seed=seed)
    baseline = ProjectBaseline(git_commit="abc123", clean=True)

    assert comparison_key(seed, evaluation, baseline) != comparison_key(
        changed,
        evaluation,
        baseline,
    )


def test_comparison_key_tracks_semantic_seed_metadata() -> None:
    seed = _seed()
    seed_data = seed.to_dict()
    seed_data["metadata"] = {**seed_data["metadata"], "ambiguity_score": 0.2}
    changed = Seed.from_dict(seed_data)
    evaluation = _evaluation(True, False, seed=seed)
    baseline = ProjectBaseline(git_commit="abc123", clean=True)

    assert comparison_key(seed, evaluation, baseline) != comparison_key(
        changed,
        evaluation,
        baseline,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("seed_id", "seed_other"),
        ("created_at", "2030-01-01T00:00:00Z"),
        ("interview_id", "interview-other"),
        ("parent_seed_id", "seed-parent-other"),
    ],
)
def test_comparison_key_excludes_only_volatile_seed_identity(field: str, value: object) -> None:
    seed = _seed()
    seed_data = seed.to_dict()
    seed_data["metadata"] = {**seed_data["metadata"], field: value}
    changed = Seed.from_dict(seed_data)
    evaluation = _evaluation(True, False, seed=seed)
    baseline = ProjectBaseline(git_commit="abc123", clean=True)

    assert comparison_key(seed, evaluation, baseline) == comparison_key(
        changed,
        evaluation,
        baseline,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("highest_stage_passed", 3),
        ("drift_score", 0.2),
        ("reward_hacking_risk", 0.3),
        ("failure_reason", "new failure context"),
        (
            "task_results",
            [
                {
                    "task_index": 0,
                    "task_content": "implementation",
                    "status": "completed",
                    "completed": True,
                }
            ],
        ),
        (
            "feedback_metadata",
            [
                {
                    "code": "changed",
                    "message": "changed feedback",
                }
            ],
        ),
        ("execution_completion_status", "failed"),
    ],
)
def test_comparison_key_tracks_complete_previous_evaluation(field: str, value: object) -> None:
    seed = _seed()
    evaluation = _evaluation(True, False, seed=seed)
    evaluation_data = evaluation.model_dump(mode="json")
    evaluation_data[field] = value
    changed = EvaluationSummary.model_validate(evaluation_data)
    baseline = ProjectBaseline(git_commit="abc123", clean=True)

    assert comparison_key(seed, evaluation, baseline) != comparison_key(
        seed,
        changed,
        baseline,
    )


def test_observation_uses_runtime_tokens_and_fail_closed_grounding() -> None:
    dispatched = BaseEvent(
        type="execution.ac.attempt.dispatched",
        aggregate_type="execution",
        aggregate_id="exec",
        data={
            "execution_id": "exec",
            "ac_id": "leaf-1",
            "retry_attempt": 1,
            "session_attempt_id": "leaf-1-attempt-2",
            "ac_dispatch_id": "a" * 32,
            "root_ac_index": 1,
            "dispatch_kind": "primary",
            "request_authority_digest": "sha256:" + "f" * 64,
        },
    )
    token = BaseEvent(
        type="execution.ac.token_attribution.reported",
        aggregate_type="execution",
        aggregate_id="exec",
        data={
            "execution_id": "exec",
            "ac_id": "leaf-1",
            "token_spend": 40,
            "token_source": "runtime_usage",
            "retry_attempt": 1,
            "session_attempt_id": "leaf-1-attempt-2",
            "ac_dispatch_id": "a" * 32,
            "root_ac_index": 1,
            "model": "test-model",
            "effective_model": "test-model",
            "request_authority_digest": "sha256:" + "f" * 64,
            "model_tier": "standard",
            "model_mode": "enforced",
            "effort_level": "high",
            "effort_mode": "enforced",
            "runtime_backend": "test",
            "llm_backend": "test-provider",
            "permission_mode": "acceptEdits",
        },
    )
    grounded = BaseEvent(
        type="execution.ac.deliver_verdict",
        aggregate_type="execution",
        aggregate_id="exec",
        data={
            "execution_id": "exec",
            "ac_id": "leaf-1",
            "retry_attempt": 1,
            "session_attempt_id": "leaf-1-attempt-2",
            "ac_dispatch_id": "a" * 32,
            "root_ac_index": 1,
            "traceguard_verdict": "accepted",
            "unsupported_claim_rate": 0.0,
            "grounding_regression": False,
        },
    )
    seed = _seed()

    observation = build_observation(
        lineage_id="lineage",
        generation_number=2,
        execution_id="exec",
        parent_seed=seed,
        current_seed=seed,
        previous_evaluation=_evaluation(True, False, seed=seed),
        current_evaluation=_evaluation(True, True, seed=seed),
        focus=EvolutionFocus(active_ac_indices=(1,), frozen_ac_indices=(0,), reason="test"),
        baseline=ProjectBaseline(
            workspace_path="/tmp/treatment",
            git_commit="abc123",
            clean=True,
        ),
        focused_evolution=True,
        scoped_reexecution=True,
        execution_events=[dispatched, token, grounded],
        executor_result=SimpleNamespace(duration_seconds=3.5, messages_processed=4),
        provider_usage=_provider_usage(),
    )

    assert observation.token_spend == 40
    assert observation.token_event_count == 1
    assert observation.expected_attempt_count == 1
    assert observation.token_evidence_complete is True
    assert observation.retry_calls == 1
    assert observation.grounding_observations == 1
    assert observation.grounding_regressions == 0
    assert observation.grounding_evidence_complete is True
    assert observation.ac_evidence_complete is True
    assert observation.regressed_ac_indices == ()


def _complete_execution_events(execution_id: str = "exec") -> list[BaseEvent]:
    common = {
        "execution_id": execution_id,
        "ac_id": "leaf-1",
        "retry_attempt": 0,
        "session_attempt_id": "leaf-1-attempt-1",
        "ac_dispatch_id": "a" * 32,
        "root_ac_index": 1,
        "model": "test-model",
        "effective_model": "test-model",
        "request_authority_digest": "sha256:" + "f" * 64,
        "model_tier": "standard",
        "model_mode": "enforced",
        "effort_level": "high",
        "effort_mode": "enforced",
        "runtime_backend": "test",
        "llm_backend": "test-provider",
        "permission_mode": "acceptEdits",
    }
    return [
        BaseEvent(
            type="execution.ac.attempt.dispatched",
            aggregate_type="execution",
            aggregate_id=execution_id,
            data={**common, "dispatch_kind": "primary"},
        ),
        BaseEvent(
            type="execution.ac.token_attribution.reported",
            aggregate_type="execution",
            aggregate_id=execution_id,
            data={**common, "token_spend": 40, "token_source": "runtime_usage"},
        ),
        BaseEvent(
            type="execution.ac.deliver_verdict",
            aggregate_type="execution",
            aggregate_id=execution_id,
            data={
                **common,
                "traceguard_verdict": "accepted",
                "unsupported_claim_rate": 0.0,
                "grounding_regression": False,
            },
        ),
    ]


@pytest.mark.parametrize(
    ("event_index", "field", "value"),
    [
        (1, "session_attempt_id", "unrelated-token-attempt"),
        (2, "session_attempt_id", "unrelated-verdict-attempt"),
        (1, "ac_dispatch_id", "b" * 32),
        (2, "ac_dispatch_id", "b" * 32),
    ],
)
def test_receipts_must_bind_the_exact_primary_dispatch(
    event_index: int,
    field: str,
    value: str,
) -> None:
    events = _complete_execution_events()
    original = events[event_index]
    events[event_index] = original.model_copy(update={"data": {**original.data, field: value}})

    observation = _built_observation(events)

    assert observation.token_evidence_complete is (event_index != 1)
    assert observation.grounding_evidence_complete is (event_index != 2)
    if event_index == 1:
        assert observation.token_spend is None
    else:
        assert observation.token_spend == 40.0
    assert observation.evidence_issues


def test_primary_dispatch_roots_must_exactly_cover_active_working_set() -> None:
    events = _complete_execution_events()
    events = [
        event.model_copy(update={"data": {**event.data, "root_ac_index": 0}}) for event in events
    ]

    observation = _built_observation(events)

    assert observation.executed_ac_indices == (0,)
    assert observation.active_ac_indices == (1,)
    assert "primary dispatch roots" in "; ".join(observation.evidence_issues)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("model", "other-model"),
        ("effective_model", "other-effective-model"),
        ("model_tier", "frugal"),
        ("model_mode", "advised"),
        ("runtime_backend", "other-runtime"),
        ("llm_backend", "other-provider"),
        ("effort_level", "low"),
        ("effort_mode", "advised"),
        ("permission_mode", "bypassPermissions"),
    ),
)
def test_execution_configuration_fingerprint_tracks_runtime_dials(
    field: str,
    value: str,
) -> None:
    baseline = _built_observation(_complete_execution_events())
    events = _complete_execution_events()
    token = events[1]
    events[1] = token.model_copy(update={"data": {**token.data, field: value}})

    changed = _built_observation(events)

    assert changed.token_evidence_complete is True
    assert (
        changed.execution_configuration_fingerprint != baseline.execution_configuration_fingerprint
    )


def test_execution_configuration_binds_dispatch_request_authority() -> None:
    baseline = _built_observation(_complete_execution_events())
    events = _complete_execution_events()
    changed_digest = "sha256:" + "e" * 64
    for index in (0, 1):
        event = events[index]
        events[index] = event.model_copy(
            update={"data": {**event.data, "request_authority_digest": changed_digest}}
        )

    changed = _built_observation(events)

    assert baseline.token_evidence_complete is True
    assert changed.token_evidence_complete is True
    assert (
        changed.execution_configuration_assignments != baseline.execution_configuration_assignments
    )


def test_primary_configuration_assignments_follow_dispatch_not_token_completion_order() -> None:
    attempts: list[list[BaseEvent]] = []
    for suffix, model in (("a", "model-a"), ("b", "model-b")):
        attempt = []
        for event in _complete_execution_events():
            attempt.append(
                event.model_copy(
                    update={
                        "data": {
                            **event.data,
                            "ac_id": f"leaf-{suffix}",
                            "session_attempt_id": f"leaf-{suffix}-attempt-1",
                            "ac_dispatch_id": suffix * 32,
                            "model": model,
                        }
                    }
                )
            )
        attempts.append(attempt)

    dispatches = [attempts[0][0], attempts[1][0]]
    verdicts = [attempts[0][2], attempts[1][2]]
    dispatch_then_completion = _built_observation(
        dispatches + [attempts[0][1], attempts[1][1]] + verdicts
    )
    dispatch_then_reverse_completion = _built_observation(
        dispatches + [attempts[1][1], attempts[0][1]] + verdicts
    )
    reverse_dispatch = _built_observation(
        list(reversed(dispatches)) + [attempts[0][1], attempts[1][1]] + verdicts
    )

    assert dispatch_then_completion.token_evidence_complete is True
    assert dispatch_then_reverse_completion.token_evidence_complete is True
    assert (
        dispatch_then_completion.execution_configuration_assignments
        == dispatch_then_reverse_completion.execution_configuration_assignments
    )
    assert (
        dispatch_then_completion.execution_configuration_assignments
        != reverse_dispatch.execution_configuration_assignments
    )


@pytest.mark.parametrize(
    "field",
    (
        "model",
        "effective_model",
        "model_tier",
        "model_mode",
        "effort_level",
        "effort_mode",
        "runtime_backend",
        "llm_backend",
        "permission_mode",
        "request_authority_digest",
    ),
)
@pytest.mark.parametrize("invalid_value", [None, "", " unknown "])
def test_primary_configuration_requires_every_runtime_dial(
    field: str,
    invalid_value: object,
) -> None:
    events = _complete_execution_events()
    token = events[1]
    events[1] = token.model_copy(update={"data": {**token.data, field: invalid_value}})

    observation = _built_observation(events)

    assert observation.token_evidence_complete is False
    assert observation.token_spend is None
    assert "token/configuration attribution" in "; ".join(observation.evidence_issues)


def test_primary_runtime_token_aggregate_overflow_fails_closed() -> None:
    events: list[BaseEvent] = []
    for suffix in ("1", "2"):
        for event in _complete_execution_events():
            data = {
                **event.data,
                "ac_id": f"leaf-{suffix}",
                "session_attempt_id": f"leaf-{suffix}-attempt-1",
                "ac_dispatch_id": suffix * 32,
            }
            if event.type == "execution.ac.token_attribution.reported":
                data["token_spend"] = 1e308
            events.append(event.model_copy(update={"data": data}))

    observation = _built_observation(events)

    assert observation.token_evidence_complete is False
    assert observation.token_spend is None
    assert "aggregate primary runtime-token usage is non-finite" in observation.evidence_issues


def test_total_generation_token_aggregate_overflow_fails_closed() -> None:
    events = _complete_execution_events()
    token = events[1]
    events[1] = token.model_copy(update={"data": {**token.data, "token_spend": 1e308}})
    provider_usage = ProviderUsageSummary(
        call_count=1,
        token_spend=1e308,
        identity_digest="sha256:" + "c" * 64,
        configuration_fingerprint="sha256:" + "e" * 64,
        configuration_assignments=(("reflect", ("sha256:" + "e" * 64,)),),
        complete=True,
    )

    observation = _built_observation(events, provider_usage=provider_usage)

    assert observation.token_evidence_complete is True
    assert observation.provider_evidence_complete is True
    assert observation.generation_token_evidence_complete is False
    assert observation.token_spend is None
    assert (
        "aggregate total-generation runtime-token usage is non-finite"
        in observation.evidence_issues
    )


def _built_observation(
    events: list[BaseEvent],
    *,
    current_evaluation: EvaluationSummary | None = None,
    provider_usage: ProviderUsageSummary | None = None,
) -> EvolutionFrugalityObservation:
    seed = _seed()
    return build_observation(
        lineage_id="lineage",
        generation_number=2,
        execution_id="exec",
        parent_seed=seed,
        current_seed=seed,
        previous_evaluation=_evaluation(True, False, seed=seed),
        current_evaluation=(
            current_evaluation
            if current_evaluation is not None
            else _evaluation(True, True, seed=seed)
        ),
        focus=EvolutionFocus(active_ac_indices=(1,), frozen_ac_indices=(0,), reason="test"),
        baseline=ProjectBaseline(
            workspace_path="/tmp/treatment",
            git_commit="abc123",
            clean=True,
        ),
        focused_evolution=True,
        scoped_reexecution=True,
        execution_events=events,
        executor_result=SimpleNamespace(duration_seconds=3.5, messages_processed=4),
        provider_usage=provider_usage or _provider_usage(),
    )


@pytest.mark.parametrize(
    "case",
    [
        "missing_dispatch",
        "duplicate_dispatch",
        "missing_token",
        "duplicate_token",
        "malformed_token",
        "zero_token",
        "unmatched_token",
        "missing_grounding",
        "duplicate_grounding",
        "malformed_grounding",
        "followup_dispatch",
        "missing_dispatch_authority",
        "mismatched_request_authority",
    ],
)
def test_runtime_evidence_requires_exact_attempt_coverage(case: str) -> None:
    events = _complete_execution_events()
    if case == "missing_dispatch":
        events.pop(0)
    elif case == "duplicate_dispatch":
        events.append(events[0].model_copy())
    elif case == "missing_token":
        events.pop(1)
    elif case == "duplicate_token":
        events.append(events[1].model_copy())
    elif case == "malformed_token":
        events[1] = events[1].model_copy(
            update={"data": {**events[1].data, "token_source": "character_estimate"}}
        )
    elif case == "zero_token":
        events[1] = events[1].model_copy(update={"data": {**events[1].data, "token_spend": 0}})
    elif case == "unmatched_token":
        events.append(events[1].model_copy(update={"data": {**events[1].data, "ac_id": "leaf-2"}}))
    elif case == "missing_grounding":
        events.pop(2)
    elif case == "duplicate_grounding":
        events.append(events[2].model_copy())
    elif case == "malformed_grounding":
        events[2] = events[2].model_copy(
            update={"data": {**events[2].data, "unsupported_claim_rate": 2.0}}
        )
    elif case == "followup_dispatch":
        events.append(
            events[0].model_copy(
                update={"data": {**events[0].data, "dispatch_kind": "session_signal_followup"}}
            )
        )
    elif case == "missing_dispatch_authority":
        dispatch_data = dict(events[0].data)
        dispatch_data.pop("request_authority_digest")
        events[0] = events[0].model_copy(update={"data": dispatch_data})
    elif case == "mismatched_request_authority":
        events[1] = events[1].model_copy(
            update={
                "data": {
                    **events[1].data,
                    "request_authority_digest": "sha256:" + "e" * 64,
                }
            }
        )

    observation = _built_observation(events)

    assert not (observation.token_evidence_complete and observation.grounding_evidence_complete)
    assert observation.evidence_issues


@pytest.mark.parametrize(
    "ac_results",
    [
        (ACResult(ac_index=0, ac_content="first", passed=True),),
        (
            ACResult(ac_index=0, ac_content="first", passed=True),
            ACResult(ac_index=0, ac_content="first", passed=True),
        ),
        (
            ACResult(ac_index=0, ac_content="first", passed=True),
            ACResult(ac_index=2, ac_content="third", passed=True),
        ),
        (
            ACResult(ac_index=0, ac_content="wrong", passed=True),
            ACResult(ac_index=1, ac_content="second", passed=True),
        ),
        (
            ACResult(
                ac_index=0,
                ac_content="first",
                semantic_ac_key="ac_0000000000000000",
                passed=True,
            ),
            ACResult(ac_index=1, ac_content="second", passed=True),
        ),
    ],
)
def test_observation_rejects_incomplete_or_stale_ac_evidence(
    ac_results: tuple[ACResult, ...],
) -> None:
    evaluation = EvaluationSummary(
        final_approved=True,
        highest_stage_passed=3,
        score=1.0,
        ac_results=ac_results,
    )

    observation = _built_observation(
        _complete_execution_events(),
        current_evaluation=evaluation,
    )

    assert observation.ac_evidence_complete is False
    assert observation.evidence_issues


@pytest.mark.parametrize(
    ("updates", "expected"),
    [
        (
            {"token_evidence_complete": False, "token_event_count": 1},
            "token attribution",
        ),
        (
            {"grounding_evidence_complete": False, "grounding_observations": 1},
            "TraceGuard",
        ),
        (
            {
                "ac_evidence_complete": False,
                "passed_ac_indices": (0,),
                "failed_ac_indices": (),
            },
            "AC verdict coverage",
        ),
    ],
)
def test_pair_never_passes_partial_evidence(
    updates: dict[str, object],
    expected: str,
) -> None:
    control, treatment = _pair()
    treatment = treatment.model_copy(update=updates)

    proof = evaluate_pair(control, treatment)

    assert proof.status is EvolutionFrugalityStatus.INSUFFICIENT_DATA
    assert expected in proof.reason


@pytest.mark.parametrize(
    "authority",
    [
        "organization",
        "headers",
        "api_version",
        "modify_params",
        "drop_params",
        "custom_client",
        "mock_transport",
        "proxy_trust",
        "transport_selection",
        "tls",
    ],
)
def test_pair_rejects_incomplete_litellm_dependency_authority(authority: str) -> None:
    control, treatment = _pair()
    treatment = treatment.model_copy(
        update={
            "provider_evidence_complete": False,
            "generation_token_evidence_complete": False,
            "evidence_issues": (f"unsealed LiteLLM {authority} authority",),
        }
    )

    proof = evaluate_pair(control, treatment)

    assert proof.status is EvolutionFrugalityStatus.INSUFFICIENT_DATA
    assert authority in proof.reason


async def _append_runtime_evidence(
    store: EventStore,
    execution_id: str,
    tokens: float,
    *,
    ac_ids: tuple[str, ...],
) -> None:
    for offset, ac_id in enumerate(ac_ids):
        root_ac_index = 1 if len(ac_ids) == 1 else offset
        common = {
            "execution_id": execution_id,
            "ac_id": ac_id,
            "retry_attempt": 0,
            "session_attempt_id": f"{ac_id}-attempt-1",
            "ac_dispatch_id": f"{offset + 1:032x}",
            "root_ac_index": root_ac_index,
            "request_authority_digest": "sha256:" + "f" * 64,
        }
        await store.append(
            BaseEvent(
                type="execution.ac.attempt.dispatched",
                aggregate_type="execution",
                aggregate_id=execution_id,
                data={**common, "dispatch_kind": "primary"},
            )
        )
        await store.append(
            BaseEvent(
                type="execution.ac.token_attribution.reported",
                aggregate_type="execution",
                aggregate_id=execution_id,
                data={
                    **common,
                    "token_spend": tokens / len(ac_ids),
                    "token_source": "runtime_usage",
                    "model": "test-model",
                    "effective_model": "test-model",
                    "model_tier": "standard",
                    "model_mode": "enforced",
                    "effort_level": "high",
                    "effort_mode": "enforced",
                    "runtime_backend": "test",
                    "llm_backend": "test-provider",
                    "permission_mode": "acceptEdits",
                },
            )
        )
        await store.append(
            BaseEvent(
                type="execution.ac.deliver_verdict",
                aggregate_type="execution",
                aggregate_id=execution_id,
                data={
                    **common,
                    "traceguard_verdict": "accepted",
                    "unsupported_claim_rate": 0.0,
                    "grounding_regression": False,
                },
            )
        )


async def test_recorded_control_completes_a_prior_focused_proof(tmp_path) -> None:
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'frugality.db'}")
    await store.initialize()
    seed = _seed()
    await _append_runtime_evidence(
        store,
        "treatment-exec",
        70,
        ac_ids=("treatment-ac-1",),
    )
    treatment = await record_generation(
        event_store=store,
        lineage_id="treatment-lineage",
        generation_number=2,
        execution_id="treatment-exec",
        parent_seed=seed,
        current_seed=seed,
        previous_evaluation=_evaluation(True, False, seed=seed),
        current_evaluation=_evaluation(True, True, seed=seed),
        focus=EvolutionFocus(active_ac_indices=(1,), frozen_ac_indices=(0,), reason="focused"),
        baseline=ProjectBaseline(
            workspace_path="/tmp/treatment-worktree",
            git_commit="abc123",
            clean=True,
        ),
        focused_evolution=True,
        scoped_reexecution=True,
        executor_result=SimpleNamespace(duration_seconds=7, messages_processed=7),
        provider_usage=_provider_usage(),
    )
    assert treatment is not None
    assert treatment.proof.status is EvolutionFrugalityStatus.INSUFFICIENT_DATA

    await _append_runtime_evidence(
        store,
        "control-exec",
        100,
        ac_ids=("control-ac-0", "control-ac-1"),
    )
    control = await record_generation(
        event_store=store,
        lineage_id="control-lineage",
        generation_number=2,
        execution_id="control-exec",
        parent_seed=seed,
        current_seed=seed,
        previous_evaluation=_evaluation(True, False, seed=seed),
        current_evaluation=_evaluation(True, True, seed=seed),
        focus=EvolutionFocus(active_ac_indices=(0, 1), frozen_ac_indices=(), reason="full"),
        baseline=ProjectBaseline(
            workspace_path="/tmp/control-worktree",
            git_commit="abc123",
            clean=True,
        ),
        focused_evolution=False,
        scoped_reexecution=False,
        executor_result=SimpleNamespace(duration_seconds=10, messages_processed=10),
        provider_usage=_provider_usage(),
    )

    assert control is not None
    assert control.proof.status is EvolutionFrugalityStatus.PASS
    assert control.proof.token_reduction_pct == 30.0
    observations = await store.query_events(event_type=OBSERVATION_EVENT, limit=10)
    proofs = await store.query_events(event_type=PROOF_EVENT, limit=10)
    assert len(observations) == 2
    assert len(proofs) == 2
    await store.close()


async def test_execution_evidence_collection_is_bounded_and_fails_closed() -> None:
    class _PagedEvidenceStore:
        event_count = 2_001

        async def query_execution_related_events(
            self,
            execution_id: str,
            *,
            event_type: str,
            limit: int,
            chronological: bool = False,
            cursor_after=None,
            cursor_through=None,
        ):
            del execution_id, event_type, cursor_through
            if not chronological:
                index = self.event_count - 1
                return [SimpleNamespace(timestamp=index, id=str(index))]
            start = 0 if cursor_after is None else int(cursor_after[1]) + 1
            stop = min(start + limit, self.event_count)
            return [SimpleNamespace(timestamp=index, id=str(index)) for index in range(start, stop)]

    events, complete = await query_execution_evidence_events(
        _PagedEvidenceStore(),
        "execution",
    )

    assert len(events) == 6_000
    assert complete is False
