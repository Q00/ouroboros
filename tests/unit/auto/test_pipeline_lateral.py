"""Phase 2.2 tests — UNSTUCK_LATERAL phase + HandlerLateralThinker + persona routing.

Covers RFC #809 Phase 2.2: when ``ouroboros_qa`` rules a run artifact did
not satisfy the Seed AC, the pipeline picks a persona deterministically
from the QA-failure shape, invokes ``ouroboros_lateral_think`` for a
reframing prompt, and surfaces the persona's output on the BLOCKED state.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ouroboros.auto.adapters import (
    EvaluateResult,
    HandlerLateralThinker,
    LateralResult,
)
from ouroboros.auto.grading import GradeResult, SeedGrade
from ouroboros.auto.interview_driver import AutoInterviewResult
from ouroboros.auto.lateral_routing import (
    classify_qa_failure_to_pattern,
    select_persona_for_qa_failure,
)
from ouroboros.auto.pipeline import AutoPipeline
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
from ouroboros.resilience.lateral import ThinkingPersona
from ouroboros.resilience.stagnation import StagnationPattern

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_seed(seed_id: str = "seed_lateral_001") -> Seed:
    return Seed(
        goal="Build a CLI",
        constraints=("Use existing project patterns",),
        acceptance_criteria=("Command prints stable output",),
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
        metadata=SeedMetadata(seed_id=seed_id, ambiguity_score=0.12),
    )


class _StubInterviewDriver:
    def __init__(self) -> None:
        self.progress_callback = None

    async def run(self, state: AutoPipelineState, ledger: Any) -> AutoInterviewResult:
        state.interview_session_id = "interview_stub"
        state.interview_completed = True
        return AutoInterviewResult(
            status="seed_ready",
            session_id="interview_stub",
            ledger=ledger,
            rounds=1,
        )


def _state_at_run_phase(tmp_path) -> AutoPipelineState:
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


async def _run_starter_ok(_seed: Seed, **kwargs: Any) -> dict[str, Any]:
    return {
        "job_id": "job_run_001",
        "session_id": "exec_session_001",
        "execution_id": "execution_001",
    }


async def _seed_generator_unused(_session_id: str) -> Seed:  # pragma: no cover
    raise AssertionError("seed generator should not run when seed_artifact is set")


class _PassReviewer(SeedReviewer):
    def __init__(self) -> None:
        pass

    def review(self, seed: Seed, *, ledger: Any = None) -> SeedReview:  # noqa: ARG002
        grade = GradeResult(grade=SeedGrade.A, scores={}, findings=[], blockers=[], may_run=True)
        return SeedReview(grade_result=grade, findings=())


def _ralph_starter(*, result_text: str = "stdout: ok\nexit_code: 0"):
    async def _starter(seed: Seed, **kwargs: Any) -> dict[str, Any]:
        return {
            "job_id": "job_ralph_001",
            "lineage_id": kwargs["lineage_id"],
            "dispatch_mode": "job",
            "terminal_status": "completed",
            "stop_reason": "qa passed",
            "result_text": result_text,
        }

    return _starter


class _StubLedger:
    def summary(self) -> dict[str, Any]:
        return {
            "provenance": {},
            "evidence_backed_sections": (),
            "assumption_only_sections": (),
        }

    def assumptions(self) -> list[str]:
        return []

    def non_goals(self) -> list[str]:
        return []


# ---------------------------------------------------------------------------
# State-machine sanity
# ---------------------------------------------------------------------------


def test_unstuck_lateral_phase_in_allowed_transitions() -> None:
    assert AutoPhase.UNSTUCK_LATERAL in _ALLOWED_TRANSITIONS[AutoPhase.EVALUATE]
    # RFC #809 Phase 2.2b — UNSTUCK_LATERAL became a retry dispatcher:
    # back to EVALUATE for another round under a different persona, or
    # BLOCKED when a recovery guard trips. SEED_REGENERATE is intentionally
    # absent (see the AutoPhase deferral comment): an automatic Seed
    # rewrite is a reward-hacking surface that mutates the spec the user
    # explicitly agreed to. The pipeline instead surfaces the operator
    # choices (re-interview, abandon) in the final BLOCKED message and
    # leaves the spec under human control. "Relax AC via edited seed" is
    # not advertised because late-phase resume reconstructs the Seed from
    # ``state.seed_artifact`` rather than rereading the on-disk seed
    # file; honoring an edited seed file on EVALUATE / UNSTUCK_LATERAL
    # resume is a separate change.

    assert _ALLOWED_TRANSITIONS[AutoPhase.UNSTUCK_LATERAL] == {
        AutoPhase.EVALUATE,
        AutoPhase.COMPLETE,
        AutoPhase.BLOCKED,
        AutoPhase.FAILED,
    }
    # Recovery from terminal phases must allow re-entering UNSTUCK_LATERAL.
    assert AutoPhase.UNSTUCK_LATERAL in _ALLOWED_TRANSITIONS[AutoPhase.BLOCKED]
    assert AutoPhase.UNSTUCK_LATERAL in _ALLOWED_TRANSITIONS[AutoPhase.FAILED]


# ---------------------------------------------------------------------------
# Persona routing — deterministic classification
# ---------------------------------------------------------------------------


def test_classify_xcode_unavailable_to_spinning() -> None:
    assert (
        classify_qa_failure_to_pattern(["Xcode is not available in the sandbox"], [])
        is StagnationPattern.SPINNING
    )


def test_classify_ambiguous_requirement_to_oscillation() -> None:
    assert (
        classify_qa_failure_to_pattern(["The requirement is ambiguous"], [])
        is StagnationPattern.OSCILLATION
    )


def test_classify_missing_context_to_no_drift() -> None:
    assert (
        classify_qa_failure_to_pattern(["missing context about the runtime"], [])
        is StagnationPattern.NO_DRIFT
    )


def test_classify_over_engineered_to_diminishing_returns() -> None:
    assert (
        classify_qa_failure_to_pattern(["solution is over-engineered"], [])
        is StagnationPattern.DIMINISHING_RETURNS
    )


def test_classify_empty_falls_to_spinning() -> None:
    assert classify_qa_failure_to_pattern([], []) is StagnationPattern.SPINNING


def test_persona_routing_picks_hacker_for_environment_unavailable() -> None:
    assert select_persona_for_qa_failure(["Xcode not installed"], []) is ThinkingPersona.HACKER


def test_persona_routing_picks_architect_for_ambiguous() -> None:
    assert (
        select_persona_for_qa_failure(["Conflicting requirements"], []) is ThinkingPersona.ARCHITECT
    )


def test_persona_routing_picks_researcher_for_missing_context() -> None:
    assert (
        select_persona_for_qa_failure(["missing documentation"], []) is ThinkingPersona.RESEARCHER
    )


def test_persona_routing_picks_simplifier_for_over_engineered() -> None:
    assert (
        select_persona_for_qa_failure(["unnecessary abstraction"], []) is ThinkingPersona.SIMPLIFIER
    )


def test_persona_routing_falls_back_to_contrarian_when_primary_tried() -> None:
    """If hacker was already tried for a SPINNING pattern, the next call
    must fall back to CONTRARIAN (universal fallback)."""
    assert (
        select_persona_for_qa_failure(
            ["Xcode unavailable"], [], already_tried_personas=(ThinkingPersona.HACKER,)
        )
        is ThinkingPersona.CONTRARIAN
    )


def test_persona_routing_deterministic_for_same_input() -> None:
    """Same input must always produce the same persona — locks in the
    deterministic-classification contract that resume idempotency relies on."""
    diffs = ["Xcode not available", "cannot run build tool"]
    persona_1 = select_persona_for_qa_failure(diffs, [])
    persona_2 = select_persona_for_qa_failure(diffs, [])
    assert persona_1 is persona_2 is ThinkingPersona.HACKER


# ---------------------------------------------------------------------------
# Pipeline UNSTUCK_LATERAL happy / fail / opt-in
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_qa_pass_does_not_enter_unstuck_lateral(tmp_path) -> None:
    """QA pass path must skip UNSTUCK_LATERAL entirely (Phase 2.1 behaviour
    preserved)."""
    state = _state_at_run_phase(tmp_path)
    lateral_calls = 0

    async def evaluator(seed: Seed, artifact: str) -> EvaluateResult:  # noqa: ARG001
        return EvaluateResult(passed=True, score=0.92, verdict="pass")

    async def lateral_thinker(**kwargs: Any) -> LateralResult:  # noqa: ARG001
        nonlocal lateral_calls
        lateral_calls += 1
        return LateralResult(persona="hacker", approach_summary="", text="")

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=_ralph_starter(),
        complete_product=True,
        evaluator=evaluator,
        lateral_thinker=lateral_thinker,
    )

    result = await pipeline.run(state)
    assert result.status == "complete"
    assert lateral_calls == 0
    assert state.last_lateral_persona is None


@pytest.mark.asyncio
async def test_pipeline_qa_fail_enters_unstuck_lateral_and_blocks_with_persona(tmp_path) -> None:
    """QA fail path with lateral_thinker wired: transitions through
    UNSTUCK_LATERAL, persists persona output, lands in BLOCKED with the
    persona summary in the blocker text."""
    state = _state_at_run_phase(tmp_path)

    async def evaluator(seed: Seed, artifact: str) -> EvaluateResult:  # noqa: ARG001
        return EvaluateResult(
            passed=False,
            score=0.30,
            verdict="fail",
            differences=("Xcode is not available in the sandbox",),
            suggestions=("try CLI build via swift test",),
        )

    captured_call: dict[str, Any] = {}

    async def lateral_thinker(**kwargs: Any) -> LateralResult:
        captured_call.update(kwargs)
        return LateralResult(
            persona="hacker",
            approach_summary="Hacker: Finds unconventional workarounds",
            text="# Lateral Thinking: Hacker\n\nReframe the verification path...",
        )

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=_ralph_starter(),
        complete_product=True,
        evaluator=evaluator,
        lateral_thinker=lateral_thinker,
    )

    result = await pipeline.run(state)
    assert result.status == "blocked"
    assert state.phase is AutoPhase.BLOCKED
    assert state.last_tool_name == "lateral_thinker"
    assert state.last_lateral_persona == "hacker"
    assert "Hacker: Finds unconventional workarounds" in state.last_lateral_approach_summary
    assert "hacker" in (state.last_error or "")
    # Lateral thinker was called with the correct persona
    assert captured_call["persona"] is ThinkingPersona.HACKER
    assert "Xcode" in str(captured_call["qa_differences"])
    # MCP-facing result fields populated
    assert result.last_lateral_persona == "hacker"
    assert result.last_lateral_text is not None


@pytest.mark.asyncio
async def test_pipeline_qa_fail_without_lateral_thinker_falls_back_to_phase_2_1(tmp_path) -> None:
    """When lateral_thinker is None, QA fail must land in BLOCKED with the
    Phase 2.1 message (no persona consultation)."""
    state = _state_at_run_phase(tmp_path)

    async def evaluator(seed: Seed, artifact: str) -> EvaluateResult:  # noqa: ARG001
        return EvaluateResult(
            passed=False, score=0.30, verdict="fail", differences=("any failure",)
        )

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=_ralph_starter(),
        complete_product=True,
        evaluator=evaluator,
        lateral_thinker=None,
    )

    result = await pipeline.run(state)
    assert result.status == "blocked"
    assert state.last_tool_name == "evaluator"  # NOT lateral_thinker
    assert state.last_lateral_persona is None


@pytest.mark.asyncio
async def test_pipeline_lateral_skipped_when_complete_product_false(tmp_path) -> None:
    """Without ``complete_product``, the EVALUATE phase doesn't run so
    UNSTUCK_LATERAL never has a chance to trigger either."""
    state = _state_at_run_phase(tmp_path)
    lateral_calls = 0

    async def lateral_thinker(**kwargs: Any) -> LateralResult:  # noqa: ARG001
        nonlocal lateral_calls
        lateral_calls += 1
        return LateralResult(persona="hacker", approach_summary="", text="")

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=None,
        complete_product=False,
        evaluator=None,
        lateral_thinker=lateral_thinker,
    )

    await pipeline.run(state)
    assert lateral_calls == 0


# ---------------------------------------------------------------------------
# Lateral timeout / error / deadline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_lateral_timeout_blocks_with_recoverable_tool_name(tmp_path) -> None:
    state = _state_at_run_phase(tmp_path)
    state.timeout_seconds_by_phase[AutoPhase.UNSTUCK_LATERAL.value] = 1

    async def evaluator(seed: Seed, artifact: str) -> EvaluateResult:  # noqa: ARG001
        return EvaluateResult(passed=False, score=0.1, verdict="fail", differences=("xx",))

    async def hanging_lateral(**kwargs: Any) -> LateralResult:  # noqa: ARG001
        await asyncio.sleep(10)
        return LateralResult(persona="hacker", approach_summary="", text="")

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=_ralph_starter(),
        complete_product=True,
        evaluator=evaluator,
        lateral_thinker=hanging_lateral,
    )

    result = await pipeline.run(state)
    assert result.status == "blocked"
    assert state.last_tool_name == "lateral_thinker"
    assert "timed out" in (state.last_error or "")


@pytest.mark.asyncio
async def test_pipeline_lateral_handler_error_blocks(tmp_path) -> None:
    state = _state_at_run_phase(tmp_path)

    async def evaluator(seed: Seed, artifact: str) -> EvaluateResult:  # noqa: ARG001
        return EvaluateResult(passed=False, score=0.1, verdict="fail", differences=("xx",))

    async def errored_lateral(**kwargs: Any) -> LateralResult:  # noqa: ARG001
        return LateralResult(
            persona="hacker",
            approach_summary="",
            text="",
            error="lateral_think tool unreachable",
        )

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=_ralph_starter(),
        complete_product=True,
        evaluator=evaluator,
        lateral_thinker=errored_lateral,
    )

    result = await pipeline.run(state)
    assert result.status == "blocked"
    assert state.last_tool_name == "lateral_thinker"
    assert "lateral_think tool unreachable" in (state.last_error or "")


@pytest.mark.asyncio
async def test_pipeline_lateral_respects_top_level_deadline(tmp_path) -> None:
    """Pipeline-deadline trip during the lateral call surfaces the
    canonical pipeline_timeout blocker, not the per-phase timeout."""
    import time as _time

    from ouroboros.auto.pipeline import PIPELINE_DEADLINE_TOOL_NAME

    state = _state_at_run_phase(tmp_path)
    state.deadline_at = _time.monotonic() + 0.1
    state.timeout_seconds_by_phase[AutoPhase.UNSTUCK_LATERAL.value] = 60

    async def evaluator(seed: Seed, artifact: str) -> EvaluateResult:  # noqa: ARG001
        return EvaluateResult(passed=False, score=0.1, verdict="fail", differences=("x",))

    async def hanging_lateral(**kwargs: Any) -> LateralResult:  # noqa: ARG001
        await asyncio.sleep(10)
        return LateralResult(persona="hacker", approach_summary="", text="")

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=_ralph_starter(),
        complete_product=True,
        evaluator=evaluator,
        lateral_thinker=hanging_lateral,
    )

    result = await pipeline.run(state)
    assert result.status == "blocked"
    assert state.last_tool_name == PIPELINE_DEADLINE_TOOL_NAME
    assert "pipeline_timeout" in (state.last_error or "")


# ---------------------------------------------------------------------------
# Resume entry: run() must let EVALUATE/UNSTUCK_LATERAL phases reach their
# handlers instead of blocking at the older resume guard.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_resume_from_unstuck_lateral_reaches_handler(tmp_path) -> None:
    """A session recovered to UNSTUCK_LATERAL (via the BLOCKED → recovery
    path) must reach ``_run_lateral`` rather than tripping the older
    "Cannot resume auto pipeline from <phase>" guard at the top of run()."""
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.arm_deadline()
    state.complete_product = True
    seed = _build_seed()
    state.seed_id = seed.metadata.seed_id
    state.seed_artifact = seed.to_dict()
    state.last_grade = "A"
    state.interview_session_id = "interview_stub"
    state.interview_completed = True
    # Walk forward to UNSTUCK_LATERAL via valid transitions
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.RUN, "run")
    state.transition(AutoPhase.RALPH_HANDOFF, "ralph")
    state.transition(AutoPhase.EVALUATE, "evaluate")
    state.transition(AutoPhase.UNSTUCK_LATERAL, "unstuck")
    # Persisted QA + lateral cache so the handler short-circuits to the
    # cached persona (no LLM call needed for this test).
    state.last_qa_passed = False
    state.last_qa_score = 0.3
    state.last_qa_verdict = "fail"
    state.last_qa_differences = ["Xcode unavailable"]
    state.last_qa_suggestions = []
    state.last_lateral_persona = "hacker"
    state.last_lateral_approach_summary = "Hacker"
    state.last_lateral_text = "advice"
    # Match the hash so the cache-hit branch fires. The cache key now
    # includes ``evaluate_artifact_hash`` (review fix: lateral cache must
    # invalidate when the evaluate artifact changes), so set both fields
    # consistently.
    import hashlib

    state.evaluate_artifact_hash = "cached_artifact_hash"
    state.lateral_input_hash = hashlib.sha256(
        b"hacker|cached_artifact_hash|Xcode unavailable|"
    ).hexdigest()

    lateral_calls = 0

    async def lateral_thinker(**kwargs: Any) -> LateralResult:  # noqa: ARG001
        nonlocal lateral_calls
        lateral_calls += 1
        return LateralResult(persona="hacker", approach_summary="", text="")

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=_ralph_starter(),
        complete_product=True,
        lateral_thinker=lateral_thinker,
    )

    result = await pipeline.run(state)
    # The handler ran (cache hit), so no Cannot-resume guard fired and the
    # session lands at BLOCKED with the cached persona summary, NOT at
    # "Cannot resume auto pipeline from unstuck_lateral".
    assert result.status == "blocked"
    assert state.last_tool_name == "lateral_thinker"
    assert lateral_calls == 0  # cache hit
    assert "Cannot resume" not in (state.last_error or "")


@pytest.mark.asyncio
async def test_run_resume_from_evaluate_without_evaluator_falls_back_to_blocked(tmp_path) -> None:
    """A session persisted in EVALUATE that resumes in a process where the
    evaluator is NOT wired (e.g. the MCP handler now skips wiring in plugin
    mode) must NOT crash on the ``_run_evaluate`` assert. Instead it must
    fall back to a Phase-2.1-shaped BLOCKED summary — symmetric to the
    UNSTUCK_LATERAL guard."""
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.arm_deadline()
    state.complete_product = True
    seed = _build_seed()
    state.seed_id = seed.metadata.seed_id
    state.seed_artifact = seed.to_dict()
    state.last_grade = "A"
    state.interview_session_id = "interview_stub"
    state.interview_completed = True
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.RUN, "run")
    state.transition(AutoPhase.RALPH_HANDOFF, "ralph")
    state.transition(AutoPhase.EVALUATE, "evaluate")
    # No evaluator/lateral wired (plugin-mode resume scenario)
    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=_ralph_starter(),
        complete_product=True,
        evaluator=None,
        lateral_thinker=None,
    )

    result = await pipeline.run(state)
    # Must NOT crash; lands in BLOCKED with the documented evaluator guard
    assert result.status == "blocked"
    assert state.last_tool_name == "evaluator"
    assert "no evaluator wired" in (state.last_error or "")


@pytest.mark.asyncio
async def test_run_resume_from_evaluate_reaches_handler(tmp_path) -> None:
    """Same fix verified for the EVALUATE phase: P2.1 added the handler
    but the resume guard at the top of run() was not extended, so any
    session blocked in EVALUATE would have been re-blocked with
    "Cannot resume". This locks in the fix for the EVALUATE direction too."""
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.arm_deadline()
    state.complete_product = True
    seed = _build_seed()
    state.seed_id = seed.metadata.seed_id
    state.seed_artifact = seed.to_dict()
    state.last_grade = "A"
    state.interview_session_id = "interview_stub"
    state.interview_completed = True
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.RUN, "run")
    state.transition(AutoPhase.RALPH_HANDOFF, "ralph")
    state.transition(AutoPhase.EVALUATE, "evaluate")
    state.evaluate_artifact = "previously graded artifact"
    state.evaluate_artifact_hash = "deadbeef"
    state.last_qa_passed = True
    state.last_qa_score = 0.95
    state.last_qa_verdict = "pass"

    eval_calls = 0

    async def evaluator(seed: Seed, artifact: str) -> EvaluateResult:  # noqa: ARG001
        nonlocal eval_calls
        eval_calls += 1
        return EvaluateResult(passed=True, score=1.0, verdict="pass")

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=_ralph_starter(),
        complete_product=True,
        evaluator=evaluator,
    )

    # Force the hash mismatch path so a fresh artifact (re-pulled from
    # state.evaluate_artifact since ralph_result_text=None on resume) is
    # re-graded — but with the cached verdict, the cache short-circuits.
    state.evaluate_artifact_hash = None  # so the new compute will set it
    result = await pipeline.run(state)
    assert result.status == "complete"  # handler reached, verdict applied
    assert "Cannot resume" not in (state.last_error or "")


# ---------------------------------------------------------------------------
# Resume idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_lateral_resume_cache_hit_skips_re_invocation(tmp_path) -> None:
    """Re-entering UNSTUCK_LATERAL with the same input hash and a persisted
    persona text must NOT re-invoke the lateral thinker — provided the same
    persona would be picked again.

    RFC #809 Phase 2.2b changed the persona-selection contract so the
    router now skips ``state.personas_invoked``; a naive resume of a
    completed lateral round would therefore pick a DIFFERENT persona on
    the second call (HACKER → CONTRARIAN), produce a different cache
    key, and re-invoke the handler. To exercise the cache-hit path
    deliberately, the test clears ``personas_invoked`` before the resume
    call so the router lands on the same primary persona and the cache
    key matches. This is exactly the contract operators rely on when
    --resume is meant to surface the previously-cached advisory rather
    than spend another persona slot."""
    state = _state_at_run_phase(tmp_path)
    call_count = 0

    async def evaluator(seed: Seed, artifact: str) -> EvaluateResult:  # noqa: ARG001
        return EvaluateResult(
            passed=False, score=0.3, verdict="fail", differences=("Xcode unavailable",)
        )

    async def lateral_thinker(**kwargs: Any) -> LateralResult:  # noqa: ARG001
        nonlocal call_count
        call_count += 1
        return LateralResult(
            persona="hacker", approach_summary="Hacker: workarounds", text="advice text"
        )

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=_ralph_starter(),
        complete_product=True,
        evaluator=evaluator,
        lateral_thinker=lateral_thinker,
    )

    await pipeline.run(state)
    assert call_count == 1
    assert state.last_lateral_text == "advice text"
    first_hash = state.lateral_input_hash

    # Drop the persona-once exclusion so the resume call lands on the
    # same HACKER primary and exercises the cache-hit path. Without
    # this, P2.2b's multi-persona advisory would (correctly) pick a
    # different persona and re-invoke the handler.
    state.personas_invoked = []

    # Simulate resume by re-entering UNSTUCK_LATERAL with the same QA shape.
    state.phase = AutoPhase.UNSTUCK_LATERAL
    result = await pipeline._run_lateral(
        state,
        ledger=_StubLedger(),
        seed=_build_seed(),
        qa_score=0.3,
        qa_verdict="fail",
        qa_differences=("Xcode unavailable",),
        qa_suggestions=(),
        cache_suffix=" [cached]",
        review=None,
        run_subagent=None,
    )
    assert call_count == 1  # NOT incremented
    assert state.lateral_input_hash == first_hash
    assert result.status == "blocked"


@pytest.mark.asyncio
async def test_pipeline_lateral_re_runs_when_qa_differences_change(tmp_path) -> None:
    """A different QA shape produces a different input hash → re-runs lateral."""
    state = _state_at_run_phase(tmp_path)
    call_count = 0

    async def evaluator(seed: Seed, artifact: str) -> EvaluateResult:  # noqa: ARG001
        return EvaluateResult(
            passed=False, score=0.3, verdict="fail", differences=("Xcode unavailable",)
        )

    async def lateral_thinker(**kwargs: Any) -> LateralResult:  # noqa: ARG001
        nonlocal call_count
        call_count += 1
        return LateralResult(
            persona="hacker", approach_summary="Hacker", text=f"advice {call_count}"
        )

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=_ralph_starter(),
        complete_product=True,
        evaluator=evaluator,
        lateral_thinker=lateral_thinker,
    )

    await pipeline.run(state)
    assert call_count == 1

    state.phase = AutoPhase.UNSTUCK_LATERAL
    await pipeline._run_lateral(
        state,
        ledger=_StubLedger(),
        seed=_build_seed(),
        qa_score=0.3,
        qa_verdict="fail",
        qa_differences=("entirely different failure",),
        qa_suggestions=(),
        cache_suffix="",
        review=None,
        run_subagent=None,
    )
    assert call_count == 2
    assert state.last_lateral_text == "advice 2"


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


def test_state_round_trips_lateral_fields(tmp_path) -> None:
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.last_lateral_persona = "hacker"
    state.last_lateral_approach_summary = "Hacker: works around"
    state.last_lateral_text = "lateral prompt body"
    state.lateral_input_hash = "abc123"
    store = AutoStore(tmp_path)
    store.save(state)
    reloaded = store.load(state.auto_session_id)
    assert reloaded.last_lateral_persona == "hacker"
    assert reloaded.last_lateral_approach_summary == "Hacker: works around"
    assert reloaded.last_lateral_text == "lateral prompt body"
    assert reloaded.lateral_input_hash == "abc123"


def test_state_loads_legacy_dump_without_lateral_fields(tmp_path) -> None:
    """Pre-Phase-2.2 state files must load with empty lateral fields."""
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    raw = state.to_dict()
    for key in (
        "last_lateral_persona",
        "last_lateral_approach_summary",
        "last_lateral_text",
        "lateral_input_hash",
    ):
        raw.pop(key, None)
    reloaded = AutoPipelineState.from_dict(raw)
    assert reloaded.last_lateral_persona is None
    assert reloaded.last_lateral_approach_summary is None
    assert reloaded.last_lateral_text is None
    assert reloaded.lateral_input_hash is None


def test_resume_capability_lateral_with_cached_text_is_resumable(tmp_path) -> None:
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.phase = AutoPhase.BLOCKED
    state.last_tool_name = "lateral_thinker"
    state.lateral_input_hash = "abc"
    state.last_lateral_text = "cached advice"
    assert state.resume_capability() is AutoResumeCapability.RESUME


def test_resume_capability_lateral_with_qa_context_only_is_resumable(tmp_path) -> None:
    """No cached lateral output but QA context intact → resume re-runs lateral."""
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.phase = AutoPhase.BLOCKED
    state.last_tool_name = "lateral_thinker"
    state.last_qa_passed = False
    state.last_qa_differences = ["something failed"]
    assert state.resume_capability() is AutoResumeCapability.RESUME


def test_resume_capability_lateral_empty_state_is_none(tmp_path) -> None:
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.phase = AutoPhase.BLOCKED
    state.last_tool_name = "lateral_thinker"
    # No lateral text, no QA differences
    assert state.resume_capability() is AutoResumeCapability.NONE


def test_resume_capability_lateral_with_qa_suggestions_only_is_resumable(tmp_path) -> None:
    """A QA fail with suggestions-only (no differences) still feeds
    ``_run_lateral``'s ``problem_context``, so resume capability must
    report RESUME — previously this branch required differences and
    suppressed the resume hint."""
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.phase = AutoPhase.BLOCKED
    state.last_tool_name = "lateral_thinker"
    state.last_qa_passed = False
    state.last_qa_differences = []
    state.last_qa_suggestions = ["use --strict markers"]
    assert state.resume_capability() is AutoResumeCapability.RESUME


@pytest.mark.asyncio
async def test_lateral_cache_invalidated_when_evaluate_artifact_changes(tmp_path) -> None:
    """The lateral cache references the evaluate artifact via its
    ``current_approach`` payload. A new EVALUATE round on a different
    artifact must invalidate the lateral cache so the persona's advice
    is regenerated against the actual artifact, not stale advice from
    the previous round."""
    state = _state_at_run_phase(tmp_path)
    eval_calls = 0
    lateral_calls = 0

    async def evaluator(seed: Seed, artifact: str) -> EvaluateResult:  # noqa: ARG001
        nonlocal eval_calls
        eval_calls += 1
        # P2.2b note: the differences string must DIFFER between rounds so
        # the same-fingerprint-twice guard does not block round 2 before
        # lateral is even invoked. The cache invalidation under test here
        # (lateral cache flushed when evaluate_artifact_hash changes) is
        # orthogonal to the fingerprint guard — both must be exercised on
        # genuinely distinct failure shapes.
        return EvaluateResult(
            passed=False,
            score=0.3,
            verdict="fail",
            differences=(f"failure on round {eval_calls}",),
        )

    async def lateral_thinker(**kwargs: Any) -> LateralResult:  # noqa: ARG001
        nonlocal lateral_calls
        lateral_calls += 1
        return LateralResult(
            persona="hacker",
            approach_summary="Hacker",
            text=f"advice for round {lateral_calls}",
        )

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=_ralph_starter(result_text="artifact A"),
        complete_product=True,
        evaluator=evaluator,
        lateral_thinker=lateral_thinker,
    )

    # First run: artifact A → QA fail → lateral called, advice cached
    await pipeline.run(state)
    assert eval_calls == 1
    assert lateral_calls == 1
    a_lateral_hash = state.lateral_input_hash
    assert state.last_lateral_text == "advice for round 1"

    # Drop the persona-once exclusion so round 2 picks the same primary
    # persona (HACKER) and isolates the lateral-cache invalidation under
    # test from P2.2b's multi-persona advisory path.
    state.personas_invoked = []

    # Now simulate a second EVALUATE call with a DIFFERENT artifact and
    # a distinct (per the evaluator above) failure shape. The
    # evaluate-artifact hash change must invalidate the lateral cache;
    # otherwise "advice for round 1" (about artifact A) would be reused
    # against artifact B.
    state.phase = AutoPhase.EVALUATE
    await pipeline._run_evaluate(
        state,
        ledger=_StubLedger(),
        seed=_build_seed(),
        review=None,
        run_subagent=None,
        ralph_result_text="artifact B (entirely different)",
        stop_reason=None,
    )
    # Lateral was re-invoked because the artifact-hash changed flushed
    # the lateral cache too.
    assert lateral_calls == 2
    assert state.last_lateral_text == "advice for round 2"
    assert state.lateral_input_hash != a_lateral_hash


def test_recoverable_phase_for_lateral_thinker_tool() -> None:
    from ouroboros.auto.pipeline import _recoverable_phase_for_tool

    assert _recoverable_phase_for_tool("lateral_thinker") is AutoPhase.UNSTUCK_LATERAL


# ---------------------------------------------------------------------------
# HandlerLateralThinker adapter unit
# ---------------------------------------------------------------------------


class _StubLateralHandler:
    """Stand-in for ``LateralThinkHandler`` capturing the call payload."""

    def __init__(self, meta: dict[str, Any] | None = None, is_err: bool = False) -> None:
        self._meta = meta or {
            "persona": "hacker",
            "approach_summary": "Hacker: Finds unconventional workarounds",
            "questions_count": 5,
        }
        self._text = "# Lateral Thinking: Hacker\n\nReframing...\n\n- Q1\n- Q2"
        self._is_err = is_err
        self.last_arguments: dict[str, Any] | None = None

    async def handle(self, arguments: dict[str, Any]):  # noqa: ANN201
        self.last_arguments = arguments
        from ouroboros.core.types import Result
        from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult

        if self._is_err:
            from ouroboros.mcp.errors import MCPToolError

            return Result.err(
                MCPToolError("lateral unavailable", tool_name="ouroboros_lateral_think")
            )
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text=self._text),),
                is_error=False,
                meta=self._meta,
            )
        )


@pytest.mark.asyncio
async def test_handler_lateral_thinker_builds_problem_context_and_returns_typed_result() -> None:
    stub = _StubLateralHandler()
    thinker = HandlerLateralThinker(stub)

    result = await thinker(
        persona=ThinkingPersona.HACKER,
        qa_differences=("Xcode is not available",),
        qa_suggestions=("try CLI build",),
        run_artifact="stdout: build failed",
    )

    assert result.persona == "hacker"
    assert result.approach_summary == "Hacker: Finds unconventional workarounds"
    assert "Reframing" in result.text
    assert result.error is None

    args = stub.last_arguments
    assert args is not None
    assert args["persona"] == "hacker"
    # Problem context summarises the QA verdict
    assert "EVALUATE failed" in args["problem_context"]
    assert "Xcode is not available" in args["problem_context"]
    assert "try CLI build" in args["problem_context"]
    # Current approach carries the artifact preview
    assert "build failed" in args["current_approach"]


@pytest.mark.asyncio
async def test_handler_lateral_thinker_maps_error_to_lateral_result() -> None:
    stub = _StubLateralHandler(is_err=True)
    thinker = HandlerLateralThinker(stub)
    result = await thinker(
        persona=ThinkingPersona.CONTRARIAN,
        qa_differences=("any",),
        qa_suggestions=(),
        run_artifact="",
    )
    assert result.persona == "contrarian"
    assert result.text == ""
    assert result.error is not None
    assert "lateral unavailable" in result.error.lower()


@pytest.mark.asyncio
async def test_handler_lateral_thinker_detects_plugin_delegation_envelope() -> None:
    """In plugin / multi-persona mode ``LateralThinkHandler`` returns a
    delegation envelope (``status="delegated_to_subagent"`` or
    ``dispatch_mode="plugin"``). The adapter must NOT persist the envelope
    payload as ``last_lateral_text`` — instead it returns an error result
    so the pipeline blocks with a clear ``"plugin-delegation"`` reason
    rather than surfacing placeholder advice."""
    stub = _StubLateralHandler(
        meta={
            "status": "delegated_to_subagent",
            "dispatch_mode": "plugin",
            "persona_count": 5,
        }
    )
    thinker = HandlerLateralThinker(stub)

    result = await thinker(
        persona=ThinkingPersona.HACKER,
        qa_differences=("any",),
        qa_suggestions=(),
        run_artifact="",
    )

    assert result.error is not None
    assert "plugin-delegation" in result.error.lower()
    assert result.text == ""


@pytest.mark.asyncio
async def test_handler_lateral_thinker_truncates_long_artifact() -> None:
    """Run artifact preview is bounded at 4_000 chars so a huge stdout dump
    doesn't dominate the token budget."""
    stub = _StubLateralHandler()
    thinker = HandlerLateralThinker(stub)
    long_artifact = "x" * 50_000

    await thinker(
        persona=ThinkingPersona.HACKER,
        qa_differences=("any",),
        qa_suggestions=(),
        run_artifact=long_artifact,
    )

    args = stub.last_arguments
    assert args is not None
    # Approach text fits the truncation marker
    assert "truncated" in args["current_approach"]
    assert str(50_000) in args["current_approach"]


