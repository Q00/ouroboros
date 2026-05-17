"""Regression coverage for the ``ooo auto`` sub-interview construction path.

The relevant entrypoint is ``AutoHandler._run`` in
``ouroboros.mcp.tools.auto_handler``.  It must construct the authoring
``InterviewHandler`` and invoke it through ``HandlerInterviewBackend`` so the
single-shot interviewer envelope is exercised from the auto path, not only by
standalone ``ouroboros_interview`` tests.
"""

from __future__ import annotations

import ast
import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from ouroboros.auto.adapters import HandlerInterviewBackend
from ouroboros.auto.interview_driver import AutoInterviewDriver
from ouroboros.auto.ledger import SeedDraftLedger
from ouroboros.auto.pipeline import AutoPipelineResult
from ouroboros.auto.state import AutoPipelineState, AutoResumeCapability, AutoStore
from ouroboros.bigbang.interview import InterviewRound, InterviewState, InterviewStatus
from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPServerError
from ouroboros.mcp.job_manager import JobLinks, JobSnapshot, JobStatus
from ouroboros.mcp.tools.authoring_handlers import InterviewHandler
from ouroboros.mcp.tools.auto_handler import (
    AutoHandler,
    _derive_goal_user_preferences,
    _merge_resume_user_preferences,
    _reconcile_execution_job_snapshot,
    _reseed_preference_ledger,
    _seed_initial_ledger_from_user_preferences,
)
from ouroboros.mcp.types import ContentType
from ouroboros.providers.base import CompletionResponse, Message, UsageInfo
from ouroboros.providers.claude_code_adapter import ClaudeCodeAdapter

_AUTO_SUB_INTERVIEW_ENTRYPOINT_TEST_MANIFEST = {
    "src/ouroboros/mcp/tools/auto_handler.py::AutoHandler._run": (
        "test_auto_handler_run_constructs_and_invokes_authoring_interviewer_path",
        "test_mocked_auto_interviewer_flow_returns_plain_text_question",
        "test_auto_sub_interview_envelope_ignores_parent_adapter_tool_context",
        "test_auto_sub_interview_prompt_omits_code_exploration_and_tool_use_cues",
        "test_auto_sub_interview_spy_adapter_fails_on_any_tool_request",
    ),
}

_TOOL_CALL_CAPABLE_LEAKAGE_PATHS = frozenset(
    {
        "Read",
        "Glob",
        "Grep",
        "Bash",
        "Task",
        "Skill",
        "WebFetch",
        "WebSearch",
        "mcp__plugin_ouroboros__interview",
        "mcp__parent_plugin__lookup",
    }
)

_OBSERVATION_GOAL = """
Goal:
Verify current ooo auto can create hello_auto.py and tests/test_hello_auto.py.

Implementation:
- Create `hello_auto.py` at the repository root.
- Add a minimal pytest test at `tests/test_hello_auto.py`.

Outputs:
- `hello_auto.py` exists.
- `tests/test_hello_auto.py` exists.

Runtime context:
- This is a local development repository.
- Local file edits are allowed.
- Running targeted tests is allowed.
- Network access is not required.
- No credentials are required.

Actors:
- A single local developer/operator using Codex and Ouroboros in the local repository.

Inputs:
- The local repository state, the requested implementation contract, and the verification commands described in this goal prompt.

Non-goals:
- Do not refactor existing code.
- Do not add dependencies.
- Do not edit unrelated files.

Success criteria:
- `ooo auto` is handled by Ouroboros auto/MCP, not plain text.
- `hello_auto.py` exists.
- `tests/test_hello_auto.py` exists.
- The targeted test command `uv run pytest tests/test_hello_auto.py` passes.
- Final report includes auto session id, seed id, files changed, exact test command, and test result.

Important dispatch rule:
If `ouroboros_auto` is unavailable or interpreted as normal text, stop and report failure.
"""


def _assert_isolated_allowed_tools(factory_kwargs: dict[str, Any]) -> None:
    """Assert the auto sub-interviewer cannot opt into tool-call surfaces."""
    allowed_tools = factory_kwargs["allowed_tools"]
    assert allowed_tools == []
    assert allowed_tools is not None
    assert set(allowed_tools).isdisjoint(_TOOL_CALL_CAPABLE_LEAKAGE_PATHS)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _job_snapshot(status: JobStatus, *, error: str | None = None) -> JobSnapshot:
    now = datetime.now(UTC)
    return JobSnapshot(
        job_id="job_failed",
        job_type="execute_seed",
        status=status,
        message=f"Job {status.value}",
        created_at=now,
        updated_at=now,
        links=JobLinks(session_id="orch_1", execution_id="exec_1"),
        error=error,
    )



def test_execution_job_running_keeps_auto_result_pollable(monkeypatch) -> None:
    class FakeJobManager:
        async def get_snapshot(self, job_id: str) -> JobSnapshot:
            assert job_id == "job_running"
            return _job_snapshot(JobStatus.RUNNING)

    monkeypatch.setattr(
        "ouroboros.mcp.tools.auto_handler.JobManager",
        lambda: FakeJobManager(),
    )
    result = AutoPipelineResult(
        status="complete",
        auto_session_id="auto_1",
        phase="complete",
        job_id="job_running",
        run_handoff_status="started",
        resume_capability=AutoResumeCapability.NONE,
    )

    reconciled = asyncio.run(_reconcile_execution_job_snapshot(result))

    assert reconciled.status == "running"
    assert reconciled.phase == "complete"
    assert reconciled.execution_job_status == "running"
    assert reconciled.blocker is None
    assert reconciled.resume_capability is AutoResumeCapability.RESUME


def test_execution_job_completed_keeps_auto_complete(monkeypatch) -> None:
    class FakeJobManager:
        async def get_snapshot(self, job_id: str) -> JobSnapshot:
            assert job_id == "job_done"
            return _job_snapshot(JobStatus.COMPLETED)

    monkeypatch.setattr(
        "ouroboros.mcp.tools.auto_handler.JobManager",
        lambda: FakeJobManager(),
    )
    result = AutoPipelineResult(
        status="running",
        auto_session_id="auto_1",
        phase="complete",
        job_id="job_done",
        resume_capability=AutoResumeCapability.RESUME,
    )

    reconciled = asyncio.run(_reconcile_execution_job_snapshot(result))

    assert reconciled.status == "complete"
    assert reconciled.execution_job_status == "completed"
    assert reconciled.resume_capability is AutoResumeCapability.NONE

def test_execution_job_failure_rewrites_complete_auto_result(monkeypatch) -> None:
    class FakeJobManager:
        async def get_snapshot(self, job_id: str) -> JobSnapshot:
            assert job_id == "job_failed"
            return _job_snapshot(JobStatus.FAILED, error="planner failed")

    monkeypatch.setattr(
        "ouroboros.mcp.tools.auto_handler.JobManager",
        lambda: FakeJobManager(),
    )
    result = AutoPipelineResult(
        status="complete",
        auto_session_id="auto_1",
        phase="complete",
        job_id="job_failed",
    )

    reconciled = asyncio.run(_reconcile_execution_job_snapshot(result))

    assert reconciled.status == "failed"
    assert reconciled.execution_job_status == "failed"
    assert reconciled.execution_job_error == "planner failed"
    assert reconciled.blocker == "execution job failed: planner failed"


def test_execution_job_cancelled_blocks_complete_auto_result(monkeypatch) -> None:
    class FakeJobManager:
        async def get_snapshot(self, job_id: str) -> JobSnapshot:
            assert job_id == "job_failed"
            return _job_snapshot(JobStatus.CANCELLED)

    monkeypatch.setattr(
        "ouroboros.mcp.tools.auto_handler.JobManager",
        lambda: FakeJobManager(),
    )
    result = AutoPipelineResult(
        status="complete",
        auto_session_id="auto_1",
        phase="complete",
        job_id="job_failed",
    )

    reconciled = asyncio.run(_reconcile_execution_job_snapshot(result))

    assert reconciled.status == "blocked"
    assert reconciled.execution_job_status == "cancelled"
    assert reconciled.blocker == "execution job cancelled: Job cancelled"


def test_execution_job_interrupted_blocks_complete_auto_result(monkeypatch) -> None:
    class FakeJobManager:
        async def get_snapshot(self, job_id: str) -> JobSnapshot:
            assert job_id == "job_failed"
            return _job_snapshot(JobStatus.INTERRUPTED)

    monkeypatch.setattr(
        "ouroboros.mcp.tools.auto_handler.JobManager",
        lambda: FakeJobManager(),
    )
    result = AutoPipelineResult(
        status="complete",
        auto_session_id="auto_1",
        phase="complete",
        job_id="job_failed",
    )

    reconciled = asyncio.run(_reconcile_execution_job_snapshot(result))

    assert reconciled.status == "blocked"
    assert reconciled.execution_job_status == "interrupted"
    assert reconciled.blocker == "execution job interrupted: Job interrupted"


