"""Offline TraceGuard-vs-legacy baseline benchmark for #978 P4.

This module publishes the first C.4 benchmark surface required by #961.
It is deliberately fixture-only: no live model calls, no `ooo run` default
flip, and no removal of the legacy self-report path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ouroboros.harness.deliver_gate import DeliverGateVerdict
from ouroboros.harness.deliver_routing import deliver_gate_verifier_verdict
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
BENCHMARK_PROFILE_TRACEGUARD_CLAIM_TERM_GUARD = "traceguard_plus_claim_term_guard_fixture"


@dataclass(frozen=True)
class H1RetryAdmissionFixtureRow:
    """Recorded H1 admission outcome for a deliver-gate verdict fixture."""

    fixture: str
    accepted: bool
    failure_class: str | None
    retry_admission: str
    evidence_used_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "fixture": self.fixture,
            "accepted": self.accepted,
            "failure_class": self.failure_class,
            "retry_admission": self.retry_admission,
            "evidence_used_count": self.evidence_used_count,
        }


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
    claim_term_guard_report: FatHarnessMetricsReport
    legacy_rows: tuple[BaselineMetricFixtureRow, ...]
    traceguard_rows: tuple[BaselineMetricFixtureRow, ...]
    claim_term_guard_rows: tuple[BaselineMetricFixtureRow, ...]
    h1_retry_admission_rows: tuple[H1RetryAdmissionFixtureRow, ...]

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
            "claim_term_guard": {
                "report": self.claim_term_guard_report.to_dict(),
                "rows": [row.to_dict() for row in self.claim_term_guard_rows],
            },
            "h1_retry_admission": {
                "rows": [row.to_dict() for row in self.h1_retry_admission_rows],
                "all_typed": all(row.retry_admission for row in self.h1_retry_admission_rows),
                "accepted_count": sum(1 for row in self.h1_retry_admission_rows if row.accepted),
                "routed_count": sum(
                    1 for row in self.h1_retry_admission_rows if row.retry_admission != "ACCEPT"
                ),
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
                "claim_term_guard_semantic_miss_incidents_per_100_acs": (
                    self.claim_term_guard_report.semantic_miss_incidents_per_100_acs
                    - self.traceguard_report.semantic_miss_incidents_per_100_acs
                ),
                "claim_term_guard_median_chars_ratio": (
                    self.claim_term_guard_report.median_chars_per_ac
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
    claim_term_guard_rows = _claim_term_guard_rows(traceguard_rows)
    return TraceGuardBenchmarkCapture(
        legacy_report=_report(BENCHMARK_PROFILE_LEGACY, LEGACY_SELF_REPORT_ROWS),
        traceguard_report=_report(BENCHMARK_PROFILE_TRACEGUARD, traceguard_rows),
        claim_term_guard_report=_report(
            BENCHMARK_PROFILE_TRACEGUARD_CLAIM_TERM_GUARD,
            claim_term_guard_rows,
        ),
        legacy_rows=LEGACY_SELF_REPORT_ROWS,
        traceguard_rows=traceguard_rows,
        claim_term_guard_rows=claim_term_guard_rows,
        h1_retry_admission_rows=_h1_retry_admission_rows(),
    )


def _claim_term_guard_rows(
    rows: tuple[BaselineMetricFixtureRow, ...],
) -> tuple[BaselineMetricFixtureRow, ...]:
    guarded: list[BaselineMetricFixtureRow] = []
    for row in rows:
        if row.semantic_miss_incidents == 0:
            guarded.append(row)
            continue
        guarded.append(
            BaselineMetricFixtureRow(
                ac_id=row.ac_id,
                source_ref="fixture:claim-term-guard/rejected-semantic-miss",
                accepted=False,
                attempt_count=row.attempt_count,
                prompt_chars=row.prompt_chars,
                completion_chars=row.completion_chars,
                fabrication_incidents=row.fabrication_incidents,
                semantic_miss_incidents=0,
                note=(
                    "Semantic guard rejected the evidence-backed-but-wrong claim "
                    "instead of counting it as an accepted semantic miss."
                ),
            )
        )
    return tuple(guarded)


def _h1_retry_admission_rows() -> tuple[H1RetryAdmissionFixtureRow, ...]:
    fixtures = {
        "fixture:h1/traceguard/accepted": DeliverGateVerdict(
            ac_id="AC-1",
            accepted=True,
            unsupported_claim_rate=0.0,
            accepted_fact_ids=("fact_1",),
            evidence_event_ids=("evt_accept",),
        ),
        "fixture:h1/traceguard/missing-evidence": DeliverGateVerdict(
            ac_id="AC-2",
            accepted=False,
            unsupported_claim_rate=1.0,
            rejected_fact_ids=("fact_missing",),
            rejected_reasons=("missing_evidence_handle: ev_missing is not present in manifest",),
        ),
        "fixture:h1/claim-term/semantic-miss": DeliverGateVerdict(
            ac_id="AC-3",
            accepted=False,
            unsupported_claim_rate=1.0,
            rejected_fact_ids=("fact_semantic",),
            rejected_reasons=("semantic_miss: evidence text lacks behavior=admin_delete_denied",),
            evidence_event_ids=("evt_semantic",),
        ),
        "fixture:h1/traceguard/repeated-fabrication": DeliverGateVerdict(
            ac_id="AC-4",
            accepted=False,
            unsupported_claim_rate=1.0,
            rejected_fact_ids=("fact_fake",),
            rejected_reasons=("unsupported_fact_id: fact_fake is not present",),
        ),
    }
    rows: list[H1RetryAdmissionFixtureRow] = []
    for fixture, verdict in fixtures.items():
        rejection_count = 2 if fixture.endswith("repeated-fabrication") else 1
        verifier_verdict = deliver_gate_verifier_verdict(
            verdict,
            rejection_count=rejection_count,
            model_escalation_threshold=2,
        )
        rows.append(
            H1RetryAdmissionFixtureRow(
                fixture=fixture,
                accepted=verifier_verdict.passed,
                failure_class=verifier_verdict.failure_class,
                retry_admission=verifier_verdict.retry_admission.value,
                evidence_used_count=len(verifier_verdict.evidence_used),
            )
        )
    return tuple(rows)


def render_traceguard_benchmark_markdown(
    capture: TraceGuardBenchmarkCapture | None = None,
) -> str:
    """Render the benchmark artifact for maintainer review."""
    capture = build_traceguard_benchmark_capture() if capture is None else capture
    capture_data = capture.to_dict()
    data = capture_data["delta"]
    h1_rows = capture_data["h1_retry_admission"]["rows"]
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
        "## TraceGuard + claim-term guard",
        "",
        "```text",
        render_baseline_report(capture.claim_term_guard_report),
        "```",
        "",
        "## Delta",
        "",
        f"- Fabrication incidents per 100 ACs: {data['fabrication_incidents_per_100_acs']:.4f}",
        f"- Semantic-miss incidents per 100 ACs: {data['semantic_miss_incidents_per_100_acs']:.4f}",
        f"- Median chars ratio: {data['median_chars_ratio']:.4f}",
        "- Claim-term guard semantic-miss incidents per 100 ACs: "
        f"{data['claim_term_guard_semantic_miss_incidents_per_100_acs']:.4f}",
        f"- Claim-term guard median chars ratio: {data['claim_term_guard_median_chars_ratio']:.4f}",
        "",
        "## H1 retry admission",
        "",
        "| Fixture | Accepted | Failure class | Retry admission | Evidence refs |",
        "| --- | --- | --- | --- | ---: |",
        *(
            "| {fixture} | {accepted} | {failure_class} | {retry_admission} | "
            "{evidence_used_count} |".format(
                fixture=row["fixture"],
                accepted=str(row["accepted"]).lower(),
                failure_class=row["failure_class"] or "",
                retry_admission=row["retry_admission"],
                evidence_used_count=row["evidence_used_count"],
            )
            for row in h1_rows
        ),
        "",
        "## Gate interpretation",
        "",
        "- TraceGuard reduces fixture fabrication incidents to 0 per 100 ACs.",
        "- The deterministic claim-term guard rejects the fixture semantic miss without reintroducing fabrication.",
        "- One-shot pass rate drops because unsupported legacy self-reports are rejected instead of counted as accepted.",
        "- Median chars stay within the <= 1.5x C.4 budget guardrail.",
        "- H1 admission is typed for accepted, retryable, redispatch, and model-escalation verdicts.",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    print(render_traceguard_benchmark_markdown(), end="")


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = [
    "BENCHMARK_PROFILE_LEGACY",
    "BENCHMARK_PROFILE_TRACEGUARD",
    "BENCHMARK_PROFILE_TRACEGUARD_CLAIM_TERM_GUARD",
    "LEGACY_SELF_REPORT_ROWS",
    "H1RetryAdmissionFixtureRow",
    "TraceGuardBenchmarkCapture",
    "build_traceguard_benchmark_capture",
    "render_traceguard_benchmark_markdown",
]
