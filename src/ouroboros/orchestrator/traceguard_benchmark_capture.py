"""Offline TraceGuard-vs-legacy baseline benchmark for #978 P4.

This module publishes the first C.4 benchmark surface required by #961.
It is deliberately fixture-only: no live model calls, no `ooo run` default
flip, and no removal of the legacy self-report path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ouroboros.orchestrator.baseline_metrics import (
    DEFAULT_MAX_RETRIES,
    FatHarnessMetricsReport,
    build_fat_harness_metrics_report,
)
from ouroboros.orchestrator.baseline_metrics_capture import (
    BASELINE_NEW_DOMAIN_LOC_DELTA,
    BASELINE_NEW_DOMAIN_YAML_DELTA,
    RECORDED_BASELINE_ROWS,
    BaselineMetricFixtureRow,
)
from ouroboros.orchestrator.baseline_metrics_format import render_baseline_report

BENCHMARK_PROFILE_LEGACY = "legacy_self_report_fixture"
BENCHMARK_PROFILE_TRACEGUARD = "traceguard_deliver_gate_fixture"


LEGACY_SELF_REPORT_ROWS: tuple[BaselineMetricFixtureRow, ...] = (
    BaselineMetricFixtureRow(
        "LEG-AC-001",
        "fixture:legacy/self-report/accepted-unsupported-file",
        True,
        1,
        900,
        280,
        fabrication_incidents=1,
        note="Legacy self-report accepted a claim for a file absent from evidence.",
    ),
    BaselineMetricFixtureRow(
        "LEG-AC-002",
        "fixture:legacy/self-report/accepted-empty-test",
        True,
        1,
        940,
        260,
        note="Evidence existed, but the cited test did not exercise the AC semantics.",
        semantic_miss_incidents=1,
    ),
    BaselineMetricFixtureRow(
        "LEG-AC-003",
        "fixture:legacy/self-report/accepted-first-try",
        True,
        1,
        1000,
        300,
        note="Ordinary accepted self-report.",
    ),
    BaselineMetricFixtureRow(
        "LEG-AC-004",
        "fixture:legacy/self-report/accepted-unsupported-symbol",
        True,
        1,
        980,
        310,
        fabrication_incidents=1,
        note="Legacy self-report accepted a non-existent symbol claim.",
    ),
    BaselineMetricFixtureRow(
        "LEG-AC-005",
        "fixture:legacy/self-report/accepted-semantic-miss",
        True,
        1,
        1020,
        330,
        note="Evidence handle existed but did not satisfy the requested behavior.",
        semantic_miss_incidents=1,
    ),
    BaselineMetricFixtureRow(
        "LEG-AC-006",
        "fixture:legacy/self-report/accepted-first-try",
        True,
        1,
        1010,
        340,
        note="Ordinary accepted self-report.",
    ),
    BaselineMetricFixtureRow(
        "LEG-AC-007",
        "fixture:legacy/self-report/accepted-first-try",
        True,
        1,
        970,
        300,
        note="Ordinary accepted self-report.",
    ),
    BaselineMetricFixtureRow(
        "LEG-AC-008",
        "fixture:legacy/self-report/failed",
        False,
        1,
        1040,
        350,
        note="Legacy path failed without recovery routing.",
    ),
)


@dataclass(frozen=True)
class TraceGuardBenchmarkCapture:
    """A/B benchmark report for legacy self-report vs TraceGuard deliver gate."""

    legacy_report: FatHarnessMetricsReport
    traceguard_report: FatHarnessMetricsReport
    legacy_rows: tuple[BaselineMetricFixtureRow, ...]
    traceguard_rows: tuple[BaselineMetricFixtureRow, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "legacy": {
                "report": self.legacy_report.to_dict(),
                "rows": [row.to_dict() for row in self.legacy_rows],
            },
            "traceguard": {
                "report": self.traceguard_report.to_dict(),
                "rows": [row.to_dict() for row in self.traceguard_rows],
            },
            "delta": {
                "fabrication_incidents_per_100_acs": (
                    self.traceguard_report.fabrication_incidents_per_100_acs
                    - self.legacy_report.fabrication_incidents_per_100_acs
                ),
                "semantic_miss_incidents_per_100_acs": (
                    self.traceguard_report.semantic_miss_incidents_per_100_acs
                    - self.legacy_report.semantic_miss_incidents_per_100_acs
                ),
                "median_chars_ratio": (
                    self.traceguard_report.median_chars_per_ac
                    / self.legacy_report.median_chars_per_ac
                ),
            },
        }


def _report(profile: str, rows: tuple[BaselineMetricFixtureRow, ...]) -> FatHarnessMetricsReport:
    return build_fat_harness_metrics_report(
        profile=profile,
        samples=(row.to_sample() for row in rows),
        new_domain_loc_delta=BASELINE_NEW_DOMAIN_LOC_DELTA,
        new_domain_yaml_delta=BASELINE_NEW_DOMAIN_YAML_DELTA,
        max_retries=DEFAULT_MAX_RETRIES,
    )


def build_traceguard_benchmark_capture() -> TraceGuardBenchmarkCapture:
    """Build the recorded #978 P4 fixture benchmark."""
    traceguard_rows = RECORDED_BASELINE_ROWS
    return TraceGuardBenchmarkCapture(
        legacy_report=_report(BENCHMARK_PROFILE_LEGACY, LEGACY_SELF_REPORT_ROWS),
        traceguard_report=_report(BENCHMARK_PROFILE_TRACEGUARD, traceguard_rows),
        legacy_rows=LEGACY_SELF_REPORT_ROWS,
        traceguard_rows=traceguard_rows,
    )


def render_traceguard_benchmark_markdown(
    capture: TraceGuardBenchmarkCapture | None = None,
) -> str:
    """Render the benchmark artifact for maintainer review."""
    capture = build_traceguard_benchmark_capture() if capture is None else capture
    data = capture.to_dict()["delta"]
    lines = [
        "# #978 P4 TraceGuard vs legacy baseline benchmark",
        "",
        "Fixture-only A/B benchmark. No live model calls, no default flip,",
        "and no legacy self-report removal.",
        "",
        "## Legacy self-report",
        "",
        "```text",
        render_baseline_report(capture.legacy_report),
        "```",
        "",
        "## TraceGuard deliver gate",
        "",
        "```text",
        render_baseline_report(capture.traceguard_report),
        "```",
        "",
        "## Delta",
        "",
        f"- Fabrication incidents per 100 ACs: {data['fabrication_incidents_per_100_acs']:.4f}",
        f"- Semantic-miss incidents per 100 ACs: {data['semantic_miss_incidents_per_100_acs']:.4f}",
        f"- Median chars ratio: {data['median_chars_ratio']:.4f}",
        "",
        "## Gate interpretation",
        "",
        "- TraceGuard reduces fixture fabrication incidents to 0 per 100 ACs.",
        "- Semantic misses remain visible and must be handled by later harness/semantic checks.",
        "- Median chars stay within the <= 1.5x C.4 budget guardrail.",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    print(render_traceguard_benchmark_markdown(), end="")


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = [
    "BENCHMARK_PROFILE_LEGACY",
    "BENCHMARK_PROFILE_TRACEGUARD",
    "LEGACY_SELF_REPORT_ROWS",
    "TraceGuardBenchmarkCapture",
    "build_traceguard_benchmark_capture",
    "render_traceguard_benchmark_markdown",
]