def test_structured_auto_goal_seeds_required_ledger_sections() -> None:
    """Observation-style prompts should seed known ledger sections before interview."""
    preferences = _derive_goal_user_preferences(_OBSERVATION_GOAL)
    ledger = _seed_initial_ledger_from_user_preferences(_OBSERVATION_GOAL, preferences)

    assert {
        "actors",
        "inputs",
        "outputs",
        "constraints",
        "non_goals",
        "acceptance_criteria",
        "verification_plan",
        "failure_modes",
        "runtime_context",
    } <= set(preferences)
    assert ledger.open_gaps() == []
    summary = ledger.summary()
    assert "runtime_context" in summary["evidence_backed_sections"]
    assert "constraints" in summary["evidence_backed_sections"]
    assert "non_goals" in summary["evidence_backed_sections"]
    assert "Final report" not in preferences["acceptance_criteria"]
    assert "`hello_auto.py` exists" in preferences["acceptance_criteria"]


def test_structured_auto_goal_does_not_preconfirm_risky_preference() -> None:
    """Pre-seeding must not bypass the answerer's risky USER_PREFERENCE gate."""
    risky_goal = """
Goal:
Build a data cleanup tool.

Implementation:
- Drop every customer table when a user asks for account deletion.

Outputs:
- A migration that truncates all customer PII tables.

Success criteria:
- The migration truncates all PII rows.
"""
    preferences = _derive_goal_user_preferences(risky_goal)

    assert "outputs" in preferences
    ledger = _seed_initial_ledger_from_user_preferences(risky_goal, preferences)

    assert "outputs" not in ledger.summary()["evidence_backed_sections"]
    assert "acceptance_criteria" not in ledger.summary()["evidence_backed_sections"]


def test_implementation_section_does_not_fabricate_io_preferences() -> None:
    """Only explicitly authored Inputs/Outputs sections may seed required IO."""
    goal = """
Goal:
Create a hello world file.

Implementation:
- Create `hello.py`.
"""

    preferences = _derive_goal_user_preferences(goal)

    assert "outputs" not in preferences
    assert "inputs" not in preferences


def test_structured_auto_goal_filters_report_only_success_criteria() -> None:
    preferences = _derive_goal_user_preferences(_OBSERVATION_GOAL)

    assert "acceptance_criteria" in preferences
    assert "manual fallback" not in preferences["acceptance_criteria"].casefold()
    assert "seed id" not in preferences["acceptance_criteria"].casefold()
    assert "uv run pytest tests/test_hello_auto.py" in preferences["acceptance_criteria"]


def test_structured_auto_goal_preserves_non_allowlisted_execution_criteria() -> None:
    goal = """
Goal:
Build a CLI validation endpoint.

Success criteria:
- `validator.py` exists.
- CLI exits 2 on invalid flags.
- HTTP 400 responses include a machine-readable error code.
- JSON output matches the documented schema.
- The command prints exactly `hello from ooo auto`.
- Final report includes auto session id, seed id, seed path, and test result.
"""

    preferences = _derive_goal_user_preferences(goal)

    assert preferences["acceptance_criteria"] == (
        "`validator.py` exists.\n"
        "CLI exits 2 on invalid flags.\n"
        "HTTP 400 responses include a machine-readable error code.\n"
        "JSON output matches the documented schema.\n"
        "The command prints exactly `hello from ooo auto`.\n"
        "Final report includes auto session id, seed id, seed path, and test result."
    )


def test_structured_auto_goal_filters_wrapper_criteria_using_full_prompt_context() -> None:
    goal = """
Goal:
Verify current ooo auto can create hello_auto.py and tests/test_hello_auto.py.

Success criteria:
- `hello_auto.py` exists.
- `tests/test_hello_auto.py` exists.
- Final report includes auto session id, seed id, seed path, and test result.

Important dispatch rule:
If `ouroboros_auto` is unavailable or interpreted as normal text, stop and report failure.
"""

    preferences = _derive_goal_user_preferences(goal)

    assert preferences["acceptance_criteria"] == (
        "`hello_auto.py` exists.\n`tests/test_hello_auto.py` exists."
    )


def test_structured_product_goal_preserves_exact_final_report_metadata_requirement() -> None:
    goal = """
Goal:
Build a product final-report endpoint.

Success criteria:
- Final report includes auto session id, seed id, seed path, and test result.
"""

    preferences = _derive_goal_user_preferences(goal)

    assert preferences["acceptance_criteria"] == (
        "Final report includes auto session id, seed id, seed path, and test result."
    )


def test_resume_preference_override_reseeds_stale_preconfirmed_ledger() -> None:
    """Resume preference overrides must update persisted ledger source of truth."""
    original_preferences = _derive_goal_user_preferences(_OBSERVATION_GOAL)
    original_ledger = _seed_initial_ledger_from_user_preferences(
        _OBSERVATION_GOAL, original_preferences
    )

    refreshed = _reseed_preference_ledger(
        _OBSERVATION_GOAL,
        original_ledger.to_dict(),
        {
            **original_preferences,
            "constraints": "Keep only the corrected local reversible constraint.",
        },
    )

    constraints = refreshed.sections["constraints"].entries
    active_constraints = [entry.value for entry in constraints if entry.status == "confirmed"]
    assert active_constraints == ["Keep only the corrected local reversible constraint."]


def test_resume_preference_override_can_clear_persisted_section() -> None:
    """Resume callers need an escape hatch for bad preallocated preferences."""
    original_preferences = {
        **_derive_goal_user_preferences(_OBSERVATION_GOAL),
        "constraints": "Stale constraint that should be removed.",
    }

    refreshed = _reseed_preference_ledger(
        _OBSERVATION_GOAL,
        _seed_initial_ledger_from_user_preferences(
            _OBSERVATION_GOAL, original_preferences
        ).to_dict(),
        _merge_resume_user_preferences(
            _OBSERVATION_GOAL,
            original_preferences,
            {"constraints": None},
        ),
    )

    assert "constraints" not in refreshed.summary()["evidence_backed_sections"]


def test_resume_preference_clear_is_durable_across_later_overrides() -> None:
    """Cleared goal-derived sections must not reappear on later resumes."""
    original_preferences = {
        **_derive_goal_user_preferences(_OBSERVATION_GOAL),
        "constraints": "Stale constraint that should be removed.",
    }
    after_clear = _merge_resume_user_preferences(
        _OBSERVATION_GOAL,
        original_preferences,
        {"constraints": None},
    )
    after_later_override = _merge_resume_user_preferences(
        _OBSERVATION_GOAL,
        after_clear,
        {"non_goals": "Keep the later non-goal override."},
    )

    assert "constraints" not in after_later_override
    assert after_later_override["non_goals"] == "Keep the later non-goal override."


def test_auto_handler_fresh_session_persists_goal_derived_ledger_preferences(
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}

    async def _capture_state(self, state):  # type: ignore[no-untyped-def]
        captured["state"] = state
        return AutoPipelineResult(
            status="complete",
            auto_session_id=state.auto_session_id,
            phase="complete",
        )

    with patch(
        "ouroboros.mcp.tools.auto_handler.AutoPipeline.run",
        new=_capture_state,
    ):
        result = asyncio.run(
            AutoHandler(store=AutoStore(tmp_path)).handle(
                {"goal": _OBSERVATION_GOAL, "cwd": str(tmp_path)}
            )
        )

    assert result.is_ok, result.error
    state = captured["state"]
    assert "runtime_context" in state.user_preferences
    assert "non_goals" in state.user_preferences
    ledger = SeedDraftLedger.from_dict(state.ledger)
    assert ledger.open_gaps() == []


def test_auto_handler_resume_preference_override_updates_persisted_ledger(
    tmp_path: Path,
) -> None:
    store = AutoStore(tmp_path)
    original_preferences = _derive_goal_user_preferences(_OBSERVATION_GOAL)
    state = AutoPipelineState(goal=_OBSERVATION_GOAL, cwd=str(tmp_path))
    state.user_preferences = dict(original_preferences)
    state.user_preferences["runtime_context"] = "Persisted explicit runtime context."
    state.ledger = _seed_initial_ledger_from_user_preferences(
        _OBSERVATION_GOAL, state.user_preferences
    ).to_dict()
    store.save(state)
    captured: dict[str, Any] = {}

    async def _capture_state(self, state):  # type: ignore[no-untyped-def]
        captured["state"] = state
        return AutoPipelineResult(
            status="complete",
            auto_session_id=state.auto_session_id,
            phase="complete",
        )

    with patch(
        "ouroboros.mcp.tools.auto_handler.AutoPipeline.run",
        new=_capture_state,
    ):
        result = asyncio.run(
            AutoHandler(store=store).handle(
                {
                    "resume": state.auto_session_id,
                    "user_preferences": {
                        "constraints": "Keep only the corrected local reversible constraint."
                    },
                }
            )
        )

    assert result.is_ok, result.error
    resumed = captured["state"]
    assert resumed.user_preferences["runtime_context"] == "Persisted explicit runtime context."
    assert resumed.user_preferences["constraints"] == (
        "Keep only the corrected local reversible constraint."
    )
    ledger = SeedDraftLedger.from_dict(resumed.ledger)
    active_constraints = [
        entry.value
        for entry in ledger.sections["constraints"].entries
        if entry.status == "confirmed"
    ]
    assert active_constraints == ["Keep only the corrected local reversible constraint."]


