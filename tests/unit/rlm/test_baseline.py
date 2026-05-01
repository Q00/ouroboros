"""Tests for vanilla Hermes RLM baselines."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ouroboros.rlm import (
    HERMES_VANILLA_BASELINE_SYSTEM_PROMPT,
    RLM_BENCHMARK_OUTPUT_SCHEMA_VERSION,
    RLM_VANILLA_BASELINE_CALL_ID,
    RLM_VANILLA_BASELINE_INPUT_SCHEMA_VERSION,
    RLM_VANILLA_BASELINE_MODE,
    RLM_VANILLA_BASELINE_QUALITY_SCHEMA_VERSION,
    RLM_VANILLA_BASELINE_RESULT_ARTIFACT_TYPE,
    RLM_VANILLA_BASELINE_RESULT_DIR,
    RLM_VANILLA_TRUNCATION_BASELINE_ID,
    RLMVanillaTruncationBaselineConfig,
    run_vanilla_truncation_baseline,
    score_vanilla_truncation_baseline_completion,
)


@pytest.mark.asyncio
async def test_vanilla_truncation_baseline_executes_fixture_in_single_hermes_call(
    tmp_path: Path,
    deterministic_rlm_hermes_runtime: Any,
    long_context_truncation_fixture: dict[str, Any],
) -> None:
    """The baseline runs the truncation fixture once without the recursive scaffold."""
    fixture = long_context_truncation_fixture
    truncation_config = fixture["truncation_config"]

    result = await run_vanilla_truncation_baseline(
        RLMVanillaTruncationBaselineConfig(
            fixture=fixture,
            cwd=tmp_path,
            hermes_runtime=deterministic_rlm_hermes_runtime,
        )
    )

    assert result.status == "completed"
    assert result.success is True
    assert result.baseline_id == RLM_VANILLA_TRUNCATION_BASELINE_ID
    assert result.fixture_id == fixture["fixture_id"]
    assert result.hermes_subcall_count == 1
    assert result.call_id == RLM_VANILLA_BASELINE_CALL_ID
    assert result.selected_chunk_ids == tuple(truncation_config["expected_selected_chunk_ids"])
    assert result.omitted_chunk_ids == tuple(truncation_config["expected_omitted_chunk_ids"])
    assert (
        result.retained_line_count == truncation_config["truncation_boundary"]["last_retained_line"]
    )
    assert (
        result.omitted_line_count == truncation_config["truncation_boundary"]["omitted_line_count"]
    )
    assert result.target_line_count == fixture["target"]["line_count"]
    assert result.result_path == (
        tmp_path / RLM_VANILLA_BASELINE_RESULT_DIR / f"{fixture['fixture_id']}.json"
    )
    assert result.output_quality.schema_version == RLM_VANILLA_BASELINE_QUALITY_SCHEMA_VERSION
    assert result.output_quality.score == 0.9
    assert result.output_quality.required_fields_missing == ()
    assert result.output_quality.missing_retained_fact_ids == ()
    assert result.output_quality.cited_omitted_chunk_ids == ()
    assert result.output_quality.claimed_omitted_fact_ids == ()
    assert result.output_quality.reports_truncation_boundary is False

    assert len(deterministic_rlm_hermes_runtime.exchanges) == 1
    exchange = deterministic_rlm_hermes_runtime.exchanges[0]
    assert exchange.call_id == RLM_VANILLA_BASELINE_CALL_ID
    assert exchange.parent_call_id is None
    assert exchange.depth == 0
    assert exchange.mode == RLM_VANILLA_BASELINE_MODE
    assert deterministic_rlm_hermes_runtime.calls[0]["tools"] == []
    assert deterministic_rlm_hermes_runtime.calls[0]["system_prompt"] == (
        HERMES_VANILLA_BASELINE_SYSTEM_PROMPT
    )

    envelope = json.loads(exchange.prompt)
    assert envelope["schema_version"] == RLM_VANILLA_BASELINE_INPUT_SCHEMA_VERSION
    assert envelope["baseline"]["single_pass"] is True
    assert envelope["baseline"]["uses_recursive_outer_loop"] is False
    assert envelope["constraints"]["must_not_call_ouroboros"] is True
    assert envelope["constraints"]["uses_recursive_outer_loop"] is False
    assert (
        envelope["trace"]["selected_chunk_ids"] == truncation_config["expected_selected_chunk_ids"]
    )
    assert envelope["trace"]["omitted_chunk_ids"] == truncation_config["expected_omitted_chunk_ids"]
    assert "outer_scaffold" not in envelope
    assert "rlm_node" not in envelope
    assert "ac_node" not in envelope

    prompt_text = exchange.prompt
    for fact in fixture["expected_retained_facts"]:
        assert fact["text"] in prompt_text
    for fact in fixture["expected_omitted_facts"]:
        assert fact["text"] not in prompt_text

    completion = json.loads(result.completion)
    assert completion["mode"] == RLM_VANILLA_BASELINE_MODE
    assert completion["result"]["call_id"] == RLM_VANILLA_BASELINE_CALL_ID
    assert (
        completion["result"]["selected_chunk_ids"]
        == truncation_config["expected_selected_chunk_ids"]
    )
    assert [item["chunk_id"] for item in completion["evidence_references"]] == (
        truncation_config["expected_selected_chunk_ids"]
    )

    assert result.result_path is not None
    persisted = json.loads(result.result_path.read_text(encoding="utf-8"))
    assert persisted["schema_version"] == RLM_BENCHMARK_OUTPUT_SCHEMA_VERSION
    assert persisted["artifact_type"] == RLM_VANILLA_BASELINE_RESULT_ARTIFACT_TYPE
    assert persisted["benchmark_id"] == RLM_VANILLA_TRUNCATION_BASELINE_ID
    assert persisted["generated_rlm_tree_depth"] == 0
    assert persisted["source_evidence"] == []
    assert persisted["cited_source_file_count"] == 0
    assert persisted["baseline"]["fixture_id"] == fixture["fixture_id"]
    assert persisted["baseline"]["completion_hash"] == result.to_dict()["completion_hash"]
    assert persisted["baseline"]["output_quality_score"] == 0.9
    assert persisted["baseline"]["output_quality"] == result.output_quality.to_dict()
    assert persisted["runner_output"]["mode"] == RLM_VANILLA_BASELINE_MODE
    assert persisted["runner_output"]["completion"] == result.completion
    assert persisted["runner_output"]["completion_json"] == completion
    assert persisted["runner_output"]["hermes_subcall_count"] == 1
    assert persisted["runner_output"]["output_quality_score"] == 0.9


@pytest.mark.asyncio
async def test_vanilla_baseline_supports_explicit_result_path(
    tmp_path: Path,
    deterministic_rlm_hermes_runtime: Any,
    long_context_truncation_fixture: dict[str, Any],
) -> None:
    """The baseline artifact can be placed where a later comparison expects it."""
    result = await run_vanilla_truncation_baseline(
        RLMVanillaTruncationBaselineConfig(
            fixture=long_context_truncation_fixture,
            cwd=tmp_path,
            hermes_runtime=deterministic_rlm_hermes_runtime,
            result_path=Path("comparison/baseline-result.json"),
        )
    )

    expected_path = tmp_path / "comparison" / "baseline-result.json"
    assert result.result_path == expected_path
    assert expected_path.exists()
    persisted = json.loads(expected_path.read_text(encoding="utf-8"))
    assert persisted["baseline"]["result_path"] == str(expected_path)


def test_vanilla_truncation_quality_score_detects_missing_or_leaked_evidence(
    long_context_truncation_fixture: dict[str, Any],
) -> None:
    """The scorer records why a single-pass baseline output lost quality."""
    completion = {
        "mode": RLM_VANILLA_BASELINE_MODE,
        "verdict": "partial",
        "confidence": 0.4,
        "result": {
            "summary": "claimed LC-005 without reporting the truncation boundary",
            "retained_facts": [
                {
                    "fact_id": "LC-005",
                    "text": "overflow fact beyond the truncation budget",
                    "evidence_chunk_id": "long_context_truncation_target.txt:9-10",
                }
            ],
        },
        "evidence_references": [
            {
                "chunk_id": "long_context_truncation_target.txt:1-2",
                "supports_fact_ids": ["LC-001"],
                "quoted_evidence": (
                    "FACT:LC-001 command isolation is mandatory: ooo rlm is the only "
                    "allowed entrypoint for this recursive run."
                ),
            },
            {
                "chunk_id": "long_context_truncation_target.txt:9-10",
                "supports_fact_ids": ["LC-005"],
                "quoted_evidence": (
                    "FACT:LC-005 overflow fact beyond the truncation budget: "
                    "tail evidence must not be claimed as retained."
                ),
            },
        ],
        "residual_gaps": [],
    }

    quality = score_vanilla_truncation_baseline_completion(
        long_context_truncation_fixture,
        json.dumps(completion, sort_keys=True),
    )

    assert quality.score == 0.3625
    assert quality.confidence_score == 0.5
    assert quality.cited_retained_fact_ids == ("LC-001",)
    assert quality.missing_retained_fact_ids == ("LC-002", "LC-003", "LC-004")
    assert quality.cited_omitted_chunk_ids == ("long_context_truncation_target.txt:9-10",)
    assert quality.claimed_omitted_fact_ids == ("LC-005",)
    assert quality.omitted_fact_safety_score == 0.0


def test_vanilla_truncation_quality_allows_guarded_omitted_fact_mentions(
    long_context_truncation_fixture: dict[str, Any],
) -> None:
    """Mentioning omitted fact IDs as unavailable gaps is not an evidence claim."""
    completion = {
        "mode": RLM_VANILLA_BASELINE_MODE,
        "verdict": "pass",
        "confidence": 0.93,
        "result": {
            "summary": (
                "The retained context supports four observed facts and indicates that "
                "two later chunks were omitted beyond the truncation boundary. No facts "
                "from omitted chunks are claimed as observed evidence."
            ),
            "retained_facts": [
                {
                    "fact_id": fact["fact_id"],
                    "text": fact["text"].removeprefix(f"FACT:{fact['fact_id']} "),
                    "evidence_chunk_id": fact["chunk_id"],
                }
                for fact in long_context_truncation_fixture["expected_retained_facts"]
            ],
            "truncation_report": {
                "last_retained_line": 8,
                "omitted_line_count": 4,
                "omitted_chunk_ids": [
                    "long_context_truncation_target.txt:9-10",
                    "long_context_truncation_target.txt:11-12",
                ],
                "note": "The omitted chunks were outside the retained context.",
            },
        },
        "evidence_references": [
            {
                "chunk_id": fact["chunk_id"],
                "supports_fact_ids": [fact["fact_id"]],
                "quoted_evidence": fact["text"],
            }
            for fact in long_context_truncation_fixture["expected_retained_facts"]
        ],
        "residual_gaps": [
            {
                "gap": "Lines 9-12 were omitted by the truncation boundary.",
                "affected_chunk_ids": [
                    "long_context_truncation_target.txt:9-10",
                    "long_context_truncation_target.txt:11-12",
                ],
                "impact": (
                    "Any facts contained in omitted chunks, including LC-005 and LC-006 "
                    "if present there, cannot be claimed as observed evidence."
                ),
            }
        ],
    }

    quality = score_vanilla_truncation_baseline_completion(
        long_context_truncation_fixture,
        json.dumps(completion, sort_keys=True),
    )

    assert quality.score == 1.0
    assert quality.claimed_omitted_fact_ids == ()
    assert quality.omitted_fact_safety_score == 1.0


def test_vanilla_truncation_quality_does_not_credit_chunk_ids_without_fact_evidence(
    long_context_truncation_fixture: dict[str, Any],
) -> None:
    """Citing a selected chunk ID is not enough to prove its retained fact."""
    selected_chunk_ids = tuple(
        long_context_truncation_fixture["truncation_config"]["expected_selected_chunk_ids"]
    )
    completion = {
        "mode": RLM_VANILLA_BASELINE_MODE,
        "verdict": "partial",
        "confidence": 0.9,
        "result": {"summary": "The selected chunks were consumed."},
        "evidence_references": [
            {"chunk_id": chunk_id, "claim": f"consumed {chunk_id}"}
            for chunk_id in selected_chunk_ids
        ],
        "residual_gaps": [],
    }

    quality = score_vanilla_truncation_baseline_completion(
        long_context_truncation_fixture,
        json.dumps(completion, sort_keys=True),
    )

    assert quality.retained_fact_citation_score == 0.0
    assert quality.cited_retained_fact_ids == ()
    assert quality.missing_retained_fact_ids == ("LC-001", "LC-002", "LC-003", "LC-004")
    assert quality.score == 0.55
