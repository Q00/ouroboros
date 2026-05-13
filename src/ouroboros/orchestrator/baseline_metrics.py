"""Fixture-only fat-harness baseline metrics report (#961 / #830).

The AgentOS SSOT (#961) requires five baseline metrics before the
`ooo run` fat-harness path can be treated as stronger by default:
1-shot AC pass rate, K=2 recovery rate, fabrication incidents,
semantic-miss incidents, char budget per AC, and new-domain cost. This module defines the deterministic
report shape and gate evaluation only; it does not invoke LLMs, run live
executions, or wire into `parallel_executor` yet.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from statistics import median
from typing import Any

DEFAULT_MAX_RETRIES: int = 2
RECOVERY_RATE_TARGET: float = 0.70
NEW_DOMAIN_LOC_TARGET: int = 50
NEW_DOMAIN_YAML_TARGET: int = 1


class FatHarnessGateStatus(StrEnum):
    """Evaluation state for a baseline metric gate."""

    PASS = "pass"
    FAIL = "fail"
    CAPTURED = "captured"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class FatHarnessMetricSample:
    """One acceptance-criterion sample used to build a baseline report.

    The sample is intentionally small and fixture-friendly so tests or
    later capture adapters can construct it without depending on live LLM
    calls. `attempt_count=1` means the AC passed or failed on the first
    leaf attempt; `attempt_count=3` with K=2 means the retry budget was
    exhausted.
    """

    ac_id: str
    accepted: bool
    attempt_count: int
    fabrication_incidents: int = 0
    semantic_miss_incidents: int = 0
    prompt_chars: int = 0
    completion_chars: int = 0

    def __post_init__(self) -> None:
        if not self.ac_id:
            msg = "ac_id must not be empty"
            raise ValueError(msg)
        if self.attempt_count < 1:
            msg = "attempt_count must be >= 1"
            raise ValueError(msg)
        for field_name in (
            "fabrication_incidents",
            "semantic_miss_incidents",
            "prompt_chars",
            "completion_chars",
        ):
            if getattr(self, field_name) < 0:
                msg = f"{field_name} must be non-negative"
                raise ValueError(msg)

    @property
    def total_chars(self) -> int:
        """Prompt + completion chars for the AC dispatch."""
        return self.prompt_chars + self.completion_chars

    @property
    def accepted_first_try(self) -> bool:
        """Whether the verifier accepted the AC on the first leaf attempt."""
        return self.accepted and self.attempt_count == 1

    def recovered_within(self, max_retries: int) -> bool:
        """Whether an initially failed AC recovered inside the retry budget."""
        return self.accepted and 1 < self.attempt_count <= max_retries + 1


@dataclass(frozen=True)
class FatHarnessGateResult:
    """One metric's gate status plus enough context for PR/report output."""

    name: str
    status: FatHarnessGateStatus
    value: float | int | None
    target: str
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""
        return {
            "status": self.status.value,
            "value": self.value,
            "target": self.target,
            "rationale": self.rationale,
        }


