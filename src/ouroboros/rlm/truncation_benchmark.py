"""Shared truncation benchmark for vanilla Hermes and recursive RLM."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from ouroboros.rlm.baseline import (
    RLMVanillaTruncationBaselineConfig,
    RLMVanillaTruncationBaselineResult,
    run_vanilla_truncation_baseline,
)
from ouroboros.rlm.fixtures import RLMRecursiveFixture, recursive_fixture_from_mapping
from ouroboros.rlm.loop import (
    RLMAtomicExecutionResult,
    RLMHermesSubcall,
    RLMRunResult,
    run_rlm_loop,
)
from ouroboros.rlm.quality import (
    RLMQualityComparison,
    compare_truncation_output_quality,
)
from ouroboros.rlm.trace import hash_trace_text

if TYPE_CHECKING:
    from ouroboros.orchestrator.adapter import AgentRuntime
    from ouroboros.rlm.trace import RLMTraceStore

RLM_SHARED_TRUNCATION_BENCHMARK_ID = "rlm-shared-truncation-comparison-v1"
RLM_SHARED_TRUNCATION_BENCHMARK_SCHEMA_VERSION = "rlm.shared_truncation_benchmark.v1"
RLM_SHARED_TRUNCATION_BENCHMARK_RESULT_ARTIFACT_TYPE = "rlm_shared_truncation_benchmark_result"
RLM_SHARED_TRUNCATION_BENCHMARK_RESULT_DIR = Path(".ouroboros") / "rlm" / "benchmarks"


@dataclass(frozen=True, slots=True)
class RLMSharedTruncationBenchmarkConfig:
    """Configuration for one shared vanilla-vs-RLM truncation benchmark run."""

    fixture: Mapping[str, Any]
    cwd: Path
    hermes_runtime: AgentRuntime | None = field(default=None, compare=False, repr=False)
    trace_store: RLMTraceStore | None = field(default=None, compare=False, repr=False)
    result_path: Path | None = None
    baseline_result_path: Path | None = None


@dataclass(frozen=True, slots=True)
class RLMSharedTruncationBenchmarkResult:
    """Side-by-side output of the shared truncation benchmark fixture."""

    benchmark_id: str
    schema_version: str
    fixture_id: str
    target_path: str
    status: Literal["completed", "failed"]
    success: bool
    vanilla_result: RLMVanillaTruncationBaselineResult
    rlm_result: RLMRunResult
    selected_chunk_ids: tuple[str, ...]
    omitted_chunk_ids: tuple[str, ...]
    quality_comparison: RLMQualityComparison | None = None
    result_path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize the comparison artifact with both model outputs recorded."""
        return {
            "schema_version": self.schema_version,
            "artifact_type": RLM_SHARED_TRUNCATION_BENCHMARK_RESULT_ARTIFACT_TYPE,
            "benchmark_id": self.benchmark_id,
            "fixture_id": self.fixture_id,
            "target_path": self.target_path,
            "status": self.status,
            "success": self.success,
            "shared_input": {
                "selected_chunk_ids": list(self.selected_chunk_ids),
                "omitted_chunk_ids": list(self.omitted_chunk_ids),
                "retained_line_count": self.vanilla_result.retained_line_count,
                "omitted_line_count": self.vanilla_result.omitted_line_count,
                "target_line_count": self.vanilla_result.target_line_count,
            },
            "vanilla_output": self.vanilla_result.to_rlm_result_dict(),
            "rlm_output": _serialize_rlm_run_output(
                self.rlm_result,
                selected_chunk_ids=self.selected_chunk_ids,
                omitted_chunk_ids=self.omitted_chunk_ids,
            ),
            "quality_comparison": (
                self.quality_comparison.to_dict() if self.quality_comparison is not None else None
            ),
            "result_path": str(self.result_path) if self.result_path is not None else None,
        }


def _string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        msg = f"RLM shared truncation fixture field {field_name!r} must be a non-empty string"
        raise ValueError(msg)
    return value


def _string_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if value else ()
    if not isinstance(value, Sequence):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def _mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        msg = f"RLM shared truncation fixture field {field_name!r} must be an object"
        raise ValueError(msg)
    return value


