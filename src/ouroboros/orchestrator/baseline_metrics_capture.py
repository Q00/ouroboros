"""Recorded fixture baseline for the #961 fat-harness metrics gate.

This module turns the fixture-only metric model into a concrete captured
baseline artifact. It is intentionally offline: no live LLM/API calls, no
``parallel_executor`` wiring, and no default-path behavior change. The
captured rows below are the reviewable source-of-truth sample set for the
pre-Tier-2 ``agentos-substrate-wiring`` gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ouroboros.orchestrator.baseline_metrics import (
    DEFAULT_MAX_RETRIES,
    FatHarnessMetricSample,
    FatHarnessMetricsReport,
    build_fat_harness_metrics_report,
)
from ouroboros.orchestrator.baseline_metrics_format import render_baseline_report

BASELINE_PROFILE = "fat_harness_fixture_baseline"
BASELINE_NEW_DOMAIN_LOC_DELTA = 42
BASELINE_NEW_DOMAIN_YAML_DELTA = 1
BASELINE_NEW_DOMAIN_SOURCE = "docs/rfc/contract-ledger.md profile fixture adapter sketch"


@dataclass(frozen=True)
class BaselineMetricFixtureRow:
    """One recorded AC row backing the #961 baseline gate artifact."""

    ac_id: str
    source_ref: str
    accepted: bool
    attempt_count: int
    prompt_chars: int
    completion_chars: int
    fabrication_incidents: int = 0
    note: str = ""

    def to_sample(self) -> FatHarnessMetricSample:
        """Convert this recorded row into the metric builder's sample type."""
        return FatHarnessMetricSample(
            ac_id=self.ac_id,
            accepted=self.accepted,
            attempt_count=self.attempt_count,
            fabrication_incidents=self.fabrication_incidents,
            prompt_chars=self.prompt_chars,
            completion_chars=self.completion_chars,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the source row in a stable JSON-serializable shape."""
        return {
            "ac_id": self.ac_id,
            "source_ref": self.source_ref,
            "accepted": self.accepted,
            "attempt_count": self.attempt_count,
            "fabrication_incidents": self.fabrication_incidents,
            "prompt_chars": self.prompt_chars,
            "completion_chars": self.completion_chars,
            "total_chars": self.prompt_chars + self.completion_chars,
            "note": self.note,
        }


@dataclass(frozen=True)
class CapturedBaselineMetrics:
    """Captured baseline report plus the fixture rows that produced it."""

    report: FatHarnessMetricsReport
    rows: tuple[BaselineMetricFixtureRow, ...]
    new_domain_source: str

    def to_dict(self) -> dict[str, Any]:
        """Return the captured report and source rows for durable evidence."""
        return {
            "report": self.report.to_dict(),
            "sources": {
                "sample_rows": [row.to_dict() for row in self.rows],
                "new_domain_cost": {
                    "source_ref": self.new_domain_source,
                    "loc_delta": self.report.new_domain_loc_delta,
                    "yaml_delta": self.report.new_domain_yaml_delta,
                },
            },
        }


RECORDED_BASELINE_ROWS: tuple[BaselineMetricFixtureRow, ...] = (
    BaselineMetricFixtureRow(
        ac_id="FH-AC-001",
        source_ref="fixture:thin-skill/decompose/accepted-first-try",
        accepted=True,
        attempt_count=1,
        prompt_chars=1180,
        completion_chars=420,
        note="Verifier accepted the first atomic AC attempt.",
    ),
    BaselineMetricFixtureRow(
        ac_id="FH-AC-002",
        source_ref="fixture:thin-skill/evidence/accepted-first-try",
        accepted=True,
        attempt_count=1,
        prompt_chars=1240,
        completion_chars=380,
        note="Evidence manifest matched the expected file claim.",
    ),
    BaselineMetricFixtureRow(
        ac_id="FH-AC-003",
        source_ref="fixture:profile/code/accepted-first-try",
        accepted=True,
        attempt_count=1,
        prompt_chars=1030,
        completion_chars=470,
        note="Profile-aware prompt stayed inside the existing wrapper contract.",
    ),
    BaselineMetricFixtureRow(
        ac_id="FH-AC-004",
        source_ref="fixture:verifier/pass/accepted-first-try",
        accepted=True,
        attempt_count=1,
        prompt_chars=1320,
        completion_chars=440,
        note="Verifier accepted without retry or redispatch.",
    ),
    BaselineMetricFixtureRow(
        ac_id="FH-AC-005",
        source_ref="fixture:retry/recovered-on-second-attempt",
        accepted=True,
        attempt_count=2,
        prompt_chars=1410,
        completion_chars=520,
        note="Initial evidence miss recovered within K=2.",
    ),
    BaselineMetricFixtureRow(
        ac_id="FH-AC-006",
        source_ref="fixture:retry/recovered-on-third-attempt",
        accepted=True,
        attempt_count=3,
        prompt_chars=1500,
        completion_chars=560,
        note="Second retry produced accepted evidence within K=2.",
    ),
    BaselineMetricFixtureRow(
        ac_id="FH-AC-007",
        source_ref="fixture:blocked/recovered-on-second-attempt",
        accepted=True,
        attempt_count=2,
        prompt_chars=1370,
        completion_chars=510,
        note="Typed blocked evidence was resolved by a retry inside budget.",
    ),
    BaselineMetricFixtureRow(
        ac_id="FH-AC-008",
        source_ref="fixture:retry/unrecovered-after-budget",
        accepted=False,
        attempt_count=3,
        prompt_chars=1600,
        completion_chars=600,
        note="Retry budget exhausted; counted in recovery denominator only.",
    ),
)


def build_captured_baseline_metrics() -> CapturedBaselineMetrics:
    """Build the recorded #961 baseline metrics artifact."""
    report = build_fat_harness_metrics_report(
        profile=BASELINE_PROFILE,
        samples=(row.to_sample() for row in RECORDED_BASELINE_ROWS),
        new_domain_loc_delta=BASELINE_NEW_DOMAIN_LOC_DELTA,
        new_domain_yaml_delta=BASELINE_NEW_DOMAIN_YAML_DELTA,
        max_retries=DEFAULT_MAX_RETRIES,
    )
    return CapturedBaselineMetrics(
        report=report,
        rows=RECORDED_BASELINE_ROWS,
        new_domain_source=BASELINE_NEW_DOMAIN_SOURCE,
    )


def render_captured_baseline_markdown(captured: CapturedBaselineMetrics | None = None) -> str:
    """Render the captured baseline as a durable Markdown evidence artifact."""
    captured = build_captured_baseline_metrics() if captured is None else captured
    report = captured.report
    lines = [
        "# #961 fat-harness baseline metrics capture",
        "",
        "This is the recorded fixture baseline for the `agentos-substrate-wiring` gate.",
        "It captures the five #830/#961 hard-gate metrics without live model calls,",
        "without `parallel_executor` wiring, and without changing `ooo run` defaults.",
        "",
        "```text",
        render_baseline_report(report),
        "```",
        "",
        "## Source sample rows",
        "",
        "| AC | source | accepted | attempts | fabrication | chars | note |",
        "|---|---|---:|---:|---:|---:|---|",
    ]
    lines.extend(
        "| {ac_id} | `{source_ref}` | {accepted} | {attempt_count} | "
        "{fabrication_incidents} | {chars} | {note} |".format(
            ac_id=row.ac_id,
            source_ref=row.source_ref,
            accepted="yes" if row.accepted else "no",
            attempt_count=row.attempt_count,
            fabrication_incidents=row.fabrication_incidents,
            chars=row.prompt_chars + row.completion_chars,
            note=row.note,
        )
        for row in captured.rows
    )
    lines.extend(
        [
            "",
            "## New-domain cost source",
            "",
            f"- Source: `{captured.new_domain_source}`",
            f"- LOC delta: {report.new_domain_loc_delta}",
            f"- YAML delta: {report.new_domain_yaml_delta}",
            "",
            "## Gate conclusion",
            "",
            "- 1-shot AC pass rate is captured as the baseline for later post-change comparison.",
            "- K=2 recovery rate is measured against the >= 70% gate.",
            "- Fabrication incidents are measured as verifier-detected incidents per 100 ACs.",
            "- Median chars per AC is captured as the token-budget proxy baseline.",
            "- New-domain cost is measured against <= 50 LOC + <= 1 YAML.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    """Print the captured baseline markdown for local maintainer review."""
    print(render_captured_baseline_markdown(), end="")


if __name__ == "__main__":  # pragma: no cover - exercised by local command use
    main()


__all__ = [
    "BASELINE_NEW_DOMAIN_LOC_DELTA",
    "BASELINE_NEW_DOMAIN_SOURCE",
    "BASELINE_NEW_DOMAIN_YAML_DELTA",
    "BASELINE_PROFILE",
    "CapturedBaselineMetrics",
    "BaselineMetricFixtureRow",
    "RECORDED_BASELINE_ROWS",
    "build_captured_baseline_metrics",
    "render_captured_baseline_markdown",
]