def _call_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def _auto_sub_interview_entrypoints() -> set[str]:
    """Return direct ooo auto sub-interview construction sites.

    This intentionally tracks the auto MCP path only.  Standalone
    ``ouroboros_interview`` remains outside this seed's implementation and
    spy-test scope.
    """
    relative_path = Path("src/ouroboros/mcp/tools/auto_handler.py")
    source_path = _repo_root() / relative_path
    module = ast.parse(source_path.read_text(encoding="utf-8"))
    discovered: set[str] = set()

    for class_node in (node for node in module.body if isinstance(node, ast.ClassDef)):
        for method_node in (
            node
            for node in class_node.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        ):
            call_names = _call_names(method_node)
            if {
                "_authoring_interview_handler",
                "HandlerInterviewBackend",
                "AutoInterviewDriver",
            }.issubset(call_names):
                discovered.add(f"{relative_path}::{class_node.name}.{method_node.name}")

    return discovered


def test_auto_sub_interview_entrypoint_manifest_has_isolation_test_mapping() -> None:
    """New auto interviewer entrypoints must declare isolation coverage.

    The manifest is a regression guard: if a future auto path constructs its
    own interviewer driver instead of flowing through the existing
    ``AutoHandler._run`` construction site, this test fails until that path is
    deliberately mapped to contract/prompt/spy isolation tests.
    """
    manifest_entrypoints = set(_AUTO_SUB_INTERVIEW_ENTRYPOINT_TEST_MANIFEST)
    discovered_entrypoints = _auto_sub_interview_entrypoints()

    assert discovered_entrypoints == manifest_entrypoints

    available_tests = {
        name for name, value in globals().items() if name.startswith("test_") and callable(value)
    }
    for entrypoint, mapped_tests in _AUTO_SUB_INTERVIEW_ENTRYPOINT_TEST_MANIFEST.items():
        assert mapped_tests, f"{entrypoint} has no explicit isolation test mapping"
        missing_tests = set(mapped_tests) - available_tests
        assert not missing_tests, f"{entrypoint} maps to missing tests: {sorted(missing_tests)}"


@dataclass(slots=True)
class _FakeInterviewEngine:
    state_dir: Path
    saved_states: list[InterviewState] = field(default_factory=list)
    states: dict[str, InterviewState] = field(default_factory=dict)

    async def start_interview(
        self,
        initial_context: str,
        cwd: str | None = None,
        interview_id: str | None = None,
    ) -> Result[InterviewState, MCPServerError]:
        state = InterviewState(
            interview_id=interview_id or "interview_0123456789abcdef",
            initial_context=initial_context,
            status=InterviewStatus.IN_PROGRESS,
        )
        self.states[state.interview_id] = state
        self.saved_states.append(state)
        return Result.ok(state)

    async def ask_next_question(self, state: InterviewState) -> Result[str, MCPServerError]:
        answered_rounds = [round_ for round_ in state.rounds if round_.user_response is not None]
        if not answered_rounds:
            return Result.ok("What should the first auto interview question clarify?")
        return Result.ok("Which acceptance signal proves the auto interview worked?")

    async def load_state(self, interview_id: str) -> Result[InterviewState, MCPServerError]:
        return Result.ok(self.states[interview_id])

    async def record_response(
        self,
        state: InterviewState,
        response: str,
        question: str,
    ) -> Result[InterviewState, MCPServerError]:
        state.rounds.append(
            InterviewRound(
                round_number=len(state.rounds) + 1,
                question=question,
                user_response=response,
            )
        )
        state.mark_updated()
        self.states[state.interview_id] = state
        return Result.ok(state)

    async def save_state(self, state: InterviewState) -> Result[Path, MCPServerError]:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        path = self.state_dir / f"interview_{state.interview_id}.json"
        path.write_text("{}", encoding="utf-8")
        self.states[state.interview_id] = state
        self.saved_states.append(state)
        return Result.ok(path)


def test_auto_handler_run_constructs_and_invokes_authoring_interviewer_path(
    tmp_path: Path,
) -> None:
    """``ooo auto`` must exercise the nested interviewer construction path.

    This intentionally starts from :class:`AutoHandler`, then lets
    ``AutoHandler._run`` construct ``AutoInterviewDriver`` →
    ``HandlerInterviewBackend`` → authoring ``InterviewHandler``.  The patched
    pipeline run invokes only the first interview question path, stopping
    before seed generation or execution handoff.
    """
    captured: dict[str, Any] = {}
    engine = _FakeInterviewEngine(state_dir=tmp_path)
    supplied_handler = InterviewHandler(
        interview_engine=engine,
        llm_backend="claude",
        data_dir=tmp_path,
    )
    handler = AutoHandler(
        interview_handler=supplied_handler,
        store=None,
        llm_backend="claude",
        agent_runtime_backend="opencode",
        opencode_mode="plugin",
    )

    async def _capture_and_start_interview(self, state):  # type: ignore[no-untyped-def]
        captured["pipeline"] = self
        captured["state"] = state
        turn = await self.interview_driver.backend.start(
            state.goal,
            cwd=state.cwd,
            interview_id="interview_0123456789abcdef",
        )
        captured["turn"] = turn
        return AutoPipelineResult(
            status="complete",
            auto_session_id=state.auto_session_id,
            phase="complete",
            runtime_backend=state.runtime_backend,
            opencode_mode=state.opencode_mode,
        )

    with (
        patch(
            "ouroboros.mcp.tools.authoring_handlers.create_llm_adapter",
            return_value=MagicMock(),
        ) as mock_factory,
        patch(
            "ouroboros.mcp.tools.authoring_handlers.backend_supports_tool_envelope",
            return_value=True,
        ),
        patch(
            "ouroboros.mcp.tools.authoring_handlers.resolve_llm_backend",
            side_effect=lambda backend: backend or "claude",
        ),
        patch(
            "ouroboros.mcp.tools.auto_handler.AutoPipeline.run",
            new=_capture_and_start_interview,
        ),
    ):
        result = asyncio.run(
            handler.handle(
                {
                    "goal": "Build an auto interviewer regression",
                    "cwd": str(tmp_path),
                    "max_interview_rounds": 1,
                    "skip_run": True,
                }
            )
        )

    assert result.is_ok, result.error
    pipeline = captured["pipeline"]
    assert isinstance(pipeline.interview_driver, AutoInterviewDriver)
    assert pipeline.interview_driver.max_rounds == 1
    assert isinstance(pipeline.interview_driver.backend, HandlerInterviewBackend)

    constructed_handler = pipeline.interview_driver.backend.handler
    assert isinstance(constructed_handler, InterviewHandler)
    assert constructed_handler is not supplied_handler
    assert constructed_handler.interview_engine is engine
    assert constructed_handler.agent_runtime_backend == "opencode"
    assert constructed_handler.opencode_mode == "subprocess"

    assert captured["turn"].session_id == "interview_0123456789abcdef"
    assert captured["turn"].question == "What should the first auto interview question clarify?"
    factory_kwargs = mock_factory.call_args.kwargs
    assert factory_kwargs["max_turns"] == 1
    _assert_isolated_allowed_tools(factory_kwargs)
    assert factory_kwargs["strict_mcp_config"] is True
    assert constructed_handler.suppress_tool_use_prompt_cues is True


def test_mocked_auto_interviewer_flow_returns_plain_text_question(
    tmp_path: Path,
) -> None:
    """The mocked ``ooo auto`` interview flow surfaces a text question.

    This uses the real ``AutoPipeline.run`` path.  The only mocked layer is
    the interview engine behind the constructed authoring ``InterviewHandler``;
    seed generation and execution are never reached because ``max_rounds=1``
    leaves the second interviewer question pending.
    """
    engine = _FakeInterviewEngine(state_dir=tmp_path / "interviews")
    supplied_handler = InterviewHandler(
        interview_engine=engine,
        llm_backend="claude",
        data_dir=tmp_path / "interviews",
    )
    handler = AutoHandler(
        interview_handler=supplied_handler,
        store=AutoStore(tmp_path / "auto"),
        llm_backend="claude",
        agent_runtime_backend="opencode",
        opencode_mode="plugin",
    )

    with (
        patch(
            "ouroboros.mcp.tools.authoring_handlers.create_llm_adapter",
            return_value=MagicMock(),
        ) as mock_factory,
        patch(
            "ouroboros.mcp.tools.authoring_handlers.backend_supports_tool_envelope",
            return_value=True,
        ),
        patch(
            "ouroboros.mcp.tools.authoring_handlers.resolve_llm_backend",
            side_effect=lambda backend: backend or "claude",
        ),
    ):
        result = asyncio.run(
            handler.handle(
                {
                    "goal": "Build an auto interviewer integration regression",
                    "cwd": str(tmp_path),
                    "max_interview_rounds": 1,
                    "skip_run": True,
                }
            )
        )

    assert result.is_ok, result.error
    value = result.value
    assert value.is_error is True
    assert value.meta["status"] == "blocked"
    assert value.meta["phase"] == "blocked"
    assert value.meta["current_round"] == 1
    assert value.meta["interview_session_id"].startswith("interview_")

    question = value.meta["pending_question"]
    assert question == "Which acceptance signal proves the auto interview worked?"
    assert isinstance(question, str)
    assert question.strip() == question
    assert "ToolUseBlock" not in question
    assert "tool_request" not in question
    assert "mcp__" not in question
    assert value.content[0].type == ContentType.TEXT

    factory_kwargs = mock_factory.call_args.kwargs
    assert factory_kwargs["max_turns"] == 1
    assert factory_kwargs["allowed_tools"] == []
    assert factory_kwargs["strict_mcp_config"] is True


