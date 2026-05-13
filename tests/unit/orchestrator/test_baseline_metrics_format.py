"""Tests for the human-readable baseline-metric report formatter.

The formatter is a pure function over
:class:`FatHarnessMetricsReport`. It must:

* Produce deterministic output (same report → same string).
* Show each gate's status glyph and value.
* Show the summary metric block.
* Tolerate ``None`` values (gates that are CAPTURED only, not yet
  PASS / FAIL evaluated).
"""

from __future__ import annotations

import pytest

from ouroboros.orchestrator.baseline_metrics import (
    FatHarnessGateResult,
    FatHarnessGateStatus,
    FatHarnessMetricSample,
    build_fat_harness_metrics_report,
)
from ouroboros.orchestrator.baseline_metrics_format import render_baseline_report


def _sample(
    ac_id: str,
    *,
    accepted: bool,
    attempt_count: int = 1,
    fabrication_incidents: int = 0,
    semantic_miss_incidents: int = 0,
    prompt_chars: int = 1000,
    completion_chars: int = 500,
) -> FatHarnessMetricSample:
    return FatHarnessMetricSample(
        ac_id=ac_id,
        accepted=accepted,
        attempt_count=attempt_count,
        fabrication_incidents=fabrication_incidents,
        semantic_miss_incidents=semantic_miss_incidents,
        prompt_chars=prompt_chars,
        completion_chars=completion_chars,
    )


@pytest.fixture
def green_report():
    samples = [
        _sample("ac_1", accepted=True, attempt_count=1),
        _sample("ac_2", accepted=True, attempt_count=2),
        _sample("ac_3", accepted=True, attempt_count=1),
    ]
    return build_fat_harness_metrics_report(
        profile="fat_harness",
        samples=samples,
        new_domain_loc_delta=40,
        new_domain_yaml_delta=1,
    )


class TestRenderBaselineReport:
    def test_returns_multiline_string(self, green_report) -> None:
        output = render_baseline_report(green_report)
        assert isinstance(output, str)
        assert output.count("\n") >= 4

    def test_header_contains_profile_and_ac_count(self, green_report) -> None:
        output = render_baseline_report(green_report)
        first_line = output.splitlines()[0]
        assert "profile=fat_harness" in first_line
        assert f"acs={green_report.total_acs}" in first_line
        assert f"K={green_report.max_retries}" in first_line

    def test_output_is_deterministic(self, green_report) -> None:
        first = render_baseline_report(green_report)
        second = render_baseline_report(green_report)
        assert first == second

    def test_every_gate_appears_in_output(self, green_report) -> None:
        output = render_baseline_report(green_report)
        for gate in green_report.gates:
            assert gate.name in output

    def test_status_glyphs_render(self, green_report) -> None:
        output = render_baseline_report(green_report)
        glyphs_present = {"[PASS]", "[FAIL]", "[CAPT]", "[ N/A]"}
        appearing = {g for g in glyphs_present if g in output}
        assert appearing, f"no status glyph rendered; output was:\n{output}"

    def test_summary_block_lists_each_metric(self, green_report) -> None:
        output = render_baseline_report(green_report)
        for label in (
            "one_shot_pass_rate",
            "k_recovery_rate",
            "fabrication_incidents_per_100_acs",
            "semantic_miss_incidents_per_100_acs",
            "median_chars_per_ac",
            "new_domain_loc_delta",
            "new_domain_yaml_delta",
        ):
            assert label in output, f"missing summary label '{label}'"

    def test_renders_none_metric_values_as_na(self) -> None:
        # k_recovery_rate is None when every AC succeeded on the first
        # try (no retries to evaluate). The report should not crash and
        # must render ``n/a`` rather than blowing up on float
        # formatting.
        samples = [_sample("ac_1", accepted=True, attempt_count=1)]
        report = build_fat_harness_metrics_report(
            profile="fat_harness",
            samples=samples,
            new_domain_loc_delta=40,
        )
        output = render_baseline_report(report)
        assert "k_recovery_rate" in output
        assert "n/a" in output


class TestRenderHandlesAllGateStatuses:
    """Smoke-test that each FatHarnessGateStatus value has a glyph."""

    def test_glyph_table_covers_all_statuses(self) -> None:
        from ouroboros.orchestrator.baseline_metrics_format import _STATUS_GLYPH

        for status in FatHarnessGateStatus:
            assert status in _STATUS_GLYPH, f"missing glyph for {status}"


class TestRenderTargetSuffix:
    def test_empty_target_omits_suffix(self) -> None:
        from ouroboros.orchestrator.baseline_metrics_format import _format_target

        gate = FatHarnessGateResult(
            name="x",
            status=FatHarnessGateStatus.CAPTURED,
            value=1.0,
            target="",
            rationale="",
        )
        assert _format_target(gate) == ""

    def test_present_target_renders(self) -> None:
        from ouroboros.orchestrator.baseline_metrics_format import _format_target

        gate = FatHarnessGateResult(
            name="x",
            status=FatHarnessGateStatus.PASS,
            value=0.8,
            target=">= 0.70",
            rationale="",
        )
        assert "target >= 0.70" in _format_target(gate)
