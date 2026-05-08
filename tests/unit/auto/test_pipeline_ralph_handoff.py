"""Regression tests for the RUN → RALPH_HANDOFF chain (Q00/ouroboros#773).

The chain is opt-in via ``--complete-product`` / ``complete_product=True`` and
maps the Ralph loop's terminal status onto an auto phase per the contract
pinned in this file. Default-off behavior must be byte-identical to the
pre-#773 result shape.
"""

from __future__ import annotations

from dataclasses import asdict
import time
from typing import Any

import pytest

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
