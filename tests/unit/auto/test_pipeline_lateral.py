"""Phase 2.2 tests — UNSTUCK_LATERAL phase + HandlerLateralThinker + persona routing.

Covers RFC #809 Phase 2.2: when ``ouroboros_qa`` rules a run artifact did
not satisfy the Seed AC, the pipeline picks a persona deterministically
from the QA-failure shape, invokes ``ouroboros_lateral_think`` for a
reframing prompt, and surfaces the persona's output on the BLOCKED state.
"""

from __future__ import annotations

from typing import Any

import pytest

from ouroboros.auto.adapters import (
    HandlerLateralThinker,
)
from ouroboros.auto.interview_driver import AutoInterviewResult
from ouroboros.auto.lateral_routing import (
    classify_qa_failure_to_pattern,
    select_persona_for_qa_failure,
)
from ouroboros.auto.pipeline import _seed_with_recovery_constraint
from ouroboros.auto.recovery_plan import AutoRecoveryPlan, RecoveryPlanAction
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


def test_lateral_recovery_constraint_sanitizes_persona_transcripts() -> None:
    seed = _build_seed()
    plan = AutoRecoveryPlan(
        action=RecoveryPlanAction.RALPH_REDISPATCH,
        safe_to_redispatch=True,
        reason="QA failed and lateral recovery advice is available.",
        qa_score=0.58,
        qa_verdict="revise",
        differences=("bad contract",),
        suggestions=("fix contract",),
        persona="hacker",
        instruction=(
            "## Persona: Hacker\n"
            "EVALUATE failed. Current Approach (Not Working): Most recent run artifact..."
        ),
    )

    recovered = _seed_with_recovery_constraint(seed, plan)
    constraints = "\n".join(recovered.constraints)

    assert "## Persona" not in constraints
    assert "Hacker" not in constraints
    assert "Most recent run artifact" not in constraints
    assert "Do not copy recovery persona prompts" in constraints


def test_lateral_recovery_constraint_preserves_clean_prefix_with_dirty_body() -> None:
    seed = _build_seed()
    plan = AutoRecoveryPlan(
        action=RecoveryPlanAction.RALPH_REDISPATCH,
        safe_to_redispatch=True,
        reason="QA failed and lateral recovery advice is available.",
        qa_score=0.58,
        qa_verdict="revise",
        differences=("bad contract",),
        suggestions=("fix contract",),
        persona="hacker",
        instruction=(
            "Rewrite ACs as exact command scenarios.\n\n"
            "## Persona: Hacker\n"
            "EVALUATE failed. Current Approach (Not Working): Most recent run artifact..."
        ),
    )

    recovered = _seed_with_recovery_constraint(seed, plan)
    constraints = "\n".join(recovered.constraints)

    assert "Rewrite ACs as exact command scenarios" in constraints
    assert "## Persona" not in constraints
    assert "Most recent run artifact" not in constraints


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
    state.transition(AutoPhase.RUN, "run")
    return state


def _stale_recovery_plan() -> dict[str, Any]:
    return {
        "action": "ralph_redispatch",
        "safe_to_redispatch": True,
        "reason": "stale successful lateral advice from an earlier round",
        "qa_score": 0.2,
        "qa_verdict": "fail",
        "differences": ["old failure"],
        "suggestions": ["old suggestion"],
        "persona": "hacker",
        "instruction": "old redispatch instruction",
    }


async def _run_starter_ok(_seed: Seed, **kwargs: Any) -> dict[str, Any]:
    return {
        "job_id": "job_run_001",
        "session_id": "exec_session_001",
        "execution_id": "execution_001",
    }


async def _seed_generator_unused(_session_id: str) -> Seed:  # pragma: no cover
    raise AssertionError("seed generator should not run when seed_artifact is set")


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

    def assumption_sources(self) -> list[Any]:
        # PR-C2 / #1157: stub satisfies the additive provenance surface
        # consumed by ``AutoPipeline._result()``. Returns an empty list for
        # the same reason ``assumptions()`` does — this stub is for tests
        # that do not exercise ledger content.
        return []

    def non_goals(self) -> list[str]:
        return []