# ---------------------------------------------------------------------------
# RFC #809 Phase 2.2b — recovery-loop guards
# ---------------------------------------------------------------------------
#
# These tests exercise the four deterministic guards that bound the
# EVALUATE ⇄ UNSTUCK_LATERAL retry cycle: round budget, same-fingerprint
# twice, persona exhaustion, and the operator-choice cue surfaced in the
# final BLOCKED message. SEED_REGENERATE is deliberately NOT exercised —
# see the AutoPhase deferral comment in state.py for the spec-first
# rationale (system must not silently rewrite the spec the user agreed to).
# Wall-clock budget reuses the existing ``state.deadline_at`` /
# ``_enforce_deadline`` machinery already exercised by the Phase 2.1
# tests, so no new test is needed here.


@pytest.mark.asyncio
async def test_recovery_round_budget_blocks_after_max(tmp_path) -> None:
    """When ``evaluate_round`` has already reached ``MAX_EVALUATE_ROUNDS``
    on entry, the next fresh evaluator call must NOT spend another budget
    cycle. The pipeline marks BLOCKED with the operator-choice cue."""
    from ouroboros.auto.state import MAX_EVALUATE_ROUNDS

    state = _state_at_run_phase(tmp_path)
    state.evaluate_round = MAX_EVALUATE_ROUNDS  # at-the-edge; next call exceeds
    eval_calls = 0

    async def evaluator(seed: Seed, artifact: str) -> EvaluateResult:  # noqa: ARG001
        nonlocal eval_calls
        eval_calls += 1
        return EvaluateResult(passed=True, score=0.95, verdict="pass")

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=_ralph_starter(result_text="artifact"),
        complete_product=True,
        evaluator=evaluator,
    )

    result = await pipeline.run(state)

    assert eval_calls == 0  # evaluator NOT invoked once budget exceeded
    assert result.status == "blocked"
    assert "MAX_EVALUATE_ROUNDS" in (state.last_error or "")
    assert "re-interview" in (state.last_error or "")


