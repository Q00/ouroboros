"""Phase 2.1 tests — EVALUATE phase + HandlerEvaluator + state plumbing.

Covers RFC #809 Phase 2.1: a successful Ralph terminal verdict no longer goes
straight to COMPLETE when an evaluator is wired. Instead the pipeline grades
the run artifact against the Seed's acceptance criteria via ``ouroboros_qa``
and only transitions to COMPLETE on QA pass. QA fail → BLOCKED with the
verdict summary in ``state.last_error``.
"""

from __future__ import annotations

from typing import Any

import pytest

from ouroboros.auto.adapters import HandlerEvaluator, HandlerSeedQAEvaluator
from ouroboros.auto.grading import GradeResult, SeedGrade
from ouroboros.auto.interview_driver import AutoInterviewResult
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
    ac_texts,
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
    for ac in ac_texts(seed.acceptance_criteria):
        assert ac in args["quality_bar"]
    # Default arg shape
    assert args["artifact_type"] == "test_output"
    assert args["pass_threshold"] == 0.80
    assert args["seed_content"]  # non-empty seed yaml
    assert args["artifact"] == "stdout: ok\nexit_code: 0"


@pytest.mark.asyncio
async def test_handler_seed_qa_enforces_semantic_ac_granularity() -> None:
    """The production Auto Seed-QA payload rejects implementation means while
    preserving genuinely independent outcomes instead of applying a count proxy."""
    stub = _StubQAHandler()
    evaluator = HandlerSeedQAEvaluator(stub)

    result = await evaluator(_build_seed(), {"goal": "Build a CLI"})

    assert result.passed is True
    args = stub.last_arguments
    assert args is not None
    quality_bar = args["quality_bar"].lower()
    assert "observable state of the finished work" in quality_bar
    assert "not an implementation means" in quality_bar
    assert "step toward a sibling's outcome" in quality_bar
    assert "preserve legitimately independent observable outcomes" in quality_bar
    assert "criterion-count target" in quality_bar
    assert args["artifact_type"] == "document"
    assert args["pass_threshold"] == 0.80


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
    result = await starter(
        _build_seed(),
        lineage_id="lineage_X",
        return_after_dispatch=False,
    )
    assert result["result_text"] == "ralph artifact text"
    assert result["terminal_status"] == "completed"
    assert captured_arguments["project_dir"] == "/tmp/auto-project"


@pytest.mark.asyncio
async def test_handler_ralph_starter_detaches_by_default() -> None:
    """Production Ralph handoff must not wait for terminal job completion unless
    a caller explicitly opts into the old blocking behavior."""
    from ouroboros.auto.adapters import HandlerRalphStarter

    class _StubJobManager:
        async def get_snapshot(self, _job_id: str):  # pragma: no cover
            raise AssertionError("default Ralph starter must not poll terminal status")

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
                        "job_id": "job_ralph_async",
                        "lineage_id": "lineage_async",
                        "dispatch_mode": "job",
                        "status": "running",
                    },
                )
            )

    starter = HandlerRalphStarter(_StubRalphHandler())  # type: ignore[arg-type]
    result = await starter(_build_seed(), lineage_id="lineage_async")

    assert result["job_id"] == "job_ralph_async"
    assert result["terminal_status"] == "running_async"
    assert result["stop_reason"] == "foreground_timeout_elapsed"


# ---------------------------------------------------------------------------
# Evaluator tool name must be recoverable on --resume
# ---------------------------------------------------------------------------


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


def test_resume_capability_evaluate_blocked_without_artifact_is_none(tmp_path) -> None:
    """Without a persisted artifact AND without a cached verdict, EVALUATE
    has nothing to drive forward on resume — capability must be NONE."""
    from ouroboros.auto.state import AutoResumeCapability

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.phase = AutoPhase.BLOCKED
    state.last_tool_name = "evaluator"
    # No evaluate_artifact, no last_qa_verdict
    assert state.resume_capability() is AutoResumeCapability.NONE


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
    result = await starter(
        _build_seed(),
        lineage_id="lineage_empty",
        return_after_dispatch=False,
    )
    # Critical: result_text must be ``""`` (a string), NOT None.
    assert result["result_text"] == ""
    assert isinstance(result["result_text"], str)


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
