"""Regression tests for the RUN → RALPH_HANDOFF chain (Q00/ouroboros#773).

The chain is opt-in via ``--complete-product`` / ``complete_product=True`` and
maps the Ralph loop's terminal status onto an auto phase per the contract
pinned in this file. Default-off behavior must be byte-identical to the
pre-#773 result shape.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import UTC, datetime
import time
from typing import Any

import pytest

from ouroboros.auto import pipeline as pipeline_module
from ouroboros.auto.grading import GradeResult, SeedGrade
from ouroboros.auto.interview_driver import AutoInterviewResult
from ouroboros.auto.pipeline import (
    _RALPH_BLOCKED_STOP_REASONS,
    PIPELINE_DEADLINE_TOOL_NAME,
    AutoPipeline,
    AutoPipelineResult,
    _recoverable_phase_for_tool,
)
from ouroboros.auto.seed_reviewer import SeedReview, SeedReviewer
from ouroboros.auto.state import (
    _ALLOWED_TRANSITIONS,
    AutoPhase,
    AutoPipelineState,
    AutoResumeCapability,
    AutoStore,
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


def test_ralph_starter_blocker_is_recoverable_to_ralph_handoff(tmp_path) -> None:
    state = _state_at_run_phase(tmp_path)
    state.ralph_lineage_id = "ralph-seed_test_001-auto_abc"
    state.mark_blocked("iteration_timeout", tool_name="ralph_starter")

    assert _recoverable_phase_for_tool("ralph_starter") is AutoPhase.RALPH_HANDOFF
    assert state.resume_capability() is AutoResumeCapability.RESUME


@pytest.mark.asyncio
async def test_resume_ralph_blocker_retries_fresh_handoff_without_terminal_reattach(
    tmp_path,
) -> None:
    state = _state_at_run_phase(tmp_path)
    state.complete_product = True
    state.ralph_job_id = "job_ralph_terminal_old"
    state.ralph_lineage_id = "ralph-seed_test_001-auto_old"
    state.ralph_dispatch_mode = "job"
    state.ralph_job_status = "failed"
    state.ralph_stop_reason = "iteration_timeout"
    state.ralph_current_generation = 7
    state.ralph_last_event_at = datetime.now(UTC).isoformat()
    state.mark_blocked("iteration_timeout", tool_name="ralph_starter")

    captured: dict[str, Any] = {}

    async def ralph_starter(_seed: Seed, **kwargs: Any) -> dict[str, Any]:
        captured["kwargs"] = kwargs
        return {
            "job_id": "job_ralph_retry_new",
            "lineage_id": kwargs["lineage_id"],
            "dispatch_mode": "job",
            "terminal_status": "completed",
            "stop_reason": "qa passed",
        }

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=ralph_starter,
        complete_product=True,
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert state.phase is AutoPhase.COMPLETE
    assert state.ralph_job_id == "job_ralph_retry_new"
    assert state.ralph_lineage_id != "ralph-seed_test_001-auto_old"
    assert "-retry-" in state.ralph_lineage_id
    assert state.ralph_job_status is None
    assert state.ralph_stop_reason is None
    assert state.ralph_current_generation is None
    assert state.ralph_last_event_at is None
    assert captured["kwargs"]["reattach_terminal"] is False
    assert captured["kwargs"]["reuse_existing"] is False


@pytest.mark.asyncio
async def test_resume_ralph_lineage_gap_reattaches_via_starter(tmp_path) -> None:
    state = _state_at_run_phase(tmp_path)
    state.complete_product = True
    state.transition(AutoPhase.RALPH_HANDOFF, "checkpointed lineage before job id")
    state.ralph_lineage_id = "ralph-seed_test_001-gap"
    state.ralph_job_id = None
    state.ralph_dispatch_mode = "job"

    captured: dict[str, Any] = {}

    async def ralph_starter(_seed: Seed, **kwargs: Any) -> dict[str, Any]:
        captured["kwargs"] = kwargs
        return {
            "job_id": "job_ralph_gap_existing",
            "lineage_id": kwargs["lineage_id"],
            "dispatch_mode": "job",
            "terminal_status": "completed",
            "stop_reason": "qa passed",
        }

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=ralph_starter,
        complete_product=True,
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert state.phase is AutoPhase.COMPLETE
    assert state.ralph_job_id == "job_ralph_gap_existing"
    assert captured["kwargs"]["lineage_id"] == "ralph-seed_test_001-gap"
    assert captured["kwargs"]["reattach_terminal"] is True


# ---------------------------------------------------------------------------
# Happy path — ralph completes ⇒ auto state COMPLETE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ralph_qa_passed_completes_auto(tmp_path) -> None:
    state = _state_at_run_phase(tmp_path)

    captured: dict[str, Any] = {}

    async def ralph_starter(seed: Seed, **kwargs: Any) -> dict[str, Any]:
        captured["seed"] = seed
        captured["kwargs"] = kwargs
        return {
            "job_id": "job_ralph_001",
            "lineage_id": kwargs["lineage_id"],
            "dispatch_mode": "job",
            "terminal_status": "completed",
            "stop_reason": "qa passed",
            "current_generation": 4,
        }

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=ralph_starter,
        complete_product=True,
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert state.phase is AutoPhase.COMPLETE
    assert state.ralph_job_id == "job_ralph_001"
    assert state.ralph_dispatch_mode == "job"
    assert state.ralph_job_status == "completed"
    assert state.ralph_stop_reason == "qa passed"
    assert state.ralph_current_generation == 4
    assert result.ralph_job_id == "job_ralph_001"
    assert result.ralph_dispatch_mode == "job"
    # ``lineage_id`` is deterministic per the issue contract
    # ``f"ralph-{seed.metadata.seed_id}-{auto_session_id[:8]}"``; the auto
    # session id always starts with the literal ``"auto_"`` prefix so the
    # 8-character slice begins after the underscore.
    assert state.ralph_lineage_id is not None
    assert state.ralph_lineage_id.startswith(f"ralph-{_build_seed().metadata.seed_id}-")
    assert captured["kwargs"]["lineage_id"] == state.ralph_lineage_id


@pytest.mark.asyncio
async def test_ralph_job_id_persisted_while_starter_waits_for_terminal(tmp_path) -> None:
    state = _state_at_run_phase(tmp_path)
    store = AutoStore(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()

    async def ralph_starter(_seed: Seed, **kwargs: Any) -> dict[str, Any]:
        kwargs["on_dispatched"](
            {
                "job_id": "job_ralph_waiting",
                "lineage_id": kwargs["lineage_id"],
                "dispatch_mode": "job",
            }
        )
        started.set()
        await release.wait()
        return {
            "job_id": "job_ralph_waiting",
            "lineage_id": kwargs["lineage_id"],
            "dispatch_mode": "job",
            "terminal_status": "completed",
            "stop_reason": "qa passed",
        }

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        store=store,
        reviewer=_PassReviewer(),
        ralph_starter=ralph_starter,
        complete_product=True,
    )

    task = asyncio.create_task(pipeline.run(state))
    await asyncio.wait_for(started.wait(), timeout=1.0)

    persisted = store.load(state.auto_session_id)
    assert persisted.phase is AutoPhase.RALPH_HANDOFF
    assert persisted.ralph_job_id == "job_ralph_waiting"
    assert persisted.ralph_dispatch_mode == "job"

    release.set()
    result = await task
    assert result.status == "complete"


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


@pytest.mark.asyncio
async def test_ralph_iteration_timeout_blocks_auto(tmp_path) -> None:
    state = _state_at_run_phase(tmp_path)

    async def ralph_starter(_seed: Seed, **_kwargs: Any) -> dict[str, Any]:
        return {
            "job_id": "job_ralph_002",
            "lineage_id": "ralph-x",
            "dispatch_mode": "job",
            "terminal_status": "failed",
            "stop_reason": "iteration_timeout",
        }

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=ralph_starter,
        complete_product=True,
    )

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert state.phase is AutoPhase.BLOCKED
    assert state.last_error == "iteration_timeout"
    assert "iteration_timeout" in (result.blocker or "")
    assert state.ralph_job_id == "job_ralph_002"


# ---------------------------------------------------------------------------
# Unmapped failure ⇒ FAILED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ralph_terminal_failure_fails_auto(tmp_path) -> None:
    state = _state_at_run_phase(tmp_path)

    async def ralph_starter(_seed: Seed, **_kwargs: Any) -> dict[str, Any]:
        return {
            "job_id": "job_ralph_003",
            "lineage_id": "ralph-x",
            "dispatch_mode": "job",
            "terminal_status": "failed",
            "stop_reason": "interrupted",
        }

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=ralph_starter,
        complete_product=True,
    )

    result = await pipeline.run(state)

    assert result.status == "failed"
    assert state.phase is AutoPhase.FAILED
    assert "interrupted" in (state.last_error or "")


# ---------------------------------------------------------------------------
# Plugin delegation ⇒ COMPLETE + dispatch_mode=plugin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ralph_plugin_delegation_completes_auto(tmp_path) -> None:
    state = _state_at_run_phase(tmp_path)

    async def ralph_starter(_seed: Seed, **_kwargs: Any) -> dict[str, Any]:
        return {
            "job_id": None,
            "lineage_id": "ralph-x",
            "dispatch_mode": "plugin",
            "terminal_status": "delegated_to_plugin",
            "stop_reason": None,
        }

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=ralph_starter,
        complete_product=True,
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert state.phase is AutoPhase.COMPLETE
    assert state.ralph_dispatch_mode == "plugin"
    assert state.ralph_job_id is None
    assert result.ralph_dispatch_mode == "plugin"
    # Plugin guidance must surface for the operator.
    assert state.run_handoff_guidance is not None
    assert "OpenCode" in state.run_handoff_guidance


# ---------------------------------------------------------------------------
# Resume safety — persisted RALPH_HANDOFF must not duplicate run/Ralph work.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ralph_handoff_resume_does_not_dispatch_duplicate_work(tmp_path) -> None:
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

    async def run_starter(_seed: Seed) -> dict[str, Any]:  # pragma: no cover
        raise AssertionError("resume must not start a duplicate run")

    async def ralph_starter(_seed: Seed, **_kwargs: Any) -> dict[str, Any]:  # pragma: no cover
        raise AssertionError("resume must not start a duplicate Ralph handoff")

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=run_starter,
        reviewer=_PassReviewer(),
        ralph_starter=ralph_starter,
        complete_product=True,
    )

    result = await pipeline.run(state)

    assert result.status == "ralph_handoff"
    assert result.resume_capability.value == "resume"
    assert state.phase is AutoPhase.RALPH_HANDOFF
    assert state.job_id == "job_run_existing"
    assert state.ralph_job_id == "job_ralph_existing"
    assert state.last_tool_name == "ralph_starter"
    assert state.last_error is None
    assert state.run_handoff_guidance is not None
    assert "did not start duplicate run or Ralph work" in state.run_handoff_guidance


# ---------------------------------------------------------------------------
# Pipeline deadline budget — insufficient Ralph budget is pipeline_timeout.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_product_blocks_when_initial_run_reports_failure(tmp_path) -> None:
    """Do not launch Ralph when the synchronous run handoff already failed."""
    state = _state_at_run_phase(tmp_path)

    async def failed_run_starter(_seed: Seed) -> dict[str, Any]:
        return {
            "job_id": "job_run_failed",
            "session_id": "exec_session_failed",
            "execution_id": "execution_failed",
            "status": "failed",
            "success": False,
        }

    async def ralph_starter(*_args: Any, **_kwargs: Any) -> dict[str, Any]:  # pragma: no cover
        raise AssertionError("ralph_starter must not run after failed execution")

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=failed_run_starter,
        reviewer=_PassReviewer(),
        ralph_starter=ralph_starter,
        complete_product=True,
    )

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert state.phase is AutoPhase.BLOCKED
    assert state.last_tool_name == "run_starter"
    assert "finished unsuccessfully" in (state.last_error or "")
    assert state.ralph_job_id is None


@pytest.mark.asyncio
async def test_complete_product_blocks_when_synchronous_run_is_paused(tmp_path) -> None:
    """A paused synchronous execution is non-terminal — do not hand off to Ralph.

    ``ExecuteSeedHandler`` maps :class:`SessionStatus.PAUSED` to
    ``status="paused"`` / ``success=None`` — the run is waiting on
    operator input, not finished. The previous guard only blocked the
    ``failed/cancelled/interrupted`` and ``queued/running/cancel_requested``
    sets, so ``paused`` fell through and started Ralph against a
    still-pending product run. Block the handoff and leave the owned job
    alone (operator may still resume it).
    """

    state = _state_at_run_phase(tmp_path)

    async def paused_run_starter(_seed: Seed) -> dict[str, Any]:
        return {
            "job_id": "job_run_paused",
            "session_id": "exec_session_paused",
            "execution_id": "execution_paused",
            "status": "paused",
            "success": None,
        }

    async def ralph_starter(*_args: Any, **_kwargs: Any) -> dict[str, Any]:  # pragma: no cover
        raise AssertionError("ralph_starter must not run while execution is paused")

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=paused_run_starter,
        reviewer=_PassReviewer(),
        ralph_starter=ralph_starter,
        complete_product=True,
    )

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert state.phase is AutoPhase.BLOCKED
    assert state.last_tool_name == "run_starter"
    assert "paused" in (state.last_error or "")
    assert state.ralph_job_id is None


@pytest.mark.asyncio
async def test_complete_product_synchronous_run_uses_pipeline_deadline_not_handoff_timeout(
    tmp_path,
) -> None:
    """Inline complete-product execution is terminal RUN work, not quick enqueue work."""
    state = _state_at_run_phase(tmp_path)
    state.pipeline_timeout_seconds = 120.0
    state.arm_deadline()

    class SlowSynchronousRunStarter:
        synchronous_execution = True

        async def __call__(
            self, _seed: Seed, *, idempotency_key: str = ""  # noqa: ARG002
        ) -> dict[str, Any]:
            await asyncio.sleep(0.02)
            return {
                "job_id": None,
                "session_id": "sync_session",
                "execution_id": "sync_exec",
                "status": "completed",
                "success": True,
            }

    async def ralph_starter(*_args: Any, **_kwargs: Any) -> dict[str, Any]:  # pragma: no cover
        raise AssertionError("successful synchronous run must not dispatch Ralph")

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=SlowSynchronousRunStarter(),
        reviewer=_PassReviewer(),
        ralph_starter=ralph_starter,
        complete_product=True,
        run_start_timeout_seconds=0.001,
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert result.run_handoff_status == "completed"
    assert state.phase is AutoPhase.COMPLETE
    assert state.execution_id == "sync_exec"
    assert state.run_handoff_status == "completed"
    assert state.ralph_job_id is None
    assert state.last_tool_name != "run_starter"


def test_complete_product_synchronous_run_timeout_has_completion_grace(tmp_path) -> None:
    """Synchronous run teardown gets a small grace beyond the strict deadline."""
    state = _state_at_run_phase(tmp_path)
    state.deadline_at = time.monotonic() + 5.0

    class SynchronousRunStarter:
        synchronous_execution = True

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=SynchronousRunStarter(),
        reviewer=_PassReviewer(),
        complete_product=True,
        run_start_timeout_seconds=0.001,
    )

    assert pipeline._run_start_timeout(state) > 5.0


@pytest.mark.asyncio
async def test_complete_product_synchronous_success_after_deadline_blocks_as_timeout(
    tmp_path,
) -> None:
    """Sync completion grace must not turn into extra execution budget.

    ``_run_start_timeout`` adds ``_SYNCHRONOUS_RUN_COMPLETION_GRACE_SECONDS``
    on top of the remaining pipeline deadline so the inline ``await`` for a
    synchronous starter can finish handler teardown without
    ``asyncio.wait_for`` tripping. The grace is for teardown only — a
    synchronous run that returns ``success=True`` AFTER ``deadline_at`` has
    already passed has still exceeded the public ``pipeline_timeout``
    contract. The pre-COMPLETE deadline gate must catch that case and
    surface ``pipeline_timeout`` BLOCKED instead of recording the
    over-budget run as complete.
    """

    state = _state_at_run_phase(tmp_path)

    class OverBudgetSyncStarter:
        synchronous_execution = True

        async def __call__(
            self, _seed: Seed, *, idempotency_key: str = ""  # noqa: ARG002
        ) -> dict[str, Any]:
            # Backdate the deadline as if the inline await had run past it
            # within the synchronous completion grace; returning success here
            # would otherwise mark COMPLETE without re-checking the deadline.
            state.deadline_at = time.monotonic() - 1.0
            state.deadline_at_epoch = time.time() - 1.0
            return {
                "job_id": None,
                "session_id": "sync_over_budget",
                "execution_id": "sync_over_budget_exec",
                "status": "completed",
                "success": True,
            }

    async def ralph_starter(*_args: Any, **_kwargs: Any) -> dict[str, Any]:  # pragma: no cover
        raise AssertionError("ralph_starter must not run when sync execution is over deadline")

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=OverBudgetSyncStarter(),
        reviewer=_PassReviewer(),
        ralph_starter=ralph_starter,
        complete_product=True,
    )

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert state.phase is AutoPhase.BLOCKED
    assert state.is_deadline_expired()
    assert state.last_tool_name == PIPELINE_DEADLINE_TOOL_NAME
    assert "pipeline_timeout" in (state.last_error or "")
    assert state.ralph_job_id is None


@pytest.mark.asyncio
async def test_first_party_synchronous_success_within_grace_completes(tmp_path) -> None:
    """First-party inline execution may return terminal metadata during teardown grace."""
    state = _state_at_run_phase(tmp_path)

    class FirstPartySyncStarter:
        synchronous_execution = True

        async def __call__(
            self, _seed: Seed, *, idempotency_key: str = ""  # noqa: ARG002
        ) -> dict[str, Any]:
            state.deadline_at = time.monotonic() - 1.0
            state.deadline_at_epoch = time.time() - 1.0
            return {
                "job_id": None,
                "session_id": "sync_grace",
                "execution_id": "sync_grace_exec",
                "status": "completed",
                "success": True,
                "_allow_deadline_completion_grace": True,
            }

    async def ralph_starter(*_args: Any, **_kwargs: Any) -> dict[str, Any]:  # pragma: no cover
        raise AssertionError("successful first-party sync execution must not dispatch Ralph")

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=FirstPartySyncStarter(),
        reviewer=_PassReviewer(),
        ralph_starter=ralph_starter,
        complete_product=True,
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert result.run_handoff_status == "completed"
    assert state.phase is AutoPhase.COMPLETE
    assert state.last_tool_name != PIPELINE_DEADLINE_TOOL_NAME
    assert state.ralph_job_id is None


@pytest.mark.asyncio
async def test_first_party_synchronous_timeout_recovers_completed_session(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If inline handler teardown times out, recover terminal session metadata."""
    monkeypatch.setattr(pipeline_module, "_SYNCHRONOUS_RUN_COMPLETION_GRACE_SECONDS", 0.05)
    state = _state_at_run_phase(tmp_path)
    state.deadline_at = time.monotonic() + 0.2
    state.deadline_at_epoch = time.time() + 0.2

    class TimeoutAfterCompletionStarter:
        synchronous_execution = True

        async def __call__(
            self, _seed: Seed, *, idempotency_key: str = ""  # noqa: ARG002
        ) -> dict[str, Any]:
            await asyncio.sleep(1.0)
            raise AssertionError("wait_for should time out first")

        async def recover_timed_out_run(self) -> dict[str, Any]:
            state.deadline_at = time.monotonic() - 0.01
            state.deadline_at_epoch = time.time() - 0.01
            return {
                "job_id": None,
                "session_id": "sync_recovered",
                "execution_id": "sync_recovered_exec",
                "status": "completed",
                "success": True,
                "_allow_deadline_completion_grace": True,
            }

    async def ralph_starter(*_args: Any, **_kwargs: Any) -> dict[str, Any]:  # pragma: no cover
        raise AssertionError("successful recovered sync execution must not dispatch Ralph")

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=TimeoutAfterCompletionStarter(),
        reviewer=_PassReviewer(),
        ralph_starter=ralph_starter,
        complete_product=True,
        run_start_timeout_seconds=0.001,
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert result.run_handoff_status == "completed"
    assert state.phase is AutoPhase.COMPLETE
    assert state.execution_id == "sync_recovered_exec"
    assert state.run_session_id == "sync_recovered"
    assert state.last_tool_name != PIPELINE_DEADLINE_TOOL_NAME
    assert state.ralph_job_id is None