@pytest.mark.asyncio
async def test_recovery_same_fingerprint_twice_blocks(tmp_path) -> None:
    """Two consecutive EVALUATE rounds with the same QA-fail fingerprint
    means lateral advice did not move the needle on the failure surface.
    The pipeline blocks rather than spending another round."""
    state = _state_at_run_phase(tmp_path)
    # Pre-seed a matching fingerprint as if a previous round had already
    # recorded it. The next failing evaluator call computes the same
    # fingerprint and trips the guard.
    fingerprint_input = "identical fail::"
    import hashlib

    pre_seeded = hashlib.sha256(fingerprint_input.encode("utf-8")).hexdigest()[:16]
    state.failure_fingerprints = [pre_seeded]

    async def evaluator(seed: Seed, artifact: str) -> EvaluateResult:  # noqa: ARG001
        return EvaluateResult(
            passed=False, score=0.3, verdict="fail", differences=("identical fail",)
        )

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=_ralph_starter(result_text="artifact"),
        complete_product=True,
        evaluator=evaluator,
    )

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert "fingerprint" in (state.last_error or "").lower()
    assert "re-interview" in (state.last_error or "")
    # The pre-seeded fingerprint is still there; we did not double-record.
    assert state.failure_fingerprints == [pre_seeded]


