"""Regression tests for the RUN → RALPH_HANDOFF chain (Q00/ouroboros#773).

The chain is opt-in via ``--complete-product`` / ``complete_product=True`` and
maps the Ralph loop's terminal status onto an auto phase per the contract
pinned in this file. Default-off behavior must be byte-identical to the
pre-#773 result shape.
"""

from __future__ import annotations

from typing import Any

import pytest

from ouroboros.auto import pipeline as pipeline_module
from ouroboros.auto.grading import GradeResult, SeedGrade
from ouroboros.auto.interview_driver import AutoInterviewResult
from ouroboros.auto.pipeline import (
    _RALPH_BLOCKED_STOP_REASONS,
    AutoPipeline,
)
from ouroboros.auto.seed_reviewer import SeedReview, SeedReviewer
from ouroboros.auto.state import (
    _ALLOWED_TRANSITIONS,
    AutoPhase,
    AutoPipelineState,
)
from ouroboros.core.seed import (
    EvaluationPrinciple,
    ExitCondition,
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
)
from ouroboros.events.base import BaseEvent
from ouroboros.mcp.job_manager import JobStatus


def _build_seed(seed_id: str = "seed_test_001") -> Seed:
    """Build the smallest valid Seed the auto pipeline tests can carry through."""
    return Seed(
        goal="Build a CLI",
        constraints=("Use existing project patterns",),
        acceptance_criteria=("Command prints stable output",),
        ontology_schema=OntologySchema(
            name="CliTask",
            description="CLI task ontology",
            fields=(
                OntologyField(
                    name="command",
                    field_type="string",
                    description="Command",
                ),
            ),
        ),
        evaluation_principles=(
            EvaluationPrinciple(
                name="testability",
                description="Observable behavior",
                weight=1.0,
            ),
        ),
        exit_conditions=(
            ExitCondition(
                name="verified",
                description="Checks pass",
                evaluation_criteria="All acceptance criteria pass",
            ),
        ),
        metadata=SeedMetadata(seed_id=seed_id, ambiguity_score=0.12),
    )


class _StubInterviewDriver:
    """Interview driver stub that returns ``seed_ready`` immediately.

    Matches the duck-typed contract used by ``AutoPipeline.run`` — the
    driver is only invoked from the INTERVIEW phase and we shortcut
    through it because the focus of these tests is the RUN → RALPH_HANDOFF
    transition, not the interview machinery.
    """

    def __init__(self) -> None:
        self.invocations = 0
        self.progress_callback = None

    async def run(self, state: AutoPipelineState, ledger: Any) -> AutoInterviewResult:
        self.invocations += 1
        state.interview_session_id = "interview_stub"
        state.interview_completed = True
        return AutoInterviewResult(
            status="seed_ready",
            session_id="interview_stub",
            ledger=ledger,
            rounds=1,
        )


def _state_at_run_phase(tmp_path) -> AutoPipelineState:
    """Build an :class:`AutoPipelineState` already armed and at RUN phase.

    Bypasses interview/seed-generation/review by setting the persisted Seed
    artifact and walking the state machine forward via ``transition`` so we
    only exercise the run-handoff → ralph-handoff transition under test.
    """
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.arm_deadline()
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.interview_session_id = "interview_stub"
    state.interview_completed = True
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    seed = _build_seed()
    state.seed_id = seed.metadata.seed_id
    state.seed_artifact = seed.to_dict()
    state.last_grade = "A"
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.RUN, "run")
    return state


async def _run_starter_ok(_seed: Seed) -> dict[str, Any]:
    """Minimal run-starter stub returning a job_id like ``HandlerRunStarter``."""
    return {
        "job_id": "job_run_001",
        "session_id": "exec_session_001",
        "execution_id": "execution_001",
    }


async def _seed_generator_unused(_session_id: str) -> Seed:  # pragma: no cover
    raise AssertionError("seed generator should not run when seed_artifact is set")


class _PassReviewer(SeedReviewer):
    """SeedReviewer stub that always passes the grade gate.

    The full GradeGate has stricter requirements than the deliberately
    minimal Seed used in these tests; bypassing it isolates the
    transition-under-test from the reviewer's evaluation logic.
    """

    def __init__(self) -> None:  # noqa: D401 - intentionally trivial
        pass

    def review(self, seed: Seed, *, ledger: Any = None) -> SeedReview:  # noqa: ARG002
        grade = GradeResult(
            grade=SeedGrade.A,
            scores={},
            findings=[],
            blockers=[],
            may_run=True,
        )
        return SeedReview(grade_result=grade, findings=())


