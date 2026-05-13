"""Tests for fat-harness baseline metrics reports (#961 / #830)."""

from __future__ import annotations

import json

import pytest

from ouroboros.orchestrator.baseline_metrics import (
    FatHarnessGateStatus,
    FatHarnessMetricSample,
    build_fat_harness_metrics_report,
)


def test_report_captures_five_baseline_gates() -> None:
    report = build_fat_harness_metrics_report(
        profile="code",
        samples=(
            FatHarnessMetricSample(
                ac_id="AC-1",
                accepted=True,
                attempt_count=1,
                prompt_chars=900,
                completion_chars=100,
            ),
            FatHarnessMetricSample(
                ac_id="AC-2",
                accepted=True,
                attempt_count=2,
                prompt_chars=1100,
                completion_chars=300,
            ),
            FatHarnessMetricSample(
                ac_id="AC-3",
                accepted=False,
                attempt_count=3,
                fabrication_incidents=1,
                prompt_chars=1300,
                completion_chars=500,
            ),
        ),
        new_domain_loc_delta=42,
        new_domain_yaml_delta=1,
    )

    assert report.total_acs == 3
    assert report.one_shot_pass_rate == pytest.approx(1 / 3)
    assert report.k_recovery_rate == pytest.approx(1 / 2)
    assert report.fabrication_incidents_per_100_acs == pytest.approx(100 / 3)
    assert report.median_chars_per_ac == 1400
    assert report.new_domain_loc_delta == 42
    assert report.new_domain_yaml_delta == 1

    gates = {gate.name: gate for gate in report.gates}
    assert set(gates) == {
        "one_shot_pass_rate",
        "k_recovery_rate",
        "fabrication_incidents_per_100_acs",
        "median_chars_per_ac",
        "new_domain_cost",
    }
    assert gates["one_shot_pass_rate"].status == FatHarnessGateStatus.CAPTURED
    assert gates["k_recovery_rate"].status == FatHarnessGateStatus.FAIL
    assert gates["fabrication_incidents_per_100_acs"].status == FatHarnessGateStatus.FAIL
    assert gates["median_chars_per_ac"].status == FatHarnessGateStatus.CAPTURED
    assert gates["new_domain_cost"].status == FatHarnessGateStatus.PASS


def test_report_accepts_public_positional_arguments_and_passes_thresholds() -> None:
    report = build_fat_harness_metrics_report(
        "code",
        (
            FatHarnessMetricSample("AC-1", accepted=True, attempt_count=1),
            FatHarnessMetricSample("AC-2", accepted=True, attempt_count=2),
            FatHarnessMetricSample("AC-3", accepted=True, attempt_count=3),
        ),
        50,
        1,
        baseline_median_chars_per_ac=0,
    )

    gates = {gate.name: gate for gate in report.gates}
    assert report.one_shot_pass_rate == pytest.approx(1 / 3)
    assert report.k_recovery_rate == 1.0
    assert gates["k_recovery_rate"].status == FatHarnessGateStatus.PASS
    assert gates["fabrication_incidents_per_100_acs"].status == FatHarnessGateStatus.PASS
    assert gates["median_chars_per_ac"].status == FatHarnessGateStatus.PASS
    assert gates["new_domain_cost"].status == FatHarnessGateStatus.PASS


def test_report_marks_char_budget_and_new_domain_cost_failures() -> None:
    report = build_fat_harness_metrics_report(
        profile="code",
        samples=(
            FatHarnessMetricSample(
                ac_id="AC-1",
                accepted=True,
                attempt_count=1,
                prompt_chars=40,
                completion_chars=60,
            ),
        ),
        new_domain_loc_delta=51,
        new_domain_yaml_delta=2,
        baseline_median_chars_per_ac=99,
    )

    gates = {gate.name: gate for gate in report.gates}
    assert report.median_chars_per_ac == 100
    assert gates["median_chars_per_ac"].status == FatHarnessGateStatus.FAIL
    assert gates["new_domain_cost"].status == FatHarnessGateStatus.FAIL


