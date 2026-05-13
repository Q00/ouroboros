"""Tests for the recorded #961 fat-harness baseline capture artifact."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ouroboros.orchestrator.baseline_metrics import FatHarnessGateStatus
from ouroboros.orchestrator.baseline_metrics_capture import (
    BASELINE_NEW_DOMAIN_LOC_DELTA,
    BASELINE_NEW_DOMAIN_YAML_DELTA,
    RECORDED_BASELINE_ROWS,
    build_captured_baseline_metrics,
    render_captured_baseline_markdown,
)


def test_captured_baseline_records_all_five_gate_values() -> None:
    captured = build_captured_baseline_metrics()
    report = captured.report

    assert report.total_acs == len(RECORDED_BASELINE_ROWS) == 8
    assert report.one_shot_pass_rate == pytest.approx(0.5)
    assert report.k_recovery_rate == pytest.approx(0.75)
    assert report.fabrication_incidents_per_100_acs == 0
    assert report.median_chars_per_ac == pytest.approx(1820)
    assert report.new_domain_loc_delta == BASELINE_NEW_DOMAIN_LOC_DELTA == 42
    assert report.new_domain_yaml_delta == BASELINE_NEW_DOMAIN_YAML_DELTA == 1

    gates = {gate.name: gate for gate in report.gates}
    assert set(gates) == {
        "one_shot_pass_rate",
        "k_recovery_rate",
        "fabrication_incidents_per_100_acs",
        "median_chars_per_ac",
        "new_domain_cost",
    }
    assert gates["one_shot_pass_rate"].status == FatHarnessGateStatus.CAPTURED
    assert gates["k_recovery_rate"].status == FatHarnessGateStatus.PASS
    assert gates["fabrication_incidents_per_100_acs"].status == FatHarnessGateStatus.PASS
    assert gates["median_chars_per_ac"].status == FatHarnessGateStatus.CAPTURED
    assert gates["new_domain_cost"].status == FatHarnessGateStatus.PASS


def test_captured_baseline_keeps_source_rows_with_report() -> None:
    captured = build_captured_baseline_metrics()
    payload = captured.to_dict()

    assert len(payload["sources"]["sample_rows"]) == captured.report.total_acs
    assert payload["sources"]["sample_rows"][0]["source_ref"].startswith("fixture:")
    assert payload["sources"]["new_domain_cost"]["loc_delta"] == 42
    json.dumps(payload)


def test_markdown_artifact_matches_renderer_output() -> None:
    expected = render_captured_baseline_markdown()
    artifact = Path("docs/agentos/fat-harness-baseline-metrics.md").read_text()

    assert artifact == expected
    for label in (
        "one_shot_pass_rate",
        "k_recovery_rate",
        "fabrication_incidents_per_100_acs",
        "median_chars_per_ac",
        "new_domain_cost",
    ):
        assert label in artifact
