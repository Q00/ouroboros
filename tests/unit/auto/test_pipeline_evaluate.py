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

    The pipeline calls ``summary()``, ``assumptions()``, ``assumption_sources()``
    (PR-C2 / #1157), and ``non_goals()`` on the ledger inside ``_result()``. All
    return empty so the test focuses on EVALUATE transition behaviour rather
    than ledger-summary coverage."""

    def summary(self) -> dict[str, Any]:
        return {
            "provenance": {},
            "evidence_backed_sections": (),
            "assumption_only_sections": (),
        }

    def assumptions(self) -> list[str]:
        return []

    def assumption_sources(self) -> list[Any]:
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
    # EVALUATE can reach COMPLETE/BLOCKED/FAILED directly, or bridge through
    # UNSTUCK_LATERAL on QA fail (RFC #809 Phase 2.2).
    assert _ALLOWED_TRANSITIONS[AutoPhase.EVALUATE] == {
        AutoPhase.UNSTUCK_LATERAL,
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


@pytest.mark.asyncio
async def test_handler_evaluator_empty_artifact_synthesizes_fail_without_calling_qa() -> None:
    """The real ``QAHandler`` rejects empty artifacts with
    ``"artifact is required"``. The adapter must synthesize the
    "empty run output is a graded failure" verdict locally instead of
    routing through QA (which would land in the transient-error path)."""
    stub = _StubQAHandler()
    evaluator = HandlerEvaluator(stub)

    result = await evaluator(_build_seed(), "")

    assert result.passed is False
    assert result.verdict == "fail"
    assert "empty" in " ".join(result.differences).lower()
    # QA was NOT called for the empty path
    assert stub.last_arguments is None


@pytest.mark.asyncio
async def test_handler_evaluator_detects_plugin_delegation_envelope() -> None:
    """In plugin mode ``QAHandler`` returns a delegation envelope
    (``status="delegated_to_subagent"``) instead of a final verdict. The
    adapter must NOT silently treat the envelope as ``passed=False``;
    instead it returns an error result so the pipeline can surface it as
    a recoverable BLOCKED rather than a misleading "QA said fail" state."""
    stub = _StubQAHandler(meta={"status": "delegated_to_subagent", "dispatch_mode": "plugin"})
    evaluator = HandlerEvaluator(stub)

    result = await evaluator(_build_seed(), "some artifact")

    assert result.passed is False
    assert result.error is not None
    assert "delegation" in result.error.lower()


# ---------------------------------------------------------------------------
# Ralph adapter must surface result_text so production EVALUATE can fire
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wait_for_job_terminal_surfaces_result_text() -> None:
    """The Ralph adapter pulls ``snapshot.result_text`` so EVALUATE has an
    artifact to grade in production. Before this fix the adapter only
    returned ``result_meta`` and real MCP/CLI runs skipped EVALUATE."""
    from ouroboros.auto.adapters import _wait_for_job_terminal
    from ouroboros.mcp.job_manager import JobStatus

    class _StubSnapshot:
        def __init__(self) -> None:
            self.is_terminal = True
            self.status = JobStatus.COMPLETED
            self.result_meta: dict[str, Any] = {"status": "completed", "stop_reason": "ok"}
            self.result_text = "stdout: hello\nexit_code: 0"

    class _StubJobManager:
        async def get_snapshot(self, _job_id: str) -> _StubSnapshot:
            return _StubSnapshot()

    meta = await _wait_for_job_terminal(_StubJobManager(), "job_X")  # type: ignore[arg-type]
    assert meta["status"] == "completed"
    assert meta["__result_text__"] == "stdout: hello\nexit_code: 0"


@pytest.mark.asyncio
async def test_handler_ralph_starter_returns_result_text() -> None:
    """The production ``HandlerRalphStarter`` chain must propagate the
    artifact into the dict the pipeline reads (`result_text` key) so
    EVALUATE actually fires for real Ralph runs."""
    from ouroboros.auto.adapters import HandlerRalphStarter
    from ouroboros.mcp.job_manager import JobStatus

    class _StubSnapshot:
        def __init__(self) -> None:
            self.is_terminal = True
            self.status = JobStatus.COMPLETED
            self.result_meta: dict[str, Any] = {"status": "completed", "stop_reason": "qa passed"}
            self.result_text = "ralph artifact text"

    class _StubJobManager:
        async def get_snapshot(self, _job_id: str) -> _StubSnapshot:
            return _StubSnapshot()

    captured_arguments: dict[str, Any] = {}

    class _StubRalphHandler:
        _job_manager = _StubJobManager()

        async def handle(self, arguments: dict[str, Any]):  # noqa: ANN201
            captured_arguments.update(arguments)
            from ouroboros.core.types import Result
            from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult

            return Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="dispatched"),),
                    is_error=False,
                    meta={
                        "job_id": "job_ralph_X",
                        "lineage_id": "lineage_X",
                        "dispatch_mode": "job",
                        "status": "running",
                    },
                )
            )

    starter = HandlerRalphStarter(  # type: ignore[arg-type]
        _StubRalphHandler(), project_dir="/tmp/auto-project"
    )
    result = await starter(_build_seed(), lineage_id="lineage_X")
    assert result["result_text"] == "ralph artifact text"
    assert result["terminal_status"] == "completed"
    assert captured_arguments["project_dir"] == "/tmp/auto-project"


# ---------------------------------------------------------------------------
# Evaluator tool name must be recoverable on --resume
# ---------------------------------------------------------------------------


def test_recoverable_phase_for_evaluator_tool() -> None:
    """When evaluator times out or returns a transient error, the session
    is marked BLOCKED with ``tool_name="evaluator"``. ``--resume`` must
    dispatch back into EVALUATE so the session is genuinely recoverable
    (the cached verdict or a fresh evaluator call drives progress).
    """
    from ouroboros.auto.pipeline import _recoverable_phase_for_tool

    assert _recoverable_phase_for_tool("evaluator") is AutoPhase.EVALUATE


# ---------------------------------------------------------------------------
# Evaluator durability: artifact must persist across timeout → resume
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evaluator_timeout_persists_artifact_for_resume(tmp_path) -> None:
    """After an evaluator timeout, ``state.evaluate_artifact`` must hold the
    artifact text so ``--resume`` can re-grade it instead of falling into
    the "no cached verdict and no artifact" BLOCKED branch."""
    state = _state_at_run_phase(tmp_path)
    state.timeout_seconds_by_phase[AutoPhase.EVALUATE.value] = 1

    async def hanging_evaluator(seed: Seed, artifact: str) -> EvaluateResult:  # noqa: ARG001
        await asyncio.sleep(10)
        return EvaluateResult(passed=True, score=1.0, verdict="pass")

    pipeline_timeout = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=_ralph_starter(result_text="ralph artifact payload"),
        complete_product=True,
        evaluator=hanging_evaluator,
    )

    await pipeline_timeout.run(state)
    assert state.phase is AutoPhase.BLOCKED
    assert state.last_tool_name == "evaluator"
    assert state.evaluate_artifact == "ralph artifact payload"
    assert state.evaluate_artifact_hash is not None


@pytest.mark.asyncio
async def test_resume_after_evaluator_timeout_re_runs_with_persisted_artifact(tmp_path) -> None:
    """Simulating the full resume contract: an evaluator that times out on
    first call, then succeeds on a retry. The persisted artifact allows
    ``_run_evaluate`` to re-grade on resume — the recovery path that the
    earlier review iteration silently broke."""
    state = _state_at_run_phase(tmp_path)
    state.timeout_seconds_by_phase[AutoPhase.EVALUATE.value] = 1
    call_count = 0

    async def flaky_evaluator(seed: Seed, artifact: str) -> EvaluateResult:  # noqa: ARG001
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            await asyncio.sleep(10)  # times out
        return EvaluateResult(
            passed=True, score=0.91, verdict="pass", differences=(), suggestions=()
        )

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=_ralph_starter(result_text="durable ralph artifact"),
        complete_product=True,
        evaluator=flaky_evaluator,
    )

    # First call → evaluator times out → BLOCKED but artifact persisted
    await pipeline.run(state)
    assert state.phase is AutoPhase.BLOCKED
    assert state.evaluate_artifact == "durable ralph artifact"
    assert call_count == 1

    # Simulate resume by re-entering EVALUATE directly (production resume
    # uses ``_recoverable_phase_for_tool("evaluator") == EVALUATE`` which we
    # just verified above). The pipeline pulls the persisted artifact from
    # state and re-invokes the evaluator.
    state.phase = AutoPhase.EVALUATE
    state.timeout_seconds_by_phase[AutoPhase.EVALUATE.value] = 60
    result = await pipeline._run_evaluate(
        state,
        ledger=_StubLedger(),
        seed=_build_seed(),
        review=None,
        run_subagent=None,
        ralph_result_text=None,  # caller passes None on resume
        stop_reason=None,
    )
    assert result.status == "complete"
    assert state.last_qa_verdict == "pass"
    assert call_count == 2  # evaluator was re-invoked with the persisted artifact


def test_state_round_trips_evaluate_artifact(tmp_path) -> None:
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.evaluate_artifact = "persisted artifact"
    state.evaluate_artifact_hash = "deadbeef"
    store = AutoStore(tmp_path)
    store.save(state)
    reloaded = store.load(state.auto_session_id)
    assert reloaded.evaluate_artifact == "persisted artifact"
    assert reloaded.evaluate_artifact_hash == "deadbeef"


# ---------------------------------------------------------------------------
# Cache correctness: a new artifact must not inherit stale verdict
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_artifact_does_not_inherit_stale_verdict(tmp_path) -> None:
    """If artifact A was graded pass, then artifact B enters EVALUATE and
    the evaluator times out, ``--resume`` must NOT reuse A's verdict
    against B. The pipeline clears stale qa fields whenever the artifact
    hash changes."""
    state = _state_at_run_phase(tmp_path)
    state.timeout_seconds_by_phase[AutoPhase.EVALUATE.value] = 1

    call_count = 0

    async def evaluator(seed: Seed, artifact: str) -> EvaluateResult:  # noqa: ARG001
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return EvaluateResult(passed=True, score=0.95, verdict="pass")
        # Second call (for artifact B) hangs → times out
        await asyncio.sleep(10)
        return EvaluateResult(passed=True, score=1.0, verdict="pass")

    pipeline_a = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=_ralph_starter(result_text="artifact A"),
        complete_product=True,
        evaluator=evaluator,
    )

    # Pass artifact A — verdict cached.
    await pipeline_a.run(state)
    assert state.phase is AutoPhase.COMPLETE
    assert state.last_qa_verdict == "pass"
    a_hash = state.evaluate_artifact_hash

    # Now feed artifact B directly into _run_evaluate. The evaluator times
    # out, leaving state in BLOCKED. Critical: the stale "pass" verdict
    # from A must be cleared so a future cache check cannot mistakenly
    # reuse it for B.
    state.phase = AutoPhase.EVALUATE
    await pipeline_a._run_evaluate(
        state,
        ledger=_StubLedger(),
        seed=_build_seed(),
        review=None,
        run_subagent=None,
        ralph_result_text="artifact B (different)",
        stop_reason=None,
    )
    assert state.phase is AutoPhase.BLOCKED
    assert state.last_qa_verdict is None  # stale verdict cleared
    assert state.last_qa_score is None
    assert state.evaluate_artifact_hash != a_hash
    assert state.evaluate_artifact == "artifact B (different)"


# ---------------------------------------------------------------------------
# Empty Ralph artifact must still be graded (not silent false-pass)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_ralph_artifact_still_enters_evaluate(tmp_path) -> None:
    """``ralph_result_text == ""`` is a valid graded artifact; the gate
    used ``ralph_result_text is not None`` (not truthiness) so the
    pipeline does not silently mark an empty-output run as a pass."""
    state = _state_at_run_phase(tmp_path)
    eval_calls = 0

    async def evaluator(seed: Seed, artifact: str) -> EvaluateResult:  # noqa: ARG001
        nonlocal eval_calls
        eval_calls += 1
        # An empty artifact should fail QA against AC like "Command prints
        # stable output". Return fail to make the contract explicit.
        return EvaluateResult(
            passed=False,
            score=0.10,
            verdict="fail",
            differences=("artifact is empty",),
        )

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=_ralph_starter(result_text=""),
        complete_product=True,
        evaluator=evaluator,
    )

    result = await pipeline.run(state)
    assert eval_calls == 1
    assert state.phase is AutoPhase.BLOCKED
    assert result.last_qa_verdict == "fail"


# ---------------------------------------------------------------------------
# Resume capability advertises EVALUATE recoverability
# ---------------------------------------------------------------------------


def test_resume_capability_advertises_evaluate_as_resumable(tmp_path) -> None:
    """An EVALUATE-blocked session with the persisted artifact in place
    must report ``RESUME`` capability so the CLI/MCP surfaces emit the
    resume hint. Previously the method fell through to ``NONE`` for
    EVALUATE-blocked states — a public-surface contract bug."""
    from ouroboros.auto.state import AutoResumeCapability

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.phase = AutoPhase.BLOCKED
    state.last_tool_name = "evaluator"
    state.evaluate_artifact = "some artifact text"
    state.evaluate_artifact_hash = "abc123"

    assert state.resume_capability() is AutoResumeCapability.RESUME


def test_resume_capability_evaluate_blocked_without_artifact_is_none(tmp_path) -> None:
    """Without a persisted artifact AND without a cached verdict, EVALUATE
    has nothing to drive forward on resume — capability must be NONE."""
    from ouroboros.auto.state import AutoResumeCapability

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.phase = AutoPhase.BLOCKED
    state.last_tool_name = "evaluator"
    # No evaluate_artifact, no last_qa_verdict
    assert state.resume_capability() is AutoResumeCapability.NONE


def test_resume_capability_evaluate_blocked_with_cached_verdict_is_resumable(tmp_path) -> None:
    """A cached pass flag + hash is enough to drive the resume to COMPLETE
    (cache-hit short-circuit), so capability is RESUME even without the
    raw artifact text."""
    from ouroboros.auto.state import AutoResumeCapability

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.phase = AutoPhase.BLOCKED
    state.last_tool_name = "evaluator"
    state.evaluate_artifact_hash = "abc123"
    state.last_qa_verdict = "pass"
    state.last_qa_passed = True
    state.last_qa_score = 0.92
    assert state.resume_capability() is AutoResumeCapability.RESUME


@pytest.mark.asyncio
async def test_evaluate_cache_reuses_passed_flag_for_revise_verdict(tmp_path) -> None:
    """End-to-end async version: ``passed=True, verdict="revise"`` must
    yield COMPLETE on first call AND cached COMPLETE on resume."""
    state = _state_at_run_phase(tmp_path)
    eval_calls = 0

    async def evaluator(seed: Seed, artifact: str) -> EvaluateResult:  # noqa: ARG001
        nonlocal eval_calls
        eval_calls += 1
        return EvaluateResult(
            passed=True,
            score=0.85,
            verdict="revise",  # NOT "pass" — verdict diverges from passed flag
            differences=("minor formatting issue",),
            suggestions=("trim trailing whitespace",),
        )

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=_ralph_starter(result_text="some artifact"),
        complete_product=True,
        evaluator=evaluator,
    )

    # First call: passed=True with verdict="revise" should still COMPLETE.
    result = await pipeline.run(state)
    assert result.status == "complete"
    assert state.last_qa_passed is True
    assert state.last_qa_verdict == "revise"
    assert eval_calls == 1

    # Simulated resume — cache must use last_qa_passed (True), not the
    # verdict string. Reset phase to EVALUATE so we can call _run_evaluate
    # directly (production resume gets here via _recoverable_phase_for_tool).
    state.phase = AutoPhase.EVALUATE
    resume_result = await pipeline._run_evaluate(
        state,
        ledger=_StubLedger(),
        seed=_build_seed(),
        review=None,
        run_subagent=None,
        ralph_result_text=None,
        stop_reason=None,
    )
    assert resume_result.status == "complete"  # NOT blocked
    assert eval_calls == 1  # cache hit, evaluator NOT re-invoked


def test_resume_capability_evaluate_blocked_with_empty_artifact_is_resumable(tmp_path) -> None:
    """An empty-string artifact is a valid graded input — resume capability
    must use ``is not None``, not truthiness. Previously a session blocked
    after persisting ``""`` was incorrectly reported as non-resumable."""
    from ouroboros.auto.state import AutoResumeCapability

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.phase = AutoPhase.BLOCKED
    state.last_tool_name = "evaluator"
    state.evaluate_artifact = ""  # empty but persisted
    state.evaluate_artifact_hash = "hash_of_empty"
    assert state.resume_capability() is AutoResumeCapability.RESUME


# ---------------------------------------------------------------------------
# Ralph adapter empty-artifact contract (production path, not stubs)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_ralph_starter_preserves_empty_result_text() -> None:
    """Production ``HandlerRalphStarter`` must propagate ``""`` (an empty-
    but-valid artifact) as a real string into the pipeline. ``_optional_str``
    would have collapsed it to None and silently skipped EVALUATE — the
    very false-pass this PR claims to fix."""
    from ouroboros.auto.adapters import HandlerRalphStarter
    from ouroboros.mcp.job_manager import JobStatus

    class _StubSnapshot:
        def __init__(self) -> None:
            self.is_terminal = True
            self.status = JobStatus.COMPLETED
            self.result_meta: dict[str, Any] = {"status": "completed", "stop_reason": "qa passed"}
            self.result_text = ""  # intentionally empty artifact

    class _StubJobManager:
        async def get_snapshot(self, _job_id: str) -> _StubSnapshot:
            return _StubSnapshot()

    class _StubRalphHandler:
        _job_manager = _StubJobManager()

        async def handle(self, _arguments: dict[str, Any]):  # noqa: ANN201
            from ouroboros.core.types import Result
            from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult

            return Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="dispatched"),),
                    is_error=False,
                    meta={
                        "job_id": "job_ralph_empty",
                        "lineage_id": "lineage_empty",
                        "dispatch_mode": "job",
                        "status": "running",
                    },
                )
            )

    starter = HandlerRalphStarter(_StubRalphHandler())  # type: ignore[arg-type]
    result = await starter(_build_seed(), lineage_id="lineage_empty")
    # Critical: result_text must be ``""`` (a string), NOT None.
    assert result["result_text"] == ""
    assert isinstance(result["result_text"], str)


@pytest.mark.asyncio
async def test_evaluator_respects_top_level_pipeline_deadline(tmp_path) -> None:
    """If only N seconds remain on the pipeline deadline when EVALUATE is
    entered, the evaluator call must be capped at ``min(N, phase_timeout)``
    and a deadline trip during the call must surface the canonical
    ``pipeline_timeout`` blocker (tool_name=``pipeline_deadline``), not the
    per-phase ``evaluator timed out`` message. This keeps EVALUATE inside
    the same budget framework as every other long-running phase."""
    import time as _time

    from ouroboros.auto.pipeline import PIPELINE_DEADLINE_TOOL_NAME

    state = _state_at_run_phase(tmp_path)
    # Force a near-expired deadline: 0.1 seconds from now.
    state.deadline_at = _time.monotonic() + 0.1
    # Per-phase timeout is much larger, so the deadline must dominate.
    state.timeout_seconds_by_phase[AutoPhase.EVALUATE.value] = 90

    async def hanging_evaluator(seed: Seed, artifact: str) -> EvaluateResult:  # noqa: ARG001
        await asyncio.sleep(10)
        return EvaluateResult(passed=True, score=1.0, verdict="pass")

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        _seed_generator_unused,
        run_starter=_run_starter_ok,
        reviewer=_PassReviewer(),
        ralph_starter=_ralph_starter(result_text="ralph artifact"),
        complete_product=True,
        evaluator=hanging_evaluator,
    )

    result = await pipeline.run(state)
    assert result.status == "blocked"
    # Deadline trip uses the canonical tool name, NOT "evaluator"
    assert state.last_tool_name == PIPELINE_DEADLINE_TOOL_NAME
    assert "pipeline_timeout" in (state.last_error or "")


@pytest.mark.asyncio
async def test_handler_ralph_poller_preserves_empty_result_text() -> None:
    """Same contract on the resume poller path."""
    from ouroboros.auto.adapters import HandlerRalphPoller
    from ouroboros.mcp.job_manager import JobStatus

    class _StubSnapshot:
        def __init__(self) -> None:
            self.is_terminal = True
            self.status = JobStatus.COMPLETED
            self.result_meta: dict[str, Any] = {"status": "completed"}
            self.result_text = ""

    class _StubJobManager:
        async def get_snapshot(self, _job_id: str) -> _StubSnapshot:
            return _StubSnapshot()

    class _StubRalphHandler:
        _job_manager = _StubJobManager()

    poller = HandlerRalphPoller(_StubRalphHandler())  # type: ignore[arg-type]
    result = await poller(job_id="job_empty")
    assert result["result_text"] == ""
    assert isinstance(result["result_text"], str)
