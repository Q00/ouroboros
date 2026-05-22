"""Canonical acceptance tests.

L0-a — minimal manual harness. Each test in this file runs once per
discovered scenario in ``tests/canonical/<slug>/`` thanks to the
``pytest_generate_tests`` hook in ``conftest.py``.

Two cost regimes:

- Hermetic (default): shape-checks only. Always runs in CI without
  LLM cost. Catches fixture rot.
- Live (``OUROBOROS_RUN_CANONICAL=1``): the maintainer-only path that
  actually invokes ``ouroboros_auto`` and asserts the documented
  terminal. Wiring for the invocation lands in a follow-up PR; for
  now it ``pytest.skip``s with a typed reason so the harness contract
  is visible without burning tokens.

L1 catalog cross-validation (verifying ``expected.yaml``'s
``domain_class`` round-trips through ``TaskClassProfile``) lands in a
follow-up PR once #1173 (L1-a catalog data) merges to main. This PR
keeps the harness self-contained.
"""

from __future__ import annotations

import pytest

from .conftest import CanonicalScenario


def test_scenario_has_nonempty_goal(scenario: CanonicalScenario) -> None:
    """``goal.txt`` is the canonical input to ``ooo auto``; it must
    exist and have meaningful content beyond whitespace."""
    assert scenario.goal, f"{scenario.slug}: goal.txt is empty after strip"
    assert len(scenario.goal) >= 10, (
        f"{scenario.slug}: goal.txt content is suspiciously short "
        f"({len(scenario.goal)} chars); did you forget the real goal?"
    )


def test_scenario_domain_class_is_lowercase_snake(scenario: CanonicalScenario) -> None:
    """``domain_class`` matches the lowercase snake_case shape that the
    L1 catalog (#1173) emits. Pin the surface so a typo in
    ``expected.yaml`` fails here rather than at runtime when the
    inference hook is wired.

    Cross-validation against the actual L1 ``TaskClass`` enum lands in
    a follow-up PR after #1173 merges to main.
    """
    value = scenario.domain_class
    assert value == value.lower(), f"{scenario.slug}: domain_class {value!r} must be lowercase"
    assert value.replace("_", "").isalnum(), (
        f"{scenario.slug}: domain_class {value!r} must be snake_case alphanumerics only"
    )


def test_scenario_completion_mode_is_canonical(scenario: CanonicalScenario) -> None:
    """``completion_mode`` matches the L1 ``CompletionMode`` StrEnum
    surface. Pinned as a string set here so the harness validates
    without importing the catalog module."""
    valid = {"code_complete", "product_complete"}
    assert scenario.completion_mode in valid, (
        f"{scenario.slug}: completion_mode {scenario.completion_mode!r} must be "
        f"one of {sorted(valid)}"
    )


def test_scenario_runtime_probe_kinds_are_strings(
    scenario: CanonicalScenario,
) -> None:
    """``runtime_probe_kinds`` is a tuple of plain strings. Cross-
    validation against the L1 catalog's per-class probe whitelist
    lands in a follow-up PR after #1173 merges; this test pins the
    surface shape only."""
    kinds = scenario.runtime_probe_kinds
    assert isinstance(kinds, tuple)
    for kind in kinds:
        assert isinstance(kind, str), (
            f"{scenario.slug}: runtime_probe_kinds entry {kind!r} must be a string"
        )
        assert kind == kind.lower(), (
            f"{scenario.slug}: runtime_probe_kinds entry {kind!r} must be lowercase"
        )


def test_scenario_wall_clock_budget_is_positive(
    scenario: CanonicalScenario,
) -> None:
    """The optional ``wall_clock_budget_seconds`` must be a positive
    integer when present (or take its default). Zero / negative
    budgets would cause the future L2 watchdog (#1172) to fire instantly."""
    assert scenario.wall_clock_budget_seconds > 0, (
        f"{scenario.slug}: wall_clock_budget_seconds must be positive; "
        f"got {scenario.wall_clock_budget_seconds}"
    )


def test_canonical_matrix_is_nonempty(
    canonical_scenarios: tuple[CanonicalScenario, ...],
) -> None:
    """The matrix must contain at least one scenario. Pins so a
    fixture-file rename does not silently disable the harness."""
    assert canonical_scenarios, (
        "no canonical scenarios discovered under tests/canonical/; "
        "either add a scenario directory or fix the discovery glob"
    )


def test_scenario_live_run_or_skip(
    scenario: CanonicalScenario,
    live_run_enabled: bool,
) -> None:
    """Live invocation of ``ouroboros_auto`` against the scenario.

    Currently skipped unconditionally — wiring lands in a follow-up
    sub-PR after the shape-check contract is exercised on `main`.
    Once wired, this test asserts the documented terminal state for
    the scenario when ``OUROBOROS_RUN_CANONICAL=1`` is set.
    """
    if not live_run_enabled:
        pytest.skip(
            "live canonical run disabled; set OUROBOROS_RUN_CANONICAL=1 to invoke "
            "ouroboros_auto against this scenario"
        )
    pytest.skip(
        f"live-run wiring for {scenario.slug} lands in the L0 follow-up sub-PR; "
        f"shape-check above already validates fixture integrity"
    )