@pytest.mark.asyncio
async def test_complete_product_waits_for_owned_run_job_before_ralph(tmp_path) -> None:
    """A queued execute_seed job must reach terminal success before Ralph starts."""
    state = _state_at_run_phase(tmp_path)

    class _RunSnapshot:
        is_terminal = True
        status = JobStatus.FAILED
        result_meta = {"status": "failed", "success": False}

    class _RunJobManager:
        async def get_snapshot(self, job_id: str) -> _RunSnapshot:
            assert job_id == "job_run_background"
            return _RunSnapshot()

    class _RunHandler:
        _job_manager = _RunJobManager()

    class BackgroundRunStarter:
        handler = _RunHandler()

        async def __call__(self, _seed: Seed) -> dict[str, Any]:
            return {
                "job_id": "job_run_background",
                "session_id": "exec_session_background",
                "execution_id": "execution_background",
                "status": "queued",
            }

    async def ralph_starter(*_args: Any, **_kwargs: Any) -> dict[str, Any]:  # pragma: no cover
        raise AssertionError("ralph_starter must not run before failed run is surfaced")

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=BackgroundRunStarter(),
        reviewer=_PassReviewer(),
        ralph_starter=ralph_starter,
        complete_product=True,
    )

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert state.last_tool_name == "run_starter"
    assert state.ralph_job_id is None


