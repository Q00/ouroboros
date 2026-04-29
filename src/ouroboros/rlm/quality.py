"""Deterministic quality scoring for RLM-vs-vanilla benchmark outputs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from ouroboros.rlm.baseline import (
    RLMVanillaBaselineQualityScore,
    RLMVanillaTruncationBaselineResult,
    score_vanilla_truncation_baseline_completion,
)
from ouroboros.rlm.contracts import RLM_HERMES_SYNTHESIZE_PARENT_MODE

if TYPE_CHECKING:
    from ouroboros.rlm.loop import RLMRunResult

RLM_TRUNCATION_OUTPUT_QUALITY_SCHEMA_VERSION = "rlm.truncation_output_quality.v1"
RLM_QUALITY_COMPARISON_SCHEMA_VERSION = "rlm.quality_comparison.v1"
RLM_QUALITY_SCORING_METHOD = "truncation_fixture_completion_and_trace_v1"
RLM_QUALITY_COMPARISON_METHOD = "deterministic_score_delta_v1"
RLM_QUALITY_OUTPERFORMANCE_EPSILON = 0.0001

RLMOutputKind = Literal["vanilla_hermes_baseline", "recursive_rlm"]
RLMQualityWinner = Literal["rlm", "vanilla", "tie"]


@dataclass(frozen=True, slots=True)
class RLMOutputQualityScore:
    """Deterministic score for one side of the truncation comparison."""

    schema_version: str
    output_kind: RLMOutputKind
    scoring_method: str
    score: float
    completion_quality: RLMVanillaBaselineQualityScore
    selected_chunk_ids: tuple[str, ...]
    omitted_chunk_ids: tuple[str, ...]
    recursive_trace_score: float | None = None
    recursive_chunk_coverage_score: float | None = None
    parent_synthesis_score: float | None = None
    successful_recursive_chunk_ids: tuple[str, ...] = ()
    missing_recursive_chunk_ids: tuple[str, ...] = ()
    parent_synthesis_observed: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serialize the quality score for benchmark artifacts."""
        return {
            "schema_version": self.schema_version,
            "output_kind": self.output_kind,
            "scoring_method": self.scoring_method,
            "score": self.score,
            "completion_quality_score": self.completion_quality.score,
            "completion_quality": self.completion_quality.to_dict(),
            "selected_chunk_ids": list(self.selected_chunk_ids),
            "omitted_chunk_ids": list(self.omitted_chunk_ids),
            "recursive_trace_score": self.recursive_trace_score,
            "recursive_chunk_coverage_score": self.recursive_chunk_coverage_score,
            "parent_synthesis_score": self.parent_synthesis_score,
            "successful_recursive_chunk_ids": list(self.successful_recursive_chunk_ids),
            "missing_recursive_chunk_ids": list(self.missing_recursive_chunk_ids),
            "parent_synthesis_observed": self.parent_synthesis_observed,
        }