@pytest.mark.asyncio
async def test_recovery_personas_exhausted_blocks(tmp_path) -> None:
    """When every persona in the deterministic fallback chain has already
    been routed in this session, the next lateral call returns ``None``
    and the pipeline blocks with a ``"personas exhausted"`` reason."""
    state = _state_at_run_phase(tmp_path)
    # All five personas in the fallback chain marked as already routed.
    state.personas_invoked = [
        ThinkingPersona.HACKER.value,
        ThinkingPersona.ARCHITECT.value,
        ThinkingPersona.RESEARCHER.value,
        ThinkingPersona.SIMPLIFIER.value,
        ThinkingPersona.CONTRARIAN.value,
    ]
    lateral_calls = 0

    async def evaluator(seed: Seed, artifact: str) -> EvaluateResult:  # noqa: ARG001
        return EvaluateResult(
            passed=False, score=0.3, verdict="fail", differences=("Xcode unavailable",)
        )

    async def lateral_thinker(**kwargs: Any) -> LateralResult:  # noqa: ARG001 # pragma: no cover
        nonlocal lateral_calls
        lateral_calls += 1
        return LateralResult(persona="hacker", approach_summary="X", text="Y")

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=_ralph_starter(result_text="artifact"),
        complete_product=True,
        evaluator=evaluator,
        lateral_thinker=lateral_thinker,
    )

    result = await pipeline.run(state)

    assert lateral_calls == 0  # router returned None → no handler call
    assert result.status == "blocked"
    last_error = state.last_error or ""
    assert "personas exhausted" in last_error.lower()
    assert "re-interview" in last_error


