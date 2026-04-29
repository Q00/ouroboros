"""Unit tests for RLM Hermes structured contracts."""

from __future__ import annotations

import json

import pytest

from ouroboros.rlm import (
    RLM_HERMES_DECOMPOSE_AC_MODE,
    RLM_HERMES_OUTPUT_SCHEMA_VERSION,
    RLMHermesACDecompositionArtifact,
    RLMHermesACDecompositionResult,
    RLMHermesACSubQuestion,
    RLMHermesContractError,
    RLMHermesControl,
    RLMHermesEvidenceReference,
)


def _decomposition_result() -> RLMHermesACDecompositionResult:
    return RLMHermesACDecompositionResult(
        rlm_node_id="rlm_node_1",
        ac_node_id="ac_parent",
        verdict="decomposed",
        confidence=0.84,
        result={"summary": "Split the parent AC into schema and docs work."},
        evidence_references=(
            RLMHermesEvidenceReference(
                chunk_id="docs:rlm:1-40",
                source_path="docs/guides/recursive-language-model.md",
                start_line=1,
                end_line=40,
                claim="The guide defines the RLM layer contract.",
            ),
        ),
        artifact=RLMHermesACDecompositionArtifact(
            is_atomic=False,
            proposed_child_acs=(
                RLMHermesACSubQuestion(
                    title="Define schema",
                    statement="Define the Hermes decomposition result schema.",
                    success_criteria=("Schema fields are explicit",),
                    rationale="The outer scaffold needs a parser contract.",
                ),
                RLMHermesACSubQuestion(
                    title="Document serialization",
                    statement="Document the trace serialization contract.",
                    success_criteria=("Round-trip behavior is specified",),
                    rationale="Trace replay needs stable JSON.",
                    depends_on=(0,),
                ),
            ),
        ),
        control=RLMHermesControl(suggested_next_mode="none"),
    )


def test_decomposition_result_round_trips_json_contract() -> None:
    """Structured decomposition outputs should serialize as the trace contract."""
    result = _decomposition_result()

    payload = result.to_json()
    raw = json.loads(payload)

    assert raw["schema_version"] == RLM_HERMES_OUTPUT_SCHEMA_VERSION
    assert raw["mode"] == RLM_HERMES_DECOMPOSE_AC_MODE
    assert raw["artifacts"][0]["artifact_type"] == "decomposition"
    assert raw["artifacts"][0]["proposed_child_acs"][1]["depends_on"] == [0]

    restored = RLMHermesACDecompositionResult.from_json(
        payload,
        expected_rlm_node_id="rlm_node_1",
        expected_ac_node_id="ac_parent",
    )

    assert restored == result
    assert restored.to_dict() == result.to_dict()


def test_decomposition_artifact_serializes_direct_round_trip_payload() -> None:
    """Mode-specific decomposition artifacts should be independently serializable."""
    artifact = RLMHermesACDecompositionArtifact(
        is_atomic=True,
        atomic_rationale="The AC is already bounded to one verifiable step.",
    )

    payload = artifact.to_dict()

    assert payload == {
        "artifact_type": "decomposition",
        "is_atomic": True,
        "atomic_rationale": "The AC is already bounded to one verifiable step.",
        "proposed_child_acs": [],
    }
    assert RLMHermesACDecompositionArtifact.from_dict(payload) == artifact


def test_decomposition_result_rejects_echoed_ac_id_mismatch() -> None:
    """Ouroboros must be able to reject Hermes output for the wrong AC."""
    payload = _decomposition_result().to_json()

    with pytest.raises(RLMHermesContractError, match="ac_node_id mismatch"):
        RLMHermesACDecompositionResult.from_json(payload, expected_ac_node_id="ac_other")


def test_decomposition_result_rejects_forward_dependency_indices() -> None:
    """Child sub-questions may only depend on prior siblings."""
    with pytest.raises(RLMHermesContractError, match="prior sibling"):
        RLMHermesACDecompositionArtifact(
            is_atomic=False,
            proposed_child_acs=(
                RLMHermesACSubQuestion(
                    title="First",
                    statement="First child.",
                    success_criteria=("Done",),
                    rationale="First rationale.",
                    depends_on=(1,),
                ),
                RLMHermesACSubQuestion(
                    title="Second",
                    statement="Second child.",
                    success_criteria=("Done",),
                    rationale="Second rationale.",
                ),
            ),
        )


def test_atomic_decomposition_artifact_requires_rationale() -> None:
    """Atomic decomposition outputs need an explicit rationale for trace replay."""
    with pytest.raises(RLMHermesContractError, match="atomic_rationale"):
        RLMHermesACDecompositionArtifact(is_atomic=True)


def test_decomposed_result_requires_child_proposals() -> None:
    """A decomposed verdict is only valid when Hermes proposes child ACs."""
    with pytest.raises(RLMHermesContractError, match="at least"):
        RLMHermesACDecompositionResult(
            rlm_node_id="rlm_node_1",
            ac_node_id="ac_parent",
            verdict="decomposed",
            confidence=0.5,
            result={"summary": "No child proposals."},
            artifact=RLMHermesACDecompositionArtifact(is_atomic=False),
        )