@pytest.mark.asyncio
async def test_complete_product_blocks_when_owned_run_job_is_still_running(tmp_path) -> None:
    """Do not start Ralph against an unfinished execute_seed job."""
    state = _state_at_run_phase(tmp_path)
    cancelled_jobs: list[str] = []

    class _RunJobManager:
        async def cancel_job(self, job_id: str) -> None:
            cancelled_jobs.append(job_id)

    class _RunHandler:
        _job_manager = _RunJobManager()

    class BackgroundRunStarter:
        handler = _RunHandler()

        async def __call__(self, _seed: Seed) -> dict[str, Any]:
            return {
                "job_id": "job_run_background",
                "session_id": "exec_session_background",
                "execution_id": "execution_background",
                "status": "queued",
            }

    async def ralph_starter(*_args: Any, **_kwargs: Any) -> dict[str, Any]:  # pragma: no cover
        raise AssertionError("ralph_starter must not run while execution is still running")

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=BackgroundRunStarter(),
        reviewer=_PassReviewer(),
        ralph_starter=ralph_starter,
        complete_product=True,
    )

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert state.last_tool_name == "run_starter"
    assert "did not finish" in (state.last_error or "")
    assert state.ralph_job_id is None
    assert cancelled_jobs == ["job_run_background"]


@pytest.mark.asyncio
async def test_insufficient_ralph_deadline_budget_blocks_as_pipeline_timeout(tmp_path) -> None:
    state = _state_at_run_phase(tmp_path)
    state.deadline_at = time.monotonic() + 0.25
    state.deadline_at_epoch = time.time() + 0.25

    async def ralph_starter(_seed: Seed, **_kwargs: Any) -> dict[str, Any]:  # pragma: no cover
        raise AssertionError("insufficient pipeline budget must not call ralph_starter")

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=ralph_starter,
        complete_product=True,
    )

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert state.phase is AutoPhase.BLOCKED
    assert state.last_tool_name == PIPELINE_DEADLINE_TOOL_NAME
    assert state.last_error is not None
    assert "pipeline_timeout" in state.last_error
    assert "below Ralph minimum" in state.last_error
    assert state.ralph_job_id is None
    assert state.ralph_dispatch_mode is None


# ---------------------------------------------------------------------------
# Flag-off regression: complete_product=False is identical to legacy behavior.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_product_off_matches_legacy_shape(tmp_path) -> None:
    """``complete_product=False`` must transition straight to COMPLETE without ralph_*."""
    state = _state_at_run_phase(tmp_path)

    async def ralph_starter(*_args: Any, **_kwargs: Any) -> dict[str, Any]:  # pragma: no cover
        raise AssertionError("ralph_starter must not run when complete_product is False")

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=ralph_starter,
        complete_product=False,
    )

    result = await pipeline.run(state)

    assert isinstance(result, AutoPipelineResult)
    assert result.status == "complete"
    assert state.phase is AutoPhase.COMPLETE
    # All ralph_* state fields must remain at their default ``None`` so the
    # persisted JSON shape and result shape stay byte-identical to pre-#773
    # for default-off callers.
    assert state.ralph_job_id is None
    assert state.ralph_lineage_id is None
    assert state.ralph_dispatch_mode is None
    payload = asdict(result)
    assert payload["ralph_job_id"] is None
    assert payload["ralph_lineage_id"] is None
    assert payload["ralph_dispatch_mode"] is None