@dataclass(frozen=True, slots=True)
class RLMQualityComparison:
    """Deterministic side-by-side quality comparison for one benchmark run."""

    schema_version: str
    comparison_method: str
    vanilla_quality: RLMOutputQualityScore
    rlm_quality: RLMOutputQualityScore
    score_delta: float
    rlm_outperforms_vanilla: bool
    winner: RLMQualityWinner
    tie_epsilon: float
    summary: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize the comparison in a stable machine-readable shape."""
        return {
            "schema_version": self.schema_version,
            "comparison_method": self.comparison_method,
            "vanilla_quality": self.vanilla_quality.to_dict(),
            "rlm_quality": self.rlm_quality.to_dict(),
            "score_delta": self.score_delta,
            "rlm_outperforms_vanilla": self.rlm_outperforms_vanilla,
            "winner": self.winner,
            "tie_epsilon": self.tie_epsilon,
            "summary": self.summary,
        }


def score_vanilla_truncation_output_quality(
    fixture: Mapping[str, Any],
    vanilla_result: RLMVanillaTruncationBaselineResult,
) -> RLMOutputQualityScore:
    """Score a vanilla single-pass Hermes output against fixture requirements."""
    completion_quality = score_vanilla_truncation_baseline_completion(
        fixture,
        vanilla_result.completion,
    )
    return RLMOutputQualityScore(
        schema_version=RLM_TRUNCATION_OUTPUT_QUALITY_SCHEMA_VERSION,
        output_kind="vanilla_hermes_baseline",
        scoring_method=RLM_QUALITY_SCORING_METHOD,
        score=completion_quality.score,
        completion_quality=completion_quality,
        selected_chunk_ids=vanilla_result.selected_chunk_ids,
        omitted_chunk_ids=vanilla_result.omitted_chunk_ids,
    )


def score_recursive_rlm_truncation_output_quality(
    fixture: Mapping[str, Any],
    rlm_result: RLMRunResult,
    *,
    selected_chunk_ids: Sequence[str] | None = None,
    omitted_chunk_ids: Sequence[str] | None = None,
) -> RLMOutputQualityScore:
    """Score a recursive RLM output using completion quality and trace coverage."""
    selected_ids = tuple(selected_chunk_ids or _fixture_selected_chunk_ids(fixture))
    omitted_ids = tuple(omitted_chunk_ids or _fixture_omitted_chunk_ids(fixture))
    atomic_execution = getattr(rlm_result, "atomic_execution", None)
    final_message = ""
    if atomic_execution is not None:
        final_message = str(getattr(atomic_execution, "final_message", ""))

    completion_quality = score_vanilla_truncation_baseline_completion(
        fixture,
        final_message,
    )
    successful_chunk_ids = _successful_recursive_chunk_ids(
        atomic_execution,
        selected_chunk_ids=selected_ids,
    )
    missing_chunk_ids = tuple(
        chunk_id for chunk_id in selected_ids if chunk_id not in successful_chunk_ids
    )
    chunk_coverage_score = len(successful_chunk_ids) / len(selected_ids) if selected_ids else 1.0
    parent_synthesis_score = _parent_synthesis_score(
        atomic_execution,
        required_child_count=len(selected_ids),
    )
    recursive_trace_score = (
        0.75 * _clamp_unit_score(chunk_coverage_score) + 0.25 * parent_synthesis_score
    )
    score = 0.80 * completion_quality.score + 0.20 * _clamp_unit_score(recursive_trace_score)

    return RLMOutputQualityScore(
        schema_version=RLM_TRUNCATION_OUTPUT_QUALITY_SCHEMA_VERSION,
        output_kind="recursive_rlm",
        scoring_method=RLM_QUALITY_SCORING_METHOD,
        score=_clamp_unit_score(score),
        completion_quality=completion_quality,
        selected_chunk_ids=selected_ids,
        omitted_chunk_ids=omitted_ids,
        recursive_trace_score=_clamp_unit_score(recursive_trace_score),
        recursive_chunk_coverage_score=_clamp_unit_score(chunk_coverage_score),
        parent_synthesis_score=parent_synthesis_score,
        successful_recursive_chunk_ids=successful_chunk_ids,
        missing_recursive_chunk_ids=missing_chunk_ids,
        parent_synthesis_observed=parent_synthesis_score > 0,
    )


def compare_truncation_output_quality(
    fixture: Mapping[str, Any],
    *,
    vanilla_result: RLMVanillaTruncationBaselineResult,
    rlm_result: RLMRunResult,
    selected_chunk_ids: Sequence[str] | None = None,
    omitted_chunk_ids: Sequence[str] | None = None,
    tie_epsilon: float = RLM_QUALITY_OUTPERFORMANCE_EPSILON,
) -> RLMQualityComparison:
    """Score both benchmark outputs and report whether RLM outperforms vanilla."""
    vanilla_quality = score_vanilla_truncation_output_quality(fixture, vanilla_result)
    rlm_quality = score_recursive_rlm_truncation_output_quality(
        fixture,
        rlm_result,
        selected_chunk_ids=selected_chunk_ids,
        omitted_chunk_ids=omitted_chunk_ids,
    )
    score_delta = _clamp_score_delta(rlm_quality.score - vanilla_quality.score)
    if score_delta > tie_epsilon:
        winner: RLMQualityWinner = "rlm"
    elif score_delta < -tie_epsilon:
        winner = "vanilla"
    else:
        winner = "tie"

    rlm_outperforms = winner == "rlm"
    summary = (
        "Recursive RLM outperformed vanilla Hermes on deterministic quality score."
        if rlm_outperforms
        else "Recursive RLM did not outperform vanilla Hermes on deterministic quality score."
    )
    return RLMQualityComparison(
        schema_version=RLM_QUALITY_COMPARISON_SCHEMA_VERSION,
        comparison_method=RLM_QUALITY_COMPARISON_METHOD,
        vanilla_quality=vanilla_quality,
        rlm_quality=rlm_quality,
        score_delta=score_delta,
        rlm_outperforms_vanilla=rlm_outperforms,
        winner=winner,
        tie_epsilon=tie_epsilon,
        summary=summary,
    )


def _successful_recursive_chunk_ids(
    atomic_execution: object | None,
    *,
    selected_chunk_ids: tuple[str, ...],
) -> tuple[str, ...]:
    if atomic_execution is None:
        return ()

    selected = set(selected_chunk_ids)
    chunk_ids: list[str] = []
    for subcall in getattr(atomic_execution, "chunk_subcalls", ()) or ():
        chunk_id = getattr(subcall, "chunk_id", None)
        if not isinstance(chunk_id, str) or chunk_id not in selected:
            continue
        if getattr(subcall, "exit_code", 1) != 0:
            continue
        if getattr(subcall, "success", True) is False:
            continue
        chunk_ids.append(chunk_id)
    return _ordered_unique(chunk_ids)


def _parent_synthesis_score(
    atomic_execution: object | None,
    *,
    required_child_count: int,
) -> float:
    if atomic_execution is None:
        return 0.0

    parent_subcall = getattr(atomic_execution, "hermes_subcall", None)
    if parent_subcall is None:
        return 0.0
    if getattr(parent_subcall, "mode", None) != RLM_HERMES_SYNTHESIZE_PARENT_MODE:
        return 0.0
    if getattr(parent_subcall, "exit_code", 1) != 0:
        return 0.0

    parent_state = getattr(atomic_execution, "parent_execution_state", None)
    recorded_results = getattr(parent_state, "recorded_subcall_results", ()) or ()
    if len(recorded_results) < required_child_count:
        return 0.0
    return 1.0


def _fixture_selected_chunk_ids(fixture: Mapping[str, Any]) -> tuple[str, ...]:
    truncation_config = _mapping(fixture.get("truncation_config"))
    return _string_tuple(truncation_config.get("expected_selected_chunk_ids"))


def _fixture_omitted_chunk_ids(fixture: Mapping[str, Any]) -> tuple[str, ...]:
    truncation_config = _mapping(fixture.get("truncation_config"))
    return _string_tuple(truncation_config.get("expected_omitted_chunk_ids"))


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _string_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if value else ()
    if not isinstance(value, Sequence):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def _ordered_unique(values: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return tuple(ordered)


def _clamp_unit_score(value: float) -> float:
    return round(max(0.0, min(1.0, value)), 4)


def _clamp_score_delta(value: float) -> float:
    return round(max(-1.0, min(1.0, value)), 4)


__all__ = [
    "RLM_QUALITY_COMPARISON_METHOD",
    "RLM_QUALITY_COMPARISON_SCHEMA_VERSION",
    "RLM_QUALITY_OUTPERFORMANCE_EPSILON",
    "RLM_QUALITY_SCORING_METHOD",
    "RLM_TRUNCATION_OUTPUT_QUALITY_SCHEMA_VERSION",
    "RLMOutputQualityScore",
    "RLMQualityComparison",
    "compare_truncation_output_quality",
    "score_recursive_rlm_truncation_output_quality",
    "score_vanilla_truncation_output_quality",
]
