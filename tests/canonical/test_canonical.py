"""Canonical acceptance tests.

L0-a — minimal manual harness. Each test in this file runs once per
discovered scenario in ``tests/canonical/<slug>/`` thanks to the
``pytest_generate_tests`` hook in ``conftest.py``.

Two cost regimes:

- Hermetic (default): shape-checks + L1 catalog cross-validation
  only. Always runs in CI without LLM cost. Catches fixture rot
  and L0 ↔ L1 contract drift.
- Live (``OUROBOROS_RUN_CANONICAL=1``): the maintainer-only path that
  actually invokes the ``ouroboros_auto`` MCP tool against the
  scenario and asserts the documented terminal state. Costs real
  LLM tokens.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ouroboros.core.types import Result

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


# ---------------------------------------------------------------------------
# L1 catalog cross-validation (folded back in from #1170 L0-a deferred list)
# ---------------------------------------------------------------------------


def test_scenario_domain_class_resolves_in_l1_catalog(
    scenario: CanonicalScenario,
) -> None:
    """``expected.yaml``'s ``domain_class`` must resolve to a real
    :class:`TaskClass` value — otherwise the L1 inference output
    cannot match it.

    This is the L0 ↔ L1 contract pin: adding a scenario whose
    ``domain_class`` is not in the catalog fails here, prompting
    either a typo fix or an L1 catalog extension PR before the
    scenario lands."""
    from ouroboros.auto.task_classes import TaskClass

    valid = {tc.value for tc in TaskClass}
    assert scenario.domain_class in valid, (
        f"{scenario.slug}: domain_class {scenario.domain_class!r} is not a "
        f"known TaskClass; valid values are {sorted(valid)}"
    )


def test_scenario_completion_mode_matches_l1_catalog_default(
    scenario: CanonicalScenario,
) -> None:
    """The scenario's declared ``completion_mode`` must match the L1
    catalog default for its ``domain_class``. A mismatch means the
    scenario is asserting a regime the catalog does not endorse."""
    from ouroboros.auto.task_classes import (
        TASK_CLASS_CATALOG,
        CompletionMode,
        TaskClass,
    )

    task_class = TaskClass(scenario.domain_class)
    expected_mode = TASK_CLASS_CATALOG[task_class].default_completion_mode
    actual_mode = CompletionMode(scenario.completion_mode)
    assert actual_mode == expected_mode, (
        f"{scenario.slug}: completion_mode {scenario.completion_mode!r} "
        f"disagrees with TaskClass.{task_class.name} catalog default "
        f"{expected_mode.value!r}"
    )


def test_scenario_runtime_probe_kinds_subset_of_l1_catalog(
    scenario: CanonicalScenario,
) -> None:
    """Every ``runtime_probe_kinds`` entry in the scenario must be
    declared in the L1 catalog for that ``domain_class``. Pins the
    L0 ↔ L1 ↔ L3 contract: scenarios cannot invent ad-hoc probe
    kinds sideways."""
    from ouroboros.auto.task_classes import TASK_CLASS_CATALOG, TaskClass

    task_class = TaskClass(scenario.domain_class)
    allowed = set(TASK_CLASS_CATALOG[task_class].runtime_probe_kinds)
    declared = set(scenario.runtime_probe_kinds)
    unknown = declared - allowed
    assert not unknown, (
        f"{scenario.slug}: runtime_probe_kinds includes {sorted(unknown)} "
        f"which are not declared for TaskClass.{task_class.name} in the "
        f"catalog (allowed: {sorted(allowed)})"
    )


# ---------------------------------------------------------------------------
# Live invocation (opt-in via OUROBOROS_RUN_CANONICAL=1)
# ---------------------------------------------------------------------------


async def _invoke_ouroboros_auto(scenario: CanonicalScenario, workdir: Path) -> Any:
    """Programmatically invoke the ``ouroboros_auto`` MCP tool for a
    canonical scenario. Returns the handler's ``Result`` so the test
    can assert on its terminal shape.

    Kept inside the test module (rather than under
    ``tests/canonical/_runner.py``) because it is invoked only on the
    opt-in live-run path and pulls in heavy MCP-handler dependencies.
    """
    from ouroboros.auto.state import AutoStore
    from ouroboros.mcp.tools.auto_handler import AutoHandler

    # Use a per-scenario, in-process store rooted under the operator's
    # workdir so the run does not collide with concurrent ``ooo auto``
    # invocations and so a re-run picks a fresh ``auto_session_id``.
    store = AutoStore(workdir / ".ouroboros-canonical")
    handler = AutoHandler(store=store)
    arguments = {
        "goal": scenario.goal,
        "cwd": str(workdir),
        "skip_run": False,
        "complete_product": scenario.completion_mode == "product_complete",
        # Bounded budget so a stuck scenario does not hang the
        # operator. The watchdog (L2) catches anything past this on
        # its own.
        "pipeline_timeout_seconds": float(scenario.wall_clock_budget_seconds),
    }
    return await handler.handle(arguments)


@pytest.mark.asyncio
async def test_scenario_live_run_or_skip(
    scenario: CanonicalScenario,
    live_run_enabled: bool,
    tmp_path: Path,
) -> None:
    """Live invocation of ``ouroboros_auto`` against the scenario.

    Skipped by default — costs real LLM tokens. Set
    ``OUROBOROS_RUN_CANONICAL=1`` to opt in. When enabled, the test:

    1. Copies the scenario's ``env/`` fixture into a fresh ``tmp_path``.
    2. Calls ``AutoHandler.handle({"goal": scenario.goal, ...})``.
    3. Asserts the result is OK (not an MCP-level error) and that the
       documented terminal status is reached.

    Any LLM / MCP misconfiguration surfaces as a failure with the
    underlying error message so the operator sees what's missing.
    """
    if not live_run_enabled:
        pytest.skip(
            "live canonical run disabled; set OUROBOROS_RUN_CANONICAL=1 to "
            "invoke ouroboros_auto against this scenario (costs real LLM tokens)"
        )

    workdir = tmp_path / scenario.slug
    workdir.mkdir(parents=True, exist_ok=True)
    if scenario.env_dir is not None:
        # Seed the env/ fixture into the run workdir if present.
        for entry in scenario.env_dir.iterdir():
            target = workdir / entry.name
            if entry.is_dir():
                import shutil

                shutil.copytree(entry, target)
            else:
                target.write_bytes(entry.read_bytes())

    result = await _invoke_ouroboros_auto(scenario, workdir)

    assert result.is_ok, (
        f"{scenario.slug}: ouroboros_auto returned MCP error: "
        f"{result.error if result.is_err else 'unknown'}"
    )

    tool_result = result.unwrap()
    assert not tool_result.is_error, (
        f"{scenario.slug}: ouroboros_auto reached a failed/blocked terminal: "
        f"{tool_result.content[0].text if tool_result.content else tool_result.meta}"
    )
    assert tool_result.meta["status"] == "complete", (
        f"{scenario.slug}: expected complete terminal status; "
        f"got {tool_result.meta.get('status')!r} with meta {tool_result.meta}"
    )
    if scenario.completion_mode == "product_complete":
        assert tool_result.meta.get("product_status") != "not_verified_complete", (
            f"{scenario.slug}: product_complete scenario must not stop at an "
            f"unverified run handoff: {tool_result.meta}"
        )
    print(
        f"CANONICAL {scenario.slug}: status={tool_result.meta['status']} "
        f"phase={tool_result.meta.get('phase')} completion_mode={scenario.completion_mode}"
    )


@pytest.mark.asyncio
async def test_live_run_formats_mcp_error_result(
    scenario: CanonicalScenario,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Pin the live-run failure message against the real Result API."""

    async def fake_invoke(selected: CanonicalScenario, workdir: Path) -> Result[object, str]:
        return Result.err(f"{selected.slug} handler unavailable at {workdir.name}")

    monkeypatch.setattr(
        "tests.canonical.test_canonical._invoke_ouroboros_auto",
        fake_invoke,
    )

    with pytest.raises(AssertionError) as exc_info:
        await test_scenario_live_run_or_skip(
            scenario=scenario,
            live_run_enabled=True,
            tmp_path=tmp_path,
        )

    message = str(exc_info.value)
    assert f"{scenario.slug}: ouroboros_auto returned MCP error" in message
    assert f"{scenario.slug} handler unavailable" in message