# ---------------------------------------------------------------------------
# Pipeline deadline contract — per-iteration cap (review-3 finding 1).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ralph_handoff_caps_per_iteration_at_remaining_budget(tmp_path) -> None:
    """A short remaining budget caps ``per_iteration_timeout_seconds``.

    ``RalphLoopRunner`` checks ``max_total_seconds`` only at the top of each
    iteration. Without a per-iteration cap, the first iteration could still
    block for the full 1800s default after the deadline expired. Pinning the
    forwarded value here ensures the deadline contract is honored even on
    ralph's first generation.
    """
    state = _state_at_run_phase(tmp_path)
    # 60s remaining is well above the 1s minimum and 30s per-iteration floor,
    # but well below the 1800s default — the cap must equal the remaining.
    remaining = 60.0
    state.deadline_at = time.monotonic() + remaining
    state.deadline_at_epoch = time.time() + remaining

    captured: dict[str, Any] = {}

    async def ralph_starter(_seed: Seed, **kwargs: Any) -> dict[str, Any]:
        captured["kwargs"] = kwargs
        return {
            "job_id": "job_ralph_capped",
            "lineage_id": kwargs["lineage_id"],
            "dispatch_mode": "job",
            "terminal_status": "completed",
            "stop_reason": "qa passed",
        }

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=ralph_starter,
        complete_product=True,
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    forwarded = captured["kwargs"]
    assert forwarded["max_total_seconds"] is not None
    assert forwarded["max_total_seconds"] <= remaining
    # The per-iteration cap must be at most the remaining budget so a single
    # ``evolve_step`` cannot block past ``deadline_at``.
    assert forwarded["per_iteration_timeout_seconds"] is not None
    assert forwarded["per_iteration_timeout_seconds"] <= remaining
    # Floor at the Ralph handler's per-iteration minimum (30s).
    assert forwarded["per_iteration_timeout_seconds"] >= 30.0


@pytest.mark.asyncio
async def test_ralph_handoff_uses_default_per_iteration_with_ample_budget(tmp_path) -> None:
    """When remaining budget exceeds the Ralph default, no tighter cap is forwarded.

    Pinning the upper bound prevents accidental over-tightening for the
    common case (2h default deadline, RUN reached early with hours of
    budget left): the bot's contract is to honor the deadline, not to
    aggressively shrink the per-iteration budget below the established
    1800s default.
    """
    state = _state_at_run_phase(tmp_path)
    # Two hours remaining — well above the 1800s default.
    state.deadline_at = time.monotonic() + 7200.0
    state.deadline_at_epoch = time.time() + 7200.0

    captured: dict[str, Any] = {}

    async def ralph_starter(_seed: Seed, **kwargs: Any) -> dict[str, Any]:
        captured["kwargs"] = kwargs
        return {
            "job_id": "job_ralph_default",
            "lineage_id": kwargs["lineage_id"],
            "dispatch_mode": "job",
            "terminal_status": "completed",
            "stop_reason": "qa passed",
        }

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=ralph_starter,
        complete_product=True,
    )

    await pipeline.run(state)

    forwarded = captured["kwargs"]
    # Remaining is much greater than 1800s, so the cap saturates at the
    # Ralph default — never above it.
    assert forwarded["per_iteration_timeout_seconds"] == 1800.0


# ---------------------------------------------------------------------------
# Persisted complete_product intent (review-3 finding 2).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_promotes_complete_product_from_persisted_state(tmp_path) -> None:
    """A session originally started with ``complete_product=True`` keeps the
    RUN → RALPH_HANDOFF chain on resume even if the caller forgot to re-pass
    the flag at construction time.
    """
    state = _state_at_run_phase(tmp_path)
    state.complete_product = True

    captured: dict[str, Any] = {}

    async def ralph_starter(_seed: Seed, **kwargs: Any) -> dict[str, Any]:
        captured["kwargs"] = kwargs
        return {
            "job_id": "job_ralph_promoted",
            "lineage_id": kwargs["lineage_id"],
            "dispatch_mode": "job",
            "terminal_status": "completed",
            "stop_reason": "qa passed",
        }

    # Construct the pipeline WITHOUT complete_product=True — only the
    # persisted state carries the intent. The pipeline must still reach the
    # ralph handoff because the persisted intent dominates an absent flag.
    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=ralph_starter,
        complete_product=False,
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert state.phase is AutoPhase.COMPLETE
    assert state.ralph_job_id == "job_ralph_promoted"
    assert "kwargs" in captured  # ralph_starter actually invoked
    # The pipeline's effective complete_product reflects the persisted truth.
    assert pipeline.complete_product is True


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


@pytest.mark.asyncio
async def test_ralph_handoff_resume_polls_persisted_job_to_complete(tmp_path) -> None:
    """Resume polls the persisted ``ralph_job_id`` and transitions to COMPLETE
    when the loop has terminated successfully — closing the bot's review-5
    finding 1 (stranded RALPH_HANDOFF on long-lived runtimes)."""
    state = _state_in_ralph_handoff(tmp_path)

    polled_job: dict[str, Any] = {}

    async def ralph_resumer(*, job_id: str) -> dict[str, Any]:
        polled_job["job_id"] = job_id
        return {
            "job_id": job_id,
            "lineage_id": state.ralph_lineage_id,
            "dispatch_mode": "job",
            "terminal_status": "completed",
            "stop_reason": "qa passed",
        }

    async def ralph_starter(*_args: Any, **_kwargs: Any) -> dict[str, Any]:  # pragma: no cover
        raise AssertionError("resume must not start a duplicate Ralph handoff")

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=ralph_starter,
        ralph_resumer=ralph_resumer,
        complete_product=True,
    )

    result = await pipeline.run(state)

    assert polled_job["job_id"] == "job_ralph_existing"
    assert result.status == "complete"
    assert state.phase is AutoPhase.COMPLETE


@pytest.mark.asyncio
async def test_ralph_handoff_resume_prefers_generations_over_iterations(tmp_path) -> None:
    """Resumed poller metadata must preserve lineage generation over iteration count."""
    state = _state_in_ralph_handoff(tmp_path)

    async def ralph_resumer(*, job_id: str) -> dict[str, Any]:
        return {
            "job_id": job_id,
            "lineage_id": state.ralph_lineage_id,
            "dispatch_mode": "job",
            "terminal_status": "completed",
            "stop_reason": "qa passed",
            "iterations": 2,
            "generations": [9, 10],
        }

    async def ralph_starter(*_args: Any, **_kwargs: Any) -> dict[str, Any]:  # pragma: no cover
        raise AssertionError("resume must not start a duplicate Ralph handoff")

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=ralph_starter,
        ralph_resumer=ralph_resumer,
        complete_product=True,
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert state.phase is AutoPhase.COMPLETE
    assert state.ralph_current_generation == 10


class _ResumeMirrorEventStore:
    """Deterministic job-event source for resume mirror lifecycle tests."""

    def __init__(self, state: AutoPipelineState) -> None:
        self.state = state
        self.terminal_ready = asyncio.Event()
        self.started = asyncio.Event()
        self.block_first_fetch = False
        self.cancelled = False

    async def get_events_after(
        self,
        aggregate_type: str,
        aggregate_id: str,
        *,
        last_row_id: int = 0,
    ) -> tuple[list[BaseEvent], int]:
        assert aggregate_type == "job"
        assert aggregate_id == self.state.ralph_job_id
        self.started.set()
        try:
            if self.block_first_fetch:
                await self.terminal_ready.wait()
            if last_row_id == 0:
                return [
                    _ralph_job_event(
                        "mcp.job.updated",
                        job_id=aggregate_id,
                        lineage_id=self.state.ralph_lineage_id or "",
                        status="running",
                        message="Generation 3 | review",
                    )
                ], 1
            if self.terminal_ready.is_set() and last_row_id == 1:
                return [
                    _ralph_job_event(
                        "mcp.job.completed",
                        job_id=aggregate_id,
                        lineage_id=self.state.ralph_lineage_id or "",
                        status="completed",
                        result_meta={
                            "status": "completed",
                            "stop_reason": "qa passed",
                            "current_generation": 4,
                        },
                    )
                ], 2
            await asyncio.sleep(0.01)
            return [], last_row_id
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class _ResumePoller:
    def __init__(self, event_store: _ResumeMirrorEventStore, auto_store: AutoStore) -> None:
        self.job_event_store = event_store
        self._event_store = event_store
        self._auto_store = auto_store

    async def __call__(self, *, job_id: str) -> dict[str, Any]:
        await asyncio.wait_for(self._event_store.started.wait(), timeout=1.0)
        deadline = asyncio.get_running_loop().time() + 1.0
        while asyncio.get_running_loop().time() < deadline:
            saved = self._auto_store.load(self._event_store.state.auto_session_id)
            if saved.ralph_job_status == "running" and saved.ralph_current_generation == 3:
                break
            await asyncio.sleep(0.01)
        else:  # pragma: no cover - assertion branch
            raise AssertionError("resume mirror did not persist running generation")
        self._event_store.terminal_ready.set()
        return {
            "job_id": job_id,
            "lineage_id": self._event_store.state.ralph_lineage_id,
            "dispatch_mode": "job",
            "terminal_status": "completed",
            "stop_reason": "qa passed",
            "current_generation": 4,
        }


