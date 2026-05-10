"""Phase 2.1 tests — EVALUATE phase + HandlerEvaluator + state plumbing.

Covers RFC #809 Phase 2.1: a successful Ralph terminal verdict no longer goes
straight to COMPLETE when an evaluator is wired. Instead the pipeline grades
the run artifact against the Seed's acceptance criteria via ``ouroboros_qa``
and only transitions to COMPLETE on QA pass. QA fail → BLOCKED with the
verdict summary in ``state.last_error``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ouroboros.auto.adapters import EvaluateResult, HandlerEvaluator
from ouroboros.auto.grading import GradeResult, SeedGrade
from ouroboros.auto.interview_driver import AutoInterviewResult
from ouroboros.auto.pipeline import AutoPipeline
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

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_seed(seed_id: str = "seed_eval_001") -> Seed:
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


class _StubLedger:
    """Minimal ledger stub for direct ``_run_evaluate`` calls.

    The pipeline only calls ``summary()``, ``assumptions()``, and ``non_goals()``
    on the ledger inside ``_result()`` (via ``ledger.summary()``). All return
    empty so the test focuses on EVALUATE transition behaviour rather than
    ledger-summary coverage."""

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


# ---------------------------------------------------------------------------
# State-machine sanity
# ---------------------------------------------------------------------------


def test_evaluate_phase_in_allowed_transitions() -> None:
    assert AutoPhase.EVALUATE in _ALLOWED_TRANSITIONS[AutoPhase.RALPH_HANDOFF]
    assert _ALLOWED_TRANSITIONS[AutoPhase.EVALUATE] == {
        AutoPhase.COMPLETE,
        AutoPhase.BLOCKED,
        AutoPhase.FAILED,
    }
    # Recovery from terminal-but-resumable phases must be able to re-enter EVALUATE
    assert AutoPhase.EVALUATE in _ALLOWED_TRANSITIONS[AutoPhase.BLOCKED]
    assert AutoPhase.EVALUATE in _ALLOWED_TRANSITIONS[AutoPhase.FAILED]


# ---------------------------------------------------------------------------
# Pipeline EVALUATE happy/fail/timeout paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_evaluate_pass_transitions_to_complete(tmp_path) -> None:
    state = _state_at_run_phase(tmp_path)
    eval_calls: list[tuple[Seed, str]] = []

    async def evaluator(seed: Seed, artifact: str) -> EvaluateResult:
        eval_calls.append((seed, artifact))
        return EvaluateResult(
            passed=True,
            score=0.92,
            verdict="pass",
            differences=(),
            suggestions=(),
        )

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=_ralph_starter(),
        complete_product=True,
        evaluator=evaluator,
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert state.phase is AutoPhase.COMPLETE
    assert state.last_qa_verdict == "pass"
    assert state.last_qa_score == 0.92
    assert result.last_qa_verdict == "pass"
    assert result.last_qa_score == 0.92
    assert len(eval_calls) == 1
    assert "stdout: ok" in eval_calls[0][1]
    assert state.evaluate_artifact_hash is not None


@pytest.mark.asyncio
async def test_pipeline_evaluate_fail_transitions_to_blocked(tmp_path) -> None:
    state = _state_at_run_phase(tmp_path)

    async def evaluator(seed: Seed, artifact: str) -> EvaluateResult:  # noqa: ARG001
        return EvaluateResult(
            passed=False,
            score=0.42,
            verdict="revise",
            differences=("missing stable stdout", "wrong exit code"),
            suggestions=("emit final newline", "return 0 on success"),
        )

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=_ralph_starter(),
        complete_product=True,
        evaluator=evaluator,
    )

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert state.phase is AutoPhase.BLOCKED
    assert state.last_qa_verdict == "revise"
    assert state.last_qa_score == 0.42
    assert state.last_tool_name == "evaluator"
    assert "missing stable stdout" in (state.last_error or "")
    assert "emit final newline" in (state.last_error or "")
    assert result.blocker is not None


@pytest.mark.asyncio
async def test_pipeline_evaluate_timeout_blocks(tmp_path) -> None:
    state = _state_at_run_phase(tmp_path)
    state.timeout_seconds_by_phase[AutoPhase.EVALUATE.value] = 1

    async def hanging_evaluator(seed: Seed, artifact: str) -> EvaluateResult:  # noqa: ARG001
        await asyncio.sleep(10)
        return EvaluateResult(passed=True, score=1.0, verdict="pass")

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=_ralph_starter(),
        complete_product=True,
        evaluator=hanging_evaluator,
    )

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert state.last_tool_name == "evaluator"
    assert "timed out" in (state.last_error or "")
    # No verdict was captured because the call timed out
    assert state.last_qa_verdict is None


@pytest.mark.asyncio
async def test_pipeline_evaluate_handler_error_blocks(tmp_path) -> None:
    state = _state_at_run_phase(tmp_path)

    async def transient_evaluator(seed: Seed, artifact: str) -> EvaluateResult:  # noqa: ARG001
        return EvaluateResult(
            passed=False, score=0.0, verdict="fail", error="QA service unreachable"
        )

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=_ralph_starter(),
        complete_product=True,
        evaluator=transient_evaluator,
    )

    result = await pipeline.run(state)

    assert result.status == "blocked"
    assert state.last_tool_name == "evaluator"
    assert "QA service unreachable" in (state.last_error or "")


# ---------------------------------------------------------------------------
# Opt-in / wiring guards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_evaluate_only_fires_when_complete_product_set(tmp_path) -> None:
    """When ``complete_product`` is False, EVALUATE must NOT run even if an
    evaluator is wired. The pipeline goes RUN → COMPLETE directly (the run is
    async and there is no synchronous artifact to grade)."""
    state = _state_at_run_phase(tmp_path)
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
        ralph_starter=None,  # no chain
        complete_product=False,
        evaluator=evaluator,
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert eval_calls == 0
    assert state.last_qa_verdict is None


@pytest.mark.asyncio
async def test_pipeline_evaluate_skipped_when_evaluator_none(tmp_path) -> None:
    """``complete_product=True`` without an evaluator wired falls through to
    legacy RALPH_HANDOFF → COMPLETE behaviour."""
    state = _state_at_run_phase(tmp_path)

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=_ralph_starter(),
        complete_product=True,
        evaluator=None,
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert state.last_qa_verdict is None
    # The pipeline never touched EVALUATE
    assert state.phase is AutoPhase.COMPLETE


@pytest.mark.asyncio
async def test_pipeline_evaluate_skipped_when_no_result_text(tmp_path) -> None:
    """If Ralph terminal meta lacks ``result_text``, EVALUATE has nothing to
    grade and the pipeline falls back to COMPLETE without invoking the
    evaluator."""
    state = _state_at_run_phase(tmp_path)
    eval_calls = 0

    async def evaluator(seed: Seed, artifact: str) -> EvaluateResult:  # noqa: ARG001
        nonlocal eval_calls
        eval_calls += 1
        return EvaluateResult(passed=True, score=1.0, verdict="pass")

    async def ralph_starter(seed: Seed, **kwargs: Any) -> dict[str, Any]:  # noqa: ARG001
        return {
            "job_id": "job_ralph_002",
            "lineage_id": kwargs["lineage_id"],
            "dispatch_mode": "job",
            "terminal_status": "completed",
            "stop_reason": "qa passed",
            # No result_text key
        }

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=ralph_starter,
        complete_product=True,
        evaluator=evaluator,
    )

    result = await pipeline.run(state)

    assert result.status == "complete"
    assert eval_calls == 0


# ---------------------------------------------------------------------------
# Resume idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_evaluate_uses_cached_verdict_on_resume(tmp_path) -> None:
    """A second pass with the same artifact hash and a persisted verdict
    must NOT re-invoke the evaluator (LLM call is cached on disk)."""
    state = _state_at_run_phase(tmp_path)
    eval_calls = 0

    async def evaluator(seed: Seed, artifact: str) -> EvaluateResult:  # noqa: ARG001
        nonlocal eval_calls
        eval_calls += 1
        return EvaluateResult(passed=True, score=0.91, verdict="pass")

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=_ralph_starter(),
        complete_product=True,
        evaluator=evaluator,
    )

    # First run — evaluator called once.
    await pipeline.run(state)
    assert eval_calls == 1

    # Simulate resume in EVALUATE phase. Bypass ``state.transition`` since
    # COMPLETE is terminal in production; real resume reloads a state file
    # that was persisted while phase=EVALUATE before COMPLETE was reached.
    state.phase = AutoPhase.EVALUATE

    seed = _build_seed()
    result = await pipeline._run_evaluate(
        state,
        ledger=_StubLedger(),
        seed=seed,
        review=None,
        run_subagent=None,
        ralph_result_text=None,  # caller passes None on resume; cache must serve
        stop_reason=None,
    )
    assert eval_calls == 1  # NOT incremented
    assert result.status == "complete"
    assert state.last_qa_verdict == "pass"


@pytest.mark.asyncio
async def test_pipeline_evaluate_reevaluates_when_artifact_changes(tmp_path) -> None:
    """A different artifact hash forces re-evaluation."""
    state = _state_at_run_phase(tmp_path)
    eval_calls = 0

    async def evaluator(seed: Seed, artifact: str) -> EvaluateResult:  # noqa: ARG001
        nonlocal eval_calls
        eval_calls += 1
        # First run: pass; subsequent runs: fail
        if eval_calls == 1:
            return EvaluateResult(passed=True, score=0.92, verdict="pass")
        return EvaluateResult(
            passed=False,
            score=0.30,
            verdict="fail",
            differences=("changed output is wrong",),
        )

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=_ralph_starter(),
        complete_product=True,
        evaluator=evaluator,
    )

    await pipeline.run(state)
    assert eval_calls == 1
    first_hash = state.evaluate_artifact_hash

    # Now simulate a resume with a fresh, different artifact. Bypass
    # ``state.transition`` per the cache test's rationale.
    state.phase = AutoPhase.EVALUATE
    seed_resume = _build_seed()
    await pipeline._run_evaluate(
        state,
        ledger=_StubLedger(),
        seed=seed_resume,
        review=None,
        run_subagent=None,
        ralph_result_text="entirely different artifact content",
        stop_reason=None,
    )
    assert eval_calls == 2
    assert state.evaluate_artifact_hash != first_hash
    assert state.last_qa_verdict == "fail"


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


def test_state_round_trips_qa_fields(tmp_path) -> None:
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.last_qa_score = 0.83
    state.last_qa_verdict = "pass"
    state.last_qa_differences = ["a", "b"]
    state.last_qa_suggestions = ["fix a", "fix b"]
    state.evaluate_artifact_hash = "deadbeef"
    store = AutoStore(tmp_path)
    store.save(state)
    reloaded = store.load(state.auto_session_id)
    assert reloaded.last_qa_score == 0.83
    assert reloaded.last_qa_verdict == "pass"
    assert reloaded.last_qa_differences == ["a", "b"]
    assert reloaded.last_qa_suggestions == ["fix a", "fix b"]
    assert reloaded.evaluate_artifact_hash == "deadbeef"


def test_state_loads_legacy_dump_without_qa_fields(tmp_path) -> None:
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    raw = state.to_dict()
    for key in (
        "last_qa_score",
        "last_qa_verdict",
        "last_qa_differences",
        "last_qa_suggestions",
        "evaluate_artifact_hash",
    ):
        raw.pop(key, None)
    reloaded = AutoPipelineState.from_dict(raw)
    assert reloaded.last_qa_score is None
    assert reloaded.last_qa_verdict is None
    assert reloaded.last_qa_differences == []
    assert reloaded.last_qa_suggestions == []
    assert reloaded.evaluate_artifact_hash is None


# ---------------------------------------------------------------------------
# HandlerEvaluator adapter unit test
# ---------------------------------------------------------------------------


class _StubQAHandler:
    """Stand-in for ``QAHandler`` capturing the call payload."""

    def __init__(self, meta: dict[str, Any] | None = None, is_err: bool = False) -> None:
        self._meta = meta or {
            "passed": True,
            "score": 0.85,
            "verdict": "pass",
            "differences": [],
            "suggestions": [],
        }
        self._is_err = is_err
        self.last_arguments: dict[str, Any] | None = None

    async def handle(self, arguments: dict[str, Any]):  # noqa: ANN201
        self.last_arguments = arguments
        # Mimic the Result wrapper shape used by QAHandler
        from ouroboros.core.types import Result
        from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult

        if self._is_err:
            from ouroboros.mcp.errors import MCPToolError

            return Result.err(MCPToolError("qa unreachable", tool_name="ouroboros_qa"))
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text="ok"),),
                is_error=False,
                meta=self._meta,
            )
        )


@pytest.mark.asyncio
async def test_handler_evaluator_builds_quality_bar_from_seed_ac() -> None:
    stub = _StubQAHandler()
    evaluator = HandlerEvaluator(stub)
    seed = _build_seed()

    result = await evaluator(seed, "stdout: ok\nexit_code: 0")

    assert result.passed is True
    assert result.score == 0.85
    assert result.verdict == "pass"
    args = stub.last_arguments
    assert args is not None
    # Quality bar must contain every acceptance criterion
    for ac in seed.acceptance_criteria:
        assert ac in args["quality_bar"]
    # Default arg shape
    assert args["artifact_type"] == "test_output"
    assert args["pass_threshold"] == 0.80
    assert args["seed_content"]  # non-empty seed yaml
    assert args["artifact"] == "stdout: ok\nexit_code: 0"


@pytest.mark.asyncio
async def test_handler_evaluator_maps_qa_error_to_evaluate_result() -> None:
    stub = _StubQAHandler(is_err=True)
    evaluator = HandlerEvaluator(stub)
    result = await evaluator(_build_seed(), "any artifact")
    assert result.passed is False
    assert result.verdict == "fail"
    assert result.error is not None
    assert "qa unreachable" in result.error.lower()