@pytest.mark.asyncio
async def test_recovery_personas_invoked_persists_after_lateral(tmp_path) -> None:
    """After a successful lateral advisory call, the picked persona is
    appended to ``state.personas_invoked`` so a subsequent --resume routes
    a different persona instead of recycling the same advice."""
    state = _state_at_run_phase(tmp_path)

    async def evaluator(seed: Seed, artifact: str) -> EvaluateResult:  # noqa: ARG001
        return EvaluateResult(
            passed=False, score=0.3, verdict="fail", differences=("Xcode unavailable",)
        )

    async def lateral_thinker(**kwargs: Any) -> LateralResult:
        return LateralResult(
            persona=kwargs["persona"].value,
            approach_summary="advice",
            text="advice body",
        )

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=_ralph_starter(result_text="artifact"),
        complete_product=True,
        evaluator=evaluator,
        lateral_thinker=lateral_thinker,
    )

    await pipeline.run(state)

    # The persona selected on this round is now persisted.
    assert state.personas_invoked == [ThinkingPersona.HACKER.value]


@pytest.mark.asyncio
async def test_recovery_blocked_message_includes_operator_choices(tmp_path) -> None:
    """Every recovery-loop BLOCKED message surfaces the two functional
    operator choices (re-interview / abandon) so the operator's next move
    is on-screen rather than buried in QA differences. "Relax AC via
    edited seed" is intentionally NOT advertised — late-phase resume
    reconstructs the Seed from ``state.seed_artifact`` and ignores edits
    to the on-disk seed file, so that path is non-functional today and
    would mislead operators if surfaced here."""
    state = _state_at_run_phase(tmp_path)

    async def evaluator(seed: Seed, artifact: str) -> EvaluateResult:  # noqa: ARG001
        return EvaluateResult(
            passed=False, score=0.3, verdict="fail", differences=("Xcode unavailable",)
        )

    async def lateral_thinker(**kwargs: Any) -> LateralResult:  # noqa: ARG001
        return LateralResult(persona="hacker", approach_summary="brew install xcode", text="…")

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=_ralph_starter(result_text="artifact"),
        complete_product=True,
        evaluator=evaluator,
        lateral_thinker=lateral_thinker,
    )

    await pipeline.run(state)

    msg = state.last_error or ""
    assert "re-interview" in msg
    assert "abandon" in msg
    # The earlier misleading guidance (edit the seed file and --resume)
    # must NOT appear — late-phase resume ignores on-disk seed edits.
    assert "edited seed" not in msg
    assert "relax AC" not in msg