@pytest.mark.asyncio
async def test_ralph_handoff_resume_poller_mirrors_live_status_until_terminal(
    tmp_path,
) -> None:
    """Resume polling starts the same Ralph status mirror used by fresh
    handoff/re-attach, so running generation progress is persisted before
    the terminal snapshot completes."""
    state = _state_in_ralph_handoff(tmp_path)
    auto_store = AutoStore(tmp_path)
    auto_store.save(state)
    event_store = _ResumeMirrorEventStore(state)
    ralph_resumer = _ResumePoller(event_store, auto_store)

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        store=auto_store,
        reviewer=_PassReviewer(),
        ralph_resumer=ralph_resumer,
        complete_product=True,
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert state.phase is AutoPhase.COMPLETE
    assert state.ralph_job_status == "completed"
    assert state.ralph_stop_reason == "qa passed"
    assert state.ralph_current_generation == 4
    saved = auto_store.load(state.auto_session_id)
    assert saved.ralph_job_status == "completed"
    assert saved.ralph_current_generation == 4


@pytest.mark.asyncio
async def test_ralph_handoff_resume_poller_cancels_status_mirror_on_poll_error(
    tmp_path,
) -> None:
    """If the resume poll fails, the background mirror task is cancelled
    instead of being left running against the auto state."""
    state = _state_in_ralph_handoff(tmp_path)
    auto_store = AutoStore(tmp_path)
    auto_store.save(state)
    event_store = _ResumeMirrorEventStore(state)
    event_store.block_first_fetch = True

    class FailingPoller:
        job_event_store = event_store

        async def __call__(self, *, job_id: str) -> dict[str, Any]:  # noqa: ARG002
            await asyncio.wait_for(event_store.started.wait(), timeout=1.0)
            raise RuntimeError("snapshot unavailable")

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        store=auto_store,
        reviewer=_PassReviewer(),
        ralph_resumer=FailingPoller(),
        complete_product=True,
    )

    result = await pipeline.run(state)

    assert result.status == "failed"
    assert state.phase is AutoPhase.FAILED
    assert state.last_error == "ralph resume poll failed: snapshot unavailable"
    assert event_store.cancelled is True


@pytest.mark.asyncio
async def test_fresh_ralph_handoff_timeout_cancels_status_mirror(tmp_path) -> None:
    """A deadline timeout during fresh Ralph handoff must not leave the mirror running."""
    state = _state_at_run_phase(tmp_path)
    state.deadline_at = time.monotonic() + 1.05
    state.deadline_at_epoch = time.time() + 1.05
    auto_store = AutoStore(tmp_path)
    event_store = _ResumeMirrorEventStore(state)
    event_store.block_first_fetch = True
    cancelled_jobs: list[str] = []

    class _JobManager:
        async def cancel_job(self, job_id: str) -> None:
            cancelled_jobs.append(job_id)

    class _Handler:
        _job_manager = _JobManager()

    class SlowStarter:
        job_event_store = event_store
        handler = _Handler()

        async def __call__(
            self,
            seed: Seed,  # noqa: ARG002
            *,
            lineage_id: str,
            on_dispatched,
            **_kwargs: Any,
        ) -> dict[str, Any]:
            on_dispatched(
                {
                    "job_id": "job_fresh_timeout",
                    "lineage_id": lineage_id,
                    "dispatch_mode": "job",
                }
            )
            await asyncio.wait_for(event_store.started.wait(), timeout=1.0)
            await asyncio.sleep(2.0)
            return {
                "job_id": "job_fresh_timeout",
                "lineage_id": lineage_id,
                "dispatch_mode": "job",
                "terminal_status": "completed",
                "stop_reason": "qa passed",
            }

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        store=auto_store,
        reviewer=_PassReviewer(),
        ralph_starter=SlowStarter(),
        complete_product=True,
    )

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert state.phase is AutoPhase.BLOCKED
    assert state.last_tool_name == PIPELINE_DEADLINE_TOOL_NAME
    assert "pipeline_timeout" in (state.last_error or "")
    assert state.ralph_job_id == "job_fresh_timeout"
    assert event_store.cancelled is True
    assert cancelled_jobs == ["job_fresh_timeout"]


@pytest.mark.asyncio
async def test_ralph_handoff_resume_polls_persisted_job_blocks_on_timeout(tmp_path) -> None:
    """Resume maps an ``iteration_timeout`` terminal status onto ``BLOCKED``
    so the same recovery contract used by fresh dispatch (#773) applies on
    the resume path too."""
    state = _state_in_ralph_handoff(tmp_path)

    async def ralph_resumer(*, job_id: str) -> dict[str, Any]:  # noqa: ARG001
        return {
            "job_id": "job_ralph_existing",
            "lineage_id": state.ralph_lineage_id,
            "dispatch_mode": "job",
            "terminal_status": "failed",
            "stop_reason": "iteration_timeout",
        }

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_resumer=ralph_resumer,
        complete_product=True,
    )

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert state.last_error == "iteration_timeout"
    assert state.last_tool_name == "ralph_starter"


@pytest.mark.asyncio
async def test_ralph_handoff_resume_polls_persisted_job_blocks_on_user_cancel(tmp_path) -> None:
    """Resume polling must map ``terminal_status="cancelled"`` to BLOCKED with the
    pinned ``RALPH_CANCEL_BLOCKER_REASON``, mirroring the live ``_handoff_to_ralph``
    contract (Q00/ouroboros#782 review-10 BLOCKING #2). Falling through to the
    generic failure branch would mark a user-cancelled session FAILED on resume,
    which is a state-machine regression for a normal user action."""
    state = _state_in_ralph_handoff(tmp_path)

    async def ralph_resumer(*, job_id: str) -> dict[str, Any]:  # noqa: ARG001
        return {
            "job_id": "job_ralph_existing",
            "lineage_id": state.ralph_lineage_id,
            "dispatch_mode": "job",
            "terminal_status": "cancelled",
            "stop_reason": None,
        }

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_resumer=ralph_resumer,
        complete_product=True,
    )

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert state.phase is AutoPhase.BLOCKED
    assert state.last_error == "ralph cancelled by user"
    assert state.last_tool_name == "ralph_starter"
    assert state.ralph_job_status == "cancelled"


@pytest.mark.asyncio
async def test_ralph_handoff_resume_falls_back_to_guidance_without_resumer(tmp_path) -> None:
    """When no ``ralph_resumer`` is wired, resume preserves legacy
    guidance-only behavior — no polling, no transition. This keeps in-process
    test/library callers without a job-manager handle from breaking."""
    state = _state_in_ralph_handoff(tmp_path)

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        # No ralph_resumer.
        complete_product=True,
    )

    result = await pipeline.run(state)

    assert result.status == "ralph_handoff"
    assert state.phase is AutoPhase.RALPH_HANDOFF
    assert state.run_handoff_guidance is not None
    assert "did not start duplicate run or Ralph work" in state.run_handoff_guidance


# ---------------------------------------------------------------------------
# Resume from RUN with persisted handle — review-5 finding 2.
#
# A crash between run-handoff and ``_handoff_to_ralph`` must NOT silently
# bypass Ralph on resume when the operator opted into ``--complete-product``.
# ---------------------------------------------------------------------------


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
async def test_run_resume_with_persisted_handle_and_complete_product_dispatches_ralph(
    tmp_path,
) -> None:
    """RUN resume with persisted run handles MUST honor ``complete_product``
    and continue to RALPH_HANDOFF, not short-circuit to COMPLETE.

    The resume gate now mirrors the fresh-path contract by polling the
    owned-job snapshot for terminal success before invoking Ralph; this
    test wires a snapshot that reports ``status="completed"`` /
    ``success=True`` so the handoff proceeds and proves the happy path
    still works.
    """

    state = _state_at_run_phase(tmp_path)
    state.run_start_attempted = True
    state.run_handoff_status = "started"
    state.job_id = "job_run_existing"
    state.execution_id = "execution_existing"
    state.run_session_id = "session_existing"
    state.complete_product = True

    captured: dict[str, Any] = {}

    async def ralph_starter(seed: Seed, **kwargs: Any) -> dict[str, Any]:
        captured["seed"] = seed
        captured["kwargs"] = kwargs
        return {
            "job_id": "job_ralph_resumed",
            "lineage_id": kwargs["lineage_id"],
            "dispatch_mode": "job",
            "terminal_status": "completed",
            "stop_reason": "qa passed",
        }

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_terminal_success_run_starter("job_run_existing"),
        reviewer=_PassReviewer(),
        ralph_starter=ralph_starter,
        complete_product=True,
    )

    result = await pipeline.run(state)

    assert "kwargs" in captured  # ralph_starter actually invoked
    assert result.status == "complete"
    assert state.phase is AutoPhase.COMPLETE
    assert state.ralph_job_id == "job_ralph_resumed"


