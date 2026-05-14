"""Tests for the #978 P4 TraceGuard-vs-legacy fixture benchmark."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ouroboros.orchestrator.traceguard_benchmark_capture import (
    LEGACY_SELF_REPORT_ROWS,
    build_traceguard_benchmark_capture,
    render_traceguard_benchmark_markdown,
)


def test_traceguard_benchmark_reports_required_ab_metrics() -> None:
    capture = build_traceguard_benchmark_capture()

    assert capture.legacy_report.total_acs == len(LEGACY_SELF_REPORT_ROWS) == 8
    assert capture.traceguard_report.total_acs == 8
    assert capture.legacy_report.fabrication_incidents_per_100_acs == pytest.approx(25.0)
    assert capture.traceguard_report.fabrication_incidents_per_100_acs == 0.0
    assert capture.legacy_report.semantic_miss_incidents_per_100_acs == pytest.approx(25.0)
    assert capture.traceguard_report.semantic_miss_incidents_per_100_acs == pytest.approx(12.5)
    assert capture.claim_term_guard_report.fabrication_incidents_per_100_acs == 0.0
    assert capture.claim_term_guard_report.semantic_miss_incidents_per_100_acs == 0.0
    assert (
        capture.traceguard_report.median_chars_per_ac / capture.legacy_report.median_chars_per_ac
    ) <= 1.5
    assert (
        capture.claim_term_guard_report.median_chars_per_ac
        / capture.legacy_report.median_chars_per_ac
    ) <= 1.5


def test_traceguard_benchmark_delta_is_json_serializable() -> None:
    payload = build_traceguard_benchmark_capture().to_dict()

    assert payload["delta"]["fabrication_incidents_per_100_acs"] == pytest.approx(-25.0)
    assert payload["delta"]["semantic_miss_incidents_per_100_acs"] == pytest.approx(-12.5)
    assert payload["delta"]["median_chars_ratio"] <= 1.5
    assert payload["delta"]["claim_term_guard_semantic_miss_incidents_per_100_acs"] == pytest.approx(
        -12.5
    )
    assert payload["delta"]["claim_term_guard_median_chars_ratio"] <= 1.5
    json.dumps(payload)


def test_traceguard_benchmark_markdown_artifact_matches_renderer() -> None:
    expected = render_traceguard_benchmark_markdown()
    artifact = Path("docs/agentos/traceguard-vs-legacy-benchmark.md").read_text()

    assert artifact == expected
    assert "Fabrication incidents per 100 ACs" in artifact
    assert "Semantic-miss incidents per 100 ACs" in artifact
    assert "TraceGuard + claim-term guard" in artifact