def _ralph_job_event(
    event_type: str,
    *,
    job_id: str,
    lineage_id: str,
    status: str,
    message: str | None = None,
    result_meta: dict[str, Any] | None = None,
) -> BaseEvent:
    data: dict[str, Any] = {
        "links": {"lineage_id": lineage_id},
        "status": status,
    }
    if message is not None:
        data["message"] = message
    if result_meta is not None:
        data["result_meta"] = result_meta
    return BaseEvent(
        type=event_type,
        aggregate_type="job",
        aggregate_id=job_id,
        data=data,
    )


# ---------------------------------------------------------------------------
# State machine — transitions added by #773
# ---------------------------------------------------------------------------


def test_state_machine_allows_run_to_ralph_handoff() -> None:
    """``RUN → RALPH_HANDOFF`` must be in ``_ALLOWED_TRANSITIONS`` per the issue."""
    assert AutoPhase.RALPH_HANDOFF in _ALLOWED_TRANSITIONS[AutoPhase.RUN]


def test_state_machine_allows_ralph_handoff_terminal_transitions() -> None:
    """RALPH_HANDOFF must reach COMPLETE/BLOCKED/FAILED plus the EVALUATE
    bridge added by RFC #809 Phase 2.1 and the UNSTUCK_LATERAL bridge
    added by L5-a / #1157 (oscillation_detected routes through lateral
    persona advisor first when complete_product + lateral_thinker are
    wired)."""
    assert _ALLOWED_TRANSITIONS[AutoPhase.RALPH_HANDOFF] == {
        AutoPhase.EVALUATE,
        AutoPhase.UNSTUCK_LATERAL,
        AutoPhase.COMPLETE,
        AutoPhase.BLOCKED,
        AutoPhase.FAILED,
    }
    assert AutoPhase.RALPH_HANDOFF in _ALLOWED_TRANSITIONS[AutoPhase.BLOCKED]
    assert AutoPhase.RALPH_HANDOFF in _ALLOWED_TRANSITIONS[AutoPhase.FAILED]


def test_blocked_stop_reasons_pinned() -> None:
    """The stop-reason → BLOCKED mapping is pinned by tests, not ad-hoc."""
    assert (
        frozenset(
            {
                "iteration_timeout",
                "wall_clock_exhausted",
                "oscillation_detected",
                "grade_regressing",
                "max_generations reached",
            }
        )
        == _RALPH_BLOCKED_STOP_REASONS
    )


@pytest.mark.asyncio
async def test_run_handoff_uses_contract_idempotency_field_and_kwarg(tmp_path, monkeypatch) -> None:
    state = _state_at_run_phase(tmp_path)
    received: dict[str, str] = {}

    monkeypatch.setattr(pipeline_module, "IDEMPOTENCY_KEY_FIELD", "goal")
    monkeypatch.setattr(pipeline_module, "IDEMPOTENCY_KWARG_NAME", "contract_key")

    async def run_starter(_seed: Seed, *, contract_key: str = "") -> dict[str, Any]:
        received["contract_key"] = contract_key
        return {
            "job_id": "job_run_contract",
            "session_id": "exec_session_contract",
            "execution_id": "execution_contract",
        }

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=run_starter,
        reviewer=_PassReviewer(),
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert received == {"contract_key": state.goal}


# ---------------------------------------------------------------------------
# Mapped-block stop_reason ⇒ BLOCKED with stop_reason in last_error
# ---------------------------------------------------------------------------


def test_state_persists_complete_product_field(tmp_path) -> None:
    """``complete_product`` must survive ``to_dict`` / ``from_dict`` round-trips.

    Without persistence, a session originally started with the flag would
    silently fall back to legacy RUN→COMPLETE behavior on resume.
    """
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.complete_product = True
    payload = state.to_dict()
    assert payload["complete_product"] is True
    restored = AutoPipelineState.from_dict(payload)
    assert restored.complete_product is True


def test_state_legacy_payload_defaults_complete_product_false(tmp_path) -> None:
    """Legacy state files without ``complete_product`` must load with default False.

    Pre-#773 (review-3) state files do not have the field; loading must not
    raise and must surface the default-off semantics so existing sessions
    keep their pre-promotion behavior.
    """
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    payload = state.to_dict()
    payload.pop("complete_product")
    restored = AutoPipelineState.from_dict(payload)
    assert restored.complete_product is False