@pytest.mark.asyncio
async def test_run_resume_with_persisted_handle_blocks_when_owned_job_paused(
    tmp_path,
) -> None:
    """Resume MUST NOT hand off to Ralph when the owned run job is paused.

    Mirrors the fresh-path paused guard. A paused complete-product run
    means the operator must resume the run itself before Ralph can take
    over.
    """

    state = _state_at_run_phase(tmp_path)
    state.run_start_attempted = True
    state.run_handoff_status = "started"
    state.job_id = "job_run_paused_resume"
    state.execution_id = "execution_paused_resume"
    state.complete_product = True

    class _RunSnapshot:
        is_terminal = True
        status = JobStatus.COMPLETED  # status enum value irrelevant; result_meta wins
        result_meta = {"status": "paused", "success": None}

    class _RunJobManager:
        async def get_snapshot(self, _job_id: str) -> _RunSnapshot:
            return _RunSnapshot()

    class _RunHandler:
        _job_manager = _RunJobManager()

    class PausedSnapshotStarter:
        handler = _RunHandler()

        async def __call__(self, _seed: Seed, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("resume must not start a duplicate run")

    async def ralph_starter(*_args: Any, **_kwargs: Any) -> dict[str, Any]:  # pragma: no cover
        raise AssertionError("ralph_starter must not run while resumed execution is paused")

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=PausedSnapshotStarter(),
        reviewer=_PassReviewer(),
        ralph_starter=ralph_starter,
        complete_product=True,
    )

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert state.phase is AutoPhase.BLOCKED
    assert state.last_tool_name == "run_starter"
    assert "paused" in (state.last_error or "")
    assert state.ralph_job_id is None


@pytest.mark.asyncio
async def test_run_resume_with_persisted_handle_blocks_when_owned_job_still_running(
    tmp_path,
) -> None:
    """Resume MUST block when the owned run job is still queued/running.

    The snapshot returns ``status="running"`` to model a poll that did not
    observe terminal completion within the per-phase budget — the fresh
    path treats this identically and we mirror that contract.
    """

    state = _state_at_run_phase(tmp_path)
    state.run_start_attempted = True
    state.run_handoff_status = "started"
    state.job_id = "job_run_pending_resume"
    state.complete_product = True

    class _RunSnapshot:
        is_terminal = False
        status = JobStatus.RUNNING
        result_meta: dict[str, Any] = {}

    class _RunJobManager:
        async def get_snapshot(self, _job_id: str) -> _RunSnapshot:
            return _RunSnapshot()

    class _RunHandler:
        _job_manager = _RunJobManager()

    class RunningSnapshotStarter:
        handler = _RunHandler()

        async def __call__(self, _seed: Seed, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("resume must not start a duplicate run")

    async def ralph_starter(*_args: Any, **_kwargs: Any) -> dict[str, Any]:  # pragma: no cover
        raise AssertionError("ralph_starter must not run while resumed execution is still pending")

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=RunningSnapshotStarter(),
        reviewer=_PassReviewer(),
        ralph_starter=ralph_starter,
        complete_product=True,
    )

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert state.phase is AutoPhase.BLOCKED
    assert state.last_tool_name == "run_starter"
    assert "did not finish" in (state.last_error or "")
    assert state.ralph_job_id is None


@pytest.mark.asyncio
async def test_run_resume_with_persisted_job_id_blocks_when_starter_has_no_snapshot_api(
    tmp_path,
) -> None:
    """Persisted ``job_id`` without a job-manager snapshot must not satisfy the resume gate.

    A persisted ``job_id`` proves the execute_seed job was dispatched,
    not that it reached terminal success. When the configured run
    starter does not expose ``handler._job_manager.get_snapshot``,
    ``_wait_owned_run_job_terminal`` returns ``None`` and the resume
    branch cannot reconcile the owned-job lifecycle. Without an
    explicit guard the previous resume path would walk past the
    terminal check and start Ralph on dispatch evidence alone; the new
    contract refuses the handoff in that case, mirroring the
    jobless-synchronous branch.
    """

    state = _state_at_run_phase(tmp_path)
    state.run_start_attempted = True
    state.run_handoff_status = "started"
    state.job_id = "job_run_unpollable"
    state.execution_id = "execution_unpollable"
    state.run_session_id = "session_unpollable"
    state.complete_product = True

    class UnpollableStarter:
        # No ``handler`` attribute => ``_wait_owned_run_job_terminal``
        # returns None because ``get_snapshot`` cannot be resolved.

        async def __call__(self, _seed: Seed, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("resume must not redispatch the run job")

    async def ralph_starter(*_args: Any, **_kwargs: Any) -> dict[str, Any]:  # pragma: no cover
        raise AssertionError(
            "ralph_starter must not run when the owned job cannot be reconciled"
        )

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=UnpollableStarter(),
        reviewer=_PassReviewer(),
        ralph_starter=ralph_starter,
        complete_product=True,
    )

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert state.phase is AutoPhase.BLOCKED
    assert state.last_tool_name == "run_starter"
    assert "snapshot is unavailable" in (state.last_error or "")
    assert state.ralph_job_id is None


@pytest.mark.asyncio
async def test_run_resume_blocks_when_snapshot_does_not_confirm_terminal_success(
    tmp_path,
) -> None:
    """An ambiguous terminal snapshot (no ``success=True`` / no ``status=completed``) must NOT pass the gate.

    ``_wait_owned_run_job_terminal`` may return ``result_meta`` that is
    empty or carries an unfamiliar status (e.g. an adapter that flips
    ``is_terminal`` but does not populate the success/status keys auto
    consumes). Such a payload is not evidence of terminal success and
    must be treated as unreconcilable, not silently fall through to the
    Ralph handoff. This guards the resume gate against snapshot shapes
    that bypass the failed/paused/queued allowlist by omission.
    """

    state = _state_at_run_phase(tmp_path)
    state.run_start_attempted = True
    state.run_handoff_status = "started"
    state.job_id = "job_run_ambiguous"
    state.complete_product = True

    class _RunSnapshot:
        is_terminal = True
        # status enum unset and result_meta empty: ``_wait_owned_run_job_terminal``
        # produces ``{}`` because the snapshot has nothing to ``setdefault`` from,
        # and the resume gate sees neither ``success=True`` nor
        # ``status="completed"``.
        status = None
        result_meta: dict[str, Any] = {}

    class _RunJobManager:
        async def get_snapshot(self, _job_id: str) -> _RunSnapshot:
            return _RunSnapshot()

    class _RunHandler:
        _job_manager = _RunJobManager()

    class AmbiguousSnapshotStarter:
        handler = _RunHandler()

        async def __call__(self, _seed: Seed, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("resume must not redispatch the run job")

    async def ralph_starter(*_args: Any, **_kwargs: Any) -> dict[str, Any]:  # pragma: no cover
        raise AssertionError(
            "ralph_starter must not run when the snapshot does not confirm terminal success"
        )

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=AmbiguousSnapshotStarter(),
        reviewer=_PassReviewer(),
        ralph_starter=ralph_starter,
        complete_product=True,
    )

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert state.phase is AutoPhase.BLOCKED
    assert state.last_tool_name == "run_starter"
    assert "did not confirm terminal success" in (state.last_error or "")
    assert state.ralph_job_id is None