# ---------------------------------------------------------------------------
# RFC #809 Phase 2.2b — sticky guard semantics (bot review #3 BLOCKING)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sticky_guard_prevents_evaluate_cache_fastpath_on_resume(tmp_path) -> None:
    """When a recovery guard has tripped, a resume that re-enters
    ``_run_evaluate`` must NOT honor the cached failing verdict and fall
    through to ``_finalize_evaluate`` (which would transition to
    ``UNSTUCK_LATERAL`` and spend another persona slot). The cache
    fast-path is bypassed and the original exhaustion blocker is
    surfaced verbatim."""
    state = _state_at_run_phase(tmp_path)
    state.phase = AutoPhase.EVALUATE
    state.recovery_guard_tripped = "duplicate_fingerprint"
    state.last_error = "recovery loop: same QA-fail fingerprint twice (abc); next: ..."
    # Plant a cached failing verdict that the legacy fast-path would honour
    state.evaluate_artifact = "stale artifact"
    import hashlib

    state.evaluate_artifact_hash = hashlib.sha256(b"stale artifact").hexdigest()
    state.last_qa_passed = False
    state.last_qa_score = 0.3
    state.last_qa_verdict = "fail"
    state.last_qa_differences = ["frozen"]

    eval_calls = 0
    lateral_calls = 0

    async def evaluator(seed: Seed, artifact: str) -> EvaluateResult:  # noqa: ARG001 # pragma: no cover
        nonlocal eval_calls
        eval_calls += 1
        return EvaluateResult(passed=True, score=0.99, verdict="pass")

    async def lateral_thinker(**kwargs: Any) -> LateralResult:  # noqa: ARG001 # pragma: no cover
        nonlocal lateral_calls
        lateral_calls += 1
        return LateralResult(persona="hacker", approach_summary="X", text="Y")

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=_ralph_starter(result_text="artifact"),
        complete_product=True,
        evaluator=evaluator,
        lateral_thinker=lateral_thinker,
    )

    result = await pipeline._run_evaluate(
        state,
        ledger=_StubLedger(),
        seed=_build_seed(),
        review=None,
        run_subagent=None,
        ralph_result_text=None,
        stop_reason=None,
    )

    # Neither the evaluator NOR the lateral handler was invoked: the
    # sticky guard short-circuited everything.
    assert eval_calls == 0
    assert lateral_calls == 0
    assert result.status == "blocked"
    assert "fingerprint" in (state.last_error or "").lower()