@dataclass(frozen=True)
class FatHarnessMetricsReport:
    """Aggregate baseline metrics for the fat-harness acceptance gates."""

    profile: str
    max_retries: int
    total_acs: int
    one_shot_pass_rate: float
    k_recovery_rate: float | None
    fabrication_incidents_per_100_acs: float
    semantic_miss_incidents_per_100_acs: float
    median_chars_per_ac: float
    new_domain_loc_delta: int
    new_domain_yaml_delta: int
    gates: tuple[FatHarnessGateResult, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a stable JSON-serializable report shape."""
        return {
            "profile": self.profile,
            "max_retries": self.max_retries,
            "total_acs": self.total_acs,
            "metrics": {
                "one_shot_pass_rate": self.one_shot_pass_rate,
                "k_recovery_rate": self.k_recovery_rate,
                "fabrication_incidents_per_100_acs": self.fabrication_incidents_per_100_acs,
                "semantic_miss_incidents_per_100_acs": self.semantic_miss_incidents_per_100_acs,
                "median_chars_per_ac": self.median_chars_per_ac,
                "new_domain_loc_delta": self.new_domain_loc_delta,
                "new_domain_yaml_delta": self.new_domain_yaml_delta,
            },
            "gates": {gate.name: gate.to_dict() for gate in self.gates},
        }


def _require_non_negative(name: str, value: int | float) -> None:
    if value < 0:
        msg = f"{name} must be non-negative"
        raise ValueError(msg)


def _status_for_threshold(value: float, target: float) -> FatHarnessGateStatus:
    return FatHarnessGateStatus.PASS if value >= target else FatHarnessGateStatus.FAIL


def build_fat_harness_metrics_report(
    profile: str,
    samples: Iterable[FatHarnessMetricSample],
    new_domain_loc_delta: int,
    new_domain_yaml_delta: int = 0,
    max_retries: int = DEFAULT_MAX_RETRIES,
    baseline_median_chars_per_ac: float | None = None,
) -> FatHarnessMetricsReport:
    """Build a deterministic report for the five #961 fat-harness gates.

    Args:
        profile: Execution profile name the samples came from.
        samples: AC-level fixture/capture samples.
        new_domain_loc_delta: Extra Python LOC needed to add one new domain.
        new_domain_yaml_delta: Extra YAML files needed to add one new domain.
        max_retries: Retry budget K. K=2 is the #830 default.
        baseline_median_chars_per_ac: Existing median char budget to compare
            against. When omitted, the char-budget metric is captured but not
            pass/fail evaluated because this report itself is the baseline.
    """
    if not profile:
        msg = "profile must not be empty"
        raise ValueError(msg)
    _require_non_negative("new_domain_loc_delta", new_domain_loc_delta)
    _require_non_negative("new_domain_yaml_delta", new_domain_yaml_delta)
    _require_non_negative("max_retries", max_retries)
    if baseline_median_chars_per_ac is not None:
        _require_non_negative("baseline_median_chars_per_ac", baseline_median_chars_per_ac)

    sample_tuple = tuple(samples)
    if not sample_tuple:
        msg = "samples must not be empty"
        raise ValueError(msg)

    total_acs = len(sample_tuple)
    one_shot_count = sum(sample.accepted_first_try for sample in sample_tuple)
    one_shot_pass_rate = one_shot_count / total_acs

    initial_failures = [sample for sample in sample_tuple if not sample.accepted_first_try]
    recovered = [sample for sample in initial_failures if sample.recovered_within(max_retries)]
    k_recovery_rate = None if not initial_failures else len(recovered) / len(initial_failures)

    fabrication_incidents = sum(sample.fabrication_incidents for sample in sample_tuple)
    fabrication_incidents_per_100_acs = fabrication_incidents * 100 / total_acs
    semantic_miss_incidents = sum(sample.semantic_miss_incidents for sample in sample_tuple)
    semantic_miss_incidents_per_100_acs = semantic_miss_incidents * 100 / total_acs
    median_chars_per_ac = float(median(sample.total_chars for sample in sample_tuple))

    recovery_status = (
        FatHarnessGateStatus.NOT_APPLICABLE
        if k_recovery_rate is None
        else _status_for_threshold(k_recovery_rate, RECOVERY_RATE_TARGET)
    )
    char_status = (
        FatHarnessGateStatus.CAPTURED
        if baseline_median_chars_per_ac is None
        else (
            FatHarnessGateStatus.PASS
            if median_chars_per_ac <= baseline_median_chars_per_ac
            else FatHarnessGateStatus.FAIL
        )
    )
    domain_status = (
        FatHarnessGateStatus.PASS
        if new_domain_loc_delta <= NEW_DOMAIN_LOC_TARGET
        and new_domain_yaml_delta <= NEW_DOMAIN_YAML_TARGET
        else FatHarnessGateStatus.FAIL
    )

    gates = (
        FatHarnessGateResult(
            name="one_shot_pass_rate",
            status=FatHarnessGateStatus.CAPTURED,
            value=one_shot_pass_rate,
            target="baseline + post-change measurement; target >= +10pp improvement",
            rationale="Baseline capture is enough for this fixture-only report.",
        ),
        FatHarnessGateResult(
            name="k_recovery_rate",
            status=recovery_status,
            value=k_recovery_rate,
            target=f">= {RECOVERY_RATE_TARGET:.0%} of initially failed ACs recover within K={max_retries}",
            rationale=(
                "No initially failed ACs were present."
                if k_recovery_rate is None
                else "Recovered ACs are accepted attempts after the first try inside the retry budget."
            ),
        ),
        FatHarnessGateResult(
            name="fabrication_incidents_per_100_acs",
            status=(
                FatHarnessGateStatus.PASS
                if fabrication_incidents_per_100_acs == 0
                else FatHarnessGateStatus.FAIL
            ),
            value=fabrication_incidents_per_100_acs,
            target="0 verifier-detected fabrication incidents per 100 ACs",
            rationale="Counts verifier-detected non-existent paths/symbols/sources.",
        ),
        FatHarnessGateResult(
            name="semantic_miss_incidents_per_100_acs",
            status=FatHarnessGateStatus.CAPTURED,
            value=semantic_miss_incidents_per_100_acs,
            target="sample and report evidence-backed-but-semantically-wrong incidents per 100 ACs",
            rationale=(
                "Counts claims backed by evidence handles where the evidence does not actually "
                "satisfy the AC semantics."
            ),
        ),
        FatHarnessGateResult(
            name="median_chars_per_ac",
            status=char_status,
            value=median_chars_per_ac,
            target=(
                "capture baseline median chars per AC"
                if baseline_median_chars_per_ac is None
                else f"<= baseline median chars per AC ({baseline_median_chars_per_ac:g})"
            ),
            rationale="Uses prompt + completion chars as the deterministic token-budget proxy.",
        ),
        FatHarnessGateResult(
            name="new_domain_cost",
            status=domain_status,
            value=new_domain_loc_delta,
            target=(
                f"<= {NEW_DOMAIN_LOC_TARGET} LOC and <= {NEW_DOMAIN_YAML_TARGET} YAML "
                "for one new profile/domain"
            ),
            rationale=f"Observed {new_domain_loc_delta} LOC and {new_domain_yaml_delta} YAML files.",
        ),
    )

    return FatHarnessMetricsReport(
        profile=profile,
        max_retries=max_retries,
        total_acs=total_acs,
        one_shot_pass_rate=one_shot_pass_rate,
        k_recovery_rate=k_recovery_rate,
        fabrication_incidents_per_100_acs=fabrication_incidents_per_100_acs,
        semantic_miss_incidents_per_100_acs=semantic_miss_incidents_per_100_acs,
        median_chars_per_ac=median_chars_per_ac,
        new_domain_loc_delta=new_domain_loc_delta,
        new_domain_yaml_delta=new_domain_yaml_delta,
        gates=gates,
    )


__all__ = [
    "DEFAULT_MAX_RETRIES",
    "FatHarnessGateResult",
    "FatHarnessGateStatus",
    "FatHarnessMetricSample",
    "FatHarnessMetricsReport",
    "build_fat_harness_metrics_report",
]