@pytest.mark.asyncio
async def test_run_resume_with_jobless_sync_handle_blocks_complete_product_ralph(
    tmp_path,
) -> None:
    """Jobless synchronous executions cannot be reconciled on resume.

    ``HandlerSynchronousRunStarter`` always returns ``job_id=None`` and
    relies on the starter's returned dict for terminal-success
    validation. On resume that dict is gone, so persisted
    ``execution_id``/``run_session_id`` alone are not evidence of
    terminal success — they only prove an execution session existed.
    Most often this branch is reached after the fresh-path paused/failed
    guard blocked the synchronous run; ``_recoverable_phase_for_tool``
    sends the resume back into RUN, where without an explicit guard
    Ralph would launch against the still-pending product session.
    Resume must block until the operator resolves the synchronous run
    itself.
    """

    state = _state_at_run_phase(tmp_path)
    state.run_start_attempted = True
    state.run_handoff_status = "started"
    state.job_id = None
    state.execution_id = "execution_paused_sync"
    state.run_session_id = "session_paused_sync"
    state.complete_product = True

    class JoblessSyncStarter:
        synchronous_execution = True

        async def __call__(
            self, _seed: Seed, *, idempotency_key: str = ""  # noqa: ARG002
        ) -> dict[str, Any]:
            raise AssertionError("resume must not redispatch a synchronous run")

    async def ralph_starter(*_args: Any, **_kwargs: Any) -> dict[str, Any]:  # pragma: no cover
        raise AssertionError(
            "ralph_starter must not run while jobless sync execution is unreconcilable"
        )

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=JoblessSyncStarter(),
        reviewer=_PassReviewer(),
        ralph_starter=ralph_starter,
        complete_product=True,
    )

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert state.phase is AutoPhase.BLOCKED
    assert state.last_tool_name == "run_starter"
    assert "jobless synchronous execution" in (state.last_error or "")
    assert state.ralph_job_id is None


@pytest.mark.asyncio
async def test_run_resume_with_persisted_handle_blocks_when_owned_job_failed(
    tmp_path,
) -> None:
    """Resume MUST surface a failed owned run job instead of starting Ralph."""

    state = _state_at_run_phase(tmp_path)
    state.run_start_attempted = True
    state.run_handoff_status = "started"
    state.job_id = "job_run_failed_resume"
    state.complete_product = True

    class _RunSnapshot:
        is_terminal = True
        status = JobStatus.FAILED
        result_meta = {"status": "failed", "success": False}

    class _RunJobManager:
        async def get_snapshot(self, _job_id: str) -> _RunSnapshot:
            return _RunSnapshot()

    class _RunHandler:
        _job_manager = _RunJobManager()

    class FailedSnapshotStarter:
        handler = _RunHandler()

        async def __call__(self, _seed: Seed, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("resume must not start a duplicate run")

    async def ralph_starter(*_args: Any, **_kwargs: Any) -> dict[str, Any]:  # pragma: no cover
        raise AssertionError("ralph_starter must not run after a failed resumed execution")

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=FailedSnapshotStarter(),
        reviewer=_PassReviewer(),
        ralph_starter=ralph_starter,
        complete_product=True,
    )

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert state.phase is AutoPhase.BLOCKED
    assert state.last_tool_name == "run_starter"
    assert "finished unsuccessfully" in (state.last_error or "")
    assert state.ralph_job_id is None


@pytest.mark.asyncio
async def test_run_resume_with_persisted_handle_complete_product_off_completes(
    tmp_path,
) -> None:
    """``complete_product=False`` resume keeps the legacy short-circuit to
    COMPLETE byte-identical so default-off callers see no behavior change."""
    state = _state_at_run_phase(tmp_path)
    state.run_start_attempted = True
    state.run_handoff_status = "started"
    state.job_id = "job_run_existing"
    state.complete_product = False

    async def ralph_starter(*_args: Any, **_kwargs: Any) -> dict[str, Any]:  # pragma: no cover
        raise AssertionError("complete_product=False must not invoke ralph_starter")

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=ralph_starter,
        complete_product=False,
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert state.phase is AutoPhase.COMPLETE
    assert state.ralph_job_id is None


# ---------------------------------------------------------------------------
# Early dispatch checkpoint — review-6.
#
# The Ralph tracking handle must be persisted IMMEDIATELY after the background
# job is created, BEFORE the auto pipeline blocks on terminal completion.
# Otherwise a process restart between dispatch and terminal would leave the
# state with only ``ralph_lineage_id`` and ``_resume_ralph_handoff`` could
# not poll the still-running Ralph job — reintroducing the stranded-resume
# bug review-5 was meant to solve.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handoff_to_ralph_persists_job_id_before_terminal_poll(tmp_path) -> None:
    """``ralph_starter`` receives an ``on_dispatched`` hook and the auto
    pipeline checkpoints ``ralph_job_id`` synchronously inside that hook —
    BEFORE the starter's terminal-status await returns."""
    state = _state_at_run_phase(tmp_path)

    captured: dict[str, Any] = {}

    async def ralph_starter(
        _seed: Seed,
        *,
        lineage_id: str,
        on_dispatched: Any | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        # Simulate a real ``HandlerRalphStarter``: emit the dispatch
        # envelope BEFORE returning the terminal envelope. Capture the
        # state snapshot at the moment the checkpoint fires so the
        # assertion can prove ``state.ralph_job_id`` was already set
        # by the time terminal completion is reached.
        if on_dispatched is not None:
            on_dispatched(
                {
                    "job_id": "job_ralph_early",
                    "lineage_id": lineage_id,
                    "dispatch_mode": "job",
                }
            )
        captured["job_id_at_dispatch"] = state.ralph_job_id
        return {
            "job_id": "job_ralph_early",
            "lineage_id": lineage_id,
            "dispatch_mode": "job",
            "terminal_status": "completed",
            "stop_reason": "qa passed",
        }

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=ralph_starter,
        complete_product=True,
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    # The checkpoint hook fired BEFORE the starter returned the terminal
    # envelope, so ``state.ralph_job_id`` was already populated when the
    # snapshot was taken — proving the dispatch handle is durable across
    # a hypothetical process death between dispatch and terminal.
    assert captured["job_id_at_dispatch"] == "job_ralph_early"


@pytest.mark.asyncio
async def test_handoff_to_ralph_falls_back_for_legacy_starter_without_hook(tmp_path) -> None:
    """Older ``RalphStarter`` implementations that don't accept the
    ``on_dispatched`` keyword must still work — the pipeline detects the
    signature before invocation and calls without the hook so the legacy
    contract is preserved (the test/library callers without a job manager
    opt out of the early-checkpoint guarantee, accepting the documented
    stranded-resume risk)."""
    state = _state_at_run_phase(tmp_path)

    async def legacy_ralph_starter(
        _seed: Seed,
        *,
        lineage_id: str,
        max_total_seconds: float | None = None,  # noqa: ARG001
        per_iteration_timeout_seconds: float | None = None,  # noqa: ARG001
    ) -> dict[str, Any]:
        return {
            "job_id": "job_legacy_ralph",
            "lineage_id": lineage_id,
            "dispatch_mode": "job",
            "terminal_status": "completed",
            "stop_reason": "qa passed",
        }

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=legacy_ralph_starter,
        complete_product=True,
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert state.ralph_job_id == "job_legacy_ralph"


@pytest.mark.asyncio
async def test_handoff_to_ralph_does_not_retry_type_error_after_dispatch(tmp_path) -> None:
    """A starter that accepts ``on_dispatched`` and then fails with
    ``TypeError`` has already dispatched Ralph work, so the pipeline must
    fail the session without invoking the starter a second time."""
    state = _state_at_run_phase(tmp_path)
    calls = 0

    async def ralph_starter(
        _seed: Seed,
        *,
        lineage_id: str,
        on_dispatched: Any | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if on_dispatched is not None:
            on_dispatched(
                {
                    "job_id": "job_ralph_dispatched_once",
                    "lineage_id": lineage_id,
                    "dispatch_mode": "job",
                }
            )
        raise TypeError("starter failed after dispatch")

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=ralph_starter,
        complete_product=True,
    )

    result = await pipeline.run(state)

    assert calls == 1
    assert result.status == "failed"
    assert state.phase is AutoPhase.FAILED
    assert state.ralph_job_id == "job_ralph_dispatched_once"
    assert state.last_error == "ralph handoff failed: starter failed after dispatch"


# ---------------------------------------------------------------------------
# Q00/ouroboros#782 review-12 BLOCKING #1 — RALPH_HANDOFF resume must
# reconcile an already-terminal Ralph job even when the top-level deadline
# has expired. Without this, a client that disconnects, lets Ralph finish
# in the background, and then resumes after ``deadline_at`` is silently
# demoted to ``pipeline_timeout`` instead of seeing the real COMPLETE/BLOCKED.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ralph_handoff_resume_reconciles_terminal_job_after_deadline_expired(
    tmp_path,
) -> None:
    """Resume must poll an already-terminal Ralph job even when ``deadline_at``
    has expired before the resume call. The poller's snapshot returns the
    terminal status immediately (the loop already finished), so transitioning
    to COMPLETE preserves the live-path contract instead of falsely tripping
    ``pipeline_timeout``."""
    state = _state_in_ralph_handoff(tmp_path)
    # Deadline already in the past — the legacy early-return would have
    # blocked this resume with ``pipeline_timeout (deadline expired before
    # resume)`` before reaching ``_resume_ralph_handoff``.
    state.deadline_at = time.monotonic() - 60.0

    polled_job: dict[str, Any] = {}

    async def ralph_resumer(*, job_id: str) -> dict[str, Any]:
        polled_job["job_id"] = job_id
        return {
            "job_id": job_id,
            "lineage_id": state.ralph_lineage_id,
            "dispatch_mode": "job",
            "terminal_status": "completed",
            "stop_reason": "qa passed",
        }

    async def ralph_starter(*_args: Any, **_kwargs: Any) -> dict[str, Any]:  # pragma: no cover
        raise AssertionError("resume must not start a duplicate Ralph handoff")

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=ralph_starter,
        ralph_resumer=ralph_resumer,
        complete_product=True,
    )

    result = await pipeline.run(state)

    assert polled_job["job_id"] == "job_ralph_existing"
    assert result.status == "complete"
    assert state.phase is AutoPhase.COMPLETE


