"""Tests for the RLM MVP benchmark fixture."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ouroboros.rlm import (
    RLM_MVP_SRC_DOGFOOD_BENCHMARK_ID,
    RLM_MVP_SRC_DOGFOOD_EXECUTION_CONFIG,
    RLM_MVP_SRC_DOGFOOD_FIXTURE,
    RLM_MVP_SRC_DOGFOOD_TARGET_CORPUS,
    RLM_SHARED_TRUNCATION_BENCHMARK_ID,
    RLM_SHARED_TRUNCATION_BENCHMARK_RESULT_ARTIFACT_TYPE,
    RLM_SHARED_TRUNCATION_BENCHMARK_SCHEMA_VERSION,
    RLM_VANILLA_BASELINE_CALL_ID,
    RLM_WONDER_REFLECT_ONTOLOGY_MIGRATION_QUESTION,
    RLMRunConfig,
    RLMSharedTruncationBenchmarkConfig,
    benchmark_fixture_for_id,
    benchmark_fixture_for_target,
    run_rlm_benchmark,
    run_rlm_loop,
    run_shared_truncation_benchmark,
)


def test_src_dogfood_fixture_configures_ouroboros_src_target_corpus() -> None:
    """The dogfood benchmark is explicitly scoped to the Ouroboros src corpus."""
    fixture = benchmark_fixture_for_target("./src/")

    assert fixture == RLM_MVP_SRC_DOGFOOD_FIXTURE
    assert fixture is not None
    assert fixture.target == "src"
    assert fixture.target_corpus == RLM_MVP_SRC_DOGFOOD_TARGET_CORPUS

    corpus_payload = fixture.to_dict()["target_corpus"]
    assert corpus_payload["corpus_id"] == "ouroboros-src"
    assert corpus_payload["root"] == "src"
    assert corpus_payload["include_globs"] == ["src/ouroboros/**/*.py"]
    assert "src/ouroboros/rlm/loop.py" in corpus_payload["required_paths"]
    assert "src/ouroboros/orchestrator/hermes_runtime.py" in corpus_payload["required_paths"]


def test_src_dogfood_fixture_configures_nested_inner_lm_execution() -> None:
    """The benchmark fixture carries loop settings that can force child calls."""
    payload = RLM_MVP_SRC_DOGFOOD_FIXTURE.to_dict()
    execution_payload = payload["execution_config"]

    assert RLM_MVP_SRC_DOGFOOD_FIXTURE.execution_config == (RLM_MVP_SRC_DOGFOOD_EXECUTION_CONFIG)
    assert execution_payload == {
        "chunk_line_limit": 1,
        "max_atomic_chunks": 6,
        "min_nested_inner_lm_calls": 1,
    }
    assert execution_payload["max_atomic_chunks"] >= 2
    assert len(RLM_MVP_SRC_DOGFOOD_FIXTURE.target_corpus.required_paths) >= 2


def test_src_dogfood_fixture_can_be_selected_by_benchmark_id() -> None:
    """The command can resolve the built-in benchmark before entering the loop."""
    fixture = benchmark_fixture_for_id(RLM_MVP_SRC_DOGFOOD_BENCHMARK_ID)

    assert fixture == RLM_MVP_SRC_DOGFOOD_FIXTURE
    assert benchmark_fixture_for_id("missing-benchmark") is None


def test_src_dogfood_fixture_includes_wonder_reflect_ontology_migration_question() -> None:
    """The dogfood benchmark pins the Wonder/Reflect ontology migration question."""
    fixture = benchmark_fixture_for_target("src")

    assert fixture == RLM_MVP_SRC_DOGFOOD_FIXTURE
    assert fixture is not None
    questions_by_id = {question.question_id: question for question in fixture.questions}
    migration_question = questions_by_id["wonder-reflect-generation-ontology-migration"]

    assert migration_question == RLM_WONDER_REFLECT_ONTOLOGY_MIGRATION_QUESTION
    assert "Wonder" in migration_question.prompt
    assert "Reflect" in migration_question.prompt
    assert "generation-level ontology migration" in migration_question.prompt
    assert "generation N evidence" in migration_question.prompt
    assert "generation N+1 ontology mutations or seed changes" in migration_question.prompt
    assert migration_question.required_evidence == (
        "src/ouroboros/evolution/wonder.py",
        "src/ouroboros/evolution/reflect.py",
        "src/ouroboros/evolution/loop.py",
    )


def test_src_dogfood_fixture_serializes_benchmark_questions() -> None:
    """Serialized fixture data is stable enough to embed in Hermes prompt context."""
    payload = RLM_MVP_SRC_DOGFOOD_FIXTURE.to_dict()

    assert payload["benchmark_id"] == RLM_MVP_SRC_DOGFOOD_BENCHMARK_ID
    assert payload["target"] == "src"
    assert payload["target_corpus"]["root"] == "src"
    assert payload["target_corpus"]["include_globs"] == ["src/ouroboros/**/*.py"]
    assert "dual-layer recursive language model constraints" in payload["root_question"]
    assert {question["question_id"] for question in payload["questions"]} >= {
        "command-isolation",
        "hermes-inner-lm-boundary",
        "wonder-reflect-generation-ontology-migration",
    }


@pytest.mark.asyncio
async def test_src_rlm_prompt_embeds_benchmark_migration_question(
    tmp_path: Path,
    deterministic_rlm_hermes_runtime: Any,
) -> None:
    """The executable RLM loop passes the benchmark question to Hermes."""
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "example.py").write_text("VALUE = 1\n", encoding="utf-8")

    result = await run_rlm_loop(
        RLMRunConfig(
            target="src",
            cwd=tmp_path,
            hermes_runtime=deterministic_rlm_hermes_runtime,
        )
    )

    assert result.status == "completed"
    prompt_envelope = json.loads(deterministic_rlm_hermes_runtime.exchanges[0].prompt)
    benchmark_payload = prompt_envelope["context"]["benchmark_fixture"]
    corpus_payload = benchmark_payload["target_corpus"]
    migration_questions = [
        question
        for question in benchmark_payload["questions"]
        if question["question_id"] == "wonder-reflect-generation-ontology-migration"
    ]

    assert prompt_envelope["run"]["seed_id"] == RLM_MVP_SRC_DOGFOOD_BENCHMARK_ID
    assert benchmark_payload["benchmark_id"] == RLM_MVP_SRC_DOGFOOD_BENCHMARK_ID
    assert corpus_payload["corpus_id"] == "ouroboros-src"
    assert corpus_payload["root"] == "src"
    assert corpus_payload["include_globs"] == ["src/ouroboros/**/*.py"]
    assert len(migration_questions) == 1
    assert "Wonder" in migration_questions[0]["prompt"]
    assert "Reflect" in migration_questions[0]["prompt"]
    assert "generation-level ontology migration" in migration_questions[0]["prompt"]


@pytest.mark.asyncio
async def test_rlm_benchmark_runner_invokes_recursive_loop_with_fixture_target(
    tmp_path: Path,
    deterministic_rlm_hermes_runtime: Any,
) -> None:
    """The benchmark runner enters the same recursive loop with benchmark context."""
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "example.py").write_text("VALUE = 1\nVALUE_2 = 2\n", encoding="utf-8")

    result = await run_rlm_benchmark(
        RLMRunConfig(
            target="ignored prompt target",
            cwd=tmp_path,
            hermes_runtime=deterministic_rlm_hermes_runtime,
        ),
        benchmark_id=RLM_MVP_SRC_DOGFOOD_BENCHMARK_ID,
    )

    assert result.status == "completed"
    assert result.target == "src"
    assert result.hermes_subcall_count == 4
    assert result.atomic_execution is not None
    assert result.benchmark_output is not None
    assert result.outer_scaffold_state is not None
    assert result.outer_scaffold_state.generated_rlm_tree_depth >= 2
    assert result.benchmark_output.generated_rlm_tree_depth >= 2
    assert result.benchmark_output.to_dict()["generated_rlm_tree_depth"] >= 2
    assert "Generated RLM tree depth: `2`" in result.benchmark_output.report_markdown
    assert len(result.atomic_execution.chunk_subcalls) == 2
    assert len(result.atomic_execution.nested_benchmark_subcalls) == 1
    assert any(
        exchange.parent_call_id == "rlm_call_atomic_synthesis" and exchange.depth == 1
        for exchange in deterministic_rlm_hermes_runtime.exchanges
    )
    assert any(
        exchange.parent_call_id == "rlm_call_atomic_chunk_001" and exchange.depth == 2
        for exchange in deterministic_rlm_hermes_runtime.exchanges
    )
    assert deterministic_rlm_hermes_runtime.exchanges[-1].call_id == ("rlm_call_atomic_synthesis")
    prompt_envelope = json.loads(deterministic_rlm_hermes_runtime.exchanges[0].prompt)
    assert prompt_envelope["run"]["seed_id"] == RLM_MVP_SRC_DOGFOOD_BENCHMARK_ID
    assert (
        prompt_envelope["context"]["benchmark_fixture"]["benchmark_id"]
        == RLM_MVP_SRC_DOGFOOD_BENCHMARK_ID
    )


@pytest.mark.asyncio
async def test_shared_truncation_benchmark_records_vanilla_and_rlm_outputs(
    tmp_path: Path,
    deterministic_rlm_hermes_runtime: Any,
    long_context_truncation_fixture: dict[str, Any],
) -> None:
    """The shared fixture runs both paths against the same truncation scenario."""
    truncation_config = long_context_truncation_fixture["truncation_config"]
    selected_chunk_ids = tuple(truncation_config["expected_selected_chunk_ids"])
    omitted_chunk_ids = tuple(truncation_config["expected_omitted_chunk_ids"])

    result = await run_shared_truncation_benchmark(
        RLMSharedTruncationBenchmarkConfig(
            fixture=long_context_truncation_fixture,
            cwd=tmp_path,
            hermes_runtime=deterministic_rlm_hermes_runtime,
            result_path=Path("comparison/shared.json"),
            baseline_result_path=Path("comparison/vanilla.json"),
        )
    )

    assert result.benchmark_id == RLM_SHARED_TRUNCATION_BENCHMARK_ID
    assert result.schema_version == RLM_SHARED_TRUNCATION_BENCHMARK_SCHEMA_VERSION
    assert result.status == "completed"
    assert result.success is True
    assert result.fixture_id == long_context_truncation_fixture["fixture_id"]
    assert result.target_path == long_context_truncation_fixture["target"]["path"]
    assert result.selected_chunk_ids == selected_chunk_ids
    assert result.omitted_chunk_ids == omitted_chunk_ids
    assert result.vanilla_result.hermes_subcall_count == 1
    assert result.rlm_result.hermes_subcall_count == 5
    assert result.rlm_result.atomic_execution is not None
    assert result.rlm_result.atomic_execution.success is True
    assert [subcall.chunk_id for subcall in result.rlm_result.atomic_execution.chunk_subcalls] == [
        *selected_chunk_ids
    ]
    assert result.quality_comparison is not None
    assert result.quality_comparison.vanilla_quality.score == 0.9
    assert result.quality_comparison.rlm_quality.score == 0.92
    assert result.quality_comparison.score_delta == 0.02
    assert result.quality_comparison.rlm_outperforms_vanilla is True

    target_file = tmp_path / long_context_truncation_fixture["target"]["path"]
    assert target_file.is_file()
    assert (
        target_file.read_text(encoding="utf-8").splitlines()
        == (long_context_truncation_fixture["target"]["lines"])
    )

    assert [exchange.call_id for exchange in deterministic_rlm_hermes_runtime.exchanges] == [
        RLM_VANILLA_BASELINE_CALL_ID,
        "rlm_call_atomic_chunk_001",
        "rlm_call_atomic_chunk_002",
        "rlm_call_atomic_chunk_003",
        "rlm_call_atomic_chunk_004",
        "rlm_call_atomic_synthesis",
    ]

    assert result.result_path == tmp_path / "comparison" / "shared.json"
    assert result.vanilla_result.result_path == tmp_path / "comparison" / "vanilla.json"
    assert result.result_path.exists()
    persisted = json.loads(result.result_path.read_text(encoding="utf-8"))

    assert persisted["schema_version"] == RLM_SHARED_TRUNCATION_BENCHMARK_SCHEMA_VERSION
    assert persisted["artifact_type"] == RLM_SHARED_TRUNCATION_BENCHMARK_RESULT_ARTIFACT_TYPE
    assert persisted["benchmark_id"] == RLM_SHARED_TRUNCATION_BENCHMARK_ID
    assert persisted["shared_input"]["selected_chunk_ids"] == [*selected_chunk_ids]
    assert persisted["shared_input"]["omitted_chunk_ids"] == [*omitted_chunk_ids]
    assert persisted["vanilla_output"]["runner_output"]["completion"] == (
        result.vanilla_result.completion
    )
    assert persisted["vanilla_output"]["runner_output"]["hermes_subcall_count"] == 1
    assert persisted["rlm_output"]["mode"] == "recursive_outer_scaffold"
    assert persisted["rlm_output"]["hermes_subcall_count"] == 5
    assert persisted["rlm_output"]["selected_chunk_ids"] == [*selected_chunk_ids]
    assert persisted["rlm_output"]["omitted_chunk_ids"] == [*omitted_chunk_ids]
    assert persisted["quality_comparison"]["rlm_outperforms_vanilla"] is True
    assert persisted["quality_comparison"]["score_delta"] == 0.02
    assert persisted["quality_comparison"]["vanilla_quality"]["score"] == 0.9
    assert persisted["quality_comparison"]["rlm_quality"]["score"] == 0.92
    assert persisted["rlm_output"]["atomic_execution"]["final_message"] == (
        result.rlm_result.atomic_execution.final_message
    )
    assert [
        subcall["call_id"] for subcall in persisted["rlm_output"]["atomic_execution"]["subcalls"]
    ] == [
        "rlm_call_atomic_chunk_001",
        "rlm_call_atomic_chunk_002",
        "rlm_call_atomic_chunk_003",
        "rlm_call_atomic_chunk_004",
        "rlm_call_atomic_synthesis",
    ]


@pytest.mark.asyncio
async def test_shared_truncation_benchmark_quality_gate_requires_rlm_outperformance(
    tmp_path: Path,
    deterministic_rlm_hermes_runtime: Any,
    long_context_truncation_fixture: dict[str, Any],
) -> None:
    """CI fails if recursive RLM does not beat the vanilla Hermes truncation baseline."""
    result = await run_shared_truncation_benchmark(
        RLMSharedTruncationBenchmarkConfig(
            fixture=long_context_truncation_fixture,
            cwd=tmp_path,
            hermes_runtime=deterministic_rlm_hermes_runtime,
            result_path=Path("comparison/shared-quality-gate.json"),
            baseline_result_path=Path("comparison/vanilla-quality-gate.json"),
        )
    )

    comparison = result.quality_comparison
    assert comparison is not None
    assert comparison.rlm_quality.score > comparison.vanilla_quality.score, (
        "recursive RLM truncation score must exceed vanilla Hermes baseline score: "
        f"rlm={comparison.rlm_quality.score}, "
        f"vanilla={comparison.vanilla_quality.score}"
    )
    assert comparison.rlm_outperforms_vanilla is True