class _CapturingAdapter:
    def __init__(self) -> None:
        self.messages: list[Message] = []

    async def complete(self, messages, config):  # type: ignore[no-untyped-def]
        self.messages = list(messages)
        return Result.ok(
            CompletionResponse(
                content="What success criterion should the Seed optimize for first?",
                model=config.model,
                usage=UsageInfo(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            )
        )


def test_auto_sub_interview_prompt_omits_code_exploration_and_tool_use_cues(
    tmp_path: Path,
) -> None:
    """Auto's nested first-question prompt must not invite tool calls.

    This starts from ``AutoHandler`` and exercises the constructed
    ``HandlerInterviewBackend`` so the assertion covers the ooo auto
    sub-interview prompt, not standalone ``ouroboros_interview``.
    """
    adapter = _CapturingAdapter()
    captured: dict[str, Any] = {}
    supplied_handler = InterviewHandler(
        data_dir=tmp_path,
        llm_backend="claude",
    )
    handler = AutoHandler(
        interview_handler=supplied_handler,
        store=None,
        llm_backend="claude",
        agent_runtime_backend="opencode",
        opencode_mode="plugin",
    )

    async def _capture_prompt(self, state):  # type: ignore[no-untyped-def]
        captured["pipeline"] = self
        turn = await self.interview_driver.backend.start(
            state.goal,
            cwd=state.cwd,
            interview_id="interview_0123456789abcdef",
        )
        captured["turn"] = turn
        return AutoPipelineResult(
            status="complete",
            auto_session_id=state.auto_session_id,
            phase="complete",
            runtime_backend=state.runtime_backend,
            opencode_mode=state.opencode_mode,
        )

    with (
        patch(
            "ouroboros.mcp.tools.authoring_handlers.create_llm_adapter",
            return_value=adapter,
        ) as mock_factory,
        patch(
            "ouroboros.mcp.tools.authoring_handlers.backend_supports_tool_envelope",
            return_value=True,
        ),
        patch(
            "ouroboros.mcp.tools.authoring_handlers.resolve_llm_backend",
            side_effect=lambda backend: backend or "claude",
        ),
        patch(
            "ouroboros.mcp.tools.auto_handler.AutoPipeline.run",
            new=_capture_prompt,
        ),
    ):
        result = asyncio.run(
            handler.handle(
                {
                    "goal": "Build an auto interviewer prompt regression",
                    "cwd": str(tmp_path),
                    "max_interview_rounds": 1,
                    "skip_run": True,
                }
            )
        )

    assert result.is_ok, result.error
    assert captured["turn"].question == "What success criterion should the Seed optimize for first?"
    constructed_handler = captured["pipeline"].interview_driver.backend.handler
    assert constructed_handler.suppress_tool_use_prompt_cues is True
    assert adapter.messages

    prompt = adapter.messages[0].content
    assert "Your ONLY job is to ask questions that reduce ambiguity" in prompt
    forbidden_cues = (
        "read the actual source code",
        "search for similar issues",
        "read from files",
        "read/glob/grep",
        "use read",
        "use glob",
        "use grep",
        "use bash",
        "direct codebase access",
        "codebase reading",
        "go find the answer",
        "gather evidence",
        "look at test cases",
    )
    prompt_lower = prompt.lower()
    for cue in forbidden_cues:
        assert cue not in prompt_lower

    factory_kwargs = mock_factory.call_args.kwargs
    assert factory_kwargs["max_turns"] == 1
    _assert_isolated_allowed_tools(factory_kwargs)
    assert factory_kwargs["strict_mcp_config"] is True


def test_auto_interviewer_role_definition_omits_tool_and_code_exploration_cues() -> None:
    """The interviewer role must remain a pure question-generator role.

    Auto's nested interviewer uses a toolless prompt variant, but the shared
    role definition must also avoid instructions that invite code exploration
    or tool use.  This guards future role edits without expanding standalone
    ``ouroboros_interview`` behavioral coverage.
    """
    from ouroboros.agents.loader import load_agent_prompt

    role = load_agent_prompt("socratic-interviewer")
    role_lower = role.lower()

    forbidden_cues = (
        "tool",
        "tools",
        "read the actual source code",
        "search for similar issues",
        "read from files",
        "read/glob/grep",
        "use read",
        "use glob",
        "use grep",
        "use bash",
        "direct codebase access",
        "codebase reading",
        "go find the answer",
        "gather evidence",
        "look at test cases",
        "explore files",
        "explore repositories",
        "explore commands",
    )
    for cue in forbidden_cues:
        assert cue not in role_lower


def _make_sdk_mock(mock_options_cls: MagicMock, mock_query: MagicMock) -> MagicMock:
    sdk_module = MagicMock()
    sdk_module.ClaudeAgentOptions = mock_options_cls
    sdk_module.query = mock_query

    errors_module = MagicMock()
    errors_module.MessageParseError = type("MessageParseError", (Exception,), {})
    sdk_module._errors = errors_module
    return sdk_module


def test_auto_sub_interview_envelope_ignores_parent_adapter_tool_context(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Auto's nested interviewer must rebuild a closed tool envelope.

    A parent MCP composition root can carry a permissive adapter for other
    work.  The ``ooo auto`` sub-interviewer is different: it is a pure
    single-shot question generator, so it must create its own adapter with an
    empty allow-list and a corresponding disallow-list instead of reusing the
    parent's tool context.  The same envelope must explicitly override
    ``setting_sources`` so parent/project Claude settings cannot leak into the
    nested interviewer.
    """
    from ouroboros.providers import claude_code_adapter as adapter_mod

    monkeypatch.setattr(
        adapter_mod,
        "_claude_options_field_names",
        lambda: frozenset(
            {
                "extra_args",
                "allowed_tools",
                "tools",
                "strict_mcp_config",
                "setting_sources",
                "skills",
                "agents",
                "plugins",
                "hooks",
                "include_hook_events",
            }
        ),
    )

    class ResultMessage:
        structured_output = None
        result = "What should the first auto interview question clarify?"
        is_error = False

    parent_setting_sources = ["user", "project", "local"]

    def _capture_options(**kwargs):  # type: ignore[no-untyped-def]
        effective_kwargs = {"setting_sources": parent_setting_sources, **kwargs}
        captured["effective_setting_sources"] = effective_kwargs["setting_sources"]
        return MagicMock()

    mock_options_cls = MagicMock(side_effect=_capture_options)

    async def fake_query(*args, **kwargs):
        yield ResultMessage()

    sdk_module = _make_sdk_mock(mock_options_cls, MagicMock(side_effect=fake_query))
    parent_adapter = ClaudeCodeAdapter(
        allowed_tools=["Read", "Glob"],
        strict_mcp_config=False,
    )
    supplied_handler = InterviewHandler(
        llm_adapter=parent_adapter,
        llm_backend="claude",
        agent_runtime_backend="opencode",
        opencode_mode="subprocess",
        data_dir=tmp_path,
    )
    handler = AutoHandler(
        interview_handler=supplied_handler,
        store=None,
        llm_backend="claude",
        agent_runtime_backend="opencode",
        opencode_mode="plugin",
    )
    captured: dict[str, Any] = {}

    async def _capture_and_start_interview(self, state):  # type: ignore[no-untyped-def]
        captured["pipeline"] = self
        turn = await self.interview_driver.backend.start(
            state.goal,
            cwd=state.cwd,
            interview_id="interview_0123456789abcdef",
        )
        captured["turn"] = turn
        return AutoPipelineResult(
            status="complete",
            auto_session_id=state.auto_session_id,
            phase="complete",
            runtime_backend=state.runtime_backend,
            opencode_mode=state.opencode_mode,
        )

    with (
        patch.dict(
            "sys.modules",
            {
                "claude_agent_sdk": sdk_module,
                "claude_agent_sdk._errors": sdk_module._errors,
            },
        ),
        patch(
            "ouroboros.mcp.tools.authoring_handlers.create_llm_adapter",
            return_value=ClaudeCodeAdapter(allowed_tools=[], strict_mcp_config=True),
        ) as mock_factory,
        patch(
            "ouroboros.mcp.tools.authoring_handlers.backend_supports_tool_envelope",
            return_value=True,
        ),
        patch(
            "ouroboros.mcp.tools.authoring_handlers.resolve_llm_backend",
            side_effect=lambda backend: backend or "claude",
        ),
        patch(
            "ouroboros.mcp.tools.auto_handler.AutoPipeline.run",
            new=_capture_and_start_interview,
        ),
    ):
        result = asyncio.run(
            handler.handle(
                {
                    "goal": "Build an auto interviewer regression",
                    "cwd": str(tmp_path),
                    "max_interview_rounds": 1,
                    "skip_run": True,
                }
            )
        )

    assert result.is_ok, result.error
    assert captured["turn"].question == "What should the first auto interview question clarify?"

    constructed_handler = captured["pipeline"].interview_driver.backend.handler
    assert constructed_handler is not supplied_handler
    assert constructed_handler.llm_adapter is None
    assert supplied_handler.llm_adapter is parent_adapter

    factory_kwargs = mock_factory.call_args.kwargs
    assert factory_kwargs["max_turns"] == 1
    _assert_isolated_allowed_tools(factory_kwargs)
    assert factory_kwargs["strict_mcp_config"] is True

    options_call_kwargs = mock_options_cls.call_args.kwargs
    assert options_call_kwargs["allowed_tools"] == []
    assert options_call_kwargs["tools"] == []
    assert options_call_kwargs["setting_sources"] == []
    assert captured["effective_setting_sources"] == []
    assert "Read" in options_call_kwargs["disallowed_tools"]
    assert "Glob" in options_call_kwargs["disallowed_tools"]
    assert options_call_kwargs["extra_args"]["allowedTools"] == ""


def test_auto_sub_interview_spy_adapter_fails_on_any_tool_request(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Auto's nested interviewer must fail closed on a tool request.

    This starts at ``AutoHandler`` and exercises the constructed
    ``HandlerInterviewBackend``.  The fake SDK emits a ``ToolUseBlock`` before
    a valid text result; the spy assertion is that the auto sub-interview
    treats the tool request as the failure, not as a recoverable prelude to the
    later question text.
    """
    from ouroboros.providers import claude_code_adapter as adapter_mod

    monkeypatch.setattr(
        adapter_mod,
        "_claude_options_field_names",
        lambda: frozenset(
            {
                "extra_args",
                "allowed_tools",
                "tools",
                "strict_mcp_config",
                "setting_sources",
                "skills",
                "agents",
                "plugins",
                "hooks",
                "include_hook_events",
            }
        ),
    )

    class ToolUseBlock:
        name = "Read"
        input = {"file_path": "README.md"}

    class AssistantMessage:
        content = [ToolUseBlock()]

    class ResultMessage:
        structured_output = None
        result = "What is the primary user goal?"
        is_error = False

    mock_options_cls = MagicMock()

    async def fake_query(*args, **kwargs):
        yield AssistantMessage()
        yield ResultMessage()

    sdk_module = _make_sdk_mock(mock_options_cls, MagicMock(side_effect=fake_query))
    captured: dict[str, Any] = {}
    supplied_handler = InterviewHandler(
        data_dir=tmp_path,
        llm_backend="claude",
    )
    handler = AutoHandler(
        interview_handler=supplied_handler,
        store=None,
        llm_backend="claude",
        agent_runtime_backend="opencode",
        opencode_mode="plugin",
    )

    async def _capture_tool_request_failure(self, state):  # type: ignore[no-untyped-def]
        turn = None
        try:
            turn = await self.interview_driver.backend.start(
                state.goal,
                cwd=state.cwd,
                interview_id="interview_0123456789abcdef",
            )
        except Exception as exc:  # noqa: BLE001 - spy captures the surfaced contract failure.
            captured["error"] = str(exc)
            captured["error_type"] = type(exc).__name__
        captured["turn"] = turn
        return AutoPipelineResult(
            status="blocked",
            auto_session_id=state.auto_session_id,
            phase="interview",
            runtime_backend=state.runtime_backend,
            opencode_mode=state.opencode_mode,
        )

    with (
        patch.dict(
            "sys.modules",
            {
                "claude_agent_sdk": sdk_module,
                "claude_agent_sdk._errors": sdk_module._errors,
            },
        ),
        patch(
            "ouroboros.mcp.tools.authoring_handlers.create_llm_adapter",
            return_value=ClaudeCodeAdapter(allowed_tools=[], strict_mcp_config=True),
        ) as mock_factory,
        patch(
            "ouroboros.mcp.tools.authoring_handlers.backend_supports_tool_envelope",
            return_value=True,
        ),
        patch(
            "ouroboros.mcp.tools.authoring_handlers.resolve_llm_backend",
            side_effect=lambda backend: backend or "claude",
        ),
        patch(
            "ouroboros.mcp.tools.auto_handler.AutoPipeline.run",
            new=_capture_tool_request_failure,
        ),
    ):
        result = asyncio.run(
            handler.handle(
                {
                    "goal": "Build an auto interviewer regression",
                    "cwd": str(tmp_path),
                    "max_interview_rounds": 1,
                    "skip_run": True,
                }
            )
        )

    assert result.is_ok, result.error
    assert captured["turn"] is None
    assert captured["error_type"] == "PartialInterviewStartError"
    assert "ToolUseBlock" in captured["error"]
    assert "What is the primary user goal?" not in captured["error"]

    factory_kwargs = mock_factory.call_args.kwargs
    assert factory_kwargs["max_turns"] == 1
    _assert_isolated_allowed_tools(factory_kwargs)
    assert factory_kwargs["strict_mcp_config"] is True

    options_call_kwargs = mock_options_cls.call_args.kwargs
    assert options_call_kwargs["max_turns"] == 1
    assert options_call_kwargs["allowed_tools"] == []
    assert options_call_kwargs["tools"] == []
    assert options_call_kwargs["extra_args"]["allowedTools"] == ""


def test_auto_sub_interview_isolates_parent_skill_invocations(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Auto's nested interviewer must not inherit parent Claude skills.

    The parent execution context can expose Claude Code skills for the main
    agentic session.  The auto sub-interviewer is a pure question generator,
    so its SDK envelope must explicitly clear ``skills`` and fail closed if a
    ``Skill`` tool request is still emitted before text.
    """
    from ouroboros.providers import claude_code_adapter as adapter_mod

    monkeypatch.setattr(
        adapter_mod,
        "_claude_options_field_names",
        lambda: frozenset(
            {
                "extra_args",
                "allowed_tools",
                "tools",
                "strict_mcp_config",
                "setting_sources",
                "skills",
                "agents",
                "plugins",
                "hooks",
                "include_hook_events",
            }
        ),
    )

    class ToolUseBlock:
        name = "Skill"
        input = {"skill": "ouroboros-auto", "instruction": "Inspect the repo before asking."}

    class AssistantMessage:
        content = [ToolUseBlock()]

    class ResultMessage:
        structured_output = None
        result = "What should the first auto interview question clarify?"
        is_error = False

    parent_skills = [{"name": "ouroboros-auto"}]
    captured: dict[str, Any] = {}

    def _capture_options(**kwargs):  # type: ignore[no-untyped-def]
        effective_kwargs = {"skills": parent_skills, **kwargs}
        captured["effective_skills"] = effective_kwargs["skills"]
        return MagicMock()

    mock_options_cls = MagicMock(side_effect=_capture_options)

    async def fake_query(*args, **kwargs):
        yield AssistantMessage()
        yield ResultMessage()

    sdk_module = _make_sdk_mock(mock_options_cls, MagicMock(side_effect=fake_query))
    supplied_handler = InterviewHandler(
        data_dir=tmp_path,
        llm_backend="claude",
    )
    handler = AutoHandler(
        interview_handler=supplied_handler,
        store=None,
        llm_backend="claude",
        agent_runtime_backend="opencode",
        opencode_mode="plugin",
    )

    async def _capture_skill_request_failure(self, state):  # type: ignore[no-untyped-def]
        turn = None
        try:
            turn = await self.interview_driver.backend.start(
                state.goal,
                cwd=state.cwd,
                interview_id="interview_0123456789abcdef",
            )
        except Exception as exc:  # noqa: BLE001 - spy captures the surfaced contract failure.
            captured["error"] = str(exc)
            captured["error_type"] = type(exc).__name__
        captured["turn"] = turn
        return AutoPipelineResult(
            status="blocked",
            auto_session_id=state.auto_session_id,
            phase="interview",
            runtime_backend=state.runtime_backend,
            opencode_mode=state.opencode_mode,
        )

    with (
        patch.dict(
            "sys.modules",
            {
                "claude_agent_sdk": sdk_module,
                "claude_agent_sdk._errors": sdk_module._errors,
            },
        ),
        patch(
            "ouroboros.mcp.tools.authoring_handlers.create_llm_adapter",
            return_value=ClaudeCodeAdapter(allowed_tools=[], strict_mcp_config=True),
        ) as mock_factory,
        patch(
            "ouroboros.mcp.tools.authoring_handlers.backend_supports_tool_envelope",
            return_value=True,
        ),
        patch(
            "ouroboros.mcp.tools.authoring_handlers.resolve_llm_backend",
            side_effect=lambda backend: backend or "claude",
        ),
        patch(
            "ouroboros.mcp.tools.auto_handler.AutoPipeline.run",
            new=_capture_skill_request_failure,
        ),
    ):
        result = asyncio.run(
            handler.handle(
                {
                    "goal": "Build an auto interviewer skill isolation regression",
                    "cwd": str(tmp_path),
                    "max_interview_rounds": 1,
                    "skip_run": True,
                }
            )
        )

    assert result.is_ok, result.error
    assert captured["turn"] is None
    assert captured["error_type"] == "PartialInterviewStartError"
    assert "ToolUseBlock" in captured["error"]
    assert "ouroboros-auto" in captured["error"]
    assert "What should the first auto interview question clarify?" not in captured["error"]

    factory_kwargs = mock_factory.call_args.kwargs
    assert factory_kwargs["max_turns"] == 1
    _assert_isolated_allowed_tools(factory_kwargs)
    assert factory_kwargs["strict_mcp_config"] is True

    options_call_kwargs = mock_options_cls.call_args.kwargs
    assert options_call_kwargs["allowed_tools"] == []
    assert options_call_kwargs["tools"] == []
    assert options_call_kwargs["skills"] == []
    assert captured["effective_skills"] == []
    assert "Skill" in options_call_kwargs["disallowed_tools"]
    assert options_call_kwargs["extra_args"]["allowedTools"] == ""


def test_auto_sub_interview_isolates_parent_agent_invocations(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Auto's nested interviewer must not inherit parent Claude sub-agents.

    The parent execution context can expose sub-agents for the main agentic
    workflow.  The auto sub-interviewer is constrained to one turn and must
    generate text only, so its SDK envelope must explicitly clear ``agents``
    and fail closed if a sub-agent ``Task`` request is still emitted before
    the question text.
    """
    from ouroboros.providers import claude_code_adapter as adapter_mod

    monkeypatch.setattr(
        adapter_mod,
        "_claude_options_field_names",
        lambda: frozenset(
            {
                "extra_args",
                "allowed_tools",
                "tools",
                "strict_mcp_config",
                "setting_sources",
                "skills",
                "agents",
                "plugins",
                "hooks",
                "include_hook_events",
            }
        ),
    )

    class ToolUseBlock:
        name = "Task"
        input = {
            "subagent_type": "researcher",
            "description": "Inspect repo before asking",
            "prompt": "Find the missing requirements yourself.",
        }

    class AssistantMessage:
        content = [ToolUseBlock()]

    class ResultMessage:
        structured_output = None
        result = "What should the first auto interview question clarify?"
        is_error = False

    parent_agents = {"researcher": {"description": "Repository research agent"}}
    captured: dict[str, Any] = {}

    def _capture_options(**kwargs):  # type: ignore[no-untyped-def]
        effective_kwargs = {"agents": parent_agents, **kwargs}
        captured["effective_agents"] = effective_kwargs["agents"]
        return MagicMock()

    mock_options_cls = MagicMock(side_effect=_capture_options)

    async def fake_query(*args, **kwargs):
        yield AssistantMessage()
        yield ResultMessage()

    sdk_module = _make_sdk_mock(mock_options_cls, MagicMock(side_effect=fake_query))
    supplied_handler = InterviewHandler(
        data_dir=tmp_path,
        llm_backend="claude",
    )
    handler = AutoHandler(
        interview_handler=supplied_handler,
        store=None,
        llm_backend="claude",
        agent_runtime_backend="opencode",
        opencode_mode="plugin",
    )

    async def _capture_agent_request_failure(self, state):  # type: ignore[no-untyped-def]
        turn = None
        try:
            turn = await self.interview_driver.backend.start(
                state.goal,
                cwd=state.cwd,
                interview_id="interview_0123456789abcdef",
            )
        except Exception as exc:  # noqa: BLE001 - spy captures the surfaced contract failure.
            captured["error"] = str(exc)
            captured["error_type"] = type(exc).__name__
        captured["turn"] = turn
        return AutoPipelineResult(
            status="blocked",
            auto_session_id=state.auto_session_id,
            phase="interview",
            runtime_backend=state.runtime_backend,
            opencode_mode=state.opencode_mode,
        )

    with (
        patch.dict(
            "sys.modules",
            {
                "claude_agent_sdk": sdk_module,
                "claude_agent_sdk._errors": sdk_module._errors,
            },
        ),
        patch(
            "ouroboros.mcp.tools.authoring_handlers.create_llm_adapter",
            return_value=ClaudeCodeAdapter(allowed_tools=[], strict_mcp_config=True),
        ) as mock_factory,
        patch(
            "ouroboros.mcp.tools.authoring_handlers.backend_supports_tool_envelope",
            return_value=True,
        ),
        patch(
            "ouroboros.mcp.tools.authoring_handlers.resolve_llm_backend",
            side_effect=lambda backend: backend or "claude",
        ),
        patch(
            "ouroboros.mcp.tools.auto_handler.AutoPipeline.run",
            new=_capture_agent_request_failure,
        ),
    ):
        result = asyncio.run(
            handler.handle(
                {
                    "goal": "Build an auto interviewer agent isolation regression",
                    "cwd": str(tmp_path),
                    "max_interview_rounds": 1,
                    "skip_run": True,
                }
            )
        )

    assert result.is_ok, result.error
    assert captured["turn"] is None
    assert captured["error_type"] == "PartialInterviewStartError"
    assert "ToolUseBlock" in captured["error"]
    assert "researcher" in captured["error"]
    assert "What should the first auto interview question clarify?" not in captured["error"]

    factory_kwargs = mock_factory.call_args.kwargs
    assert factory_kwargs["max_turns"] == 1
    _assert_isolated_allowed_tools(factory_kwargs)
    assert factory_kwargs["strict_mcp_config"] is True

    options_call_kwargs = mock_options_cls.call_args.kwargs
    assert options_call_kwargs["allowed_tools"] == []
    assert options_call_kwargs["tools"] == []
    assert options_call_kwargs["agents"] == {}
    assert captured["effective_agents"] == {}
    assert "Task" in options_call_kwargs["disallowed_tools"]
    assert options_call_kwargs["extra_args"]["allowedTools"] == ""


def test_auto_sub_interview_isolates_parent_plugin_invocations(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Auto's nested interviewer must not inherit parent Claude plugins.

    Parent plugin contexts can register additional tool surfaces for the main
    agentic run.  The auto sub-interviewer must clear that plugin list and
    fail closed if a plugin-sourced tool request is still emitted before the
    single text question.
    """
    from ouroboros.providers import claude_code_adapter as adapter_mod

    monkeypatch.setattr(
        adapter_mod,
        "_claude_options_field_names",
        lambda: frozenset(
            {
                "extra_args",
                "allowed_tools",
                "tools",
                "strict_mcp_config",
                "setting_sources",
                "skills",
                "agents",
                "plugins",
                "hooks",
                "include_hook_events",
            }
        ),
    )

    class ToolUseBlock:
        name = "mcp__parent_plugin__lookup"
        input = {"plugin": "parent-plugin", "query": "Inspect project context first."}

    class AssistantMessage:
        content = [ToolUseBlock()]

    class ResultMessage:
        structured_output = None
        result = "What should the first auto interview question clarify?"
        is_error = False

    parent_plugins = [{"name": "parent-plugin", "source": "parent-execution-context"}]
    captured: dict[str, Any] = {}

    def _capture_options(**kwargs):  # type: ignore[no-untyped-def]
        effective_kwargs = {"plugins": parent_plugins, **kwargs}
        captured["effective_plugins"] = effective_kwargs["plugins"]
        return MagicMock()

    mock_options_cls = MagicMock(side_effect=_capture_options)

    async def fake_query(*args, **kwargs):
        yield AssistantMessage()
        yield ResultMessage()

    sdk_module = _make_sdk_mock(mock_options_cls, MagicMock(side_effect=fake_query))
    supplied_handler = InterviewHandler(
        data_dir=tmp_path,
        llm_backend="claude",
    )
    handler = AutoHandler(
        interview_handler=supplied_handler,
        store=None,
        llm_backend="claude",
        agent_runtime_backend="opencode",
        opencode_mode="plugin",
    )

    async def _capture_plugin_request_failure(self, state):  # type: ignore[no-untyped-def]
        turn = None
        try:
            turn = await self.interview_driver.backend.start(
                state.goal,
                cwd=state.cwd,
                interview_id="interview_0123456789abcdef",
            )
        except Exception as exc:  # noqa: BLE001 - spy captures the surfaced contract failure.
            captured["error"] = str(exc)
            captured["error_type"] = type(exc).__name__
        captured["turn"] = turn
        return AutoPipelineResult(
            status="blocked",
            auto_session_id=state.auto_session_id,
            phase="interview",
            runtime_backend=state.runtime_backend,
            opencode_mode=state.opencode_mode,
        )

    with (
        patch.dict(
            "sys.modules",
            {
                "claude_agent_sdk": sdk_module,
                "claude_agent_sdk._errors": sdk_module._errors,
            },
        ),
        patch(
            "ouroboros.mcp.tools.authoring_handlers.create_llm_adapter",
            return_value=ClaudeCodeAdapter(allowed_tools=[], strict_mcp_config=True),
        ) as mock_factory,
        patch(
            "ouroboros.mcp.tools.authoring_handlers.backend_supports_tool_envelope",
            return_value=True,
        ),
        patch(
            "ouroboros.mcp.tools.authoring_handlers.resolve_llm_backend",
            side_effect=lambda backend: backend or "claude",
        ),
        patch(
            "ouroboros.mcp.tools.auto_handler.AutoPipeline.run",
            new=_capture_plugin_request_failure,
        ),
    ):
        result = asyncio.run(
            handler.handle(
                {
                    "goal": "Build an auto interviewer plugin isolation regression",
                    "cwd": str(tmp_path),
                    "max_interview_rounds": 1,
                    "skip_run": True,
                }
            )
        )

    assert result.is_ok, result.error
    assert captured["turn"] is None
    assert captured["error_type"] == "PartialInterviewStartError"
    assert "ToolUseBlock" in captured["error"]
    assert "mcp__parent_plugin__lookup" in captured["error"]
    assert "What should the first auto interview question clarify?" not in captured["error"]

    factory_kwargs = mock_factory.call_args.kwargs
    assert factory_kwargs["max_turns"] == 1
    _assert_isolated_allowed_tools(factory_kwargs)
    assert factory_kwargs["strict_mcp_config"] is True

    options_call_kwargs = mock_options_cls.call_args.kwargs
    assert options_call_kwargs["allowed_tools"] == []
    assert options_call_kwargs["tools"] == []
    assert options_call_kwargs["plugins"] == []
    assert captured["effective_plugins"] == []
    assert options_call_kwargs["extra_args"]["allowedTools"] == ""


def test_auto_sub_interview_isolates_parent_hook_context(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Auto's nested interviewer must not inherit parent Claude hooks.

    Parent Claude sessions can attach hooks that run commands around tool and
    prompt events.  The auto sub-interviewer must clear those hooks, suppress
    hook event streaming, and fail closed if any hook-adjacent tool request is
    still emitted before the single text question.
    """
    from ouroboros.providers import claude_code_adapter as adapter_mod

    monkeypatch.setattr(
        adapter_mod,
        "_claude_options_field_names",
        lambda: frozenset(
            {
                "extra_args",
                "allowed_tools",
                "tools",
                "strict_mcp_config",
                "setting_sources",
                "skills",
                "agents",
                "plugins",
                "hooks",
                "include_hook_events",
            }
        ),
    )

    class ToolUseBlock:
        name = "Bash"
        input = {"command": "run-parent-hook-before-asking"}

    class AssistantMessage:
        content = [ToolUseBlock()]

    class ResultMessage:
        structured_output = None
        result = "What should the first auto interview question clarify?"
        is_error = False

    parent_hooks = {
        "PreToolUse": [
            {
                "matcher": "*",
                "hooks": [{"type": "command", "command": "run-parent-hook-before-asking"}],
            }
        ]
    }
    captured: dict[str, Any] = {}

    def _capture_options(**kwargs):  # type: ignore[no-untyped-def]
        effective_kwargs = {
            "hooks": parent_hooks,
            "include_hook_events": True,
            **kwargs,
        }
        captured["effective_hooks"] = effective_kwargs["hooks"]
        captured["effective_include_hook_events"] = effective_kwargs["include_hook_events"]
        return MagicMock()

    mock_options_cls = MagicMock(side_effect=_capture_options)

    async def fake_query(*args, **kwargs):
        yield AssistantMessage()
        yield ResultMessage()

    sdk_module = _make_sdk_mock(mock_options_cls, MagicMock(side_effect=fake_query))
    supplied_handler = InterviewHandler(
        data_dir=tmp_path,
        llm_backend="claude",
    )
    handler = AutoHandler(
        interview_handler=supplied_handler,
        store=None,
        llm_backend="claude",
        agent_runtime_backend="opencode",
        opencode_mode="plugin",
    )

    async def _capture_hook_request_failure(self, state):  # type: ignore[no-untyped-def]
        turn = None
        try:
            turn = await self.interview_driver.backend.start(
                state.goal,
                cwd=state.cwd,
                interview_id="interview_0123456789abcdef",
            )
        except Exception as exc:  # noqa: BLE001 - spy captures the surfaced contract failure.
            captured["error"] = str(exc)
            captured["error_type"] = type(exc).__name__
        captured["turn"] = turn
        return AutoPipelineResult(
            status="blocked",
            auto_session_id=state.auto_session_id,
            phase="interview",
            runtime_backend=state.runtime_backend,
            opencode_mode=state.opencode_mode,
        )

    with (
        patch.dict(
            "sys.modules",
            {
                "claude_agent_sdk": sdk_module,
                "claude_agent_sdk._errors": sdk_module._errors,
            },
        ),
        patch(
            "ouroboros.mcp.tools.authoring_handlers.create_llm_adapter",
            return_value=ClaudeCodeAdapter(allowed_tools=[], strict_mcp_config=True),
        ) as mock_factory,
        patch(
            "ouroboros.mcp.tools.authoring_handlers.backend_supports_tool_envelope",
            return_value=True,
        ),
        patch(
            "ouroboros.mcp.tools.authoring_handlers.resolve_llm_backend",
            side_effect=lambda backend: backend or "claude",
        ),
        patch(
            "ouroboros.mcp.tools.auto_handler.AutoPipeline.run",
            new=_capture_hook_request_failure,
        ),
    ):
        result = asyncio.run(
            handler.handle(
                {
                    "goal": "Build an auto interviewer hook isolation regression",
                    "cwd": str(tmp_path),
                    "max_interview_rounds": 1,
                    "skip_run": True,
                }
            )
        )

    assert result.is_ok, result.error
    assert captured["turn"] is None
    assert captured["error_type"] == "PartialInterviewStartError"
    assert "ToolUseBlock" in captured["error"]
    assert "Bash" in captured["error"]
    assert "What should the first auto interview question clarify?" not in captured["error"]

    factory_kwargs = mock_factory.call_args.kwargs
    assert factory_kwargs["max_turns"] == 1
    _assert_isolated_allowed_tools(factory_kwargs)
    assert factory_kwargs["strict_mcp_config"] is True

    options_call_kwargs = mock_options_cls.call_args.kwargs
    assert options_call_kwargs["allowed_tools"] == []
    assert options_call_kwargs["tools"] == []
    assert options_call_kwargs["hooks"] == {}
    assert options_call_kwargs["include_hook_events"] is False
    assert captured["effective_hooks"] == {}
    assert captured["effective_include_hook_events"] is False
    assert "Bash" in options_call_kwargs["disallowed_tools"]
    assert options_call_kwargs["extra_args"]["allowedTools"] == ""


def test_handle_does_not_mutate_shared_interview_engine(
    tmp_path: Path,
) -> None:
    """A shared ``InterviewEngine`` must not be mutated by ``handle()``.

    Regression for PR #979 review (commit 0399211): the previous fix saved
    and restored ``engine.llm_adapter`` and ``engine.suppress_tool_use_prompt_cues``
    in-place, which is race-prone under concurrent MCP requests. The correct
    behavior is to never touch the shared engine: build a per-call replica
    that carries the isolated adapter and suppress flag.
    """
    from unittest.mock import AsyncMock, sentinel

    from ouroboros.bigbang.interview import InterviewEngine
    from ouroboros.mcp.tools.authoring_handlers import InterviewHandler

    parent_adapter = sentinel.parent_adapter
    isolated_adapter = sentinel.isolated_adapter

    shared_engine = InterviewEngine.__new__(InterviewEngine)
    shared_engine.suppress_tool_use_prompt_cues = False
    shared_engine.llm_adapter = parent_adapter
    shared_engine.state_dir = tmp_path
    shared_engine.model = "shared-model"

    # The shared engine's start_interview must NEVER be called — the handler
    # should build its own per-call engine instead.
    shared_engine.start_interview = AsyncMock(
        side_effect=AssertionError("shared engine.start_interview must not be called")
    )

    captured: dict[str, Any] = {}

    real_engine_init = InterviewEngine.__init__

    def _capture_init(self, *args: Any, **kwargs: Any) -> None:
        real_engine_init(self, *args, **kwargs)
        captured["new_engine_adapter"] = self.llm_adapter
        captured["new_engine_suppress"] = self.suppress_tool_use_prompt_cues

        async def _stop(*a: Any, **kw: Any) -> Any:
            raise RuntimeError("forced stop after engine construction is observed")

        self.start_interview = _stop  # type: ignore[assignment]

    handler = InterviewHandler(
        interview_engine=shared_engine,
        event_store=MagicMock(),
        llm_adapter=None,
        llm_backend="claude_code",
        agent_runtime_backend="claude_code",
        opencode_mode=None,
        data_dir=tmp_path,
        suppress_tool_use_prompt_cues=True,
    )
    handler._owns_event_store = False
    handler._initialized = True

    async def _run() -> None:
        try:
            await handler.handle({"initial_context": "Test goal"})
        except Exception:
            pass

    with (
        patch(
            "ouroboros.mcp.tools.authoring_handlers.create_llm_adapter",
            return_value=isolated_adapter,
        ),
        patch.object(InterviewEngine, "__init__", _capture_init),
    ):
        asyncio.run(_run())

    assert captured.get("new_engine_adapter") is isolated_adapter, (
        "expected the per-call engine to be constructed with the isolated adapter"
    )
    assert captured.get("new_engine_suppress") is True
    assert shared_engine.llm_adapter is parent_adapter, (
        "shared engine.llm_adapter must not be mutated"
    )
    assert shared_engine.suppress_tool_use_prompt_cues is False, (
        "shared engine.suppress_tool_use_prompt_cues must not be mutated"
    )


def test_concurrent_handle_calls_do_not_corrupt_shared_engine(
    tmp_path: Path,
) -> None:
    """Two concurrent ``handle()`` calls must not race on the shared engine.

    Regression for PR #979 review (commit 0399211): under save/restore
    mutation, two concurrent requests could clobber each other's adapter or
    leave the shared engine permanently pointed at an isolated adapter. With
    per-call cloning, the shared engine is never mutated, so the race
    disappears by construction. This test asserts that even when many
    concurrent handlers run, the shared engine's fields are pristine and each
    handler saw its own isolated adapter.
    """
    from unittest.mock import AsyncMock, sentinel

    from ouroboros.bigbang.interview import InterviewEngine
    from ouroboros.mcp.tools.authoring_handlers import InterviewHandler

    parent_adapter = sentinel.parent_adapter

    shared_engine = InterviewEngine.__new__(InterviewEngine)
    shared_engine.suppress_tool_use_prompt_cues = False
    shared_engine.llm_adapter = parent_adapter
    shared_engine.state_dir = tmp_path
    shared_engine.model = "shared-model"
    shared_engine.start_interview = AsyncMock(
        side_effect=AssertionError("shared engine.start_interview must not be called")
    )

    seen_adapters: list[Any] = []
    real_engine_init = InterviewEngine.__init__

    def _capture_init(self, *args: Any, **kwargs: Any) -> None:
        real_engine_init(self, *args, **kwargs)
        seen_adapters.append(self.llm_adapter)

        async def _stop(*a: Any, **kw: Any) -> Any:
            await asyncio.sleep(0)
            raise RuntimeError("forced stop after engine construction is observed")

        self.start_interview = _stop  # type: ignore[assignment]

    def _make_handler() -> InterviewHandler:
        handler = InterviewHandler(
            interview_engine=shared_engine,
            event_store=MagicMock(),
            llm_adapter=None,
            llm_backend="claude_code",
            agent_runtime_backend="claude_code",
            opencode_mode=None,
            data_dir=tmp_path,
            suppress_tool_use_prompt_cues=True,
        )
        handler._owns_event_store = False
        handler._initialized = True
        return handler

    async def _run_one(idx: int) -> None:
        try:
            await _make_handler().handle({"initial_context": f"goal-{idx}"})
        except Exception:
            pass

    async def _run_all() -> None:
        await asyncio.gather(*(_run_one(i) for i in range(8)))

    isolated_adapters = [MagicMock(name=f"isolated-{i}") for i in range(8)]
    adapter_iter = iter(isolated_adapters)
    # Patch once outside the gather so concurrent tasks share a single patch
    # lifetime — overlapping `with patch.object(InterviewEngine, "__init__")`
    # contexts from inside each task would leak between gather members and
    # into other tests.
    with (
        patch(
            "ouroboros.mcp.tools.authoring_handlers.create_llm_adapter",
            side_effect=lambda *_a, **_kw: next(adapter_iter),
        ),
        patch.object(InterviewEngine, "__init__", _capture_init),
    ):
        asyncio.run(_run_all())

    assert len(seen_adapters) == 8
    assert all(a is not parent_adapter for a in seen_adapters), (
        "every per-call engine must use its own isolated adapter, never the parent"
    )
    assert shared_engine.llm_adapter is parent_adapter, (
        "shared engine.llm_adapter must remain untouched after concurrent calls"
    )
    assert shared_engine.suppress_tool_use_prompt_cues is False, (
        "shared engine.suppress_tool_use_prompt_cues must remain untouched"
    )


def test_authoring_interview_handler_does_not_shortcut_when_backend_changes() -> None:
    """The reuse short-circuit must respect explicit ``llm_backend`` overrides.

    Regression for PR #979 review (commit cb2b7e7): previously, when an
    existing handler already had ``suppress_tool_use_prompt_cues=True`` and
    ``llm_adapter is None``, ``_authoring_interview_handler`` returned the
    existing handler unchanged even if the caller supplied a different
    ``llm_backend``. That silently kept the previous backend.
    """
    from ouroboros.mcp.tools.authoring_handlers import InterviewHandler
    from ouroboros.mcp.tools.auto_handler import _authoring_interview_handler

    existing = InterviewHandler(
        llm_backend="claude_code",
        agent_runtime_backend="claude_code",
        opencode_mode=None,
        suppress_tool_use_prompt_cues=True,
    )

    same_backend = _authoring_interview_handler(
        existing,
        llm_backend="claude_code",
        agent_runtime_backend="claude_code",
        opencode_mode=None,
    )
    assert same_backend is existing, (
        "matching backend should still allow the cheap direct-return short-circuit"
    )

    different_backend = _authoring_interview_handler(
        existing,
        llm_backend="codex_cli",
        agent_runtime_backend="claude_code",
        opencode_mode=None,
    )
    assert different_backend is not existing, (
        "a different llm_backend must produce a fresh handler, not reuse the previous one"
    )
    assert different_backend.llm_backend == "codex_cli"
    assert different_backend.suppress_tool_use_prompt_cues is True


def test_authoring_interview_handler_preserves_explicit_llm_adapter() -> None:
    """An explicit ``llm_adapter`` on the handler must NOT be silently dropped.

    Regression for PR #979 review (commit ffe4487): the isolation rewrite
    set ``llm_adapter=None`` on every reuse / rebuild path, which silently
    replaced any caller-injected non-default adapter with whatever
    ``create_llm_adapter(resolve_llm_backend(...))`` would later pick inside
    ``InterviewHandler.handle()``. The intent of this seed is to close the
    parent's tool-capable Claude envelope, not to discard an explicitly
    chosen adapter entirely.
    """
    from unittest.mock import sentinel

    from ouroboros.mcp.tools.authoring_handlers import InterviewHandler
    from ouroboros.mcp.tools.auto_handler import _authoring_interview_handler

    explicit_adapter = sentinel.explicit_adapter

    existing = InterviewHandler(
        llm_adapter=explicit_adapter,
        llm_backend="claude_code",
        agent_runtime_backend="claude_code",
        opencode_mode=None,
        suppress_tool_use_prompt_cues=True,
    )

    same_backend = _authoring_interview_handler(
        existing,
        llm_backend="claude_code",
        agent_runtime_backend="claude_code",
        opencode_mode=None,
    )
    assert same_backend.llm_adapter is explicit_adapter, (
        "matching-backend reuse must preserve the caller-supplied llm_adapter"
    )

    different_backend = _authoring_interview_handler(
        existing,
        llm_backend="codex_cli",
        agent_runtime_backend="claude_code",
        opencode_mode=None,
    )
    assert different_backend.llm_adapter is explicit_adapter, (
        "different-backend rebuild must still preserve the caller-supplied llm_adapter"
    )
    assert different_backend.suppress_tool_use_prompt_cues is True


def test_handle_falls_through_when_interview_engine_is_patched_to_non_type(
    tmp_path: Path,
) -> None:
    """``handle()`` must not crash when ``InterviewEngine`` is patched to a non-type.

    Regression for the PR #979 follow-up review: even with the
    ``template is not None`` short-circuit, once ``template`` is non-None the
    inner ``isinstance(template, InterviewEngine)`` still runs. When other
    tests (or a caller's dependency-injection harness) have monkey-patched
    ``ouroboros.mcp.tools.authoring_handlers.InterviewEngine`` to a non-type
    such as a ``MagicMock`` instance, that ``isinstance`` call raises
    ``TypeError: isinstance() arg 2 must be a type``. The guard must check
    ``isinstance(InterviewEngine, type)`` first and otherwise fall through to
    the passthrough branch.
    """
    from unittest.mock import AsyncMock

    from ouroboros.mcp.tools import authoring_handlers
    from ouroboros.mcp.tools.authoring_handlers import InterviewHandler

    supplied_engine = MagicMock(name="supplied_engine")
    supplied_engine.start_interview = AsyncMock(
        side_effect=RuntimeError("forced stop after engine selection is observed")
    )
    supplied_engine.resume_interview = AsyncMock()
    supplied_engine.score_interview = AsyncMock()

    handler = InterviewHandler(
        interview_engine=supplied_engine,
        event_store=MagicMock(),
        llm_adapter=None,
        llm_backend="claude_code",
        agent_runtime_backend="claude_code",
        opencode_mode=None,
        data_dir=tmp_path,
        suppress_tool_use_prompt_cues=True,
    )
    handler._owns_event_store = False
    handler._initialized = True

    async def _run() -> None:
        result = await handler.handle({"initial_context": "Test goal"})
        assert result.is_err
        assert "forced stop after engine selection is observed" in str(result.error)

    with (
        patch(
            "ouroboros.mcp.tools.authoring_handlers.create_llm_adapter",
            return_value=MagicMock(name="isolated_adapter"),
        ),
        patch.object(authoring_handlers, "InterviewEngine", MagicMock(name="patched_non_type")),
    ):
        asyncio.run(_run())

    # The supplied engine must have been used directly (passthrough), since
    # ``InterviewEngine`` was patched to a non-type and the clone arm cannot
    # safely fire. ``start_interview`` is the first method the handler calls
    # on the engine after selection, so its invocation proves the passthrough
    # branch was taken.
    supplied_engine.start_interview.assert_awaited()
