"""The verify-command gate runs at new-session preparation only.

Stage one warns and proceeds; ``block`` refuses. Sessions already in flight go
through ``execute_precreated_session`` and are therefore never re-judged under
a gate tightened after they started.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.core import seed_verify_gate
from ouroboros.core.seed import (
    AcceptanceCriterionSpec,
    OntologySchema,
    Seed,
    SeedMetadata,
)
from ouroboros.core.types import Result
from ouroboros.orchestrator.runner import OrchestratorRunner
from ouroboros.orchestrator.session import SessionTracker


def _seed(*criteria: AcceptanceCriterionSpec | str) -> Seed:
    return Seed(
        goal="ship it",
        acceptance_criteria=criteria,
        ontology_schema=OntologySchema(name="n", description="d"),
        metadata=SeedMetadata(ambiguity_score=0.05),
    )


@pytest.fixture
def runner(tmp_path: Path) -> OrchestratorRunner:
    adapter = MagicMock()
    adapter.working_directory = str(tmp_path)
    adapter.runtime_backend = "claude"
    runner = OrchestratorRunner(adapter, AsyncMock(), MagicMock())

    async def reconstruct(tracker: SessionTracker) -> Result[SessionTracker, Exception]:
        return Result.ok(tracker)

    runner._reconstruct_precreated_durable_tracker = AsyncMock(side_effect=reconstruct)
    return runner


def test_warn_stage_reports_and_lets_preparation_continue(
    runner: OrchestratorRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(seed_verify_gate, "verify_command_gate_mode", lambda: "warn")
    seed = _seed("Make the dashboard nicer")

    assert runner._apply_verify_command_gate(seed) is None
    printed = " ".join(str(call.args[0]) for call in runner._console.print.call_args_list)
    assert "AC 1: Make the dashboard nicer" in printed


def test_block_stage_refuses_and_names_every_offending_criterion(
    runner: OrchestratorRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(seed_verify_gate, "verify_command_gate_mode", lambda: "block")
    seed = _seed(
        "Make the dashboard nicer",
        AcceptanceCriterionSpec(description="tests pass", verify_command="pytest -q"),
        AcceptanceCriterionSpec(description="refactor"),
    )

    result = runner._apply_verify_command_gate(seed)

    assert result is not None
    assert result.is_err
    assert result.error.details["unverifiable_ac_indices"] == [1, 3]


def test_fully_verified_seed_passes_both_stages(
    runner: OrchestratorRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(seed_verify_gate, "verify_command_gate_mode", lambda: "block")
    seed = _seed(
        AcceptanceCriterionSpec(description="tests pass", verify_command="pytest -q"),
        AcceptanceCriterionSpec(
            description="the copy reads well",
            verify_exemption_reason="prose quality has no deterministic command",
        ),
    )

    assert runner._apply_verify_command_gate(seed) is None


@pytest.mark.asyncio
async def test_block_stage_stops_preparation_before_a_session_exists(
    runner: OrchestratorRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(seed_verify_gate, "verify_command_gate_mode", lambda: "block")
    create_session = AsyncMock()

    with patch.object(runner._session_repo, "create_session", create_session):
        result = await runner.prepare_session(_seed("Make the dashboard nicer"))

    assert result.is_err
    create_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_warn_stage_still_prepares_the_session(
    runner: OrchestratorRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(seed_verify_gate, "verify_command_gate_mode", lambda: "warn")
    seed = _seed("Make the dashboard nicer")
    tracker = SessionTracker.create(
        "exec_prepared",
        seed.metadata.seed_id,
        session_id="orch_prepared",
    )
    create_session = AsyncMock(return_value=Result.ok(tracker))

    with patch.object(runner._session_repo, "create_session", create_session):
        result = await runner.prepare_session(
            seed, execution_id="exec_prepared", session_id="orch_prepared"
        )

    try:
        assert result.is_ok
        create_session.assert_awaited_once()
    finally:
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )


def test_warn_stage_survives_rich_markup_in_seed_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Descriptions and commands are seed text; a stray Rich closing tag must
    render as text, not abort session preparation with a MarkupError."""
    import io

    from rich.console import Console

    monkeypatch.setattr(seed_verify_gate, "verify_command_gate_mode", lambda: "warn")
    adapter = MagicMock()
    adapter.working_directory = str(tmp_path)
    adapter.runtime_backend = "claude"
    buffer = io.StringIO()
    runner = OrchestratorRunner(adapter, AsyncMock(), Console(file=buffer, width=200))
    seed = _seed("Close the [/yellow] tag [bold]boldly[/bold]")

    assert runner._apply_verify_command_gate(seed) is None
    assert "[/yellow]" in buffer.getvalue()