def test_report_is_json_serializable() -> None:
    report = build_fat_harness_metrics_report(
        profile="research",
        samples=(
            FatHarnessMetricSample(
                ac_id="AC-1",
                accepted=True,
                attempt_count=1,
                prompt_chars=500,
                completion_chars=250,
            ),
        ),
        new_domain_loc_delta=0,
        new_domain_yaml_delta=1,
        baseline_median_chars_per_ac=800,
    )

    payload = report.to_dict()

    assert payload["profile"] == "research"
    assert payload["metrics"]["median_chars_per_ac"] == 750
    assert payload["gates"]["median_chars_per_ac"]["status"] == "pass"
    json.dumps(payload)


def test_recovery_rate_is_not_applicable_without_initial_failures() -> None:
    report = build_fat_harness_metrics_report(
        profile="analysis",
        samples=(
            FatHarnessMetricSample(
                ac_id="AC-1",
                accepted=True,
                attempt_count=1,
                prompt_chars=100,
                completion_chars=50,
            ),
        ),
        new_domain_loc_delta=0,
        new_domain_yaml_delta=1,
    )

    assert report.k_recovery_rate is None
    gates = {gate.name: gate for gate in report.gates}
    assert gates["k_recovery_rate"].status == FatHarnessGateStatus.NOT_APPLICABLE


def test_empty_sample_set_is_rejected() -> None:
    with pytest.raises(ValueError, match="samples must not be empty"):
        build_fat_harness_metrics_report(profile="code", samples=(), new_domain_loc_delta=0)


def test_invalid_report_inputs_are_rejected() -> None:
    sample = FatHarnessMetricSample(ac_id="AC-1", accepted=True, attempt_count=1)

    with pytest.raises(ValueError, match="profile must not be empty"):
        build_fat_harness_metrics_report(profile="", samples=(sample,), new_domain_loc_delta=0)

    with pytest.raises(ValueError, match="new_domain_loc_delta must be non-negative"):
        build_fat_harness_metrics_report(profile="code", samples=(sample,), new_domain_loc_delta=-1)

    with pytest.raises(ValueError, match="new_domain_yaml_delta must be non-negative"):
        build_fat_harness_metrics_report(
            profile="code",
            samples=(sample,),
            new_domain_loc_delta=0,
            new_domain_yaml_delta=-1,
        )

    with pytest.raises(ValueError, match="max_retries must be non-negative"):
        build_fat_harness_metrics_report(
            profile="code",
            samples=(sample,),
            new_domain_loc_delta=0,
            max_retries=-1,
        )

    with pytest.raises(ValueError, match="baseline_median_chars_per_ac must be non-negative"):
        build_fat_harness_metrics_report(
            profile="code",
            samples=(sample,),
            new_domain_loc_delta=0,
            baseline_median_chars_per_ac=-1,
        )


def test_invalid_sample_values_are_rejected() -> None:
    with pytest.raises(ValueError, match="ac_id must not be empty"):
        FatHarnessMetricSample(ac_id="", accepted=True, attempt_count=1)

    with pytest.raises(ValueError, match="attempt_count must be >= 1"):
        FatHarnessMetricSample(ac_id="AC-1", accepted=True, attempt_count=0)

    with pytest.raises(ValueError, match="non-negative"):
        FatHarnessMetricSample(
            ac_id="AC-1",
            accepted=True,
            attempt_count=1,
            fabrication_incidents=-1,
        )


def test_sample_helpers_describe_first_try_and_recovery() -> None:
    first_try = FatHarnessMetricSample(
        ac_id="AC-1",
        accepted=True,
        attempt_count=1,
        prompt_chars=12,
        completion_chars=8,
    )
    recovered = FatHarnessMetricSample(ac_id="AC-2", accepted=True, attempt_count=3)
    exhausted = FatHarnessMetricSample(ac_id="AC-3", accepted=True, attempt_count=4)

    assert first_try.total_chars == 20
    assert first_try.accepted_first_try is True
    assert first_try.recovered_within(max_retries=2) is False
    assert recovered.accepted_first_try is False
    assert recovered.recovered_within(max_retries=2) is True
    assert exhausted.recovered_within(max_retries=2) is False