# ---------------------------------------------------------------------------
# Q00/ouroboros#782 review-12 BLOCKING #2 — plugin pre-call checkpoint must
# distinguish "dispatch attempted" from "dispatch confirmed". A crash before
# the bridge actually receives the ``_subagent`` envelope leaves persisted
# state with ``ralph_dispatch_mode == "plugin_pending"``; resume must retry
# rather than falsely transition to COMPLETE.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ralph_handoff_resume_plugin_pending_retries_dispatch(tmp_path) -> None:
    """``ralph_dispatch_mode == "plugin_pending"`` means the auto pipeline
    persisted the dispatch intent BEFORE the ``ouroboros_ralph`` handler
    actually emitted ``mcp.subagent.dispatched``. On resume, this must
    retry the handoff via ``_handoff_to_ralph`` with the same persisted
    lineage instead of trusting the unconfirmed marker and short-circuiting
    to COMPLETE."""
    state = _state_in_ralph_handoff(tmp_path)
    state.ralph_dispatch_mode = "plugin_pending"
    state.ralph_job_id = None

    retry_calls: list[dict[str, Any]] = []

    async def ralph_starter(seed: Seed, **kwargs: Any) -> dict[str, Any]:
        retry_calls.append({"seed": seed, "kwargs": kwargs})
        return {
            "job_id": None,
            "lineage_id": kwargs["lineage_id"],
            "dispatch_mode": "plugin",
            "terminal_status": "delegated_to_plugin",
            "stop_reason": None,
        }

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=ralph_starter,
        complete_product=True,
    )

    result = await pipeline.run(state)

    assert len(retry_calls) == 1, (
        "plugin_pending resume must retry _handoff_to_ralph exactly once, not "
        "trust the unconfirmed checkpoint as a completed dispatch"
    )
    # Retried with the same persisted lineage so any half-emitted events
    # stay correlated.
    assert retry_calls[0]["kwargs"]["lineage_id"] == "ralph-seed_test_001-auto_abc"
    assert result.status == "complete"
    assert state.phase is AutoPhase.COMPLETE
    # Post-call confirmation overrode the unconfirmed marker.
    assert state.ralph_dispatch_mode == "plugin"


@pytest.mark.asyncio
async def test_ralph_handoff_resume_plugin_pending_blocks_when_starter_unwired(
    tmp_path,
) -> None:
    """Without a wired ``ralph_starter`` (e.g. a library caller without a
    job-manager handle), the pipeline cannot retry safely on a
    ``plugin_pending`` checkpoint, so it must surface a recoverable
    BLOCKED rather than silently transitioning to COMPLETE."""
    state = _state_in_ralph_handoff(tmp_path)
    state.ralph_dispatch_mode = "plugin_pending"
    state.ralph_job_id = None

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        # No ralph_starter — cannot retry plugin dispatch.
        complete_product=True,
    )

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert state.phase is AutoPhase.BLOCKED
    assert "interrupted before confirmation" in (state.last_error or "")


# ---------------------------------------------------------------------------
# Q00/ouroboros#782 review-13 BLOCKING #1 — Resuming a plugin-dispatched
# Ralph handoff must NOT re-emit the persisted ``state.run_subagent``. The
# bridge already received the original ``_subagent`` envelope (that's what
# "confirmed plugin dispatch" means); replaying it on resume can spawn a
# duplicate OpenCode child session via ``meta["_subagent"]``.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ralph_handoff_resume_confirmed_plugin_does_not_replay_subagent(
    tmp_path,
) -> None:
    """A RALPH_HANDOFF resume with ``ralph_dispatch_mode == "plugin"`` is a
    confirmed one-shot dispatch — the persisted ``run_subagent`` envelope
    must be suppressed in the result, not replayed via ``_result()``'s
    fallback to ``state.run_subagent``."""
    state = _state_in_ralph_handoff(tmp_path)
    state.ralph_dispatch_mode = "plugin"
    state.ralph_job_id = None
    # Simulate a session whose RUN handoff persisted a ``_subagent`` envelope
    # before the Ralph plugin dispatch confirmed. Without the fix,
    # ``_result()`` would fall back to this dict and re-emit it as
    # ``meta["_subagent"]``, causing the bridge to spawn another child.
    state.run_subagent = {
        "type": "ralph_loop",
        "lineage_id": state.ralph_lineage_id,
    }

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        complete_product=True,
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert state.phase is AutoPhase.COMPLETE
    # The result must NOT replay the persisted envelope.
    assert result.run_subagent is None
    # And the persisted state must be cleared so a future re-resume also
    # doesn't replay it.
    assert state.run_subagent == {}


@pytest.mark.asyncio
async def test_expired_deadline_after_recovery_allows_persisted_ralph_job_poll(
    tmp_path,
) -> None:
    """A recoverable BLOCKED state with a persisted Ralph job must poll first.

    The deadline exception has to be recomputed after BLOCKED -> RALPH_HANDOFF
    recovery; otherwise an expired deadline masks an already-finished Ralph job
    as ``pipeline_timeout``.
    """
    state = _state_in_ralph_handoff(tmp_path)
    state.deadline_at = time.monotonic() - 5.0
    state.deadline_at_epoch = time.time() - 5.0
    state.mark_blocked("ralph handoff timed out before terminal status", tool_name="ralph_starter")

    polled: list[str] = []

    async def ralph_resumer(*, job_id: str) -> dict[str, Any]:
        polled.append(job_id)
        return {
            "job_id": job_id,
            "lineage_id": state.ralph_lineage_id,
            "dispatch_mode": "job",
            "terminal_status": "completed",
            "stop_reason": "qa passed",
        }

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_resumer=ralph_resumer,
        complete_product=True,
    )

    result = await pipeline.run(state)

    assert polled == ["job_ralph_existing"]
    assert result.status == "complete"
    assert state.phase is AutoPhase.COMPLETE
    assert state.last_tool_name != PIPELINE_DEADLINE_TOOL_NAME


@pytest.mark.asyncio
async def test_expired_deadline_after_recovery_allows_confirmed_plugin_checkpoint(
    tmp_path,
) -> None:
    """Confirmed plugin dispatches are also reconciled after recovery."""
    state = _state_in_ralph_handoff(tmp_path)
    state.ralph_job_id = None
    state.ralph_dispatch_mode = "plugin"
    state.deadline_at = time.monotonic() - 5.0
    state.deadline_at_epoch = time.time() - 5.0
    state.mark_blocked("ralph handoff timed out before terminal status", tool_name="ralph_starter")

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        complete_product=True,
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert state.phase is AutoPhase.COMPLETE
    assert state.last_tool_name != PIPELINE_DEADLINE_TOOL_NAME


@pytest.mark.asyncio
async def test_expired_deadline_after_recovery_blocks_unconfirmed_plugin_pending(
    tmp_path,
) -> None:
    """Unconfirmed plugin_pending checkpoints still obey normal deadlines."""
    state = _state_in_ralph_handoff(tmp_path)
    state.ralph_job_id = None
    state.ralph_dispatch_mode = "plugin_pending"
    state.deadline_at = time.monotonic() - 5.0
    state.deadline_at_epoch = time.time() - 5.0
    state.mark_blocked("ralph handoff timed out before terminal status", tool_name="ralph_starter")

    async def ralph_starter(*_args: Any, **_kwargs: Any) -> dict[str, Any]:  # pragma: no cover
        raise AssertionError("plugin_pending must not retry after deadline expiry")

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=ralph_starter,
        complete_product=True,
    )

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert state.phase is AutoPhase.BLOCKED
    assert state.last_tool_name == PIPELINE_DEADLINE_TOOL_NAME
    assert "pipeline_timeout" in (state.last_error or "")