# ---------------------------------------------------------------------------
# State-machine sanity
# ---------------------------------------------------------------------------


def test_unstuck_lateral_phase_in_allowed_transitions() -> None:
    assert AutoPhase.UNSTUCK_LATERAL in _ALLOWED_TRANSITIONS[AutoPhase.EVALUATE]
    # RFC #809 Phase 2.2b — UNSTUCK_LATERAL became a retry dispatcher:
    # into a fresh RALPH_HANDOFF that returns through EVALUATE, directly back
    # to EVALUATE for recovered artifacts, or BLOCKED when a recovery guard
    # trips. SEED_REGENERATE is intentionally
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
        AutoPhase.RALPH_HANDOFF,
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


def test_state_round_trips_lateral_fields(tmp_path) -> None:
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.last_lateral_persona = "hacker"
    state.last_lateral_approach_summary = "Hacker: works around"
    state.last_lateral_text = "lateral prompt body"
    state.lateral_input_hash = "abc123"
    state.last_recovery_plan = {
        "action": "ralph_redispatch",
        "safe_to_redispatch": True,
        "reason": "QA failed and lateral advice is available",
        "qa_score": 0.3,
        "qa_verdict": "fail",
        "differences": ["missing stdout"],
        "suggestions": ["run cli"],
        "persona": "hacker",
        "instruction": "Run the CLI directly",
    }
    store = AutoStore(tmp_path)
    store.save(state)
    reloaded = store.load(state.auto_session_id)
    assert reloaded.last_lateral_persona == "hacker"
    assert reloaded.last_lateral_approach_summary == "Hacker: works around"
    assert reloaded.last_lateral_text == "lateral prompt body"
    assert reloaded.lateral_input_hash == "abc123"
    assert reloaded.last_recovery_plan is not None
    assert reloaded.last_recovery_plan["action"] == "ralph_redispatch"


def test_state_loads_legacy_dump_without_lateral_fields(tmp_path) -> None:
    """Pre-Phase-2.2 state files must load with empty lateral fields."""
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    raw = state.to_dict()
    for key in (
        "last_lateral_persona",
        "last_lateral_approach_summary",
        "last_lateral_text",
        "lateral_input_hash",
        "last_recovery_plan",
    ):
        raw.pop(key, None)
    reloaded = AutoPipelineState.from_dict(raw)
    assert reloaded.last_lateral_persona is None
    assert reloaded.last_lateral_approach_summary is None
    assert reloaded.last_lateral_text is None
    assert reloaded.lateral_input_hash is None
    assert reloaded.last_recovery_plan is None


def test_resume_capability_lateral_empty_state_is_none(tmp_path) -> None:
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.phase = AutoPhase.BLOCKED
    state.last_tool_name = "lateral_thinker"
    # No lateral text, no QA differences
    assert state.resume_capability() is AutoResumeCapability.NONE


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


def test_resume_capability_is_none_when_recovery_guard_tripped(tmp_path) -> None:
    """``resume_capability`` must return NONE when ``recovery_guard_tripped``
    is set: a guarded BLOCKED session cannot make forward progress on
    --resume (``_run_evaluate`` / ``_run_lateral`` short-circuit back to
    BLOCKED), so advertising the session as resumable in CLI/MCP status
    surfaces is a user-facing contract bug. This test pins the
    contract for all three guard tags."""
    for tag in ("round_budget", "duplicate_fingerprint", "personas_exhausted"):
        state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
        state.phase = AutoPhase.BLOCKED
        state.last_tool_name = "evaluator"
        # Plant cache fields that would normally make the session
        # advertise RESUME — the guard tag must override.
        state.evaluate_artifact = "x"
        state.evaluate_artifact_hash = "abc"
        state.last_qa_passed = False
        state.recovery_guard_tripped = tag
        assert state.resume_capability() is AutoResumeCapability.NONE, tag