@pytest.mark.asyncio
async def test_sticky_guard_prevents_lateral_cache_fastpath_on_resume(tmp_path) -> None:
    """Resume that lands directly in ``UNSTUCK_LATERAL`` (via
    ``_recoverable_phase_for_tool("lateral_thinker")``) also bypasses
    the lateral cache when a guard has tripped — the persona-once
    contract cannot be repaired by spending another slot."""
    state = _state_at_run_phase(tmp_path)
    state.phase = AutoPhase.UNSTUCK_LATERAL
    state.recovery_guard_tripped = "personas_exhausted"
    state.last_error = "recovery loop: all lateral personas exhausted; next: ..."

    lateral_calls = 0

    async def evaluator(seed: Seed, artifact: str) -> EvaluateResult:  # noqa: ARG001 # pragma: no cover
        return EvaluateResult(passed=True, score=1.0, verdict="pass")

    async def lateral_thinker(**kwargs: Any) -> LateralResult:  # noqa: ARG001 # pragma: no cover
        nonlocal lateral_calls
        lateral_calls += 1
        return LateralResult(persona="hacker", approach_summary="X", text="Y")

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=_ralph_starter(result_text="artifact"),
        complete_product=True,
        evaluator=evaluator,
        lateral_thinker=lateral_thinker,
    )

    result = await pipeline._run_lateral(
        state,
        ledger=_StubLedger(),
        seed=_build_seed(),
        qa_score=0.3,
        qa_verdict="fail",
        qa_differences=("any",),
        qa_suggestions=(),
        cache_suffix="",
        review=None,
        run_subagent=None,
    )

    assert lateral_calls == 0
    assert result.status == "blocked"
    assert "personas exhausted" in (state.last_error or "").lower()