def _parse_json_object(text: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _fixture_selected_chunk_ids(fixture: Mapping[str, Any]) -> tuple[str, ...]:
    truncation_config = _mapping(fixture.get("truncation_config"), "truncation_config")
    return _string_tuple(truncation_config.get("expected_selected_chunk_ids"))


def _fixture_omitted_chunk_ids(fixture: Mapping[str, Any]) -> tuple[str, ...]:
    truncation_config = _mapping(fixture.get("truncation_config"), "truncation_config")
    return _string_tuple(truncation_config.get("expected_omitted_chunk_ids"))


def _rlm_selected_chunk_ids(result: RLMRunResult) -> tuple[str, ...]:
    atomic_execution = result.atomic_execution
    if atomic_execution is None:
        return ()
    if atomic_execution.chunk_subcalls:
        return tuple(
            subcall.chunk_id
            for subcall in atomic_execution.chunk_subcalls
            if subcall.chunk_id is not None
        )
    return atomic_execution.hermes_subcall.selected_chunk_ids


def _serialize_subcall(subcall: RLMHermesSubcall) -> dict[str, Any]:
    return {
        "mode": subcall.mode,
        "call_id": subcall.call_id,
        "subcall_id": subcall.subcall_id,
        "parent_call_id": subcall.parent_call_id,
        "trace_id": subcall.trace_id,
        "parent_trace_id": subcall.parent_trace_id,
        "causal_parent_event_id": subcall.causal_parent_event_id,
        "rlm_node_id": subcall.rlm_node_id,
        "ac_node_id": subcall.ac_node_id,
        "depth": subcall.depth,
        "chunk_id": subcall.chunk_id,
        "selected_chunk_ids": list(subcall.selected_chunk_ids),
        "generated_child_ac_node_ids": list(subcall.generated_child_ac_node_ids),
        "success": subcall.success,
        "exit_code": subcall.exit_code,
        "elapsed_ms": subcall.elapsed_ms,
        "completion": subcall.completion,
        "completion_json": _parse_json_object(subcall.completion),
        "completion_hash": subcall.response_hash or hash_trace_text(subcall.completion),
        "prompt_hash": subcall.prompt_hash or hash_trace_text(subcall.prompt),
    }


def _serialize_atomic_execution(
    atomic_execution: RLMAtomicExecutionResult | None,
) -> dict[str, Any] | None:
    if atomic_execution is None:
        return None
    subcalls = [
        *atomic_execution.chunk_subcalls,
        *atomic_execution.nested_benchmark_subcalls,
        atomic_execution.hermes_subcall,
    ]
    return {
        "ac_node_id": atomic_execution.ac_node_id,
        "generation_id": atomic_execution.generation_id,
        "success": atomic_execution.success,
        "final_message": atomic_execution.final_message,
        "final_message_json": _parse_json_object(atomic_execution.final_message),
        "final_message_hash": hash_trace_text(atomic_execution.final_message),
        "chunk_subcall_count": len(atomic_execution.chunk_subcalls),
        "nested_benchmark_subcall_count": len(atomic_execution.nested_benchmark_subcalls),
        "subcalls": [_serialize_subcall(subcall) for subcall in subcalls],
    }


def _serialize_rlm_run_output(
    result: RLMRunResult,
    *,
    selected_chunk_ids: tuple[str, ...],
    omitted_chunk_ids: tuple[str, ...],
) -> dict[str, Any]:
    scaffold = result.outer_scaffold_state
    termination_reason = result.termination_reason
    return {
        "mode": "recursive_outer_scaffold",
        "status": result.status,
        "success": result.status == "completed"
        and result.atomic_execution is not None
        and result.atomic_execution.success,
        "target": result.target,
        "target_kind": result.target_kind,
        "generation_id": result.generation_id,
        "message": result.message,
        "hermes_subcall_count": result.hermes_subcall_count,
        "selected_chunk_ids": list(selected_chunk_ids),
        "omitted_chunk_ids": list(omitted_chunk_ids),
        "termination_reason": termination_reason.value if termination_reason is not None else None,
        "generated_rlm_tree_depth": (
            scaffold.generated_rlm_tree_depth if scaffold is not None else None
        ),
        "ac_tree_max_depth": scaffold.ac_tree.max_depth if scaffold is not None else None,
        "benchmark_output": (
            result.benchmark_output.to_dict() if result.benchmark_output is not None else None
        ),
        "atomic_execution": _serialize_atomic_execution(result.atomic_execution),
    }


def _default_shared_truncation_benchmark_result_path(cwd: Path, fixture_id: str) -> Path:
    """Return the default shared benchmark artifact path."""
    return cwd / RLM_SHARED_TRUNCATION_BENCHMARK_RESULT_DIR / f"{fixture_id}.json"


def _resolve_shared_truncation_benchmark_result_path(
    config: RLMSharedTruncationBenchmarkConfig,
    fixture_id: str,
) -> Path:
    if config.result_path is None:
        return _default_shared_truncation_benchmark_result_path(config.cwd, fixture_id)
    if config.result_path.is_absolute():
        return config.result_path
    return config.cwd / config.result_path


def _validate_shared_outputs(
    *,
    fixture: Mapping[str, Any],
    recursive_fixture: RLMRecursiveFixture,
    vanilla_result: RLMVanillaTruncationBaselineResult,
    rlm_result: RLMRunResult,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    expected_selected = _fixture_selected_chunk_ids(fixture)
    expected_omitted = _fixture_omitted_chunk_ids(fixture)
    recursive_fixture.assert_result_matches(rlm_result)

    if vanilla_result.selected_chunk_ids != expected_selected:
        msg = "Vanilla baseline selected chunks do not match the shared truncation fixture"
        raise ValueError(msg)
    if vanilla_result.omitted_chunk_ids != expected_omitted:
        msg = "Vanilla baseline omitted chunks do not match the shared truncation fixture"
        raise ValueError(msg)

    rlm_selected = _rlm_selected_chunk_ids(rlm_result)
    if rlm_selected != expected_selected:
        msg = "Recursive RLM selected chunks do not match the shared truncation fixture"
        raise ValueError(msg)
    return expected_selected, expected_omitted


def persist_shared_truncation_benchmark_result(
    result: RLMSharedTruncationBenchmarkResult,
    path: Path,
) -> Path:
    """Persist the side-by-side truncation benchmark artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


async def run_shared_truncation_benchmark(
    config: RLMSharedTruncationBenchmarkConfig,
) -> RLMSharedTruncationBenchmarkResult:
    """Run vanilla Hermes and recursive RLM against the same truncation fixture."""
    fixture_id = _string(config.fixture.get("fixture_id"), "fixture_id")
    recursive_fixture = recursive_fixture_from_mapping(config.fixture)
    recursive_fixture.write_target(config.cwd)

    vanilla_result = await run_vanilla_truncation_baseline(
        RLMVanillaTruncationBaselineConfig(
            fixture=config.fixture,
            cwd=config.cwd,
            hermes_runtime=config.hermes_runtime,
            result_path=config.baseline_result_path,
        )
    )
    rlm_result = await run_rlm_loop(
        recursive_fixture.to_run_config(
            cwd=config.cwd,
            hermes_runtime=config.hermes_runtime,
            trace_store=config.trace_store,
        )
    )
    selected_chunk_ids, omitted_chunk_ids = _validate_shared_outputs(
        fixture=config.fixture,
        recursive_fixture=recursive_fixture,
        vanilla_result=vanilla_result,
        rlm_result=rlm_result,
    )
    quality_comparison = compare_truncation_output_quality(
        config.fixture,
        vanilla_result=vanilla_result,
        rlm_result=rlm_result,
        selected_chunk_ids=selected_chunk_ids,
        omitted_chunk_ids=omitted_chunk_ids,
    )

    target = _mapping(config.fixture.get("target"), "target")
    target_path = _string(target.get("path"), "target.path")
    success = (
        vanilla_result.success
        and rlm_result.status == "completed"
        and rlm_result.atomic_execution is not None
        and rlm_result.atomic_execution.success
    )
    result_path = _resolve_shared_truncation_benchmark_result_path(config, fixture_id)
    result = RLMSharedTruncationBenchmarkResult(
        benchmark_id=RLM_SHARED_TRUNCATION_BENCHMARK_ID,
        schema_version=RLM_SHARED_TRUNCATION_BENCHMARK_SCHEMA_VERSION,
        fixture_id=fixture_id,
        target_path=target_path,
        status="completed" if success else "failed",
        success=success,
        vanilla_result=vanilla_result,
        rlm_result=rlm_result,
        selected_chunk_ids=selected_chunk_ids,
        omitted_chunk_ids=omitted_chunk_ids,
        quality_comparison=quality_comparison,
        result_path=result_path,
    )
    persist_shared_truncation_benchmark_result(result, result_path)
    return result


__all__ = [
    "RLM_SHARED_TRUNCATION_BENCHMARK_ID",
    "RLM_SHARED_TRUNCATION_BENCHMARK_RESULT_ARTIFACT_TYPE",
    "RLM_SHARED_TRUNCATION_BENCHMARK_RESULT_DIR",
    "RLM_SHARED_TRUNCATION_BENCHMARK_SCHEMA_VERSION",
    "RLMSharedTruncationBenchmarkConfig",
    "RLMSharedTruncationBenchmarkResult",
    "persist_shared_truncation_benchmark_result",
    "run_shared_truncation_benchmark",
]
