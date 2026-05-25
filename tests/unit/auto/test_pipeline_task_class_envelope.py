"""Integration tests for the L1-d/L1-e envelope surface (#1171).

Asserts:
- After ``ouroboros_auto`` runs, ``AutoPipelineResult.active_task_class``
  is populated when the ledger unambiguously matches a single class.
- The Seed passed downstream has the catalog's ``default_ac_template``
  prepended to ``acceptance_criteria``.
- An unmatched ledger applies the safe LIBRARY fallback; an ambiguous
  ledger leaves ``active_task_class`` as None and AC untouched.
"""

from __future__ import annotations

import pytest

from ouroboros.auto.interview_driver import (
    AutoInterviewDriver,
    FunctionInterviewBackend,
    InterviewTurn,
)
from ouroboros.auto.ledger import (
    LedgerEntry,
    LedgerSource,
    LedgerStatus,
    SeedDraftLedger,
)
from ouroboros.auto.pipeline import AutoPipeline
from ouroboros.auto.state import AutoPipelineState, AutoStore
from ouroboros.auto.task_classes import TASK_CLASS_CATALOG, TaskClass
from ouroboros.core.seed import (
    EvaluationPrinciple,
    ExitCondition,
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
)


def _build_seed(ac: tuple[str, ...] = ("user-AC entry",)) -> Seed:
    return Seed(
        goal="Build a CLI",
        constraints=("Use existing project patterns",),
        acceptance_criteria=ac,
        ontology_schema=OntologySchema(
            name="CliTask",
            description="CLI task ontology",
            fields=(OntologyField(name="command", field_type="string", description="Command"),),
        ),
        evaluation_principles=(
            EvaluationPrinciple(name="testability", description="Observable behavior", weight=1.0),
        ),
        exit_conditions=(
            ExitCondition(
                name="verified",
                description="Checks pass",
                evaluation_criteria="All acceptance criteria pass",
            ),
        ),
        metadata=SeedMetadata(seed_id="seed_test_envelope", ambiguity_score=0.12),
    )


def _seed_strong_ac() -> Seed:
    """Seed that already includes verifiable AC so the reviewer awards
    A grade without going through the repair loop. Keeps the envelope
    test focused on L1-d/L1-e instead of the grade gate."""
    return _build_seed(ac=("`habit list` prints stable stdout containing created habits",))


def _add(ledger: SeedDraftLedger, section: str, value: str, key: str) -> None:
    ledger.add_entry(
        section,
        LedgerEntry(
            key=key,
            value=value,
            source=LedgerSource.USER_PREFERENCE,
            confidence=0.9,
            status=LedgerStatus.CONFIRMED,
        ),
    )


def _cli_ledger() -> SeedDraftLedger:
    """Build a ledger whose entries unambiguously match TaskClass.CLI."""
    ledger = SeedDraftLedger.from_goal("Build a habit-tracker CLI tool")
    # Ledger needs to be seed-ready so the interview completes cleanly.
    _add(ledger, "actors", "Single local CLI user", "actors.cli")
    _add(ledger, "inputs", "Command-line arguments", "inputs.cli")
    _add(
        ledger,
        "outputs",
        "Deterministic stdout and exit code 0",
        "outputs.cli",
    )
    _add(ledger, "constraints", "Local CLI only", "constraints.cli")
    _add(ledger, "non_goals", "No GUI", "non_goals.cli")
    _add(
        ledger,
        "acceptance_criteria",
        "Command prints stable output",
        "acceptance_criteria.cli",
    )
    _add(ledger, "verification_plan", "Run command-level tests", "verification_plan.cli")
    _add(ledger, "failure_modes", "Invalid input exits non-zero", "failure_modes.cli")
    _add(
        ledger,
        "runtime_context",
        "Local shell / terminal subprocess",
        "runtime_context.cli",
    )
    return ledger


def _unmatched_ledger() -> SeedDraftLedger:
    """Build a seed-ready ledger with no task-class-specific signals."""
    ledger = SeedDraftLedger.from_goal("Improve the project")
    _add(ledger, "actors", "Repository maintainer", "actors.generic")
    _add(ledger, "inputs", "Existing repository context", "inputs.generic")
    _add(ledger, "outputs", "Documented code changes", "outputs.generic")
    _add(ledger, "constraints", "Use existing project patterns", "constraints.generic")
    _add(ledger, "non_goals", "No unrelated redesign", "non_goals.generic")
    _add(
        ledger,
        "acceptance_criteria",
        "Changed behavior is covered by tests",
        "acceptance_criteria.generic",
    )
    _add(ledger, "verification_plan", "Run targeted tests", "verification_plan.generic")
    _add(ledger, "failure_modes", "Regression in existing behavior", "failure_modes.generic")
    _add(
        ledger, "runtime_context", "Existing repository test environment", "runtime_context.generic"
    )
    return ledger