@pytest.mark.asyncio
async def test_round_budget_guard_sets_sticky_flag(tmp_path) -> None:
    """The round-budget guard persists ``recovery_guard_tripped`` so a
    subsequent resume cannot un-exhaust the loop by waiting out the
    counter."""
    state = _state_at_run_phase(tmp_path)
    from ouroboros.auto.state import MAX_EVALUATE_ROUNDS

    state.evaluate_round = MAX_EVALUATE_ROUNDS

    async def evaluator(seed: Seed, artifact: str) -> EvaluateResult:  # noqa: ARG001
        return EvaluateResult(passed=True, score=1.0, verdict="pass")

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=_ralph_starter(result_text="artifact"),
        complete_product=True,
        evaluator=evaluator,
    )

    await pipeline.run(state)

    assert state.recovery_guard_tripped == "round_budget"


@pytest.mark.asyncio
async def test_fingerprint_guard_sets_sticky_flag(tmp_path) -> None:
    """Same-fingerprint-twice guard also persists the sticky flag."""
    state = _state_at_run_phase(tmp_path)
    import hashlib

    fp = hashlib.sha256(b"frozen fail::").hexdigest()[:16]
    state.failure_fingerprints = [fp]

    async def evaluator(seed: Seed, artifact: str) -> EvaluateResult:  # noqa: ARG001
        return EvaluateResult(
            passed=False, score=0.3, verdict="fail", differences=("frozen fail",)
        )

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=_ralph_starter(result_text="artifact"),
        complete_product=True,
        evaluator=evaluator,
    )

    await pipeline.run(state)

    assert state.recovery_guard_tripped == "duplicate_fingerprint"


@pytest.mark.asyncio
async def test_personas_exhausted_guard_sets_sticky_flag(tmp_path) -> None:
    """Persona-chain-exhausted guard also persists the sticky flag."""
    state = _state_at_run_phase(tmp_path)
    state.personas_invoked = [
        ThinkingPersona.HACKER.value,
        ThinkingPersona.ARCHITECT.value,
        ThinkingPersona.RESEARCHER.value,
        ThinkingPersona.SIMPLIFIER.value,
        ThinkingPersona.CONTRARIAN.value,
    ]

    async def evaluator(seed: Seed, artifact: str) -> EvaluateResult:  # noqa: ARG001
        return EvaluateResult(
            passed=False, score=0.3, verdict="fail", differences=("any",)
        )

    async def lateral_thinker(**kwargs: Any) -> LateralResult:  # noqa: ARG001 # pragma: no cover
        return LateralResult(persona="hacker", approach_summary="X", text="Y")

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=_ralph_starter(result_text="artifact"),
        complete_product=True,
        evaluator=evaluator,
        lateral_thinker=lateral_thinker,
    )

    await pipeline.run(state)

    assert state.recovery_guard_tripped == "personas_exhausted"
