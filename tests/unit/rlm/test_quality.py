"""Tests for deterministic RLM quality comparison."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ouroboros.rlm import (
    RLM_HERMES_EXECUTE_ATOMIC_MODE,
    RLM_HERMES_SYNTHESIZE_PARENT_MODE,
    RLM_QUALITY_COMPARISON_SCHEMA_VERSION,
    RLM_TRUNCATION_OUTPUT_QUALITY_SCHEMA_VERSION,
    RLMAtomicExecutionResult,
    RLMHermesSubcall,
    RLMParentExecutionState,
    RLMRunResult,
    RLMVanillaTruncationBaselineResult,
    compare_truncation_output_quality,
    score_recursive_rlm_truncation_output_quality,
    score_vanilla_truncation_baseline_completion,
)

_FACTS_BY_CHUNK = {
    "long_context_truncation_target.txt:1-2": (
        "LC-001",
        "FACT:LC-001 command isolation is mandatory: ooo rlm is the only allowed entrypoint for this recursive run.",
    ),
    "long_context_truncation_target.txt:3-4": (
        "LC-002",
        "FACT:LC-002 Hermes must be invoked through execute_task_to_result with an empty tools list.",
    ),
    "long_context_truncation_target.txt:5-6": (
        "LC-003",
        "FACT:LC-003 the AC tree depth cap is 5 and the ambiguity threshold is 0.2.",
    ),
    "long_context_truncation_target.txt:7-8": (
        "LC-004",
        "FACT:LC-004 trace replay must link rlm_node_id, ac_node_id, call_id, and parent_call_id.",
    ),
}


def _completion(selected_chunk_ids: tuple[str, ...]) -> str:
    return json.dumps(
        {
            "mode": RLM_HERMES_SYNTHESIZE_PARENT_MODE,
            "verdict": "passed",
            "confidence": 0.9,
            "result": {
                "summary": "synthesized retained evidence",
                "retained_facts": [
                    {
                        "fact_id": _FACTS_BY_CHUNK[chunk_id][0],
                        "text": _FACTS_BY_CHUNK[chunk_id][1],
                        "evidence_chunk_id": chunk_id,
                    }
                    for chunk_id in selected_chunk_ids
                    if chunk_id in _FACTS_BY_CHUNK
                ],
            },
            "evidence_references": [
                {
                    "chunk_id": chunk_id,
                    "claim": f"retained {chunk_id}",
                    "supports_fact_ids": [_FACTS_BY_CHUNK[chunk_id][0]]
                    if chunk_id in _FACTS_BY_CHUNK
                    else [],
                    "quoted_evidence": _FACTS_BY_CHUNK[chunk_id][1]
                    if chunk_id in _FACTS_BY_CHUNK
                    else "",
                }
                for chunk_id in selected_chunk_ids
            ],
            "residual_gaps": [],
        },
        sort_keys=True,
    )


def _recursive_rlm_result(
    tmp_path: Path,
    *,
    selected_chunk_ids: tuple[str, ...],
) -> RLMRunResult:
    chunk_subcalls = tuple(
        RLMHermesSubcall(
            mode=RLM_HERMES_EXECUTE_ATOMIC_MODE,
            generation_id="rlm_generation_0",
            rlm_node_id=f"rlm_node_atomic_chunk_{index:03d}",
            ac_node_id=f"rlm_ac_atomic_chunk_{index:03d}",
            completion=_completion((chunk_id,)),
            parent_call_id="rlm_call_atomic_synthesis",
            depth=1,
            exit_code=0,
            call_id=f"rlm_call_atomic_chunk_{index:03d}",
            chunk_id=chunk_id,
            selected_chunk_ids=(chunk_id,),
            success=True,
        )
        for index, chunk_id in enumerate(selected_chunk_ids, start=1)
    )
    parent_state = RLMParentExecutionState.from_hermes_subcalls(
        parent_node_id="rlm_node_root",
        parent_ac_node_id="rlm_ac_root",
        generation_id="rlm_generation_0",
        subcalls=chunk_subcalls,
    )
    atomic_execution = RLMAtomicExecutionResult(
        ac_node_id="rlm_ac_root",
        generation_id="rlm_generation_0",
        hermes_subcall=RLMHermesSubcall(
            mode=RLM_HERMES_SYNTHESIZE_PARENT_MODE,
            generation_id="rlm_generation_0",
            rlm_node_id="rlm_node_root",
            ac_node_id="rlm_ac_root",
            completion=_completion(selected_chunk_ids),
            depth=0,
            exit_code=0,
            call_id="rlm_call_atomic_synthesis",
            selected_chunk_ids=selected_chunk_ids,
            success=True,
        ),
        success=True,
        final_message=_completion(selected_chunk_ids),
        chunk_subcalls=chunk_subcalls,
        parent_execution_state=parent_state,
    )
    return RLMRunResult(
        status="completed",
        target="long_context_truncation_target.txt",
        target_kind="path",
        cwd=tmp_path,
        max_depth=5,
        ambiguity_threshold=0.2,
        message="RLM completed",
        hermes_subcall_count=len(chunk_subcalls) + 1,
        atomic_execution=atomic_execution,
    )


def _vanilla_result(
    tmp_path: Path,
    *,
    fixture: dict[str, Any],
    selected_chunk_ids: tuple[str, ...],
    omitted_chunk_ids: tuple[str, ...],
) -> RLMVanillaTruncationBaselineResult:
    completion = _completion(selected_chunk_ids)
    return RLMVanillaTruncationBaselineResult(
        baseline_id="rlm-vanilla-truncation-baseline-v1",
        fixture_id=fixture["fixture_id"],
        target_path=fixture["target"]["path"],
        status="completed",
        success=True,
        call_id="rlm_call_vanilla_truncation_baseline",
        prompt="{}",
        completion=completion,
        selected_chunk_ids=selected_chunk_ids,
        omitted_chunk_ids=omitted_chunk_ids,
        retained_line_count=8,
        omitted_line_count=4,
        target_line_count=12,
        elapsed_ms=1,
        output_quality=score_vanilla_truncation_baseline_completion(
            fixture,
            completion,
        ),
        result_path=tmp_path / "vanilla.json",
    )


def test_quality_comparison_scores_both_outputs_and_reports_rlm_outperformance(
    tmp_path: Path,
    long_context_truncation_fixture: dict[str, Any],
) -> None:
    """The comparison is deterministic and exposes the outperformance verdict."""
    fixture = long_context_truncation_fixture
    truncation_config = fixture["truncation_config"]
    selected_chunk_ids = tuple(truncation_config["expected_selected_chunk_ids"])
    omitted_chunk_ids = tuple(truncation_config["expected_omitted_chunk_ids"])

    rlm_result = _recursive_rlm_result(
        tmp_path,
        selected_chunk_ids=selected_chunk_ids,
    )
    vanilla_result = _vanilla_result(
        tmp_path,
        fixture=fixture,
        selected_chunk_ids=selected_chunk_ids,
        omitted_chunk_ids=omitted_chunk_ids,
    )

    comparison = compare_truncation_output_quality(
        fixture,
        vanilla_result=vanilla_result,
        rlm_result=rlm_result,
        selected_chunk_ids=selected_chunk_ids,
        omitted_chunk_ids=omitted_chunk_ids,
    )

    assert comparison.schema_version == RLM_QUALITY_COMPARISON_SCHEMA_VERSION
    assert comparison.vanilla_quality.schema_version == (
        RLM_TRUNCATION_OUTPUT_QUALITY_SCHEMA_VERSION
    )
    assert comparison.rlm_quality.schema_version == RLM_TRUNCATION_OUTPUT_QUALITY_SCHEMA_VERSION
    assert comparison.vanilla_quality.score == 0.9
    assert comparison.rlm_quality.score == 0.92
    assert comparison.score_delta == 0.02
    assert comparison.rlm_outperforms_vanilla is True
    assert comparison.winner == "rlm"
    assert comparison.rlm_quality.recursive_chunk_coverage_score == 1.0
    assert comparison.rlm_quality.parent_synthesis_score == 1.0
    assert comparison.rlm_quality.successful_recursive_chunk_ids == selected_chunk_ids
    assert comparison.rlm_quality.missing_recursive_chunk_ids == ()

    payload = comparison.to_dict()
    assert payload["rlm_outperforms_vanilla"] is True
    assert payload["vanilla_quality"]["score"] == 0.9
    assert payload["rlm_quality"]["score"] == 0.92


def test_recursive_rlm_quality_penalizes_missing_chunk_subcalls(
    tmp_path: Path,
    long_context_truncation_fixture: dict[str, Any],
) -> None:
    """Trace coverage drops when selected chunks were not executed recursively."""
    fixture = long_context_truncation_fixture
    selected_chunk_ids = tuple(fixture["truncation_config"]["expected_selected_chunk_ids"])
    rlm_result = _recursive_rlm_result(
        tmp_path,
        selected_chunk_ids=selected_chunk_ids[:2],
    )

    quality = score_recursive_rlm_truncation_output_quality(
        fixture,
        rlm_result,
        selected_chunk_ids=selected_chunk_ids,
    )

    assert quality.output_kind == "recursive_rlm"
    assert quality.recursive_chunk_coverage_score == 0.5
    assert quality.parent_synthesis_score == 0.0
    assert quality.missing_recursive_chunk_ids == selected_chunk_ids[2:]
