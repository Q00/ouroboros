"""Regression tests for the RUN → RALPH_HANDOFF chain (Q00/ouroboros#773).

The chain is opt-in via ``--complete-product`` / ``complete_product=True`` and
maps the Ralph loop's terminal status onto an auto phase per the contract
pinned in this file. Default-off behavior must be byte-identical to the
pre-#773 result shape.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

import pytest

from ouroboros.auto.grading import GradeResult, SeedGrade
from ouroboros.auto.interview_driver import AutoInterviewResult
from ouroboros.auto.pipeline import (
    _RALPH_BLOCKED_STOP_REASONS,
    AutoPipeline,
    AutoPipelineResult,
)
from ouroboros.auto.seed_reviewer import SeedReview, SeedReviewer
from ouroboros.auto.state import (
    _ALLOWED_TRANSITIONS,
    AutoPhase,
    AutoPipelineState,
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


# ---------------------------------------------------------------------------
# State machine — transitions added by #773
# ---------------------------------------------------------------------------


def test_state_machine_allows_run_to_ralph_handoff() -> None:
    """``RUN → RALPH_HANDOFF`` must be in ``_ALLOWED_TRANSITIONS`` per the issue."""
    assert AutoPhase.RALPH_HANDOFF in _ALLOWED_TRANSITIONS[AutoPhase.RUN]


def test_state_machine_allows_ralph_handoff_terminal_transitions() -> None:
    """RALPH_HANDOFF must be terminal-bound to COMPLETE/BLOCKED/FAILED."""
    assert _ALLOWED_TRANSITIONS[AutoPhase.RALPH_HANDOFF] == {
        AutoPhase.COMPLETE,
        AutoPhase.BLOCKED,
        AutoPhase.FAILED,
    }


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
async def test_ralph_job_id_persisted_before_terminal_wait(tmp_path) -> None:
    """RUN → RALPH_HANDOFF saves the ralph job id as soon as ralph starts."""
    state = _state_at_run_phase(tmp_path)
    store = AutoStore(tmp_path)
    store.save(state)
    observed: dict[str, Any] = {}

    async def ralph_starter(_seed: Seed, **kwargs: Any) -> dict[str, Any]:
        kwargs["on_started"](
            {
                "job_id": "job_ralph_live",
                "lineage_id": kwargs["lineage_id"],
                "dispatch_mode": "job",
                "status": "running",
            }
        )
        persisted = store.load(state.auto_session_id)
        observed["phase"] = persisted.phase
        observed["job_id"] = persisted.ralph_job_id
        observed["status"] = persisted.ralph_job_status
        return {
            "job_id": "job_ralph_live",
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

    result = await pipeline.run(state)

    assert observed == {
        "phase": AutoPhase.RALPH_HANDOFF,
        "job_id": "job_ralph_live",
        "status": "running",
    }
    assert result.status == "complete"


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