def test_ralph_opencode_mode_persisted_for_resume() -> None:
    """``ralph_opencode_mode`` must round-trip so plugin resume rebuilds plugin Ralph.

    Q00/ouroboros#782 review-8 BLOCKING #1. ``state.opencode_mode`` is
    overwritten with the demoted form (``plugin``→``subprocess``) by the CLI
    entrypoint, so it cannot be the source of truth for plugin Ralph
    dispatch on resume — the un-demoted value lives on this field.
    """
    state = AutoPipelineState(goal="g", cwd="/tmp")
    assert state.ralph_opencode_mode is None  # legacy default

    state.ralph_opencode_mode = "plugin"
    payload = state.to_dict()
    assert payload["ralph_opencode_mode"] == "plugin"

    loaded = AutoPipelineState.from_dict(payload)
    assert loaded.ralph_opencode_mode == "plugin"


def test_ralph_opencode_mode_legacy_state_dict_loads_as_none() -> None:
    """Legacy state files (no ``ralph_opencode_mode`` key) must default to None."""
    state = AutoPipelineState(goal="g", cwd="/tmp")
    payload = state.to_dict()
    payload.pop("ralph_opencode_mode", None)

    loaded = AutoPipelineState.from_dict(payload)
    assert loaded.ralph_opencode_mode is None


# ---------------------------------------------------------------------------
# Resume polling — review-5 finding 1.
#
# A session interrupted in ``RALPH_HANDOFF`` (e.g. MCP client disconnects
# while the background Ralph job keeps running) must be reconciled to a
# terminal auto phase on ``--resume``, not stranded forever.
# ---------------------------------------------------------------------------


def _state_in_ralph_handoff(tmp_path) -> AutoPipelineState:
    """Build a state already persisted at ``RALPH_HANDOFF`` for resume tests."""
    state = _state_at_run_phase(tmp_path)
    state.run_start_attempted = True
    state.run_handoff_status = "started"
    state.job_id = "job_run_existing"
    state.execution_id = "execution_existing"
    state.run_session_id = "session_existing"
    state.ralph_lineage_id = "ralph-seed_test_001-auto_abc"
    state.ralph_job_id = "job_ralph_existing"
    state.ralph_dispatch_mode = "job"
    state.transition(AutoPhase.RALPH_HANDOFF, "persisted ralph checkpoint")
    return state


def _terminal_success_run_starter(job_id: str = "job_run_existing") -> Any:
    """Build a run_starter adapter whose owned-job snapshot reports terminal success.

    Mirrors the production ``HandlerRunStarter`` / ``HandlerSynchronousRunStarter``
    shape: ``adapter.handler._job_manager.get_snapshot(job_id)`` returns a
    snapshot whose ``is_terminal`` is True and ``result_meta`` advertises
    ``status="completed"`` / ``success=True``. The starter callable itself
    raises so the test proves resume does NOT redispatch the run job; the
    only signal feeding the resume gate is the persisted run handle plus
    the snapshot poll.
    """

    class _RunSnapshot:
        is_terminal = True
        status = JobStatus.COMPLETED
        result_meta = {"status": "completed", "success": True}

    class _RunJobManager:
        async def get_snapshot(self, snapshot_job_id: str) -> _RunSnapshot:
            assert snapshot_job_id == job_id
            return _RunSnapshot()

    class _RunHandler:
        _job_manager = _RunJobManager()

    class TerminalSuccessStarter:
        handler = _RunHandler()

        async def __call__(self, _seed: Seed, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("resume must not start a duplicate run")

    return TerminalSuccessStarter()


@pytest.mark.asyncio
async def test_legacy_complete_product_run_resume_blocks_missing_successor_owner(tmp_path) -> None:
    """The pre-retirement RUN crash window must not report false completion."""
    state = _state_at_run_phase(tmp_path)
    state.complete_product = True
    state.run_start_attempted = True
    state.run_handoff_status = "started"
    state.job_id = "job_run_existing"
    state.execution_id = "execution_existing"
    state.run_session_id = "session_existing"
    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_terminal_success_run_starter(),
        reviewer=_PassReviewer(),
    )

    first = await pipeline.run(state)

    assert first.status == "blocked"
    assert "legacy run job was dispatched without evaluate/ralph successors" in (
        first.blocker or ""
    )
    assert state.last_tool_name == "retired_phase_migration"

    second = await pipeline.run(state)

    assert second.status == "blocked"
    assert second.blocker == first.blocker
    assert state.last_tool_name == "retired_phase_migration"