def _ambiguous_ledger() -> SeedDraftLedger:
    """Build a ledger that matches BOTH CLI and WEBHOOK — ambiguous."""
    ledger = SeedDraftLedger.from_goal("Build a CLI that also receives webhooks")
    _add(ledger, "actors", "User and webhook clients", "actors.both")
    _add(
        ledger,
        "inputs",
        "CLI args plus incoming webhook POST payloads",
        "inputs.both",
    )
    _add(
        ledger,
        "outputs",
        "Stdout shows status; DB row stored on each event",
        "outputs.both",
    )
    _add(ledger, "constraints", "Local only", "constraints.both")
    _add(ledger, "non_goals", "No GUI", "non_goals.both")
    _add(
        ledger,
        "acceptance_criteria",
        "stdout exit code 0; webhook 200 returned",
        "ac.both",
    )
    _add(ledger, "verification_plan", "tests cover both", "vp.both")
    _add(ledger, "failure_modes", "bad input exits non-zero", "fm.both")
    _add(
        ledger,
        "runtime_context",
        "Local shell or background daemon",
        "runtime_context.both",
    )
    return ledger


@pytest.mark.asyncio
async def test_envelope_carries_active_task_class_on_single_match(tmp_path) -> None:
    """Single-match inference → ``active_task_class`` populated and the
    catalog template AC entries are prepended to the Seed."""
    profile = TASK_CLASS_CATALOG[TaskClass.CLI]

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What should we verify?", "interview_envelope")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", session_id, seed_ready=True, completed=True)

    async def generate_seed(_session_id: str) -> Seed:
        return _seed_strong_ac()

    state = AutoPipelineState(goal="Build a habit-tracker CLI tool", cwd=str(tmp_path))
    ledger = _cli_ledger()
    state.ledger = ledger.to_dict()

    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer),
        store=AutoStore(tmp_path),
        max_rounds=1,
    )
    pipeline = AutoPipeline(
        driver,
        generate_seed,
        store=AutoStore(tmp_path),
        skip_run=True,
    )

    result = await pipeline.run(state)

    assert result.active_task_class == TaskClass.CLI.value
    ac = state.seed_artifact["acceptance_criteria"]
    # Template entries are prepended in order.
    for index, expected in enumerate(profile.default_ac_template):
        assert ac[index] == expected, (
            f"AC[{index}] should be the L1-d-prepended template entry; got {ac[index]!r}"
        )
    # The user's original AC remains present after the template.
    assert "`habit list` prints stable stdout containing created habits" in ac


@pytest.mark.asyncio
async def test_envelope_uses_library_fallback_when_unmatched(tmp_path) -> None:
    """Unmatched inference applies the L1-b safe LIBRARY fallback instead
    of silently skipping default AC injection."""
    profile = TASK_CLASS_CATALOG[TaskClass.LIBRARY]

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What should we verify?", "interview_envelope_unmatched")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", session_id, seed_ready=True, completed=True)

    user_ac = ("Changed behavior is covered by tests",)

    async def generate_seed(_session_id: str) -> Seed:
        return _build_seed(ac=user_ac)

    state = AutoPipelineState(goal="Improve the project", cwd=str(tmp_path))
    ledger = _unmatched_ledger()
    state.ledger = ledger.to_dict()

    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer),
        store=AutoStore(tmp_path),
        max_rounds=1,
    )
    pipeline = AutoPipeline(
        driver,
        generate_seed,
        store=AutoStore(tmp_path),
        skip_run=True,
    )

    result = await pipeline.run(state)

    assert result.active_task_class == TaskClass.LIBRARY.value
    ac = state.seed_artifact["acceptance_criteria"]
    for index, expected in enumerate(profile.default_ac_template):
        # The pipeline may wrap generic/library AC in execution-oriented
        # phrasing, but it preserves the injected template text as the
        # original requirement payload.
        assert expected.removesuffix(".").replace(" are ", " ") in ac[index]
    assert len(ac) >= len(profile.default_ac_template) + 1
    assert any("Changed behavior" in item and "tests" in item for item in ac)


@pytest.mark.asyncio
async def test_envelope_leaves_active_task_class_none_when_ambiguous(tmp_path) -> None:
    """Ambiguous inference (multiple patterns fire) → ``active_task_class``
    is None and no template entries are injected. The interview-driver
    disambiguation hook (L1-c) is a future PR; for now ambiguous inputs
    simply leave the user's AC untouched."""

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What should we verify?", "interview_envelope_amb")

    async def answer(session_id: str, text: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("done", session_id, seed_ready=True, completed=True)

    user_ac = ("`habit list` prints stable stdout containing created habits",)

    async def generate_seed(_session_id: str) -> Seed:
        return _build_seed(ac=user_ac)

    state = AutoPipelineState(goal="Build a CLI that also receives webhooks", cwd=str(tmp_path))
    ledger = _ambiguous_ledger()
    state.ledger = ledger.to_dict()

    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer),
        store=AutoStore(tmp_path),
        max_rounds=1,
    )
    pipeline = AutoPipeline(
        driver,
        generate_seed,
        store=AutoStore(tmp_path),
        skip_run=True,
    )

    result = await pipeline.run(state)

    assert result.active_task_class is None
    ac = state.seed_artifact["acceptance_criteria"]
    # No catalog templates were injected — user AC stays as-is (plus
    # any normalization the existing pipeline applies, but the
    # template entries from L1-d.CLI are not added).
    cli_template_entries = TASK_CLASS_CATALOG[TaskClass.CLI].default_ac_template
    for template_entry in cli_template_entries:
        assert template_entry not in ac, (
            f"ambiguous inference must not inject CLI template entry: {template_entry!r}"
        )
