"""Tests for MCP client-side interview gate metadata."""

import pytest

from ouroboros.bigbang.ambiguity import AmbiguityScore, ComponentScore, ScoreBreakdown
from ouroboros.bigbang.interview import InterviewState
from ouroboros.core.types import Result
from ouroboros.mcp.tools.authoring_handlers import (
    GenerateSeedHandler,
    InterviewHandler,
    get_client_gate_status,
)


def test_client_gate_status_reports_missing_required_gates() -> None:
    status = get_client_gate_status({})

    assert status["required_client_gates"] == (
        "seed_ready_acceptance_guard",
        "restate_goal_approved",
    )
    assert status["missing_client_gates"] == status["required_client_gates"]
    assert "client_gate_warning" in status


def test_client_gate_status_accepts_all_required_gates() -> None:
    status = get_client_gate_status(
        {"client_gates": ["restate_goal_approved", "seed_ready_acceptance_guard"]}
    )

    assert status["missing_client_gates"] == ()
    assert "client_gate_warning" not in status


def test_generate_seed_definition_types_client_gates_as_string_array() -> None:
    param = next(p for p in GenerateSeedHandler().definition.parameters if p.name == "client_gates")

    assert param.items == {"type": "string"}


@pytest.mark.asyncio
async def test_generate_seed_hard_blocks_missing_client_gates_when_required(monkeypatch) -> None:
    monkeypatch.setenv("OUROBOROS_REQUIRE_CLIENT_GATES", "1")
    handler = GenerateSeedHandler()

    result = await handler.handle({"session_id": "session-gate"})

    assert result.is_err
    assert "requires acknowledged client-side interview gates" in result.error.message
    assert "seed_ready_acceptance_guard" in result.error.message
    assert "restate_goal_approved" in result.error.message


@pytest.mark.asyncio
async def test_generate_seed_hard_gate_allows_complete_client_gates(monkeypatch) -> None:
    monkeypatch.setenv("OUROBOROS_REQUIRE_CLIENT_GATES", "1")

    class Engine:
        async def load_state(self, session_id: str):
            return Result.err("stop after client-gate precondition")

    handler = GenerateSeedHandler(interview_engine=Engine())

    result = await handler.handle(
        {
            "session_id": "session-gate",
            "client_gates": ["seed_ready_acceptance_guard", "restate_goal_approved"],
        }
    )

    assert result.is_err
    assert "requires acknowledged client-side interview gates" not in result.error.message
    assert "Failed to load interview state" in result.error.message


@pytest.mark.asyncio
async def test_seed_ready_response_exposes_required_client_gates() -> None:
    class Engine:
        async def complete_interview(self, state: InterviewState):
            return Result.ok(state)

        async def save_state(self, state: InterviewState):
            return Result.ok(None)

    handler = InterviewHandler()
    score = AmbiguityScore(
        overall_score=0.1,
        breakdown=ScoreBreakdown(
            goal_clarity=ComponentScore(
                name="Goal Clarity",
                clarity_score=0.9,
                weight=0.4,
                justification="clear",
            ),
            constraint_clarity=ComponentScore(
                name="Constraint Clarity",
                clarity_score=0.9,
                weight=0.3,
                justification="clear",
            ),
            success_criteria_clarity=ComponentScore(
                name="Success Criteria Clarity",
                clarity_score=0.9,
                weight=0.3,
                justification="clear",
            ),
        ),
    )

    result = await handler._complete_interview_response(
        Engine(),
        InterviewState(interview_id="session-gate"),
        "session-gate",
        score,
    )

    assert result.is_ok
    assert result.value.meta["required_client_gates"] == (
        "seed_ready_acceptance_guard",
        "restate_goal_approved",
    )
